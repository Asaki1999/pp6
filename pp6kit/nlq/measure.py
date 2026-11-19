"""度量模板：把「按项目统计工时」「工时最多的 5 个人」落成 维度 + 度量的 GROUP BY。

## 为什么需要这一层

原来的写法把「工时」当成一个**对象**去数行数：

    按项目统计工时 → SELECT 项目, COUNT(*) FROM pp_timesheet GROUP BY 项目

得到的是「每个人在项目下随手填了几条记录」，不是工时。原因不是某条规则写错，
而是引擎只有「选表 + 数行」，没有「度量」这个概念——句子里的「工时」「金额」
「进度」本该是**被聚合的列**，却只能被当成分组对象或干脆忽略。

## 模型借自 8112 图表自定义的数据源

系统里的图表配置（`custom_page_widget.config`）把一件事拆成两半：

    labelKey / groupKey   → 维度（分组列，字符串、码值、日期）
    valueKeys + displayType → 度量（列 + sum/avg/count）

统计时前端只传 `dimension`、`group`、`calculateField`（field + method），
后端拼 GROUP BY。维度和度量分开了，就不会出现「把工时数量成记录数」这种错。

本模块照这个模型做最小实现：
- `MEASURES`：哪些词是度量，落到哪张表哪一列，默认怎么聚合
- `_dimension()`：把「按 X」「每个 X」「X 最多的 N 个」里的 X 解成维度列
- 时间维度：按月份/季度/年 → DATE_FORMAT 分组（图表里的 periodKey）

原则：**认不准就返回 None**，交回原来的启发式路径，绝不猜着拼 SQL。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import graph
from .introspect import SchemaPack, Table, Column

EMPLOYEE_TABLE = "iform_party_employee"


@dataclass(frozen=True)
class Measure:
    """一个业务度量：词 → 表.列 + 默认聚合。"""

    keys: tuple[str, ...]          # 问句里可能出现的说法
    table: str                     # 主表
    column: str                    # 度量列
    agg: str                       # SUM / AVG
    label: str                     # 结果列名
    # 需要区分场景时才用：问句里出现这些词就换表（任务工期 vs 项目工期）
    alt: tuple[tuple[str, str, str], ...] = ()   # (触发词, 表, 列)


# 只收「库里真有数据、且业务上真会被问」的度量。
# 加新度量先对着库确认列不是全 NULL：pp_project.total_cost_ 全空，
# 实际成本只能走 pp_project_cost.amount_。
MEASURES: tuple[Measure, ...] = (
    Measure(("工时", "小时数", "投入工时", "工作量"),
            "pp_timesheet", "normal_hours_", "SUM", "工时合计"),
    Measure(("成本", "实际成本", "费用", "花费", "支出"),
            "pp_project_cost", "amount_", "SUM", "成本合计"),
    Measure(("预算",), "pp_project", "total_budget_", "SUM", "预算合计"),
    Measure(("合同金额", "合同额", "签约额", "签约金额"),
            "pp_contract", "amount_", "SUM", "合同金额"),
    Measure(("回款", "收款", "已收金额"),
            "pp_contract", "total_payment_amount", "SUM", "已收金额"),
    Measure(("进度",), "pp_project", "progress_", "AVG", "平均进度",
            alt=(("任务", "pp_task", "progress_"),)),
    Measure(("工期", "时长"), "pp_project", "duration_", "SUM", "工期合计",
            alt=(("任务", "pp_task", "duration_"),)),
    Measure(("标准工时",), "pp_task", "standard_hours_", "SUM", "标准工时合计"),
)

# 聚合意图：没有这些词就还是明细清单，别乱聚合。
# 「按 X 统计」「每个 X 的 Y 合计」「Y 最多」都是图表式问法。
_AGG_INTENT = re.compile(
    r"合计|总计|总和|总量|总共|共计|一共|汇总|平均|均值|"
    r"最多|最少|最高|最低|最大|最小|"
    r"前\s*\d+|top\s*\d+|"
    r"按.{1,12}?(?:统计|汇总|分组|分布|分一下)|"
    r"(?:每个|各个|每).{1,12}?(?:的)?.{0,6}(?:合计|总量|总和|平均|多少)"
)
_AVG = re.compile(r"平均|均值|人均")
# 「每个项目有多少预算条目」问的是条数，不是度量——这种必须交回计数路径
_COUNT_ASK = re.compile(r"多少(?:个|条|项|笔|份|次|人|位)?|几个|几条|几项|几位")
_MEASURE_STRONG = re.compile(r"合计|总计|总和|总量|总共|共计|一共|汇总|累计|平均|均值")

# 允许跨表取的维度白名单。
# 8112 里维度只能从数据集声明过的列里选（labelKey/groupKey），不是任意表都能当维度；
# 这里同理——只放业务上组含义明确的维度，其余交回通用路径，避免拼出
# 「按预算大类汇总项目预算」这种语义错、结果空的 SQL。
DIM_WHITELIST: tuple[str, ...] = (
    "项目", "项目类型", "部门", "客户", "产品", "阶段", "状态", "类型",
    "等级", "优先级", "人", "人员", "成员", "负责人", "责任人", "填报人",
)
_TOP = re.compile(r"(?:前|top\s*)(\d+)|最[多高大小].{0,6}?(\d+)\s*个", re.I)
_TIME_DIM = re.compile(r"(?:按|每个|分)?\s*(月份|月度|月|季度|年份|年度|年|周|星期|天|日)")
_TIME_FMT = {
    "月": "%Y-%m", "月份": "%Y-%m", "月度": "%Y-%m",
    "季度": "%Y-Q%q", "年": "%Y", "年份": "%Y", "年度": "%Y",
    "周": "%x-W%v", "星期": "%x-W%v", "天": "%Y-%m-%d", "日": "%Y-%m-%d",
}

# 维度词 → 首选匹配的列语义（标签里含这些词的列优先）
_DIM_HINT: dict[str, tuple[str, ...]] = {
    "人": ("填报人", "责任人", "负责人", "处理人", "成员", "人员", "执行人"),
    "人员": ("填报人", "责任人", "负责人", "处理人", "成员", "人员", "执行人"),
    "成员": ("填报人", "责任人", "负责人", "成员", "人员"),
    "项目": ("项目",),
    "部门": ("部门", "组织"),
}


def _match_measure(pack: SchemaPack, q: str) -> tuple[Measure, Table] | None:
    """找出问句提到的度量；同一个词命中多张表时按问句里的对象名定表。"""
    best: tuple[int, Measure, Table] | None = None
    for m in MEASURES:
        if m.table not in pack.tables:
            continue
        for key in m.keys:
            if key not in q:
                continue
            table_name, column = m.table, m.column
            for trigger, alt_table, alt_col in m.alt:
                # 问句点名了「任务」→ 度量落到任务表，而不是项目表
                if trigger in q and alt_table in pack.tables:
                    table_name, column = alt_table, alt_col
                    break
            t = pack.tables[table_name]
            if not any(c.name == column for c in t.columns):
                continue
            # 越长的词越具体（「合同金额」优先于「金额」），同长取靠前的
            score = len(key)
            if best is None or score > best[0]:
                best = (score, m, t)
    if best is None:
        return None
    return best[1], best[2]


def _agg_of(q: str, default: str) -> str:
    if _AVG.search(q):
        return "AVG"
    if re.search(r"合计|总计|总和|总量|总共|共计|一共|汇总|累计|总", q):
        return "SUM"
    return default


def _limit_of(q: str) -> int:
    m = _TOP.search(q)
    if m:
        n = m.group(1) or m.group(2)
        try:
            return max(1, min(200, int(n)))
        except (TypeError, ValueError):
            pass
    return 200


def _norm_dim(word: str) -> str:
    """维度词归一：「5 个人」→「人」，「项目类型」保持原样。"""
    w = re.sub(r"^\d+\s*个", "", word.strip())
    w = re.sub(r"^个", "", w)
    return w or word


def _dim_word(q: str) -> str | None:
    """抽出维度词。认不出返回 None。"""
    # 每个项目类型的平均进度 → 「每个 X 的」里的 X 才是维度
    m = re.search(r"(?:每个|各个|每)\s*([\u4e00-\u9fa5A-Za-z]{1,10}?)\s*的", q)
    if m:
        return _norm_dim(m.group(1))
    # 工时最多的 5 个人 / 成本最高的前 3 个项目：句尾的名词是维度
    m = re.search(r"(?:最多|最少|最高|最低|最大|最小)[^，。]{0,10}?"
                  r"(?:\d+\s*个)?\s*([\u4e00-\u9fa5]{1,6})$", q)
    if m:
        return _norm_dim(m.group(1))
    # 按 X 统计 / 按 X 汇总
    m = re.search(r"按\s*([\u4e00-\u9fa5A-Za-z]{1,10}?)\s*(?:来)?\s*(?:统计|汇总|分组|分布|分一下|算|看)", q)
    if m:
        return _norm_dim(m.group(1))
    # 每个 X（句尾没有「的」）
    m = re.search(r"(?:每个|各个|每)\s*([\u4e00-\u9fa5A-Za-z]{1,10})$", q)
    if m:
        return _norm_dim(m.group(1))
    # X 分布 / X 占比
    m = re.search(r"([\u4e00-\u9fa5]{2,6})\s*(?:分布|占比|构成)", q)
    if m:
        return _norm_dim(m.group(1))
    return None


def _date_col(table: Table, q: str) -> Column | None:
    """时间维度落到哪一列：优先问句点名的（工时日期/发生日期），否则 create_time_。"""
    dates = [c for c in table.columns if c.dtype in ("date", "datetime")]
    if not dates:
        return None
    for c in dates:
        if c.label and c.label[:2] in q:
            return c
    for c in dates:
        if c.dtype == "date" and not c.name.startswith("update"):
            return c
    for c in dates:
        if c.name == "create_time_":
            return c
    return dates[0]


def _time_expr(col: Column, alias: str, word: str) -> str:
    fmt = _TIME_FMT.get(word, "%Y-%m")
    if fmt == "%Y-Q%q":
        return f"CONCAT(YEAR(`{alias}`.`{col.name}`), '-Q', QUARTER(`{alias}`.`{col.name}`))"
    return f"DATE_FORMAT(`{alias}`.`{col.name}`, '{fmt}')"


def _col_by_label(table: Table, word: str, pack: SchemaPack) -> Column | None:
    """在表内按标签找维度列。

    顺序很重要：精确 → 后接（「归属项目」对「项目」）→ 语义提示 → 目标表标签。
    自引用外键（父项目、上级项目）不参与——「按项目看工时」要的是项目本身，
    不是「按父项目看」。
    """
    hints = _DIM_HINT.get(word, ())
    cols = [c for c in table.columns
            if c.label and c.label not in ("", "对象的类型")
            and not (c.ref_table == table.name)]      # 自引用外键跳过

    for c in cols:
        if c.label == word:
            return c
    for c in cols:
        if len(word) >= 2 and (c.label or "").endswith(word):
            return c
    for h in hints:
        for c in cols:
            if h in (c.label or ""):
                return c
    for c in cols:
        if word and len(word) >= 2 and (c.label or "").endswith(word[-2:]):
            return c
    for c in cols:
        if c.ref_table and c.ref_table in pack.tables:
            tl = pack.tables[c.ref_table].label or ""
            for h in (hints or (word,)):
                if h and len(h) >= 2 and (h in tl or tl in h):
                    return c
    return None


def _table_by_label(pack: SchemaPack, word: str) -> Table | None:
    for t in pack.tables.values():
        lab = (t.label or "").replace("表", "")
        if not lab or not t.usable:
            continue
        if lab == word or (len(word) >= 2 and word in lab):
            return t
    return None


def _fk_expr(col: Column, alias: str, label: str, pack: SchemaPack
             ) -> tuple[str, list[str], str]:
    """外键维度显示名称而不是 id。多值字段仍用子查询，避免行数被拆开。

    注意：人员/组织表（iform_party_*）不在 pack.tables 里（它们由 guard 单独放行），
    所以这里不能要求目标表必须在 pack.tables 中，否则「工时最多的人」会吐出原始 id。
    """
    if not col.ref_table:
        return f"`{alias}`.`{col.name}`", [], f"`{alias}`.`{col.name}`"
    target = pack.tables.get(col.ref_table)
    lc = None
    if target is not None:
        lc = next((c.name for c in target.columns if c.label in ("名称", "标题", "任务名称")), None) \
            or ("name_" if any(c.name == "name_" for c in target.columns) else None)
    lc = lc or "name_"
    multi = col.multi or col.name.endswith("_ids_")
    if multi:
        expr = (f"(SELECT GROUP_CONCAT(`j`.`{lc}` SEPARATOR '、') FROM `{col.ref_table}` `j` "
                f"WHERE FIND_IN_SET(`j`.`id_`, `{alias}`.`{col.name}`))")
        return expr, [], expr
    ja = f"d_{col.name}"
    join = f"LEFT JOIN `{col.ref_table}` `{ja}` ON `{ja}`.`id_` = `{alias}`.`{col.name}`"
    # 按 id 分组、显示名称：两个人同名时不会被合成一行
    return f"`{ja}`.`{lc}`", [join], f"`{ja}`.`id_`"


def _dimension(pack: SchemaPack, table: Table, q: str, alias: str,
               ) -> tuple[str, list[str], str, str] | None:
    """解析维度 → (SELECT 表达式, JOIN 子句, 维度名, GROUP BY 表达式)。认不出返回 None。"""
    word = _dim_word(q)
    if not word:
        return None

    # 时间维度
    if _TIME_DIM.fullmatch(word) or word in _TIME_FMT:
        col = _date_col(table, q)
        if col is None:
            return None
        expr = _time_expr(col, alias, word)
        return expr, [], word, expr

    # 维度就是这个对象本身（「进度最低的项目」）→ 按主表标题列分组
    own = _table_by_label(pack, word)
    if own is not None and own.name == table.name:
        title = next((c for c in table.columns if c.label in ("名称", "标题")), None) \
            or next((c for c in table.columns if c.name == "name_"), None)
        if title is not None:
            return f"`{alias}`.`{title.name}`", [], title.label or word, f"`{alias}`.`id_`"

    # 表内维度（含外键名称化）
    col = _col_by_label(table, word, pack)
    if col is not None:
        expr, joins, group = _fk_expr(col, alias, col.label or word, pack)
        return expr, joins, col.label or word, group

    # 跨表维度只在白名单内放行：工时 → 项目 → 项目类型 可以，
    # 「预算大类」这种不在白名单的维度交回通用路径
    if word not in DIM_WHITELIST:
        return None
    target = _table_by_label(pack, word)
    if target is None or target.name == table.name:
        return None
    path = graph.shortest_path(pack, table.name, target.name, max_hops=3)
    if not path:
        return None
    joins, amap = graph.join_clauses(pack, table.name, path, alias, {alias})
    a = amap.get(target.name)
    if not a:
        return None
    lc = next((c.name for c in target.columns if c.label in ("名称", "标题")), None) \
        or ("name_" if any(c.name == "name_" for c in target.columns) else None)
    if not lc:
        return None
    return f"`{a}`.`{lc}`", joins, target.label or word, f"`{a}`.`{lc}`"


def build(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    """命中度量模板返回 (sql, 说明)，否则 None（交回原路径）。"""
    if not _AGG_INTENT.search(q):
        return None
    # 「有多少/几个」是计数不是求和：除非同时出现「合计/平均/总量」这类强度量词
    if _COUNT_ASK.search(q) and not _MEASURE_STRONG.search(q):
        return None

    hit = _match_measure(pack, q)
    if hit is None:
        return None
    measure, table = hit
    column = next((c for c in table.columns if c.name == measure.column), None)
    if column is None:
        return None

    # 「进度最高的 5 个项目」这类**实体排名**归原有的排名路径：它要的是每个项目一行、
    # 按进度排序后取前 N，不是「按项目聚合出平均值」。度量模板在这里退位更稳。
    word = _dim_word(q)
    if word and re.search(r"最多|最少|最高|最低|最大|最小|前\s*\d+|top\s*\d+", q, re.I):
        own = _table_by_label(pack, word)
        if own is not None and own.name == table.name:
            return None

    agg = _agg_of(q, measure.agg)
    # 平均「工时」= AVG(normal_hours_)；平均但度量本身是 AVG（进度）不重复算
    if agg == "AVG" and measure.agg == "AVG":
        agg = "AVG"

    alias = "t"
    dim = _dimension(pack, table, q, alias)
    if dim is None:
        return None                       # 没有维度就不是图表式问法，交回原路径
    dim_expr, joins, dim_label, group_expr = dim

    label = measure.label
    if agg == "AVG" and "平均" not in label:
        label = "平均" + label.replace("合计", "")
    if agg == "SUM" and "合计" not in label and "金额" not in label:
        label = label.replace("合计", "") + "合计"

    where = []
    if any(c.name == "deleted_" for c in table.columns):
        where.append(f"COALESCE(`{alias}`.`deleted_`, 'N') <> 'Y'")
    # 去掉软删除后仍可能有 NULL 维度，单独成组没有意义
    is_time = dim_expr.startswith("DATE_FORMAT") or dim_expr.startswith("CONCAT(YEAR")
    if not is_time:
        where.append(f"{dim_expr} IS NOT NULL")

    # 时间维度按时间正序（趋势图要顺着看）；其余按度量排序，
    # 「最低/最少」是升序，「最多/最高」和不带方向时按降序（图表默认 TopN 视角）
    if is_time:
        order = f"ORDER BY {dim_expr} ASC"
    elif re.search(r"最低|最少|最小|升序|从小到大|从低到高", q):
        order = f"ORDER BY `{label}` ASC"
    else:
        order = f"ORDER BY `{label}` DESC"

    sql = (
        f"SELECT {dim_expr} AS `{dim_label}`,\n"
        f"       {agg}(`{alias}`.`{column.name}`) AS `{label}`\n"
        f"FROM `{table.name}` `{alias}`\n"
        + "".join(j + "\n" for j in joins)
        + (f"WHERE {' AND '.join(where)}\n" if where else "")
        + f"GROUP BY {group_expr}\n"
        f"{order}\n"
        f"LIMIT {_limit_of(q)}"
    )
    note = f"度量模板：{measure.label} {'/'.join(measure.keys[:2])} → {agg}({column.name})"
    note += f"；维度「{dim_label}」；数据源 {table.name}"
    return sql, note
