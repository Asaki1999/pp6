"""主表选择：一次打分决策，而不是散落的 if 改写。

背景：原来的 build() 里有 14 处 `table = xxx`，每修一条口语就会破坏一条旧用例。
这里借鉴 WrenAI 语义层的思路——把「物理表」和「业务概念」分开：

    用户说的话  →  匹配到哪个业务概念（对象/字段/口语/句式）
                →  每个候选表按命中情况累计权重
                →  取最高分，只选一次

这样新增口语只要往 ALIAS / ORAL 表里加一行，不用再插 if，
也不会影响已有的判定。

对外只暴露一个函数：choose(pack, question) -> Choice
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .introspect import SchemaPack, Table

# ---------------------------------------------------------------- 词表
# 全部集中在这里，新增口语只加数据、不改逻辑。

# 业务口语 → 表名（按顺序尝试，命中即用）
# 这些是「用户不会说库里的名字」时最可靠的线索
ORAL_ENTITY: tuple[tuple[str, str], ...] = (
    (r"收入|营收|流水|回款|签单|合同额", "pp_contract"),
    (r"花在|花销|花哪|花费|预算明细|预算项", "pp_project_budget_item"),
    (r"待办|todo", "pp_todo"),
    (r"迭代|sprint", "pp_dev_sprint"),
    (r"缺陷|bug", "pp_defect"),
    (r"需求", "pp_requirement"),
    (r"里程碑", "pp_task"),
)

# 统计对象替换：「挂了多少人」问的是人，不是项目
ORAL_COUNT_OBJECT: tuple[tuple[str, str], ...] = (
    (r"(?:多少|几)个?(?:人|成员)", "pp_project_member"),
)

# 表名后缀 → 该表是「头表」还是「明细表」
# 用户说「预算」时通常指明细（pp_project_budget_item），不是抬头表
DETAIL_SUFFIX = ("_item", "_detail", "_record", "_line", "_entry")
HEAD_SUFFIX = ("_config", "_setting", "_type", "_state", "_grade", "_domain_sum")

# 状态类条件：主表状态列没有可用枚举时，换到同族里带状态码的表
STATE_WORDS = re.compile(r"(?:状态|进度).{0,6}(?:是|为|不是|不为)")

# 中文问句的核心在**句尾**，句首多是修饰。这三种句式必须按语序纠正，
# 否则「每个项目有多少阶段」会因为「项目」在句首而落到 pp_project。
#   每个 X 有多少 Y      → Y 是被查对象
#   每个 X 的 Y 数       → Y 是被查对象
#   X 都有哪些 Y         → Y 是被查对象
RE_EACH_COUNT = re.compile(r"(?:每个|各个|各)\s*([\u4e00-\u9fa5]{2,10}?)\s*有多少(?:个|条|项)?\s*([\u4e00-\u9fa5]{2,8})")
RE_EACH_NUM = re.compile(r"(?:每个|各个|各)\s*([\u4e00-\u9fa5]{2,10}?)\s*的\s*([\u4e00-\u9fa5]{2,8}?)(?:数|量|数量)$")
RE_HAVE_WHAT = re.compile(r"[\u4e00-\u9fa5]{2,10}?都有哪些([\u4e00-\u9fa5]{2,8})")
# 按 X 统计 Y：X 是分组维度（常是 X 类型/分类），Y 才是被查对象
RE_BY_GROUP = re.compile(r"按\s*([\u4e00-\u9fa5]{2,10}?)\s*(?:统计|分组|汇总|分一下)([\u4e00-\u9fa5]{0,10})?")
# 「XX最多的项目」：XX 是被统计的对象，「项目」只是分组维度。
# 和「每个项目有多少 XX」意思一样，只是语序倒过来了——中文里很常见。
RE_MOST_OF = re.compile(
    r"^([\u4e00-\u9fa5]{2,10}?)(?:最多|最少|数量最多|数量最少)的?([\u4e00-\u9fa5]{2,8})$")
# 「每个项目的工时合计」「每类客户的数量」：X 只是分组维度，
# Y（工时/数量/金额）才是被统计的对象，主表该落在 Y 上。
RE_EACH_OF = re.compile(
    r"^(?:每个|各个|各|按|分)\s*([\u4e00-\u9fa5]{2,10}?)\s*的\s*"
    r"([\u4e00-\u9fa5]{2,10}?)\s*(?:合计|总计|总和|总量|总数|数量|总数|平均|均值|分布|占比)$")


def _by_group(question: str) -> tuple[str, str] | None:
    """「按项目类型统计项目数」→ (项目类型, 项目)。Y 为空表示没说统计对象。"""
    m = RE_BY_GROUP.search(question)
    if not m:
        return None
    return _norm(m.group(1)), _norm((m.group(2) or "").rstrip("数"))


def count_target(question: str) -> tuple[str, str] | None:
    """解析出 (分组对象, 被统计对象)。

    支持三种语序：
        每个项目有多少阶段   → (项目, 阶段)
        每个开发迭代的任务数 → (开发迭代, 任务)
        成员最多的项目       → (项目, 成员)   ← 语序倒过来，意思一样
    """
    m = RE_EACH_COUNT.search(question) or RE_EACH_NUM.search(question)
    if m:
        return _norm(m.group(1)), _norm(m.group(2))

    # 「XX最多的项目」：XX 被统计，「项目」是分组
    m = RE_MOST_OF.search(question.strip())
    if m:
        return _norm(m.group(2)), _norm(m.group(1))

    # 「每个项目的工时合计」：项目是分组，「工时」才是被统计对象
    m = RE_EACH_OF.search(question.strip())
    if m:
        return _norm(m.group(1)), _norm(m.group(2))

    m = RE_HAVE_WHAT.search(question)
    if m:
        return "", _norm(m.group(1))
    return None


@dataclass
class Choice:
    """选表结果，附带打分明细，方便排查「为什么选了这张」。"""
    table: Table
    score: float
    reasons: list[str] = field(default_factory=list)
    ranking: list[tuple[float, str]] = field(default_factory=list)

    @property
    def note(self) -> str:
        return f"{self.table.label}（{('、'.join(self.reasons[:3]))}）" if self.reasons else self.table.label


def _norm(text: str) -> str:
    return re.sub(r"[的地得\s]", "", text or "")


def _table_label(t: Table) -> str:
    return _norm(t.label).replace("表", "")


def _alias_hits(t: Table, qn: str) -> list[str]:
    """问句里命中的别名（含全名），按长度降序。"""
    out = [a for a in t.aliases if len(a) >= 2 and a in qn]
    full = _table_label(t)
    if full and full in qn and full not in out:
        out.append(full)
    return sorted(set(out), key=len, reverse=True)


def _column_hits(t: Table, qn: str, helpers) -> list[str]:
    """问句里点名的字段标签。"""
    out = []
    for c in t.columns:
        for lab in helpers(c, t):
            if len(lab) >= 3 and lab in qn:
                out.append(lab)
                break
    return out


def _has_state_enum(t: Table) -> bool:
    return any(c.has_cn_enums for c in t.columns
               if "state" in c.name or "status" in c.name)


def _has_usable_state(t: Table) -> bool:
    """状态列是否真的能筛。

    判据看**数据**而不是列名：
      pp_task.state_          601 行全是 0      → 不能筛
      pp_task.schedule_state_ 598 个 0、3 个 1  → 能筛（这才是真状态）
      pp_dev_task.state_      1700 行全 NULL    → 不能筛
    """
    for c in t.columns:
        if not ("state" in c.name or "status" in c.name):
            continue
        if c.name == "state_id_" and c.ref_table:
            return True                  # 外键指向状态表，值在关联表里
        # filled 是 annotate_columns 统计的非空行数；distinct 是取值个数
        if c.filled and c.distinct >= 2:
            return True
        if c.has_cn_enums:
            return True
    return False


def _score_table(t: Table, qn: str, ctx: dict, helpers) -> tuple[float, list[str]]:
    """单表打分。权重含义写在这里，改权重只改这一处。"""
    score = 0.0
    why: list[str] = []

    # 1) 对象名命中：全名最高，别名次之（越长的别名越具体）
    labels = _alias_hits(t, qn)
    if labels:
        best = labels[0]
        full = _table_label(t)
        if best == full and full in qn:
            score += len(full) * 8
            why.append(f"对象名「{best}」")
        else:
            score += len(best) * 7
            why.append(f"别名「{best}」")

    # 2) 字段命中：问句点名的字段，这张表有就加分
    cols = _column_hits(t, qn, helpers)
    if cols:
        score += min(len(cols), 4) * 4
        why.append("字段 " + "、".join(cols[:2]))

    # 3) 缺字段扣分：问句点名的具体字段（≥4 字）这张表缺一个就扣
    missing = [lab for lab in ctx["specific"] if lab not in cols and not _has_column(t, lab, helpers)]
    if missing:
        score -= 10 * min(len(missing), 3)

    # 4) 有数据加分（空表基本不可能是用户要的）
    if t.rows > 0:
        score += 3
    else:
        score -= 6
        why.append("空表")

    # 5) 关联表惩罚：没有 name_ 但有 2+ 外键 → 它多半是关系表，不是业务对象
    named = bool(labels)
    has_name = any(c.name == "name_" for c in t.columns)
    refs = sum(1 for c in t.columns if c.ref_table)
    if not named and not has_name and refs >= 2:
        score -= 12

    # 6) 配置/类型表惩罚：问数据而不是问配置
    if t.name.endswith(HEAD_SUFFIX) and not named:
        score -= 15
        why.append("配置表")

    # 7) 结构奖励：有 name_ + 有 create_time_ 说明是正经业务表
    if has_name and any(c.name == "create_time_" for c in t.columns):
        score += 2

    # 8) 句式：被统计的是 Y（「每个X有多少个Y」「X都有哪些Y」）
    target = ctx.get("target")
    if target:
        group, want = target
        if want and _matches_label(t, want):
            score += 30
            why.append(f"被查询对象「{want}」")
            # 同时挂到分组对象上：每个开发迭代的任务数 → pp_dev_task(dev_sprint_id_)
            # 权重必须大于「有数据」的加分，否则行数多的 pp_task 会赢
            if group and any(group in pl or pl in group
                             for pl in ctx["parents"].get(t.name, [])):
                score += 18
                why.append(f"挂在「{group}」上")
        elif want and _matches_alias(t, want):
            score += 18
            why.append(f"被查询对象「{want}」（别名）")
        # 分组对象只是限定范围，不该抢主表
        if group and _matches_label(t, group) and not (want and _matches_label(t, want)):
            score -= 12

    # 9) 分组对象是它的父级：走关系图而不是换主表，这里不加分

    # 10) 状态条件：主表没有可分辨的状态列时降权，让同族表上位。
    #     判据不是「有中文枚举」——pp_task.state_ 只有一个值（全是 0），
    #     等于没有状态；pp_dev_task 才有 notStarted/released 这种真状态。
    if ctx.get("asks_state") and not _has_usable_state(t):
        score -= 10

    # 11) 空表重罚：pp_risk_rating_config 名字正好叫「风险等级」，但一行数据都没有，
    #     它只可能是配置表，用户问的一定不是它
    if t.rows == 0:
        score -= 20

    # 12) 「按 X 统计 Y」：X 常是分类表（项目类型），Y 才是主体；X 只做分组
    by_group = ctx.get("by_group")
    if by_group:
        gword, yword = by_group
        if _matches_label(t, gword) or gword in _table_label(t):
            if not (yword and _matches_label(t, yword)):
                score -= 14
        if yword and _matches_label(t, yword):
            score += 14
            why.append(f"按「{gword}」统计「{yword}」")

    # 13) 抬头表 vs 明细表：用户说「预算」通常要的是明细（花了什么），不是抬头
    if ctx.get("wants_detail") and not any(a.endswith("明细") or a.endswith("条目") for a in labels):
        if t.name.endswith(DETAIL_SUFFIX):
            score += 10
            why.append("明细表")
        elif t.name.endswith("_budget") or t.name.endswith("_contract"):
            score -= 8

    return score, why


def _matches_label(t: Table, word: str) -> bool:
    lab = _table_label(t)
    if not word or not lab:
        return False
    return lab == word or lab.endswith(word) or word.endswith(lab)


def _matches_alias(t: Table, word: str) -> bool:
    return any(word == a or word.endswith(a) or a.endswith(word) for a in t.aliases if len(a) >= 2)


def _fk_groups_target(t: Table, want: str) -> bool:
    """这张表有没有指向「want」对象的外键（每个X有多少Y → Y 该挂 X）。

    比按名字找可靠：pp_task 也叫「任务」但它不挂迭代，pp_dev_task 才挂。
    """
    return any(c.ref_table and want == c.ref_table.split("_")[-1] for c in t.columns)


def _has_column(t: Table, label: str, helpers) -> bool:
    return any(label in helpers(c, t) for c in t.columns)


# ---------------------------------------------------------------- 公开接口

def choose(pack: SchemaPack, question: str, helpers,
           count_subject: str | None = None) -> Choice:
    """给问句选一张主表。

    helpers(c, t) -> list[str]  字段的候选叫法（local._label_variants）
    count_subject: 句式解析出的「被统计对象」表名，可空
    """
    qn = _norm(question)

    # 问句里点名的具体字段短语（≥4 字，避免「名称」这种泛词）
    specific = set()
    for t in pack.tables.values():
        for c in t.columns:
            for lab in helpers(c, t):
                if len(lab) >= 4 and lab in qn:
                    specific.add(lab)

    # 每张表指向哪些表（用于句式「Y 挂在 X 上」判定，一次算好）
    parents: dict[str, list[str]] = {}
    for t in pack.tables.values():
        parents[t.name] = [pack.tables[c.ref_table].label
                           for c in t.columns
                           if c.ref_table and c.ref_table in pack.tables]

    ctx = {"specific": specific, "count_subject": count_subject,
           "target": count_target(question), "parents": parents,
           "by_group": _by_group(question),
           "wants_detail": bool(re.search(r"花在|花销|花哪|花费|明细|明细项", question)),
           "asks_state": bool(STATE_WORDS.search(question))}

    scored: list[tuple[float, Table, list[str]]] = []
    for t in pack.tables.values():
        s, why = _score_table(t, qn, ctx, helpers)
        if s > 0 or any(a in qn for a in t.aliases):
            scored.append((s, t, why))
    if not scored:
        raise KeyError("没认出你要查哪个对象，试着带上对象名（如「项目」「任务」「风险」）")

    scored.sort(key=lambda x: (-x[0], -x[1].rows, -len(x[1].columns)))
    best_score, best, best_why = scored[0]

    # ---- 口语兜底：只在「没有任何对象被点名」时生效
    named = any(_alias_hits(t, qn) for _, t, _ in scored[:3])
    if not named:
        for pattern, tname in ORAL_ENTITY:
            if re.search(pattern, question) and tname in pack.tables:
                best, best_why = pack.tables[tname], [f"口语「{pattern}」"]
                best_score = max(best_score, 1)
                break

    ranking = [(s, t.name) for s, t, _ in scored[:5]]
    return Choice(table=best, score=best_score, reasons=best_why, ranking=ranking)


def refine_for_count(pack: SchemaPack, choice: Choice, question: str) -> Choice:
    """统计类问句的对象替换：「每个项目挂了多少人」→ 数的是成员。"""
    for pattern, tname in ORAL_COUNT_OBJECT:
        if re.search(pattern, question) and tname in pack.tables:
            t = pack.tables[tname]
            return Choice(table=t, score=choice.score,
                          reasons=["统计对象是「人」"], ranking=choice.ranking)
    return choice


def link_tables(pack: SchemaPack, question: str, helpers,
                limit: int = 10) -> list[str]:
    """schema linking：挑出这个问句**真的可能用到**的表，连同外键目标表。

    比原来的「标签包含即命中」准得多：
      - 问「任务状态是已完成」不会把 149 张表都塞给模型
      - 外键目标表自动带上，模型才能写子查询
    供 engine.build_messages（大模型路径）使用。
    """
    qn = _norm(question)
    picked: list[str] = []

    # 1) 打分前几名
    try:
        ch = choose(pack, question, helpers)
        picked.append(ch.table.name)
        for _, name in ch.ranking[1:4]:
            if name in pack.tables:
                picked.append(name)
    except KeyError:
        pass

    # 2) 问句里明确提到的其它对象（客户/合同/项目）
    for t in pack.tables.values():
        if t.name in picked:
            continue
        if any(len(a) >= 2 and a in qn for a in t.aliases):
            picked.append(t.name)

    # 3) 外键目标表：模型要写子查询就得知道关联表
    extra = []
    for name in list(picked):
        t = pack.tables.get(name)
        if t is None:
            continue
        for c in t.columns:
            if c.ref_table and c.ref_table not in picked and c.ref_table not in extra:
                extra.append(c.ref_table)
    picked.extend(extra[:8])

    # 4) 多对象时补中间表（客户→合同→项目 这类两跳路径）
    if len(picked) > 1:
        from . import graph
        head = picked[0]
        for other in picked[1:3]:
            for e in graph.shortest_path(pack, head, other, max_hops=3) or []:
                for n in (e.src, e.dst):
                    if n not in picked and n in pack.tables:
                        picked.insert(1, n)     # 中间表放前面，别被截断
    return list(dict.fromkeys(picked))[:limit + 4]
