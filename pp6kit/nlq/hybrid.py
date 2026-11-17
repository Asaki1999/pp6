"""规则 + AI 混合引擎：规则先跑，AI 兜底，好的结果自动沉淀。

为什么长这样
------------
单纯靠规则：中文语义千变万化，永远补不完（实测 152 条语料全过，
但换一批复杂问法 10 条里 8 条答非所问）。

单纯靠大模型：慢（每次 2-5 秒）、贵、免费额度会被刷光，
而且简单问题（「每个客户有多少项目」）规则其实答得很好。

所以：
    1. 规则先答，快且免费
    2. 规则答不好（或用户点了「让 AI 重答」）→ AI 上
    3. AI 答对了 → 存成示例，下次同类问题直接命中，不用再调 AI

这里的「答不好」有三档判定：
    - 生成失败 / SQL 报错           → 一定换 AI
    - 0 行                          → 多半是条件写错，换 AI
    - 结构可疑（问「多少」却没聚合、
      问 A 却查成 B、问了没筛）      → 换 AI
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp


@dataclass
class Verdict:
    """规则答案的质量判定。"""
    ok: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        return "、".join(self.reasons) if self.reasons else "看起来正常"


# 问句里出现这些词，SQL 里就该有对应的聚合
COUNT_WORDS = ("多少", "几个", "数量", "条数", "统计", "总数", "分布", "占比")
AGG_WORDS = ("平均", "均值", "总和", "合计", "最大", "最小")
# 问句要求排序取前 N。
# 中文里「进度最高的5个项目」比「前5个项目」更常用，「最X」和数字之间还常隔一个「的」，
# 所以中间允许夹「的」——local._rank_order 里修过同一个坑，两个模块必须一致。
TOP_RE = re.compile(
    r"(?:前|最高|最低|最大|最小|最多|最少|最长|最短|top)\s*(?:的)?\s*(\d+)", re.I)
# 问句点名了某个对象/角色，SQL 里就该出现对应线索
ROLE_RE = re.compile(r"(?:项目经理|项目负责人|责任人|负责人|执行人|处理人)")
# 「超支/超期/延期/逾期」这类语义，SQL 必须真的比较数字
OVER_RE = re.compile(r"超支|超额|超期|延期|逾期|超出预算")
# 比较类 AST 节点：只认大小比较，不认 `=`/`<>`——
# `WHERE state_ = 'finished'` 是字符串等值，不是「做了数值比较」。
CMP_NODES = (exp.GT, exp.GTE, exp.LT, exp.LTE)
# 明确的业务状态词：问句里出现，SQL 就必须有状态/阶段筛选。
# 「立项」这类会出现在对象名里的词不算，避免误伤。
STATUS_RE = re.compile(
    r"执行中|进行中|已完成|已关闭|未关闭|已暂停|暂停|已终止|终止|"
    r"未开始|已开始|审批中|待审批|已立项|待立项|已归档|草稿")
# 时间条件词：SQL 里必须出现日期函数/字段，不能只查全量。
DATE_RE = re.compile(
    r"上个月|上月|本月|这个月|今年|去年|明年|本周|上周|本季度|上季度|"
    r"今天|昨天|最近\s*\d+\s*(?:天|周|月|年)|近\s*\d+\s*(?:天|周|月|年)")
# 「张三负责的 / 李四提交的」：SQL 里要么出现这个人名，要么关联人员表。
PERSON_RE = re.compile(r"([\u4e00-\u9fa5]{2,4})\s*(?:负责|经办|提交|创建|处理)的")
# 问句明确要求排序
SORT_RE = re.compile(r"按.+(?:排序|倒序|升序|正序)|倒序|升序|正序")

# 解析不了时的兜底（极少发生）：宁可放过，也别把正确答案判成不合格
NUM_CMP_FALLBACK = re.compile(
    r"[<>]=?\s*\d|\bDATEDIFF\b|\b(TIMESTAMPDIFF|SUBDATE)\b", re.I)
# 问句点名了某个对象，SQL 主表就该是它
OBJECT_RE = re.compile(r"(?:每个|各个|各)\s*([\u4e00-\u9fa5]{2,10}?)\s*(?:有|的|负责|下)")


def has_numeric_comparison(sql: str) -> bool:
    """SQL 里是不是真的做了数值/时间比较？

    为什么看 AST 而不是正则：「超支」最标准的写法是

        WHERE actual_amount_ > plan_amount_

    两列相比，**没有数字字面量**。旧的正则 `[<>]=?\\s*\\d` 只认 `> 100`，
    于是规则答对了却被判不合格，每次都白升级到 AI（多 2-5 秒 + 一次额度），
    违背本模块「宁可让规则答」的设计意图。

    判定规则：存在 >、>=、<、<= 节点，且**至少一侧不是纯字面量**（含列引用即可）。
      - `actual_amount_ > plan_amount_`  → 列 vs 列      → 算
      - `actual_amount_ > 100`           → 列 vs 数字    → 算
      - `DATEDIFF(end_date_, CURDATE()) < 0` → 函数含列  → 算
      - `state_ = 'finished'`            → 不是大小比较  → 不算
    """
    if not sql:
        return False
    try:
        ast = sqlglot.parse_one(sql, dialect="mysql")
    except Exception:
        return bool(NUM_CMP_FALLBACK.search(sql))
    for node in ast.find_all(*CMP_NODES):
        if node.this.find(exp.Column) is not None \
                or node.expression.find(exp.Column) is not None:
            return True
    return False


def judge(question: str, sql: str, rows: int,
          expected_tables: list[str] | None = None) -> Verdict:
    """判断规则生成的 SQL 是否真的回答了问题。

    只做**能确定**的判定，猜不出来就放过（宁可让规则答，也别无谓地调 AI）。
    """
    q = question or ""
    up = (sql or "").upper()
    bad: list[str] = []

    if not sql:
        return Verdict(False, ["没生成 SQL"])

    # 1) 0 行：条件多半写错了
    if rows == 0:
        bad.append("查询结果为空")

    # 2) 问「多少/几个」却没 COUNT
    if any(w in q for w in COUNT_WORDS) and "COUNT(" not in up:
        bad.append("问数量但没做统计")

    # 3) 问「平均/合计」却没对应聚合
    if any(w in q for w in AGG_WORDS) and not re.search(r"\b(AVG|SUM|MAX|MIN)\(", up):
        bad.append("问聚合但没聚合")

    # 4) 问「前 N」却没 ORDER BY
    if TOP_RE.search(q) and "ORDER BY" not in up:
        bad.append("问排名但没排序")

    # 5) 主表选错：问句点名的对象不在 SQL 里
    if expected_tables:
        first_from = re.search(r"FROM\s+`?([a-z0-9_]+)`?", sql, re.I)
        if first_from and first_from.group(1) not in expected_tables:
            # 只有「问旬点名的对象一个都没用到」才算错。
            # 「每个客户有多少项目」主表是 pp_project、经合同连到客户，是对的。
            used_all = set(re.findall(r"`?(pp_[a-z0-9_]+)`?", sql))
            if not (used_all & set(expected_tables)):
                bad.append(f"主表是 {first_from.group(1)}，不是问的那个对象")

    # 6) 问句里的数字条件没出现在 SQL 里（「进度低于50」却没 50）
    for num in re.findall(r"(?:低于|高于|大于|小于|超过|不足)\s*(\d+)", q):
        if num not in sql:
            bad.append(f"条件 {num} 没进 SQL")

    # 7) 问「超支/超期/延期」这类比较语义，SQL 里却没有任何数值比较
    if OVER_RE.search(q) and not has_numeric_comparison(sql):
        bad.append("问了超支/超期，但没做数值比较")

    # 8) 问「项目经理/负责人」这类角色，SQL 里却没有人员表
    if ROLE_RE.search(q) and "iform_party_employee" not in sql \
            and "iform_party_org" not in sql:
        bad.append("问了角色/负责人，但没关联人员表")

    # 只看 WHERE：SELECT 里的「项目阶段」别名不算筛选条件。
    wsql = ""
    wcols: set[str] = set()
    try:
        _ast = sqlglot.parse_one(sql, dialect="mysql")
        _where = _ast.find(exp.Where)
        if _where is not None:
            wsql = _where.sql().upper()
            wcols = {c.name.lower() for c in _where.find_all(exp.Column)}
    except Exception:
        wsql = up

    # 9.1) 问了明确状态（执行中/已完成…），WHERE 却没有状态/阶段列
    if STATUS_RE.search(q):
        has_state = bool(wcols & {"state_id_", "status_", "phase_id_", "state", "status"}) \
            or "STATE" in wsql or "STATUS" in wsql or "PHASE" in wsql
        if not has_state:
            bad.append("问了状态，但 SQL 没有状态筛选")

    # 9.2) 问了时间范围（上个月/今年/最近7天），WHERE 却没有日期条件
    if DATE_RE.search(q) and not re.search(
            r"DATE|TIME|YEAR|MONTH|DAY|INTERVAL|BETWEEN|DATEDIFF|TIMESTAMP", wsql):
        bad.append("问了时间范围，但 SQL 没有日期条件")

    # 9.3) 点名某人负责/提交，SQL 里既没有人名也没有人员表
    # 去掉「查一下/帮我/请」这类前缀，否则会把「一下张三」当成名字
    q_name = re.sub(r"^(?:请|帮我|麻烦)?\s*(?:查一下|查一查|查询|查|看看|看一下|列出|找出)\s*", "", q)
    pm = PERSON_RE.search(q_name)
    if pm:
        who = pm.group(1)
        roles = {"项目经理", "项目负责人", "负责人", "责任人", "执行人", "处理人", "创建人"}
        # 光有关联人员表不算：那张表可能只是用来显示责任人。
        # 人名必须真的出现在 SQL 里（AI 是内联字面量，不参数化）。
        if who not in roles and who not in wsql:
            bad.append(f"问了「{who}」，但 SQL 没有按这个人筛选")

    # 9.4) 明确要求排序，SQL 却没有 ORDER BY
    if SORT_RE.search(q) and "ORDER BY" not in up:
        bad.append("问了排序，但 SQL 没有 ORDER BY")

    # 9) 问「每个 X 的 Y」，分组维度却是 X 自己的父级（选错对象）
    m = re.match(r"^(?:每个|各个|各)\s*([\u4e00-\u9fa5]{2,10}?)\s*(?:负责|的|有)", q)
    if m and "GROUP BY" in up and "COUNT(" in up:
        # 分组列名里出现「父」「上级」，多半选错了维度
        if re.search(r"AS\s*`?[^`]*父[^`]*`?", sql):
            bad.append("分组维度选成了父级，不是问的对象")

    return Verdict(not bad, bad)


def should_try_ai(verdict: Verdict, mode: str) -> bool:
    """mode: auto / local / llm / hybrid"""
    if mode == "local":
        return False
    if mode == "llm":
        return True
    return not verdict.ok


def fallback_note(verdict: Verdict) -> str:
    return "规则没把握（" + verdict.note + "），已用 AI 重答"


# ---------------------------------------------------------------- 沉淀

def learn_worthy(question: str, sql: str, rows: int,
                 rule_sql: str = "") -> bool:
    """这条 AI 结果值不值得存成示例？

    只存「确实答出来了」的：有行、SQL 不是 SELECT *、
    而且不是那种一次性问句（带具体数字/人名，换个场景就失效）。

    rule_sql：规则引擎当时给出的 SQL。
    如果规则有结果、而 AI 的结论**和规则冲突**（表或列完全不同），
    就不沉淀——AI 也可能答错，错了还沉淀会把错误固化下来。
    """
    if not sql or rows <= 0:
        return False
    if re.search(r"SELECT\s+\*", sql or "", re.I):
        return False
    # 带明确数字的一次性问法（「前 5 个」「低于 50」）不沉淀，
    # 否则下次问「前 10 个」会命中「前 5」的 SQL
    if re.search(r"(?:前|低于|高于|大于|小于|超过)\s*\d+", question or ""):
        return False
    # 太短的问题（「项目」）不沉淀，泛化性太差
    if len((question or "").strip()) < 6:
        return False
    return True


def conflicts(rule_sql: str, rule_rows: int, ai_sql: str) -> bool:
    """AI 的答案和规则结论是否**明显冲突**。

    只在「规则本来有结果」时才比较——规则答不出来时 AI 说了算。
    冲突的判据：两边主表不同，且互不引用。
    """
    if not rule_sql or rule_rows <= 0 or not ai_sql:
        return False

    def main_table(sql: str) -> str:
        m = re.search(r"FROM\s+`?([a-z0-9_]+)`?", sql or "", re.I)
        return m.group(1).lower() if m else ""

    a, b = main_table(rule_sql), main_table(ai_sql)
    if not a or not b or a == b:
        return False
    # 两边互相引用了对方的表 → 是同一套查法，不算冲突
    if a in ai_sql.lower() and b in rule_sql.lower():
        return False
    return True
