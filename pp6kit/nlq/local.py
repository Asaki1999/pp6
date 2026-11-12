"""本地规则引擎：不依赖大模型，直接把中文问题解析成 SQL。

覆盖面是「实施最常问的那几类」：
    <对象>的<字段 A>、<字段 B>           → 选列
    <字段>为/是<值>                      → WHERE（自动区分外键/字典/普通值）
    每个<字段>多少个 / 按<字段>统计       → GROUP BY + COUNT
    最近的/最新的                        → ORDER BY create_time_ DESC
    <人员字段>的所属部门                  → 两跳 join（人员 → 组织）

解析不了的会明确说"解析不了"，不会硬编一条错 SQL 出来。
"""
from __future__ import annotations

import re

from . import graph, select
from .introspect import Column, SchemaPack, Table

# 人员字段 → 组织的关系列（iform_party_employee.group_id_ = iform_party_org.id_）
EMPLOYEE_TABLE = "iform_party_employee"
ORG_TABLE = "iform_party_org"
EMPLOYEE_ORG_COL = "group_id_"

COUNT_WORDS = ("多少", "几个", "数量", "条数", "统计", "总数", "分布", "占比", "排名", "汇总")
# 「按X分一下」「按X看」「按X拆」这类口语也算分组统计
GROUP_VERBS = ("分一下", "分组", "分类", "看一下", "看看", "拆一下", "统计", "汇总", "分布")

# ---- 排名与聚合 ----
# CSpider 9693 条真实中文问法里，「最X」类占 17%（最大的一类），
# 但原来的规则引擎完全没处理，问「进度最高的5个项目」会返回全部 200 行。
RANK_RE = re.compile(
    r"(最高|最低|最大|最小|最多|最少|最长|最短|最快|最慢|最早|最晚|最新|最旧)"
    r"|前\s*(\d+)|top\s*(\d+)", re.I)
# 「最X」→ (比较方向, 暗示的列名关键词)
RANK_DIR = {
    "最高": ("DESC", ("进度", "金额", "优先级", "等级", "分数", "人数", "数量")),
    "最大": ("DESC", ("金额", "数量", "工期", "进度", "预算", "成本")),
    "最多": ("DESC", ("数量",)),
    "最长": ("DESC", ("工期", "时长")),
    "最快": ("ASC", ("工期", "时长")),
    "最晚": ("DESC", ("时间", "日期")),
    "最新": ("DESC", ("时间", "日期")),
    "最低": ("ASC", ("进度", "金额", "优先级", "等级", "分数")),
    "最小": ("ASC", ("金额", "数量", "工期", "进度", "预算", "成本")),
    "最少": ("ASC", ("数量",)),
    "最短": ("ASC", ("工期", "时长")),
    "最慢": ("DESC", ("工期", "时长")),
    "最早": ("ASC", ("时间", "日期")),
    "最旧": ("ASC", ("时间", "日期")),
}
# 平均 / 求和
AVG_RE = re.compile(r"(?:平均|均值|avg)", re.I)
# 注意：「一共/总共」后面跟「几个/多少」时是数量，不是求和，
# 所以要求后面不能紧跟数量词
SUM_RE = re.compile(r"(?:合计|总和|总计|求和|加起来|sum|(?:一共|总共)(?!\s*[有几个多少]))", re.I)

# 条件前的否定词：「排除任务名称为根任务的」「不要状态是已关闭的」
NEG_PREFIX = ("排除", "不包括", "不包含", "不含", "不要", "除了", "剔除",
              "去掉", "过滤掉", "非", "除去", "不看", "不要看", "排除掉")

# ---- 否定：按中文构词规则识别，不靠穷举 ----
# 之前「去除掉任务名称为根任务的」漏了，因为词表里只有「去掉」。
# 中文的否定动词是「动词语素 + 结果补语」的组合，穷举必然漏：
#   去除/排除/剔除/过滤/删/移/拿掉/捞掉 … × 掉/去/除/走 …
# 所以改成「词根 + 补语」两步判定，组合一次覆盖全部。
NEG_VERBS = ("去除", "排除", "剔除", "过滤", "除去", "删", "移", "拿", "抹",
             "剔", "滤", "屏", "蔽", "舍", "弃", "忽", "略", "跳",
             "筛", "滤出", "挑出", "挡", "拦", "禁", "闭", "藏", "隐")
NEG_COMPLEMENTS = ("掉", "去", "除", "走", "开", "出去", "走开", "掉去")
# 有些否定动词是「光杆」的，不带补语：「跳过 X」「忽略 X」「略过 X」
NEG_BARE = ("跳过", "忽略", "略过", "撇开", "不算", "不计", "除掉", "除去")
# 已经成词的常见否定前缀（直接匹配，速度快于组合）
NEG_FIXED = ("排除", "不包括", "不包含", "不含", "不要", "除了", "除外", "剔除",
             "去掉", "去除", "除去", "不看", "非", "剔除掉", "过滤掉")

RE_NEG_VERB = re.compile(
    "(" + "|".join(NEG_VERBS) + r")[\u4e00-\u9fa5]{0,2}?(?:"
    + "|".join(NEG_COMPLEMENTS) + ")"
)

# 否定词经常夹在中间：「名称不能包含」「状态不为」「责任人不是」。
# 光看条件前面 10 个字不够（「筛选的任务名称不能包含」中间隔了 5 个字），
# 所以两个方向都查：条件前 12 字 / 字段名到值之间 8 字。
NEG_INFIX = ("不能包含", "不可包含", "不能含", "不包含", "不含", "不包括",
             "不为", "不是", "不等于", "不要", "非", "不能是", "≠",
             "不能为", "不允许是", "不能带", "并非", "并无",
             "不能有", "没有", "不带有", "不要有", "不是以", "不以")


def has_negation(text: str) -> bool:
    """这段话里有否定意思吗？用「词根+补语」组合判定，不穷举具体词。"""
    if any(w in text for w in NEG_FIXED):
        return True
    if any(w in text for w in NEG_BARE):
        return True
    if any(w in text for w in NEG_INFIX):
        return True
    return RE_NEG_VERB.search(text) is not None


# 疑问词/虚词出现在「= 」右边时不能当值用：
#   「责任人是谁」「谁在管」「有哪些」—— 这些是问法，不是人名
class _GroupHit:
    """只提供 group(1) 的小对象：让「每个X的Y」拆出来的分组词能沿用原有接口。"""

    def __init__(self, text: str):
        self._text = text

    def group(self, idx: int = 0) -> str:
        return self._text


STOP_VALUES = frozenset({
    "谁", "谁人", "什么", "哪些", "哪个", "哪里", "多少", "几", "几个",
    "怎么样", "如何", "谁在管", "谁负责", "所有人", "全部", "有哪些",
})


def is_negated(q: str, start: int, lab_pos: int) -> bool:
    """判断某个条件是不是否定说法（排除 / 不能包含 / 不为 …）。

    start: 整个条件匹配的起点；lab_pos: 字段名在问句里的位置。
    """
    before = q[max(0, start - 12):start]
    if has_negation(before):
        return True
    if lab_pos >= 0:
        # 字段名到值之间（含模糊匹配动词）出现的否定词
        mid = q[lab_pos:start + 14]
        if has_negation(mid):
            return True
    return False

# 否定说法 -> 对应的肯定标签（用于「未关闭的问题」）
NEG_PAIRS = (("未", "已"), ("没", "已"), ("不", "已"))

# 软删除：平台里 deleted_ 的写法三种并存（全 NULL / N-Y / Y-N）。
# 用 `deleted_ <> 'Y'` 会把「全 NULL」的表整表过滤光（pp_task 601 行变 0 行），
# 必须套 IFNULL。
SOFT_DELETE = "IFNULL(`{alias}`.`deleted_`, 'N') <> 'Y'"
RECENT_WORDS = ("最近", "最新", "近期")
STOP = set(" 　的了和与及或是在有为把将请问帮我查询一下想看看")


class NotUnderstood(Exception):
    pass


def _norm(text: str) -> str:
    return re.sub(r"[的地得\s]", "", text or "")


def _strip_prefix(label: str, table_label: str) -> str:
    lab = _norm(label)
    tl = _norm(table_label).replace("表", "")
    for prefix in (tl, tl[:-1] if len(tl) > 2 else ""):
        if prefix and lab.startswith(prefix) and len(lab) > len(prefix):
            return lab[len(prefix):]
    return lab


def _find_tables(pack: SchemaPack, question: str) -> list[tuple[int, Table]]:
    """按综合打分选主表，返回 (分数, 表) 列表（首个是主表）。

    实现在 select.py：一张权重表，一次决策。这里只做适配。
    """
    # 主表选择交给 select.choose —— 一次打分，不再在 build() 里散落 if 改写。
    # 这里保留旧接口，返回 (分数, 表) 列表供 build 取候选。
    try:
        choice = select.choose(pack, question, _label_variants)
    except KeyError:
        return []
    best = choice.table
    rest = [(s, pack.tables[n]) for s, n in choice.ranking[1:] if n in pack.tables]
    return [(int(choice.score), best)] + rest


_LABEL_COLS = ("name_", "title_", "label_", "role_name_", "code_")


def _label_col_of(pack: SchemaPack, table: str) -> str:
    """取某张表用来代表「一行」的列名（名称/标题）。

    iform_party_employee 这类平台表不在语义层里（没业务对象定义），
    但它的姓名列固定叫 name_，所以未知表一律按 name_ 处理，
    否则会退化成 id_，结果里全是 UUID。
    """
    t = pack.tables.get(table)
    if t is None:
        return "name_"
    names = {c.name for c in t.columns}
    for cand in _LABEL_COLS:
        if cand in names:
            return cand
    for c in t.columns:
        if (c.dtype or "").lower() in ("varchar", "char") and not c.name.endswith("_id_"):
            return c.name
    return "name_"


def _fk_select(col: Column, alias: str, label: str,
               pack: SchemaPack | None = None) -> tuple[str, str | None]:
    """外键列 → (SELECT 表达式, 需要的 JOIN 子句)。

    单值走 LEFT JOIN（能用到索引）；
    多值（逗号分隔多个 id）改用标量子查询 + GROUP_CONCAT，
    否则一条项目会被拆成多行，行数虚增。
    """
    # 多值字段：即使 meta 里没标 multi，列名以 _ids_ 结尾也是逗号分隔多值，
    # 直接 = 关联会一行都匹配不上（assignee_ids_ 里存的是 "a,b"）
    multi = col.multi or col.name.endswith("_ids_")
    if multi:
        lc0 = _label_col_of(pack, col.ref_table) if pack is not None else "name_"
        expr = (
            f"(SELECT GROUP_CONCAT(`j`.`{lc0}` SEPARATOR '、') "
            f"FROM `{col.ref_table}` `j` "
            f"WHERE FIND_IN_SET(`j`.`id_`, `{alias}`.`{col.name}`))"
        )
        return f"{expr} AS `{label}`", None
    ja = f"r_{col.name}"
    sql = f"LEFT JOIN `{col.ref_table}` `{ja}` ON `{ja}`.`id_` = `{alias}`.`{col.name}`"
    lc = _label_col_of(pack, col.ref_table) if pack is not None else "name_"
    return f"`{ja}`.`{lc}` AS `{label}`", sql


def _org_select(col: Column, alias: str, label: str) -> tuple[str, list[str]]:
    """人员的所属部门：人员表 → 组织表两跳。多值同样用子查询聚合。"""
    if col.multi:
        expr = (
            f"(SELECT GROUP_CONCAT(DISTINCT `o`.`name_` SEPARATOR '、') "
            f"FROM `{EMPLOYEE_TABLE}` `e` "
            f"JOIN `{ORG_TABLE}` `o` ON `o`.`id_` = `e`.`{EMPLOYEE_ORG_COL}` "
            f"WHERE FIND_IN_SET(`e`.`id_`, `{alias}`.`{col.name}`))"
        )
        return f"{expr} AS `{label}`", []
    ja, oa = f"r_{col.name}", f"o_{col.name}"
    return f"`{oa}`.`name_` AS `{label}`", [
        f"LEFT JOIN `{EMPLOYEE_TABLE}` `{ja}` ON `{ja}`.`id_` = `{alias}`.`{col.name}`",
        f"LEFT JOIN `{ORG_TABLE}` `{oa}` ON `{oa}`.`id_` = `{ja}`.`{EMPLOYEE_ORG_COL}`",
    ]


def _user_org_phrases(question: str, table: Table) -> dict[str, str]:
    """找出「<人员字段>的(所属)部门」这类说法，返回 {列名: 别名}。"""
    q = _norm(question)
    found = {}
    for c in table.columns:
        if c.ref_table != EMPLOYEE_TABLE:
            continue
        lab = _norm(c.label)
        for suffix in ("的所属部门", "的所在部门", "的部门", "所属部门", "所在部门", "部门"):
            if f"{lab}{suffix}" in q:
                found[c.name] = f"{c.label}所属部门"
                break
    return found


TITLE_SUFFIX = ("名称", "标题", "姓名", "主题")


def _title_column(table: Table) -> Column | None:
    """挑「一行叫什么」的列。"""
    for key in ("name_", "title_", "label_", "subject_"):
        c = table.column(key)
        if c is not None and (c.filled != 0):
            return c
    for c in table.columns:
        if c.name in ("name_", "title_", "label_") or _norm(c.label) in ("名称", "标题", "姓名", "名称简称"):
            if c.filled != 0:
                return c
    return None


# 纯系统列，任何排序里都排最后
_SYS_NAMES = frozenset({
    "id_", "tenant_id_", "ip_", "version_", "baseline_version_", "is_draft_",
    "create_by_", "update_by_", "create_time_", "update_time_", "deleted_",
    "cut_flow_inst_id_", "cut_flow_bo_id_",
})


_NUMERIC = ("int", "bigint", "decimal", "double", "float", "smallint", "tinyint")
# 部分库把数值列标成 number/numeric/real，同样不能当文本做 LIKE
_NUMERIC_EXTRA = ("number", "numeric", "real")


def _is_numeric_col(c: Column) -> bool:
    """这列是数值类型吗？数值列只能做数值比较，绝不能 LIKE '%中文%'。"""
    return (c.dtype or "").lower() in _NUMERIC + _NUMERIC_EXTRA


# AVG / SUM 的目标列必须来自哪类语义标签
AGG_HINTS = {
    "AVG": ("工时", "进度", "工期", "金额"),
    "SUM": ("工时", "金额", "预算", "成本", "工期"),
}


def _agg_column(question: str, table: Table, agg: str) -> Column | None:
    """给 AVG/SUM 找一列**真的能统计**的数值列；找不到返回 None。

    为什么必须有这个：`AVG(*)` / `SUM(*)` 在 MySQL 里是语法错误
    （只有 `COUNT(*)` 合法），而 sqlglot 能解析它，所以安全闸看不出问题——
    坏 SQL 会直接打到客户库执行失败。所以宁可不出聚合，也不能写 `(*)`。
    """
    qn = _norm(question)
    # 1) 问句点名的字段（要求数值类型，且不是「是否xx」这种布尔开关）
    for c in sorted(table.columns, key=_bind_priority):
        if not _is_numeric_col(c) or _is_bool_flag(c):
            continue
        for lab in _label_variants(c, table):
            if len(lab) >= 2 and lab in qn:
                return c
    # 2) 按语义关键词找，但**必须有值**——人工成本常常整列为空，
    #    聚合它只会得到一行 NULL（工时合计应该聚合 normal_hours_）
    hints = AGG_HINTS.get(agg, ())
    cands = [c for c in table.columns
             if any(h in _norm(c.label) for h in hints) and _is_numeric_col(c)]
    cands.sort(key=lambda c: (c.filled == 0, c.filled if c.filled > 0 else 10**9))
    return cands[0] if cands else None


def _require_agg_column(question: str, table: Table, agg: str) -> Column:
    """AVG/SUM 必须落到真实数值列上，拿不到就明确报「解析不了」。

    不允许退化成 `AVG(*)`/`SUM(*)`——那是 MySQL 语法错误，而 sqlglot 能解析、
    安全闸看不出问题，坏 SQL 会直接打到客户库执行失败。
    也不允许随手换一列凑合（问「工时」却求和「金额」，数字看着合理但是错的）。
    """
    col = _agg_column(question, table, agg)
    if col is None:
        nm = {"AVG": "平均", "SUM": "合计"}.get(agg, agg)
        raise NotUnderstood(
            f"问的是{nm}，但「{table.label}」上没有可统计的数值列；"
            f"试着点名具体字段（如「金额」「工时」「进度」）")
    return col


def _is_bool_flag(c: Column) -> bool:
    """是不是「是否xx」「已xx」这类布尔开关。

    这类列只有 0/1 两个值，放进默认结果里信息量为零
    （如「是否是里程碑」「是否已阅」），除非用户点名要。
    """
    lab = _norm(c.label)
    if lab.startswith("是否") or lab.startswith("有无"):
        return True
    if c.distinct == 2 and (c.dtype or "").lower() in ("tinyint", "bit") \
            and c.name.startswith(("is_", "has_", "can_")):
        return True
    return False


def _default_columns(table: Table) -> list[Column]:
    """没点名字段时，给一组「业务上真有信息量」的列。

    踩过的坑：工时表没有 name_、没有状态枚举，
    旧逻辑按列名关键词挑（status/priority/责任人/进度/时间），
    最后只挑出一个 create_time_，用户问「查询工时数据」只看到一列创建时间。

    现在按**数据画像**排，而不是猜列名：
      1. 标题列（有 name_ 的表）
      2. 有中文枚举的列（状态/类型/优先级，人能看懂）
      3. 外键列（填报人/项目/产品，信息量大）
      4. 数值列（工时数、金额、进度）
      5. 日期列（优先业务日期 timesheet_date_ 这类）
      6. 文本列（工作内容、描述）
    纯系统列（id/tenant/审计）一律不选；
    filled == 0 才是真没数据，-1 表示没统计过，算未知。
    """
    out: list[Column] = []
    seen: set[str] = set()

    def add(c: Column | None, limit: int = 8) -> None:
        if c is None or c.name in seen or len(out) >= limit:
            return
        if c.name in _SYS_NAMES:
            return
        if c.filled == 0:              # 明确整列为空才排除
            return
        # 布尔开关（是否xx）信息量极低，除非没别的可选
        if _is_bool_flag(c):
            return
        seen.add(c.name)
        out.append(c)

    # 1) 标题：一行叫什么
    add(_title_column(table))

    # 2) 状态 / 类型 / 优先级（有中文枚举的才算，否则显示 0/1 没意义）
    for c in table.columns:
        if c.has_cn_enums and any(k in _norm(c.label) for k in ("状态", "类型", "级别", "类别", "优先级")):
            add(c)
    for c in table.columns:
        if c.has_cn_enums:
            add(c)

    # 3) 人：责任人 / 负责人 / 填报人 / 处理人
    for kw in ("责任人", "负责人", "填报人", "处理人", "执行人", "待办人", "创建人"):
        for c in table.columns:
            if kw in _norm(c.label) and re.match(r"(user|person|assignee|owner|handler|manager|create_by)", c.name):
                add(c)
                break

    # 4) 归属对象：项目 / 客户 / 产品 / 阶段
    for kw in ("项目", "客户", "产品", "阶段", "迭代", "合同", "部门"):
        for c in table.columns:
            if c.ref_table and kw in _norm(c.label):
                add(c)
                break

    # 5) 进度 / 工时 / 金额 这类数值
    for kw in ("进度", "工时", "金额", "成本", "预算", "工期", "数量", "完成率"):
        for c in table.columns:
            if kw in _norm(c.label) and (c.dtype or "").lower() in _NUMERIC:
                add(c)
                break

    # 6) 关键时间：优先业务日期，不是审计时间
    for key in ("timesheet_date_", "work_date_", "plan_finish_", "finish_",
                "due_time_", "plan_start_", "start_", "actual_finish_"):
        add(table.column(key))
    for kw in ("日期", "截止", "到期"):
        for c in table.columns:
            if kw in _norm(c.label) and (c.dtype or "").lower() in ("date", "datetime", "timestamp"):
                add(c)
                break

    # 7) 其余外键（兜底，别让表看起来太空）
    for c in table.columns:
        if c.ref_table:
            add(c)

    return out


def _bind_priority(c: Column) -> tuple[int, int]:
    """同名字段的绑定优先级：数据上真能筛的排前面。

    pp_task 有 state_（601 行全是 0）和 schedule_state_（0/1 两种），
    问「任务状态是已完成」应该绑后者。
    """
    if any(k in c.name for k in ("state", "status")):
        return (0, 0) if (c.filled and c.distinct >= 2) else (1, 0)
    return (0, 1)


def _pick_columns(question: str, table: Table, skip: set[str]) -> list[tuple[Column, str]]:
    """按在问题里出现的先后顺序挑列。"""
    q = _norm(question)
    hits = []
    claimed: set[str] = set()
    for c in sorted(table.columns, key=_bind_priority):
        if c.name in skip:
            continue
        for lab in _label_variants(c, table):
            if lab in claimed:
                continue
            if lab in q:
                hits.append((q.index(lab), c, c.label))
                claimed.add(lab)
                break
    hits.sort(key=lambda x: x[0])
    seen, out = set(), []
    for _, c, label in hits:
        if c.name in seen:
            continue
        # 外键列会把「开发任务责任人」选进来，但它渲染成 id_ 或 NULL，
        # 用户点名字段时更可能是想要可读值，这类列不优先展示
        if c.ref_table and label.endswith(("责任人", "负责人")) and out:
            seen.add(c.name)
            continue
        seen.add(c.name)
        out.append((c, label))
    return out


# 用户口语常省略这些前缀：「实际进度」直接说「进度」
CORE_PREFIX = ("项目", "实际", "计划", "当前", "所属", "关联", "隶属", "本")


from .introspect import COL_SYNONYM


def _label_variants(c: Column, table: Table) -> list[str]:
    """一个字段可能有多种叫法：完整标签、去掉对象前缀、去掉修饰前缀。"""
    out = [_norm(c.label)]
    short = _strip_prefix(c.label, table.label)
    if short and short not in out:
        out.append(short)
    base = out[-1]
    for pre in CORE_PREFIX:
        if base.startswith(pre) and len(base) - len(pre) >= 2:
            core = base[len(pre):]
            if core not in out:
                out.append(core)
            break
    # 列级口语：「金额」也接「钱」「费用」
    for stem, words in COL_SYNONYM.items():
        if stem in out:
            out.extend(w for w in words if w not in out)
    return [x for x in out if len(x) >= 2]


def _condition(question: str, table: Table, pack: SchemaPack,
               alias: str) -> tuple[str, list[str]]:
    """识别条件：等于 / 包含 / 数值比较。返回 WHERE 片段与额外 join。"""
    q = question
    where: list[str] = []
    used: set[str] = set()
    spans: list[tuple[int, int]] = []
    used_values: set[str] = set()

    def taken(start: int, end: int) -> bool:
        return any(a < end and start < b for a, b in spans)

    # 字段绑定顺序：同名的状态列可能有好几个（state_ / schedule_state_ /
    # approval_state_），先把「数据上真的能筛」的排前面。
    # pp_task.state_ 601 行全是 0，schedule_state_ 才是 0/1 的真实状态。
    def _bind_priority(c: Column) -> tuple[int, int]:
        usable = 0
        if any(k in c.name for k in ("state", "status")):
            usable = 0 if (c.filled and c.distinct >= 2) else 1
        return (usable, 0)

    for c in sorted(table.columns, key=_bind_priority):
        if c.name in used:
            continue
        for lab_text in _label_variants(c, table):
            lab = re.escape(lab_text)

            # 1) 数值比较
            mc = re.search(
                rf"{lab}\s*(?:低于|小于|少于|不足|高于|大于|超过|多于|>=|<=|>|<)\s*(\d+(?:\.\d+)?)",
                q,
            )
            if mc and not taken(mc.start(), mc.end()):
                op = "<" if re.search(r"低于|小于|少于|不足|<=|<", mc.group(0)) else ">"
                where.append(f"`{alias}`.`{c.name}` {op} {mc.group(1)}")
                spans.append((mc.start(), mc.end()))
                used.add(c.name)
                break

            # 2) 等于 / 包含
            m = re.search(rf"{lab}\s*(?:是|为|等于|=)\s*[「\"']?([^\s，,。；;]+)", q)
            fuzzy = False
            if not m:
                # 模糊匹配动词：肯定（带/包含/含有/包括）与否定（不能有/不含/没有）都要认。
                # 注意「有」必须放在否定式里用，否则「每个项目有多少个」会被当成条件。
                m2 = re.search(
                    rf"{lab}(?:里|中|上)?\s*(?:带|包含|含有|包括|"
                    rf"不能包含|不包含|不含|不能含|不包括|不能有|没有|"
                    rf"不带有|不要有|不出现)\s*[「\"']?([^\s，,。；;]+)", q
                )
                if m2:
                    m, fuzzy = m2, True
            prefix_value = ""
            if not m:
                # 「高优先级」「低风险」这种值在标签前面的说法
                mp = re.search(rf"(高|中|低|紧急|普通|严重)\s*(?:的)?\s*{lab}", q)
                if mp and not taken(mp.start(), mp.end()):
                    prefix_value = mp.group(1)
                    m = mp
            if not m or taken(m.start(), m.end()):
                continue
            # 「排除X为Y的」「X不能包含Y」：同一个条件要反过来写
            negated = is_negated(q, m.start(), q.find(lab))

            raw = prefix_value or m.group(1).strip("「」\"'")
            value = re.split(r"的|有|吗|呢|记录", raw)[0].strip()
            if not value or value in STOP_VALUES:
                # 「责任人是谁」「有哪些」：这是问法，不是筛选条件
                continue
            orig_value = value            # 翻译成码之前的中文，用来防重复消费
            target = c
            col = f"`{alias}`.`{target.name}`"

            # 库里状态/优先级存英文码，先按列画像翻译
            if not fuzzy and target.enums:
                if value not in target.enums:
                    other = next((x for x in table.columns
                                  if x is not target and value in (x.enums or {})), None)
                    if other is not None:
                        target = other
                        col = f"`{alias}`.`{target.name}`"
                        used.add(other.name)
                value = target.enums.get(value, value)

            # 数值列不能走文本匹配：`进度 LIKE '%高%'` 在 MySQL 里恒不匹配，
            # 返回空表但不报错——比报错更难发现，用户以为「真的没有高进度项目」。
            # 数值列只接受真正的数值条件；映射不出数值（「高」没有对应数字）就丢掉这个条件。
            if _is_numeric_col(target) and not target.enums:
                num = None if fuzzy else re.search(r"\d+(?:\.\d+)?", value)
                if num is not None:
                    where.append(f"{col} {'<>' if negated else '='} {num.group(0)}")
                    used_values.add(str(orig_value))
                    used_values.add(str(value))
                spans.append((m.start(), m.end()))
                used.add(c.name)
                break

            if fuzzy:
                if target.ref_table and not target.multi:
                    where.append(
                        f"{col} {'NOT IN' if negated else 'IN'} "
                        f"(SELECT `id_` FROM `{target.ref_table}` "
                        f"WHERE `name_` LIKE '%{value}%')"
                    )
                else:
                    where.append(f"{col} {'NOT LIKE' if negated else 'LIKE'} '%{value}%'")
            elif negated and not target.ref_table and not target.dic_type:
                where.append(f"{col} NOT LIKE '%{value}%'")
            elif target.ref_table == EMPLOYEE_TABLE:
                where.append(f"{col} {'NOT IN' if negated else 'IN'} "
                             f"(SELECT `id_` FROM `{EMPLOYEE_TABLE}` WHERE `name_` = '{value}')")
            elif target.ref_table == ORG_TABLE:
                where.append(f"{col} {'NOT IN' if negated else 'IN'} "
                             f"(SELECT `id_` FROM `{ORG_TABLE}` WHERE `name_` = '{value}')")
            elif target.ref_table:
                where.append(f"{col} {'NOT IN' if negated else 'IN'} "
                             f"(SELECT `id_` FROM `{target.ref_table}` WHERE `name_` = '{value}')")
            elif target.dic_type and target.dic_type in pack.dictionaries:
                rev = {v: k for k, v in pack.dictionaries[target.dic_type].items()}
                where.append(f"{col} {'<>' if negated else '='} '{rev.get(value, value)}'")
            else:
                where.append(f"{col} {'NOT LIKE' if negated else 'LIKE'} '%{value}%'")
            spans.append((m.start(), m.end()))
            used.add(c.name)
            used_values.add(str(orig_value))
            used_values.add(str(value))
            break

    # 业务口语没点名具体字段时，按语义补一个
    ORAL = (
        (r"收入|金额|费用|花了|花在|钱|成本", ("金额", "total_budget_", "total_cost_", "budget")),
        (r"谁在管|谁负责|负责人|牵头", ("责任人", "负责人", "处理人")),
        (r"超期|延期|逾期", ("到期时间", "计划完成时间", "finish_", "due_time_")),
    )
    for pattern, wanted in ORAL:
        if not re.search(pattern, q):
            continue
        col = next((c for w in wanted for c in table.columns
                    if w in _norm(c.label) or w in c.name), None)
        if col is not None and f"`{alias}`.`{col.name}`" not in " ".join(where):
            pass
    # 状态类条件落在 pp_task（state_ 全是 0）时，换到真带状态码的表
    if re.search(r"(?:状态|进度).{0,4}(?:是|为)", q):
        if not any(c.has_cn_enums for c in table.columns
                   if "state" in c.name or "status" in c.name):
            pass
        state_cols = [c for c in table.columns if "state_" == c.name or c.name.endswith("state_id_")]
        if not any(c.has_cn_enums or (c.distinct > 1 and c.filled > 0) for c in state_cols):
            alt = next((t for t in pack.tables.values()
                        if t is not table and _norm(t.label).endswith(_norm(table.label))
                        and any(c.has_cn_enums for c in t.columns
                                if "state" in c.name or "status" in c.name)), None)
            if alt is not None:
                table = alt

    # 枚举值直接出现在问句里：「高风险的风险」「进行中的任务」
    for c in table.columns:
        if not c.has_cn_enums or c.name in used:
            continue
        if any(f"`{alias}`.`{c.name}`" in w for w in where):
            continue
        hit = next(((lab, code) for lab, code in c.enums.items()
                    if len(lab) >= 3 and lab not in used_values
                    and any("\u4e00" <= ch <= "\u9fa5" for ch in lab)
                    and lab in q), None)
        if hit:
            # 「除了高风险以外」：值前面有否定词就反过来
            pos = q.find(hit[0])
            negated = pos > 0 and is_negated(q, pos, pos)
            where.append(f"`{alias}`.`{c.name}` {'<>' if negated else '='} '{hit[1]}'")

        # 「风险等级不是高的」：枚举标签是单字（高/中/低）时上面那条不触发，
        # 因为要求标签至少 3 个字。这里单独处理「字段 + 不是/不为 + 单字值」。
        elif re.search(rf"{_norm(c.label)}[^，,。；;]{{0,4}}(?:不是|不为|不等于|≠)\s*[「\"']?"
                       rf"([高中低一二三四五级])", q):
            mv = re.search(rf"{_norm(c.label)}[^，,。；;]{{0,4}}(?:不是|不为|不等于|≠)\s*[「\"']?"
                           rf"([高中低一二三四五级])", q)
            short = mv.group(1)
            # 中文短值 → 库里码（高→High），模糊匹配枚举标签
            code = next((v for k, v in c.enums.items() if short in k or k in short), None)
            if code:
                where.append(f"`{alias}`.`{c.name}` <> '{code}'")

    # 否定：「未关闭」→ 找「已关闭」这种精确对应的枚举标签
    neg = re.search(r"(?:未|没|不)([\u4e00-\u9fa5]{1,4})", q)
    if neg:
        word = neg.group(1)
        for c in table.columns:
            hit = next(((lab, code) for lab, code in (c.enums or {}).items()
                        if lab.startswith("已") and lab.endswith(word)), None)
            if hit:
                where.append(f"`{alias}`.`{c.name}` <> '{hit[1]}'")
                break
    return " AND ".join(where), []


# 相对时间：中文说法 -> SQL 片段（{c} 为列全名）
TIME_RULES: list[tuple[str, str]] = [
    (r"今天|今日", "DATE({c}) = CURDATE()"),
    (r"昨天|昨日", "DATE({c}) = CURDATE() - INTERVAL 1 DAY"),
    (r"本周|这周|本星期", "YEARWEEK({c}, 1) = YEARWEEK(CURDATE(), 1)"),
    (r"上周|上个星期", "YEARWEEK({c}, 1) = YEARWEEK(CURDATE(), 1) - 1"),
    (r"本月|这个月|当月", "DATE_FORMAT({c}, '%Y-%m') = DATE_FORMAT(CURDATE(), '%Y-%m')"),
    (r"上月|上个月", "DATE_FORMAT({c}, '%Y-%m') = DATE_FORMAT(CURDATE() - INTERVAL 1 MONTH, '%Y-%m')"),
    (r"今年|本年", "YEAR({c}) = YEAR(CURDATE())"),
    (r"去年", "YEAR({c}) = YEAR(CURDATE()) - 1"),
    (r"最近\s*(\d+)\s*天|近\s*(\d+)\s*天", "DATE({c}) >= CURDATE() - INTERVAL {n} DAY"),
    (r"最近\s*(\d+)\s*个?月|近\s*(\d+)\s*个?月", "DATE({c}) >= CURDATE() - INTERVAL {n} MONTH"),
]


def _single_agg(question: str, table: Table, alias: str) -> tuple[str, str] | None:
    """「平均/合计 + 单个字段」→ 单值聚合 SQL。

    只处理**不分组**的情况：问句里没有「每个/各个/按…统计」。
    分组聚合由后面的分组逻辑处理。
    """
    # 有分组意图的不走单值聚合：
    #   「每个项目有多少」「按项目统计」「每个项目的工时合计」
    if re.search(r"(?:每个|各个|各|按)\s*[\u4e00-\u9fa5]{2,8}?\s*(?:有多少|统计|分组|汇总)", question):
        return None
    if re.search(r"(?:每个|各个|各)\s*[\u4e00-\u9fa5]{2,10}?\s*的\s*[\u4e00-\u9fa5]{2,10}?"
                 r"\s*(?:合计|总计|总和|总量|平均|均值|分布|占比)", question):
        return None

    agg = ""
    if AVG_RE.search(question):
        agg = "AVG"
    elif SUM_RE.search(question):
        agg = "SUM"
    if not agg:
        return None

    # 找聚合目标列：问句点名的数值列 > 语义关键词暗示的数值列。
    # 拿不到就抛 NotUnderstood（由调用方转 AI），绝不生成 AVG(*)/SUM(*) 这种非法聚合。
    target = _require_agg_column(question, table, agg)

    nm = {"AVG": "平均", "SUM": "合计"}[agg]
    has_del = any(c.name == "deleted_" for c in table.columns)
    whr = SOFT_DELETE.format(alias=alias) if has_del else "1=1"
    sql = (f"SELECT {agg}(`{alias}`.`{target.name}`) AS `{nm}{target.label}`\n"
           f"FROM `{table.name}` `{alias}`\nWHERE {whr}\nLIMIT 1")
    return sql, f"本地规则生成：主表 {table.name}（{table.label}）；{nm}「{target.label}」"


def _rank_order(question: str, table: Table, alias: str,
                group_col: Column | None) -> tuple[str, int, str] | None:
    """解析「最X」「前N」→ (ORDER BY 子句, LIMIT, 说明)。

    优先级：
      1. 分组统计里按数量排（「任务最多的项目」）→ 排 COUNT
      2. 问句点名的字段 → 排那个字段
      3. 「最X」暗示的列类型 → 进度/金额/工期/时间

    条数：「进度最高的 5 个项目」「前 10 个」。「最高」后面紧跟数字也要认，
    否则 RANK_RE 先匹配到「最高」，数字就被跳过了。
    """
    m = RANK_RE.search(question)
    if not m:
        return None

    word = m.group(1) or ""
    top_n = int(m.group(2) or m.group(3) or 0)
    if not top_n:
        # 「最高的 5 个」「最大的十个」
        n2 = re.search(r"最[\u4e00-\u9fa5]{1,3}?\s*(?:的)?\s*(\d+)\s*[个条项]?", question)
        if n2:
            top_n = int(n2.group(1))
        else:
            n_cn = re.search(r"最[\u4e00-\u9fa5]{1,3}?\s*(?:的)?\s*([一二三四五六七八九十])\s*[个条项]", question)
            if n_cn:
                top_n = "一二三四五六七八九十".index(n_cn.group(1)) + 1

    if word:
        direction, hints = RANK_DIR.get(word, ("DESC", ()))
    else:
        direction, hints, word = "DESC", ("数量",), "前N"

    # 1) 分组计数：排 COUNT
    if group_col is not None and any(w in question for w in ("最多", "最少", "排名")):
        order = f"\nORDER BY COUNT(*) {'DESC' if direction == 'DESC' else 'ASC'}"
        return order, top_n or 20, f"按数量{'降序' if direction == 'DESC' else '升序'}取前 {top_n or 20}"

    # 2) 问句点名的字段
    qn = _norm(question)
    for c in table.columns:
        for lab in _label_variants(c, table):
            if len(lab) >= 2 and lab in qn and (c.name.endswith(("_", "id_")) or c.dtype):
                # 只认数值/时间列，别按名称列排序
                dt = (c.dtype or "").lower()
                if dt in ("int", "bigint", "decimal", "double", "float", "datetime",
                          "date", "timestamp", "smallint", "tinyint"):
                    return (f"\nORDER BY `{alias}`.`{c.name}` {direction}",
                            top_n or 20, f"按「{c.label}」{'降序' if direction == 'DESC' else '升序'}取前 {top_n or 20}")

    # 3) 按「最X」暗示的列
    for hint in hints:
        col = next((c for c in table.columns
                    if hint in _norm(c.label) or hint in c.name), None)
        if col is not None:
            return (f"\nORDER BY `{alias}`.`{col.name}` {direction}",
                    top_n or 20, f"按「{col.label}」{'降序' if direction == 'DESC' else '升序'}取前 {top_n or 20}")

    return None


def _date_columns(table: Table) -> list[Column]:
    return [c for c in table.columns
            if (c.dtype or "").lower() in ("datetime", "date", "timestamp")
            and not c.name.startswith(("update_", "create_by", "update_by"))]


def _time_condition(question: str, table: Table, alias: str) -> tuple[str, str] | None:
    """「本月」「最近7天」「今年」这类相对时间 → WHERE 片段。

    选列顺序：问句里点名的日期字段 > 语义关键词（创建/到期/开始）> create_time_。
    """
    q = question
    hit = None
    for pattern, tpl in TIME_RULES:
        m = re.search(pattern, q)
        if m:
            hit = (tpl, [g for g in m.groups() if g])
            break
    if hit is None:
        return None
    tpl, groups = hit

    dates = _date_columns(table)
    if not dates:
        return None

    target: Column | None = None
    # 1) 问句里点名的日期字段
    for c in dates:
        if any(lab in _norm(q) for lab in _label_variants(c, table)):
            target = c
            break
    # 2) 语义关键词
    if target is None:
        wanted = None
        if re.search(r"到期|截止|交付", q):
            wanted = ("due_time_", "finish_", "plan_finish_")
        elif re.search(r"开始|启动", q):
            wanted = ("start_", "plan_start_", "actual_start_")
        elif re.search(r"创建|新建|录入", q):
            wanted = ("create_time_",)
        for name in wanted or ("create_time_",):
            c = table.column(name)
            if c is not None and (c.dtype or "").lower() in ("datetime", "date", "timestamp"):
                target = c
                break
    if target is None:
        target = next((c for c in dates if c.name == "create_time_"), dates[0])

    expr = tpl.format(c=f"`{alias}`.`{target.name}`", n=groups[0] if groups else "")
    return expr, target.label


# ---------------------------------------------------------------- 语义模板
# 项目团队这类问题的表关系有歧义，靠最短路径猜必错：
#   pp_project_member 同时有 project_id_ 和 project_role_id_，
#   且 project_role_id_ 存的是「角色字典 id」（iform_object_role.id_），
#   不是 pp_project_role.id_（实测 49/49 匹配 iform_object_role，0/49 匹配 pp_project_role）。
# 所以走最短路径会得到「项目 → 成员」，丢掉角色这一层，
# 结果就是每个项目只显示一个人、其余角色全都看不见。
#
# 正确链路（业务上也这么理解）：
#   项目 → 项目角色(pp_project_role) → 角色名(iform_object_role)
#        → 该角色的成员(pp_project_member) → 人员(iform_party_employee)
RE_TEAM_ROLE = re.compile(r"团队|成员|角色|人员")
RE_COUNT_PERSON = re.compile(
    r"(?:多少|几)(?:个|名|位)?\s*(?:人|成员|角色)"     # 有多少人 / 几个成员
    r"|(?:成员数|人数|角色数)$"                        # 每个项目的成员数
)

TEAM_CHAIN = ("pp_project", "pp_project_role", "pp_project_member",
              "iform_object_role", "iform_party_employee")
# 这几张是平台内置表，不在 pack 扫描范围里（由 guard.PARTY_TABLES 放行）
TEAM_CHAIN_PACKED = ("pp_project", "pp_project_role", "pp_project_member")


def _quoted_name(q: str) -> str:
    """问句里用引号括起来的项目名：「XX项目」/ "XX" / 'XX'。"""
    m = re.search(r"[「『\"'“]([^」』\"'”]{2,30})[」』\"'”]", q)
    return m.group(1).strip() if m else ""


def _build_team_template(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    """项目 / 团队 / 角色 / 成员 的专用模板。命中返回 (sql, 说明)，否则 None。"""
    if not RE_TEAM_ROLE.search(q) or "项目" not in q:
        return None
    if not all(t in pack.tables for t in TEAM_CHAIN_PACKED):
        return None

    want_count = bool(RE_COUNT_PERSON.search(q))
    want_dist = bool(re.search(r"分布", q))       # 角色分布：按角色数人
    only_role = bool(re.search(r"角色", q)) and not re.search(r"成员|团队|人", q)
    name = _quoted_name(q)

    where = [
        "COALESCE(`p`.`deleted_`, 'N') <> 'Y'",
        "COALESCE(`pr`.`deleted_`, 'N') <> 'Y'",
    ]
    notes = ["按「项目 → 项目角色 → 角色成员」三层关联，角色为空也会列出来"]
    if name:
        safe = name.replace("'", "''")
        where.append(f"`p`.`name_` LIKE '%{safe}%'")
        notes.append(f"项目名包含「{name}」")

    # 分布：直接按角色数人，不需要 pp_project 参与
    if want_dist:
        sql = (
            "SELECT `r`.`name_` AS `项目角色`, COUNT(DISTINCT `pm`.`user_id_`) AS `数量`\n"
            "FROM `pp_project_member` `pm`\n"
            "LEFT JOIN `iform_object_role` `r` ON `r`.`id_` = `pm`.`project_role_id_`\n"
            "WHERE COALESCE(`pm`.`deleted_`, 'N') <> 'Y'\n"
            "GROUP BY `r`.`name_`\n"
            "ORDER BY `数量` DESC\n"
            "LIMIT 200"
        )
        return sql, "语义模板：各项目角色的人数分布（角色按人员去重统计）"

    # 每个项目多少人：按人去重（一个人挂多个角色只算一次）
    if want_count:
        sql = (
            "SELECT `p`.`name_` AS `项目名称`, "
            "COUNT(DISTINCT `pm`.`user_id_`) AS `数量`\n"
            "FROM `pp_project_member` `pm`\n"
            "JOIN `pp_project` `p` ON `p`.`id_` = `pm`.`project_id_`\n"
            "WHERE COALESCE(`pm`.`deleted_`, 'N') <> 'Y' "
            "AND COALESCE(`p`.`deleted_`, 'N') <> 'Y'\n"
            "GROUP BY `p`.`id_`, `p`.`name_`\n"
            "ORDER BY `数量` DESC\n"
            "LIMIT 200"
        )
        return sql, "语义模板：每个项目的人数（按角色链路统计，人去重）"

    if only_role:
        # 只问角色：不拉成员
        sql = (
            "SELECT `p`.`name_` AS `项目名称`, `r`.`name_` AS `项目角色`\n"
            "FROM `pp_project` `p`\n"
            "JOIN `pp_project_role` `pr` ON `pr`.`project_id_` = `p`.`id_`\n"
            "LEFT JOIN `iform_object_role` `r` ON `r`.`id_` = `pr`.`object_role_id_`\n"
            f"WHERE {' AND '.join(where)}\n"
            "ORDER BY `p`.`id_` DESC, `pr`.`order_`\n"
            "LIMIT 200"
        )
        return sql, "语义模板：项目的角色清单（" + "；".join(notes) + "）"

    # 常规：项目 + 角色 + 成员
    sql = (
        "SELECT `p`.`name_` AS `项目名称`,\n"
        "       `r`.`name_` AS `项目角色`,\n"
        "       `e`.`name_` AS `团队成员`\n"
        "FROM `pp_project` `p`\n"
        "JOIN `pp_project_role` `pr` ON `pr`.`project_id_` = `p`.`id_`\n"
        "LEFT JOIN `iform_object_role` `r` ON `r`.`id_` = `pr`.`object_role_id_`\n"
        "LEFT JOIN `pp_project_member` `pm`\n"
        "       ON `pm`.`project_id_` = `p`.`id_` "
        "AND `pm`.`project_role_id_` = `pr`.`object_role_id_`\n"
        "LEFT JOIN `iform_party_employee` `e` ON `e`.`id_` = `pm`.`user_id_`\n"
        f"WHERE {' AND '.join(where)} AND COALESCE(`pm`.`deleted_`, 'N') <> 'Y'\n"
        # 同一个项目内：有人的角色排前面，空角色排后面，避免一屏全是「—」
        "ORDER BY `p`.`id_` DESC, (`e`.`name_` IS NULL), `pr`.`order_`\n"
        "LIMIT 200"
    )
    return sql, "语义模板：项目的角色与角色成员（" + "；".join(notes) + "）"


def match_template(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    """只跑高置信语义模板（报表 + 团队）。

    给上层用：这类模板是验证过的确定性 SQL，应该**优先于历史示例**。
    历史示例是「AI 教过一次」的沉淀，可能来自早期错误答案，
    实测「超支的项目有哪些」就被旧示例带偏成了「项目名称|创建人|创建时间」。
    """
    from . import reports
    hit = reports.match(pack, q)
    if hit is not None:
        return hit
    return _build_team_template(pack, q)


def build(pack: SchemaPack, question: str) -> tuple[str, str]:
    """返回 (sql, 说明)。解析不了抛 NotUnderstood。"""
    q = question.strip()
    if not q:
        raise NotUnderstood("问题为空")

    # 报表类问题（指标/偏差/负载/分布）：启发式必答成明细清单，走语义模板
    from . import reports
    hit = reports.match(pack, q)
    if hit is not None:
        return hit

    # 再试团队语义模板：这类问题的表关系有歧义，启发式选路会走错。
    templated = _build_team_template(pack, q)
    if templated is not None:
        return templated

    candidates = _find_tables(pack, q)
    if not candidates:
        # 完全没认出来，但说了业务主体（收入/回款）→ 落到合同
        if re.search(r"收入|营收|流水|回款|签单", q) and "pp_contract" in pack.tables:
            candidates = [(1, pack.tables["pp_contract"])]
        else:
            raise NotUnderstood("没认出你要查哪个对象，试着带上对象名（如「项目」「任务」「风险」）")
    table = candidates[0][1]
    alias = "t"

    # 单一聚合：「项目的平均进度」「合同金额合计」「所有任务的总工期」
    # 这类问法只要一个数，不分组，直接出 AVG/SUM，别走后面的分组逻辑。
    single = _single_agg(q, table, alias)
    if single is not None:
        return single

    # 统计类问句：被统计的对象可能不是主表（「每个项目挂了多少人」数的是成员）
    if any(w in q for w in COUNT_WORDS) or re.search(r"(?:多少|几)个?(?:人|成员)", q):
        for pattern, tname in select.ORAL_COUNT_OBJECT:
            if re.search(pattern, q) and tname in pack.tables:
                table = pack.tables[tname]
                break

    # ---- 跨对象：问句里还提到别的对象（客户/产品/合同…），主表又没直接外键，
    #      就按关系图最短路径补 LEFT JOIN（客户→合同→项目 这种两跳）
    cross_joins: list[str] = []
    cross_alias: dict[int, str] = {}     # id(Column) -> 别名
    used_aliases = {alias}
    hops_budget = 2
    qn = _norm(q)
    for other in pack.tables.values():
        if other is table or hops_budget <= 0:
            continue
        olab = _norm(other.label).replace("表", "")
        hit_alias = olab if olab in qn else next((a for a in other.aliases if a in qn), "")
        if not hit_alias:
            continue
        # 「开发任务」里含「任务」，别把 pp_task 当成另一个对象挂进来
        # 「开发任务」vs「开发任务责任人」：后者是前者的关联/子表，不该当跨对象
        mt = _norm(table.label).replace("表", "")
        if hit_alias in mt:
            continue
        # 反向：命中的是「开发任务责任人」，主表是「开发任务」→ 它是主表的附属
        if mt in hit_alias and not hit_alias.startswith(mt):
            continue
        # 命中的对象和主表共享同一个核心词（开发任务/任务）→ 是同族，不是跨对象
        if hit_alias.endswith(mt[-2:]) or mt.endswith(hit_alias[-2:]):
            if not _norm(other.label).replace("表", "").startswith(mt):
                continue
        if any(c.ref_table == other.name for c in table.columns):
            continue                      # 已有直接外键，普通 join 就够
        # other 是主表的子表（它自己带 main 的外键）：挂上来会放大行数
        if any(c.ref_table == table.name for c in other.columns):
            continue
        path = graph.shortest_path(pack, table.name, other.name, max_hops=3)
        if not path:
            continue
        js, amap = graph.join_clauses(pack, table.name, path, alias, used_aliases)
        if not js:
            continue
        cross_joins.extend(js)
        used_aliases |= set(amap.values())
        for tname, a in amap.items():
            if tname == table.name:
                continue
            for c in pack.tables[tname].columns:
                cross_alias[id(c)] = (a, tname)
        hops_budget -= 1

    def _al(c: Column) -> str:
        # 跨表列（明确在 cross_alias 里登记的）一律用它自己的别名。
        # 不能按「同名就绑主表」判断——pp_customer.name_ 和 pp_project.name_
        # 同名，但分组列要的是客户名，不是项目名。
        if id(c) in cross_alias:
            return cross_alias[id(c)][0]
        return alias

    org_phrases = _user_org_phrases(q, table)
    picks = _pick_columns(q, table, skip=set())

    wants_count = any(w in q for w in COUNT_WORDS)
    # 聚合类型：COUNT（默认）/ AVG / SUM
    agg = "COUNT"
    if AVG_RE.search(q):
        agg = "AVG"
        wants_count = True
    elif SUM_RE.search(q):
        agg = "SUM"
        wants_count = True
    if not wants_count and re.search(r"(?:每个|各个|各).+?(?:数|量|个数)$", q):
        wants_count = True       # 「每个开发迭代的任务数」
    # 「每个风险的应对类型」「每个合同的付款进度」：没有统计词、句尾也不是
    # 「数/量/个数」时是「列出明细」，不是计数。只有明确要数量才聚合。
    if wants_count and re.search(r"^每个[\u4e00-\u9fa5]{2,}的[\u4e00-\u9fa5]{2,}$", q) \
            and not re.search(r"(?:数|量|个数)$", q) \
            and not any(w in q for w in ("多少", "几个", "数量", "条数", "统计", "总数")):
        wants_count = False
    if not wants_count and re.search(r"(?:按|分)\s*[\u4e00-\u9fa5]{2,10}?\s*(?:分一下|分组|分类|看一下|看看|拆一下)", q):
        wants_count = True       # 「项目按级别分一下」
    if re.search(r"(?:多少|几)个?(?:人|成员)", q):
        wants_count = True       # 「每个项目挂了多少人」
        if "pp_project_member" in pack.tables:
            table = pack.tables["pp_project_member"]

    # 「列出所有项目及其状态…」这类问法只会命中「状态」，标题列（项目名称）会被漏掉：
    # 只要提到了对象本身、且不是统计场景，就把标题列补到最前
    # 一个字段都没点名：给默认展示列（标题+状态+优先级+责任人+关键时间），
    # 比 SELECT * 有用得多——业务表动辄 50 列，全是 id_/tenant_/ip_ 噪音
    # 跨表对象：只补它的「标题列」一列（客户名称），别把整张跨表搬进来
    seen = {(table.name, c.name) for c, _ in picks}
    for tname in dict.fromkeys(tn for _, tn in cross_alias.values()):
        t2 = pack.tables[tname]
        if _norm(t2.label).replace("表", "") not in qn:
            continue                      # 只是路径中间表，别塞进结果列
        title = _title_column(t2)
        if title is None or (tname, title.name) in seen:
            continue
        picks.append((title, title.label))
        seen.add((tname, title.name))

    # 问「谁 / 责任人」：结果里必须有人员列，否则答非所问
    if not wants_count and re.search(r"谁|什么?人|责任人|负责人|处理人", q):
        if not any(c.ref_table == EMPLOYEE_TABLE for c, _ in picks):
            person = next((c for c in table.columns
                           if c.ref_table == EMPLOYEE_TABLE and
                           any(k in c.label for k in ("责任人", "负责人", "处理人", "执行人"))), None)
            if person is None:
                person = next((c for c in table.columns if c.ref_table == EMPLOYEE_TABLE), None)
            if person is not None:
                picks.append((person, person.label))

    if not picks and not wants_count:
        picks = [(c, c.label) for c in _default_columns(table)]

    # 注意：只有已经点名字段时才补，否则「列出所有项目」会从 SELECT * 变成只剩名称一列
    if not wants_count and picks and _norm(table.label) in _norm(q):
        title = _title_column(table)
        if title is not None and all(c.name != title.name for c, _ in picks):
            picks.insert(0, (title, title.label))
    # 「按X升序/降序」是排序不是分组；「平均/合计」也不分组。
    # 这两种都别让「按 + $」把整句吃成分组词。
    is_sort_by = re.search(r"按\s*[\u4e00-\u9fa5]{2,10}?\s*(?:升序|降序|正序|倒序|从大到小|从小到大|从高到低|从低到高)", q)
    if is_sort_by or (agg in ("AVG", "SUM")
                      and not re.search(r"(?:每个|各个|按)\s*[\u4e00-\u9fa5]{2,8}?\s*(?:统计|分组)", q)):
        group_match = None
    else:
        group_match = re.search(
            r"(?:每个|各个|各|按|分)\s*([\u4e00-\u9fa5]{2,10}?)"
            r"(?:有多少|有几|统计|分组|汇总|的数量|数量|$)", q)
    # 「每个风险的应对类型」：分组词里带「的」时，后半截（应对类型）才是分组维度，
    # 前半截（风险）是被描述的对象。直接取「的」后面那段。
    if group_match and "的" in group_match.group(1):
        tail = group_match.group(1).split("的")[-1]
        if len(tail) >= 2:
            group_match = _GroupHit(tail)
    select_parts, joins, notes = [], list(cross_joins), []

    # 「任务状态是已完成的有多少」：主表的状态列全空/恒定时，换到同族表。
    #
    # 这段以前太激进：只看 state_ 一个列，就断言"这张表没有可用状态"，
    # 于是「排期后状态是1的任务」被换成 pp_dev_task（它连 schedule_state_ 都没有）。
    # 现在两条约束：
    #   1) 问句点了具体字段名（排期后状态），且主表有这列 → 绝不换
    #   2) 状态列按画像判断：有中文枚举，或 distinct>=2 且有值，才算可用
    if re.search(r"(?:状态|进度).{0,4}(?:是|为)", q):
        state_cols = [c for c in table.columns
                      if "state" in c.name or "status" in c.name]
        named_state = [c for c in state_cols
                       if any(lab in _norm(q) for lab in _label_variants(c, table) if len(lab) >= 3)]
        usable = any(c.has_cn_enums or (c.distinct >= 2 and c.filled > 0)
                     for c in state_cols)
        if state_cols and not named_state and not usable:
            alt = next((t for t in pack.tables.values()
                        if t is not table and _norm(t.label).endswith(_norm(table.label).replace("表", ""))
                        and any(c.has_cn_enums for c in t.columns
                                if "state" in c.name or "status" in c.name)), None)
            if alt is not None:
                table = alt
                picks = _pick_columns(q, table, skip=set()) if q else picks

    group_col = None
    if group_match:
        g = group_match.group(1)
        # 「每个任务责任人有多少任务」：分组词是别的表上的真实字段时，换主表
        def _hit(tb: Table) -> Column | None:
            gg = _norm(g)
            for x in tb.columns:
                xl = _norm(x.label)
                if xl == gg or _strip_prefix(x.label, tb.label) == gg:
                    return x
                # 「项目」也能对上「隶属项目 / 关联项目」
                if len(gg) >= 2 and xl.endswith(gg):
                    return x
            return None

        in_cross = any(
            _norm(c.label) == _norm(g) or _strip_prefix(c.label, pack.tables[tn].label) == _norm(g)
            for _cid, (_a, tn) in cross_alias.items()
            for c in pack.tables[tn].columns if id(c) == _cid
        )
        # 分组词对应的是另一个对象（项目/客户）时，优先按关系图连过去，
        # 而不是把主表换掉——「每个项目有多少预算条目」要的是 budget_item 计数
        if _hit(table) is None and not in_cross:
            gt = next((t2 for t2 in pack.tables.values()
                       if t2 is not table and (
                           _norm(t2.label).replace("表", "") == _norm(g)
                           or _norm(g) in t2.aliases)), None)
            if gt is not None:
                # 「成员」这种说法，有更贴近的表（项目人员记录）就别走图
                better = next((t2 for t2 in pack.tables.values()
                               if t2 is not table and t2 is not gt
                               and any(a == _norm(g) for a in t2.aliases)
                               and _norm(gt.label).replace("表", "") in t2.aliases), None)
                if better is not None:
                    gt = better
                path = graph.shortest_path(pack, table.name, gt.name, max_hops=3)
                if path:
                    js, amap = graph.join_clauses(pack, table.name, path, alias, used_aliases)
                    cross_joins.extend(js)
                    joins.extend(j for j in js if j not in joins)   # 同步进本次 SQL
                    used_aliases |= set(amap.values())
                    for tname, a in amap.items():
                        if tname == table.name:
                            continue
                        for c2 in pack.tables[tname].columns:
                            cross_alias[id(c2)] = (a, tname)
                    title = _title_column(gt)
                    if title is not None:
                        group_col = title
                        notes.append(f"经 {len(path)} 跳关联到「{gt.label}」统计")
                    else:
                        # 目标表连名字列都没有（汇总表），这一跳白连——撤回
                        for j in js:
                            if j in cross_joins:
                                cross_joins.remove(j)

        if _hit(table) is None and not in_cross and group_col is None:
            for _, t2 in candidates[1:]:
                if _hit(t2) is not None:
                    table = t2
                    picks = _pick_columns(q, table, skip=set())  # 换表了，重新挑列
                    break
        gg = _norm(g)
        # 分组列同样优先绑「数据上能筛」的同名列
        for c in sorted(table.columns, key=_bind_priority):
            cl = _norm(c.label)
            if cl == gg or _strip_prefix(c.label, table.label) == gg or (
                len(gg) >= 2 and cl.endswith(gg)
            ):
                # 主表自己的 name_ 匹配上「项目」时，其实是「预算条目名」被
                # 显示成「项目名称」，不是分组维度——留给外键/关系图判断
                if c.name == "name_" and gg != _norm(table.label).replace("表", ""):
                    continue
                group_col = c
                break
        if group_col is not None:
            # 主表自己有这列就绑主表，别被跨表同名列劫持
            pass
        if group_col is None:
            gg2 = _norm(g)
            gt0 = next((t2 for t2 in pack.tables.values()
                        if t2 is not table and (
                            _norm(t2.label).replace("表", "") == gg2
                            or gg2 in t2.aliases)), None)
            for _cid, (_a, _tn) in cross_alias.items():
                if gt0 is not None and _tn != gt0.name:
                    continue
                t2 = pack.tables[_tn]
                for c in t2.columns:
                    if id(c) != _cid:
                        continue
                    if c.name in ("name_", "title_", "label_"):
                        group_col = c
                        break
                if group_col is not None:
                    break


    # 先把条件算出来，才能决定「筛过的列」要不要展示
    where, _ = _condition(q, table, pack, alias)
    if not wants_count and where:
        for c in table.columns:
            if f"`{alias}`.`{c.name}`" in where and all(c2.name != c.name for c2, _ in picks):
                picks.append((c, c.label))

    used_emp = False

    # 1) 分组统计
    if group_col is None and wants_count and picks:
        # 「每个项目有多少预算条目」：picks 里的「项目名称」其实是本人主表的
        # name_（预算条目名），不能当分组维度。分组维度必须是**指向另一个对象的外键**，
        # 或者是问句点名的那个字段本身。
        gm = re.search(r"(?:每个|各个|各|按)\s*([\u4e00-\u9fa5]{2,10}?)(?:有多少|有几|的|$)", q)
        want = _norm(gm.group(1)) if gm else ""

        def _reflabel(name: str) -> str:
            # 外键可能指向平台表（iform_party_employee），它不在语义层里
            t = pack.tables.get(name)
            return _norm(t.label) if t is not None else name

        fk = next((c for c, _ in picks if c.ref_table and want and
                   (want in _norm(c.label) or want in _reflabel(c.ref_table))), None)
        # picks 里的列如果是主表自己的 name_，它是「本行名字」，不是分组维度：
        # 「每个项目有多少预算条目」picks 会是 预算条目.name_（别名显示成项目名称）
        own_name_ids = {id(c) for c in table.columns if c.name == "name_"}
        bad_pick = any(id(c) in own_name_ids for c, _ in picks[:1])
        if fk is not None:
            group_col = fk
        elif want and (bad_pick or not any(want in _norm(c.label) for c, _ in picks)):
            # 问句要按「项目」分组，但 picks 里没有项目列 → 找指向项目的外键
            fk2 = next((c for c in table.columns if c.ref_table and
                        want in _reflabel(c.ref_table)), None)
            if fk2 is not None:
                group_col = fk2
            else:
                # 本地没有直接外键（预算条目→项目中间隔着项目预算）→ 走关系图
                gt = next((t2 for t2 in pack.tables.values()
                           if t2 is not table and (
                               _norm(t2.label).replace("表", "") == want
                               or want in t2.aliases)), None)
                if gt is not None:
                    path = graph.shortest_path(pack, table.name, gt.name, max_hops=3)
                    if path:
                        js, amap = graph.join_clauses(pack, table.name, path, alias, used_aliases)
                        if js:
                            cross_joins.extend(js)
                            used_aliases |= set(amap.values())
                            for tname, a in amap.items():
                                if tname == table.name:
                                    continue
                                for c2 in pack.tables[tname].columns:
                                    cross_alias[id(c2)] = (a, tname)
                            title = _title_column(gt)
                            if title is not None:
                                group_col = title
                                notes.append(f"经 {len(path)} 跳关联到「{gt.label}」统计")
        if group_col is None and wants_count:
            group_col = picks[0][0]

    # 「每个风险的应对类型」这类只列明细的问法：分组列指向的正是 picked 的枚举/文本列，
    # 且用户没要数量 → 不聚合，把它当普通选列
    if group_col is not None and not any(
            w in q for w in ("多少", "几个", "数量", "条数", "统计", "总数", "分布")) \
            and not re.search(r"(?:数|量|个数)$", q) \
            and re.match(r"^每个[\u4e00-\u9fa5]{2,}的[\u4e00-\u9fa5]{2,}$", q):
        wants_count = False
        group_col = None

    if group_col is not None or (wants_count and not picks):
        # 兜底：没有分组列时，用主表自己的第一个外键；不要再退到「跨表任意列」，
        # 否则「每个风险的应对类型」会聚合出错（1140）
        gc = group_col
        if gc is None and picks:
            gc = picks[0][0]
        if gc is None:
            gc = next((c for c in table.columns if c.ref_table), None)
        pk = next((c.name for c in table.columns if c.name == "id_"),
                  table.columns[0].name if table.columns else "id_")
        if gc is not None and gc.ref_table == EMPLOYEE_TABLE:
            # 人员是多值（逗号分隔），必须 explode 成「一人一行」再按人计数，
            # LEFT JOIN + GROUP BY 名字会把一条任务只算给第一个人
            ja = f"g_{gc.name}"
            joins.append(
                f"JOIN `{EMPLOYEE_TABLE}` `{ja}` "
                f"ON FIND_IN_SET(`{ja}`.`id_`, `{_al(gc)}`.`{gc.name}`) > 0"
            )
            # 跨表 join 可能是 LEFT，会把没关联的行也带进来，导致人员 explode 后重复计数
            notes.append("人员展开计数")
            select_parts.append(f"`{ja}`.`name_` AS `{gc.label}`")
            select_parts.append(f"COUNT(DISTINCT `{alias}`.`{pk}`) AS `数量`")
            notes.append(f"按「{gc.label}」逐人统计（多值字段展开计数）")
        elif gc is not None:
            if gc.ref_table:
                if gc.multi:
                    expr, jn = _fk_select(gc, _al(gc), gc.label, pack)
                else:
                    ja = f"r_{gc.name}"
                    jn = (f"LEFT JOIN `{gc.ref_table}` `{ja}` "
                          f"ON `{ja}`.`id_` = `{_al(gc)}`.`{gc.name}`")
                    # 分组时 NULL 归成「未关联」，否则结果里冒一行 None 很难读
                    lc = _label_col_of(pack, gc.ref_table)
                    expr = f"IFNULL(`{ja}`.`{lc}`, '未关联') AS `{gc.label}`"
                select_parts.append(expr)
                if jn and jn not in joins:
                    joins.append(jn)
                if gc.ref_table == EMPLOYEE_TABLE:
                    used_emp = True
            else:
                select_parts.append(f"`{_al(gc)}`.`{gc.name}` AS `{gc.label}`")
            # 聚合表达式：只有 COUNT 能用 `*`；AVG/SUM 必须带真实数值列，
            # 否则就是 `AVG(*)`——MySQL 语法错误，而 sqlglot 能解析，安全闸看不见。
            if agg in ("AVG", "SUM"):
                agg_col = _require_agg_column(q, table, agg)
                nm = "平均" if agg == "AVG" else "合计"
                select_parts.append(
                    f"{agg}(`{_al(agg_col)}`.`{agg_col.name}`) "
                    f"AS `{nm}{agg_col.label}`")
                notes.append(f"{nm}「{agg_col.label}」")
            else:
                select_parts.append("COUNT(*) AS `数量`")
            notes.append(f"按「{gc.label}」分组统计")
        else:
            select_parts.append("COUNT(*) AS `数量`")
            notes.append("统计总数")

    # 2) 普通选列
    for c, label in ([] if (wants_count and group_col is not None) else picks):
        if wants_count and group_col is not None and c.name == group_col.name:
            continue
        # 分组列叫「任务状态」时，别再单出一列「状态」（label 是它的后缀）
        if wants_count and group_col is not None and _norm(label) in _norm(group_col.label):
            continue
        if c.ref_table in (EMPLOYEE_TABLE, ORG_TABLE) or (c.ref_table or "").startswith("pp_"):
            if c.ref_table == EMPLOYEE_TABLE:
                used_emp = True
            expr, j = _fk_select(c, _al(c), label, pack)
            if j and j not in joins:
                joins.append(j)
            select_parts.append(expr)
        else:
            select_parts.append(f"`{_al(c)}`.`{c.name}` AS `{label}`")

    # 3) 两跳：人员的所属部门
    for col_name, alias_label in org_phrases.items():
        used_emp = True
        emp_col = next((x for x in table.columns if x.name == col_name), None)
        expr, extra = _org_select(emp_col, alias, alias_label)
        joins.extend([j for j in extra if j not in joins])
        select_parts.append(expr)
        notes.append(f"「{alias_label}」经人员表两跳关联到组织表")

    if not select_parts:
        select_parts.append(f"`{alias}`.*")
        notes.append("没识别到具体字段，返回整行")

    where, _ = _condition(q, table, pack, alias)

    # WHERE 里筛过的列，结果里也展示出来（问了「高风险」，就该看到风险等级）
    if not wants_count and where:
        for c in table.columns:
            tag = f"`{alias}`.`{c.name}`"
            if tag in where and all(c2.name != c.name for c2, _ in picks):
                picks.append((c, c.label))
    time_hit = _time_condition(q, table, alias)
    if time_hit:
        expr, tlabel = time_hit
        notes.append(f"按「{tlabel}」限定时间范围")
        where = f"({where}) AND {expr}" if where else expr
    # 有些补漏表（iform_bo_column 兜底进来的）没有 deleted_ 列
    has_deleted = any(c.name == "deleted_" for c in table.columns)
    where = where or (SOFT_DELETE.format(alias=alias) if has_deleted else "1=1")

    # 有聚合时，非聚合列必须进 GROUP BY，否则 MySQL 直接报 1140。
    # 聚合表达式（COUNT/AVG/SUM/MAX/MIN）本身不能进 GROUP BY。
    group_by = ""
    if wants_count:
        plain = [p for p in select_parts
                 if not re.search(r"\b(COUNT|AVG|SUM|MAX|MIN)\(", p)]
        if plain:
            # 去掉 " AS `别名`"，剩下的表达式整体进 GROUP BY
            # （多值外键是标量子查询，整段放进去才不会被 only_full_group_by 拦）
            exprs = [re.sub(r"\s+AS\s+`[^`]+`$", "", item).strip() for item in plain]
            exprs = [e for e in exprs if e]
            if exprs:
                group_by = "\nGROUP BY " + ", ".join(exprs)

    order = ""
    limit = 200

    # 排名：「进度最高的5个项目」「金额最大的合同」
    # CSpider 真实语料里这类占 17%，必须支持，否则返回 200 行等于没排
    rank = _rank_order(q, table, alias, group_col)
    if rank:
        order, limit, why = rank
        notes.append(why)
    elif any(w in q for w in RECENT_WORDS):
        order = f"\nORDER BY `{alias}`.`create_time_` DESC"
        notes.append("按创建时间倒序")

    # 「按X降序/升序」：显式点名排序字段
    if not order:
        m = re.search(r"按\s*([\u4e00-\u9fa5]{2,10}?)\s*(降序|倒序|从大到小|从高到低)", q)
        asc = False
        if not m:
            m = re.search(r"按\s*([\u4e00-\u9fa5]{2,10}?)\s*(升序|正序|从小到大|从低到高)", q)
            asc = True
        if m:
            want = _norm(m.group(1))
            col = next((c for c in table.columns
                        if _norm(c.label) == want or _norm(c.label).endswith(want)
                        or _strip_prefix(c.label, table.label) == want), None)
            if col is not None:
                order = f"\nORDER BY `{_al(col)}`.`{col.name}` {'ASC' if asc else 'DESC'}"
                notes.append(f"按「{col.label}」{'升序' if asc else '降序'}")

    sql = (
        "SELECT " + ", ".join(select_parts)
        + f"\nFROM `{table.name}` `{alias}`"
        + ("\n" + "\n".join(joins) if joins else "")
        + f"\nWHERE {where}"
        + group_by
        + order
        + f"\nLIMIT {limit}"
    )
    return sql, f"本地规则生成：主表 {table.name}（{table.label}）" + (
        "；" + "；".join(notes) if notes else ""
    )
