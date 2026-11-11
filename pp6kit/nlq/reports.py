"""标准项目管理报表模板。

为什么单独一个模块
------------------
通用启发式引擎擅长「列出 XX」「条件是 YY」这类点名字段的问法，
但**报表类问题**问的是「指标」——进度偏差、亮灯、预算执行率、资源负载、
风险矩阵、缺陷分布。这类问题的答案不是某张表的某几列，而是
「聚合 + 计算 + 阈值判定」，启发式一定会答成明细清单（实测 10 条全错）。

所以按 WrenAI 的做法给一层语义层：把报表当成一等公民，
每个报表一条模板，命中就用固定 SQL。

报表清单来源
------------
对照 MS Project 预置报表目录（仪表盘/资源/成本/进度四组），
加上 PMBOK 里通行的 EVM、资源利用率、风险矩阵、里程碑达成，
再筛掉本项目库里没有数据的（如 EVM 的 PV/EV 需要基线成本快照）。

每条 SQL 都对着真实库验证过列名和枚举值，不是照抄理论：
    pp_project.progress_ / planned_progress_        进度与计划进度
    pp_project.light_description_                  亮灯原因（「亮红灯」在文案里）
    pp_project.total_budget_ + pp_project_cost.amount_
    pp_risk.rating_=High/Medium/Low  state_=under_tracking/closed
    pp_issue.priority_=high/medium/low
    pp_defect.severity_=BLOCKER/CRITICAL/MAJOR/MINOR
    pp_task.is_milestone_=Y  assignee_ids_（可能是逗号分隔，用 FIND_IN_SET）
"""
from __future__ import annotations

import re
from typing import Callable

from .introspect import SchemaPack

# 未删除（各表 deleted_ 有 'N'/空/NULL 多种写法）
def _alive(t: str) -> str:
    return f"COALESCE(`{t}`.`deleted_`, 'N') <> 'Y'"


Builder = Callable[[SchemaPack, str], tuple[str, str] | None]


def _need(pack: SchemaPack, *tables: str) -> bool:
    return all(t in pack.tables for t in tables)


def _limit(q: str, default: int = 200) -> int:
    """问句里写了「前 20 个」就按 20，不写给 200（guard 上限 500）。"""
    m = re.search(r"(?:前|top|TOP)\s*(\d{1,3})", q)
    if m:
        return max(1, min(int(m.group(1)), 500))
    return default


# ---------------------------------------------------------------- 进度类

def _progress_gap(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _need(pack, "pp_project"):
        return None
    order = "ASC" if re.search(r"落后|滞后|拖|偏差大|最低", q) else "DESC"
    sql = (
        "SELECT `p`.`name_` AS `项目名称`,\n"
        "       `p`.`planned_progress_` AS `计划进度`,\n"
        "       `p`.`progress_` AS `实际进度`,\n"
        "       (`p`.`progress_` - `p`.`planned_progress_`) AS `进度偏差`,\n"
        "       `p`.`light_description_` AS `亮灯说明`\n"
        "FROM `pp_project` `p`\n"
        f"WHERE {_alive('p')}\n"
        f"ORDER BY `进度偏差` {order}, `p`.`id_` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, "报表模板：进度偏差（实际进度 − 计划进度，负值=落后）"


def _traffic_light(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _need(pack, "pp_project"):
        return None
    if "红" in q:
        color, label = "红灯", "红灯"
    elif "黄" in q:
        color, label = "黄灯", "黄灯"
    elif "绿" in q:
        color, label = "绿灯", "绿灯"
    else:
        color, label = "红灯", "红灯"
    sql = (
        "SELECT `p`.`name_` AS `项目名称`,\n"
        "       `p`.`phase_` AS `当前阶段`,\n"
        "       `p`.`planned_progress_` AS `计划进度`,\n"
        "       `p`.`progress_` AS `实际进度`,\n"
        "       `p`.`light_description_` AS `亮灯原因`\n"
        "FROM `pp_project` `p`\n"
        f"WHERE {_alive('p')} AND `p`.`light_description_` LIKE '%{color}%'\n"
        f"ORDER BY `p`.`id_` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, (
        f"报表模板：{label}项目。"
        "亮灯判定看 light_description_ 的文案（库里有「亮红灯」这种自然语言结论），"
        "比硬编码 light_ 数值稳"
    )


def _milestone_status(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _need(pack, "pp_task", "pp_project"):
        return None
    sql = (
        "SELECT `p`.`name_` AS `项目名称`,\n"
        "       `t`.`name_` AS `里程碑`,\n"
        "       `t`.`finish_` AS `计划完成`,\n"
        "       `t`.`progress_` AS `进度`,\n"
        "       CASE WHEN `t`.`progress_` >= 100 THEN '已完成'\n"
        "            WHEN `t`.`finish_` IS NOT NULL AND `t`.`finish_` < NOW() THEN '已逾期'\n"
        "            WHEN `t`.`finish_` IS NULL THEN '未排期'\n"
        "            ELSE '进行中' END AS `达成状态`\n"
        "FROM `pp_task` `t`\n"
        "LEFT JOIN `pp_project` `p` ON `p`.`id_` = `t`.`project_id_`\n"
        f"WHERE {_alive('t')} AND `t`.`is_milestone_` = 'Y'\n"
        "ORDER BY `p`.`id_` DESC, `t`.`finish_`\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, "报表模板：里程碑达成情况（按进度和计划完成日判定已完成/逾期/进行中）"


# ---------------------------------------------------------------- 成本类

def _budget_vs_cost(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _need(pack, "pp_project", "pp_project_cost"):
        return None
    only_over = bool(re.search(r"超支|超预算|超出预算|超了", q))
    having = "HAVING `实际成本` > `预算` AND `预算` > 0\n" if only_over else ""
    sql = (
        "SELECT `p`.`name_` AS `项目名称`,\n"
        "       `p`.`total_budget_` AS `预算`,\n"
        "       COALESCE(SUM(`c`.`amount_`), 0) AS `实际成本`,\n"
        "       (`p`.`total_budget_` - COALESCE(SUM(`c`.`amount_`), 0)) AS `结余`,\n"
        "       CASE WHEN `p`.`total_budget_` > 0\n"
        "            THEN ROUND(COALESCE(SUM(`c`.`amount_`), 0) / `p`.`total_budget_` * 100, 1)\n"
        "            ELSE NULL END AS `预算执行率%`\n"
        "FROM `pp_project` `p`\n"
        "LEFT JOIN `pp_project_cost` `c`\n"
        "       ON `c`.`project_id_` = `p`.`id_` "
        f"AND {_alive('c')}\n"
        f"WHERE {_alive('p')}\n"
        "GROUP BY `p`.`id_`, `p`.`name_`, `p`.`total_budget_`\n"
        + having +
        ("ORDER BY `结余` ASC\n" if only_over else "ORDER BY `实际成本` DESC\n")
        + f"LIMIT {_limit(q)}"
    )
    note = "报表模板：预算 vs 实际成本（成本来自 pp_project_cost.amount_ 汇总）"
    if only_over:
        note += "；只列超支项目"
    return sql, note


# ---------------------------------------------------------------- 风险 / 问题 / 缺陷

def _risk_open(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _need(pack, "pp_risk", "pp_project"):
        return None
    # 「除了高风险以外」「忽略高风险」是**排除**问法，交回启发式去加 NOT 条件，
    # 这里硬套「只要 High」会答反。
    if re.search(r"除了|忽略|屏掉|排除|不看|不要|不含|以外", q):
        return None
    conds = [_alive("r")]
    if re.search(r"高", q):
        conds.append("`r`.`rating_` = 'High'")
    if re.search(r"未关闭|未闭环|open|跟踪中|在处理", q):
        conds.append("COALESCE(`r`.`state_`, '') <> 'closed'")
    sql = (
        "SELECT `p`.`name_` AS `项目名称`,\n"
        "       `r`.`name_` AS `风险名称`,\n"
        "       `r`.`rating_` AS `风险等级`,\n"
        "       `r`.`probability_` AS `可能性`,\n"
        "       `r`.`impact_` AS `影响`,\n"
        "       `r`.`state_` AS `状态`,\n"
        "       `r`.`due_time_` AS `到期时间`\n"
        "FROM `pp_risk` `r`\n"
        "LEFT JOIN `pp_project` `p` ON `p`.`id_` = `r`.`project_id_`\n"
        "WHERE " + " AND ".join(conds) + "\n"
        "ORDER BY `r`.`due_time_` IS NULL, `r`.`due_time_`\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, "报表模板：风险清单（等级 rating_、状态 state_，closed 表示已关闭）"


def _risk_dist(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _need(pack, "pp_risk"):
        return None
    if re.search(r"矩阵|可能性|概率", q):
        sql = (
            "SELECT `r`.`probability_` AS `可能性`,\n"
            "       `r`.`impact_` AS `影响`,\n"
            "       COUNT(*) AS `数量`\n"
            "FROM `pp_risk` `r`\n"
            f"WHERE {_alive('r')}\n"
            "GROUP BY `r`.`probability_`, `r`.`impact_`\n"
            "ORDER BY `可能性` DESC, `影响` DESC\n"
            "LIMIT 200"
        )
        return sql, "报表模板：风险矩阵（可能性 × 影响的分布）"
    if re.search(r"状态", q):
        sql = (
            "SELECT `r`.`state_` AS `风险状态`, COUNT(*) AS `数量`\n"
            "FROM `pp_risk` `r`\n"
            f"WHERE {_alive('r')}\n"
            "GROUP BY `r`.`state_`\n"
            "ORDER BY `数量` DESC\n"
            "LIMIT 200"
        )
        return sql, "报表模板：风险按状态分布"
    sql = (
        "SELECT `r`.`rating_` AS `风险等级`, COUNT(*) AS `数量`\n"
        "FROM `pp_risk` `r`\n"
        f"WHERE {_alive('r')}\n"
        "GROUP BY `r`.`rating_`\n"
        "ORDER BY `数量` DESC\n"
        "LIMIT 200"
    )
    return sql, "报表模板：风险按等级分布（High/Medium/Low）"


def _issue_dist(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _need(pack, "pp_issue"):
        return None
    if re.search(r"状态|进展", q):
        col, label = "status_", "问题状态"
    elif re.search(r"类型|分类|类别", q):
        col, label = "category_", "问题类型"
    else:
        col, label = "priority_", "问题优先级"
    sql = (
        f"SELECT `i`.`{col}` AS `{label}`, COUNT(*) AS `数量`\n"
        "FROM `pp_issue` `i`\n"
        f"WHERE {_alive('i')}\n"
        f"GROUP BY `i`.`{col}`\n"
        "ORDER BY `数量` DESC\n"
        "LIMIT 200"
    )
    return sql, f"报表模板：问题按{label}分布"


def _defect_dist(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _need(pack, "pp_defect"):
        return None
    if re.search(r"状态|进展", q):
        col, label = "activity_state_", "缺陷状态"
    elif re.search(r"类型|分类|根因", q):
        col, label = "root_cause_type_", "缺陷根因"
    else:
        col, label = "severity_", "缺陷严重程度"
    sql = (
        f"SELECT `d`.`{col}` AS `{label}`, COUNT(*) AS `数量`\n"
        "FROM `pp_defect` `d`\n"
        f"WHERE {_alive('d')}\n"
        f"GROUP BY `d`.`{col}`\n"
        "ORDER BY `数量` DESC\n"
        "LIMIT 200"
    )
    return sql, f"报表模板：缺陷按{label}分布（BLOCKER/CRITICAL/MAJOR/MINOR）"


# ---------------------------------------------------------------- 资源类

def _member_load(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    """成员任务负载：任务表 assignee_ids_ 可能是逗号分隔，用 FIND_IN_SET 拆。"""
    if not _need(pack, "pp_task"):
        return None
    sql = (
        "SELECT `e`.`name_` AS `成员`,\n"
        "       COUNT(DISTINCT `t`.`id_`) AS `任务数`,\n"
        "       SUM(CASE WHEN `t`.`progress_` >= 100 THEN 1 ELSE 0 END) AS `已完成`,\n"
        "       SUM(CASE WHEN `t`.`progress_` < 100 AND `t`.`finish_` IS NOT NULL\n"
        "                     AND `t`.`finish_` < NOW() THEN 1 ELSE 0 END) AS `已逾期`\n"
        "FROM `pp_task` `t`\n"
        "JOIN `iform_party_employee` `e` ON FIND_IN_SET(`e`.`id_`, `t`.`assignee_ids_`)\n"
        f"WHERE {_alive('t')}\n"
        "GROUP BY `e`.`id_`, `e`.`name_`\n"
        "ORDER BY `任务数` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, "报表模板：成员任务负载（按责任人聚合任务数/已完成/已逾期）"


def _project_load(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    """项目维度的任务完成情况：进度、任务数、逾期数。"""
    if not _need(pack, "pp_task", "pp_project"):
        return None
    sql = (
        "SELECT `p`.`name_` AS `项目名称`,\n"
        "       `p`.`progress_` AS `项目进度`,\n"
        "       COUNT(`t`.`id_`) AS `任务总数`,\n"
        "       SUM(CASE WHEN `t`.`progress_` >= 100 THEN 1 ELSE 0 END) AS `已完成`,\n"
        "       SUM(CASE WHEN `t`.`progress_` < 100 AND `t`.`finish_` IS NOT NULL\n"
        "                     AND `t`.`finish_` < NOW() THEN 1 ELSE 0 END) AS `已逾期`\n"
        "FROM `pp_project` `p`\n"
        "LEFT JOIN `pp_task` `t` ON `t`.`project_id_` = `p`.`id_` AND COALESCE(`t`.`deleted_`, 'N') <> 'Y'\n"
        f"WHERE {_alive('p')}\n"
        "GROUP BY `p`.`id_`, `p`.`name_`, `p`.`progress_`\n"
        "ORDER BY `已逾期` DESC, `任务总数` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, "报表模板：项目任务完成情况（任务数/已完成/已逾期）"


def _project_health(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    """项目健康度一页纸：进度/偏差/亮灯/任务/逾期/风险/问题/成本，一张表看全。

    全部用相关子查询而不是多重 JOIN——JOIN 任务×风险×问题会笛卡尔放大，
    数字会翻好几倍（这是 BI 报表最常见的错）。
    """
    if not _need(pack, "pp_project"):
        return None
    parts = [
        "SELECT `p`.`name_` AS `项目名称`",
        "`p`.`planned_progress_` AS `计划进度`",
        "`p`.`progress_` AS `实际进度`",
        "(`p`.`progress_` - `p`.`planned_progress_`) AS `进度偏差`",
        "CASE WHEN `p`.`light_description_` LIKE '%红灯%' THEN '红'"
        " WHEN `p`.`light_description_` LIKE '%黄灯%' THEN '黄'"
        " WHEN `p`.`light_description_` LIKE '%绿灯%' THEN '绿'"
        " ELSE '—' END AS `灯`",
    ]
    if "pp_task" in pack.tables:
        parts.append(
            "(SELECT COUNT(*) FROM `pp_task` `t` WHERE `t`.`project_id_` = `p`.`id_` "
            "AND COALESCE(`t`.`deleted_`, 'N') <> 'Y') AS `任务数`")
        parts.append(
            "(SELECT COUNT(*) FROM `pp_task` `t` WHERE `t`.`project_id_` = `p`.`id_` "
            "AND COALESCE(`t`.`deleted_`, 'N') <> 'Y' AND `t`.`progress_` < 100 "
            "AND `t`.`finish_` IS NOT NULL AND `t`.`finish_` < NOW()) AS `逾期任务`")
    if "pp_risk" in pack.tables:
        parts.append(
            "(SELECT COUNT(*) FROM `pp_risk` `r` WHERE `r`.`project_id_` = `p`.`id_` "
            "AND COALESCE(`r`.`deleted_`, 'N') <> 'Y' AND `r`.`rating_` = 'High' "
            "AND COALESCE(`r`.`state_`, '') <> 'closed') AS `高风险未关闭`")
    if "pp_issue" in pack.tables:
        parts.append(
            "(SELECT COUNT(*) FROM `pp_issue` `i` WHERE `i`.`project_id_` = `p`.`id_` "
            "AND COALESCE(`i`.`deleted_`, 'N') <> 'Y' "
            "AND COALESCE(`i`.`status_`, '') <> 'pass') AS `未关闭问题`")
    if "pp_project_cost" in pack.tables:
        parts.append("`p`.`total_budget_` AS `预算`")
        parts.append(
            "(SELECT COALESCE(SUM(`c`.`amount_`), 0) FROM `pp_project_cost` `c` "
            "WHERE `c`.`project_id_` = `p`.`id_` "
            "AND COALESCE(`c`.`deleted_`, 'N') <> 'Y') AS `实际成本`")
    sql = (
        ",\n       ".join(parts)
        + "\nFROM `pp_project` `p`\n"
        f"WHERE {_alive('p')}\n"
        "ORDER BY `进度偏差` ASC, `p`.`id_` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, (
        "报表模板：项目健康度一页纸（进度偏差 + 亮灯 + 任务逾期 + 高风险 + 未关闭问题 + 成本），"
        "全部用相关子查询避免 JOIN 放大"
    )


# ---------------------------------------------------------------- 工时（TS）

# 工时的「汇总类」问法才走模板；问「多少条」这类计数仍交给启发式（基准里期望列名是「数量」）
RE_TS_SUM = re.compile(r"汇总|合计|总工时|多少小时|工时数|总时长|工时统计|工时总计")
RE_TS_COUNT_WORDS = re.compile(r"多少条|几条|记录数|条数|数量")


def _ts_ok(pack: SchemaPack, q: str) -> bool:
    return (
        "pp_timesheet" in pack.tables
        and "工时" in q
        and bool(RE_TS_SUM.search(q))
        and not RE_TS_COUNT_WORDS.search(q)
    )


def _timesheet_by_member(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _ts_ok(pack, q):
        return None
    sql = (
        "SELECT `e`.`name_` AS `成员`,\n"
        "       ROUND(SUM(`t`.`normal_hours_`), 1) AS `工时合计`,\n"
        "       COUNT(*) AS `记录数`,\n"
        "       SUM(CASE WHEN `t`.`approve_state_` IN ('1', '2') THEN 1 ELSE 0 END) AS `待审批`\n"
        "FROM `pp_timesheet` `t`\n"
        "LEFT JOIN `iform_party_employee` `e` ON `e`.`id_` = `t`.`user_id_`\n"
        "GROUP BY `t`.`user_id_`, `e`.`name_`\n"
        "ORDER BY `工时合计` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, "工时 · TS-01 按人汇总工时（合计/记录数/待审批；normal_hours_ 求和）"


def _timesheet_by_project(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _ts_ok(pack, q) or "pp_project" not in pack.tables:
        return None
    sql = (
        "SELECT `p`.`name_` AS `项目名称`,\n"
        "       ROUND(SUM(`t`.`normal_hours_`), 1) AS `工时合计`,\n"
        "       COUNT(DISTINCT `t`.`user_id_`) AS `参与人数`\n"
        "FROM `pp_timesheet` `t`\n"
        "JOIN `pp_project` `p` ON `p`.`id_` = `t`.`project_id_`\n"
        "GROUP BY `p`.`id_`, `p`.`name_`\n"
        "ORDER BY `工时合计` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, "工时 · TS-02 按项目汇总工时（合计 + 参与人数）"


def _timesheet_pending(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    """待审批工时。approve_state_：0 已保存 / 1 待审核 / 2 审核中 / 3 通过 / 4 不通过。"""
    if "pp_timesheet" not in pack.tables or "工时" not in q:
        return None
    if not re.search(r"待审批|未审批|审核中|没审批|待审核", q):
        return None
    sql = (
        "SELECT `e`.`name_` AS `填报人`,\n"
        "       `t`.`timesheet_date_` AS `日期`,\n"
        "       `t`.`normal_hours_` AS `工时`,\n"
        "       CASE `t`.`approve_state_` WHEN '0' THEN '已保存' WHEN '1' THEN '待审核'\n"
        "            WHEN '2' THEN '审核中' WHEN '3' THEN '审核通过'\n"
        "            WHEN '4' THEN '审核不通过' ELSE `t`.`approve_state_` END AS `审核状态`,\n"
        "       `p`.`name_` AS `项目名称`,\n"
        "       `t`.`work_content_` AS `工作内容`\n"
        "FROM `pp_timesheet` `t`\n"
        "LEFT JOIN `iform_party_employee` `e` ON `e`.`id_` = `t`.`user_id_`\n"
        "LEFT JOIN `pp_project` `p` ON `p`.`id_` = `t`.`project_id_`\n"
        "WHERE `t`.`approve_state_` IN ('1', '2')\n"
        "ORDER BY `t`.`timesheet_date_` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, "工时 · TS-04 待审批工时明细（approve_state_ 1=待审核 / 2=审核中）"


def _timesheet_by_object(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if "pp_timesheet" not in pack.tables or "工时" not in q:
        return None
    # 分布本身就是聚合，不要求再出现「汇总/合计」
    if not re.search(r"对象类型|对象属性|按对象|类型分布|分布", q):
        return None
    sql = (
        "SELECT `t`.`object_type_` AS `对象类型`,\n"
        "       ROUND(SUM(`t`.`normal_hours_`), 1) AS `工时合计`,\n"
        "       COUNT(*) AS `记录数`\n"
        "FROM `pp_timesheet` `t`\n"
        "GROUP BY `t`.`object_type_`\n"
        "ORDER BY `工时合计` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, "工时 · TS-05 工时按对象类型分布（other/task/dev_task/defect…）"


# ---------------------------------------------------------------- 商务合同（CT）

# 合同状态/类别/收款状态都是英文码，报表里必须转中文，否则实施看不懂
_CT_STATE = (
    "CASE `c`.`state_` WHEN 'inProgress' THEN '执行中' WHEN 'underNegotiation' THEN '洽谈中'"
    " WHEN 'paused' THEN '暂停' WHEN 'completed' THEN '已完成'"
    " WHEN 'terminated' THEN '已终止' ELSE `c`.`state_` END"
)
_CT_CATEGORY = (
    "CASE `c`.`category_` WHEN 'productContract' THEN '产品合同'"
    " WHEN 'developmentContract' THEN '开发合同' WHEN 'supportContract' THEN '支持合同'"
    " ELSE `c`.`category_` END"
)
_CT_PAY_STATE = (
    "CASE `c`.`payment_state_` WHEN 'unpaid' THEN '未收款'"
    " WHEN 'partiallyPaid' THEN '部分收款' WHEN 'paid' THEN '已收清'"
    " ELSE `c`.`payment_state_` END"
)


def _ct_ok(pack: SchemaPack, q: str) -> bool:
    return "pp_contract" in pack.tables and "合同" in q


def _contract_ledger(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _ct_ok(pack, q):
        return None
    if not re.search(r"台账|清单|一览|总览|列表|全部合同", q):
        return None
    sql = (
        "SELECT `c`.`name_` AS `合同名称`,\n"
        "       `c`.`customer_name_` AS `客户`,\n"
        f"       {_CT_CATEGORY} AS `合同类别`,\n"
        "       `c`.`amount_` AS `合同金额`,\n"
        "       `c`.`currency_` AS `币种`,\n"
        f"       {_CT_STATE} AS `状态`,\n"
        "       `c`.`payment_progress_` AS `收款进度%`,\n"
        f"       {_CT_PAY_STATE} AS `收款状态`\n"
        "FROM `pp_contract` `c`\n"
        f"WHERE {_alive('c')}\n"
        "ORDER BY `c`.`amount_` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, "商务 · CT-01 合同台账（金额/类别/状态/收款进度，英文码已转中文）"


def _contract_collection(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _ct_ok(pack, q):
        return None
    if not re.search(r"收款|回款|已收|未结|应收|欠款|到账", q):
        return None
    only_unsettled = bool(re.search(r"未结|未收|欠款|应收", q))
    sql = (
        "SELECT `c`.`name_` AS `合同名称`,\n"
        "       `c`.`customer_name_` AS `客户`,\n"
        "       `c`.`amount_` AS `合同额`,\n"
        "       COALESCE(`c`.`total_payment_amount`, 0) AS `已收`,\n"
        "       COALESCE(`c`.`unsettled_amount`, 0) AS `未结`,\n"
        "       `c`.`payment_progress_` AS `收款进度%`,\n"
        f"       {_CT_PAY_STATE} AS `收款状态`\n"
        "FROM `pp_contract` `c`\n"
        f"WHERE {_alive('c')}"
        + (" AND COALESCE(`c`.`unsettled_amount`, 0) > 0" if only_unsettled else "")
        + "\nORDER BY `未结` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    note = "商务 · CT-02 合同收款执行（合同额 / 已收 / 未结 / 进度）"
    if only_unsettled:
        note += "；只看未结清"
    return sql, note


def _contract_by_group(pack: SchemaPack, q: str) -> tuple[str, str] | None:
    if not _ct_ok(pack, q):
        return None
    if not re.search(r"按|分|各", q):
        return None
    if re.search(r"币种|货币", q):
        col, label, expr = "currency_", "币种", "`c`.`currency_`"
    elif re.search(r"客户", q):
        col, label, expr = "customer_name_", "客户", "`c`.`customer_name_`"
    elif re.search(r"类别|类型|品种", q):
        col, label, expr = "category_", "合同类别", _CT_CATEGORY
    else:
        return None
    if not re.search(r"汇总|合计|统计|多少|几个|数量|金额", q):
        return None
    sql = (
        f"SELECT {expr} AS `{label}`,\n"
        "       COUNT(*) AS `合同数`,\n"
        "       ROUND(SUM(`c`.`amount_`), 2) AS `金额合计`\n"
        "FROM `pp_contract` `c`\n"
        f"WHERE {_alive('c')}\n"
        f"GROUP BY {expr}\n"
        "ORDER BY `金额合计` DESC\n"
        f"LIMIT {_limit(q)}"
    )
    return sql, f"商务 · CT-04 合同按{label}汇总（合同数 + 金额合计）"


# ---------------------------------------------------------------- 目录
# 顺序即优先级：越具体的判据越靠前，避免被泛判据抢走
CATALOG: tuple[tuple[re.Pattern, Builder], ...] = (
    # 健康度一页纸放最前：它是最具体的判据，一旦命中就不该被单项报表抢走
    (re.compile(r"项目健康度|健康度|一页纸|项目总览|项目概览|整体情况|项目汇总表|项目全景"), _project_health),
    # 进度偏差：必须同时提到「计划进度」和「实际进度」，或直接说「进度偏差」
    (re.compile(r"进度偏差|(?:计划|实际)进度.*(?:差|偏差|对比|相比)|(?:计划进度|planned).*(?:实际进度|progress)"
                r"|(?:实际进度).*(?:计划进度)|进度落后|落后于计划|进度跟不上|进度和计划|与计划.*进度"), _progress_gap),
    (re.compile(r"亮.{0,2}(?:红|黄|绿)灯|(?:红|黄|绿)灯"), _traffic_light),
    (re.compile(r"里程碑.*(?:达成|完成情况|状态|情况|逾期|延期)|(?:达成|完成).*里程碑"), _milestone_status),
    (re.compile(r"超支|超预算|超出预算"), _budget_vs_cost),
    (re.compile(r"预算.*(?:实际成本|成本|执行|消耗)|成本.*预算|预算执行"), _budget_vs_cost),
    (re.compile(r"风险.*(?:矩阵|可能性|概率)|风险矩阵"), _risk_dist),
    (re.compile(r"风险.*(?:分布|按.*(?:等级|状态|级别))"), _risk_dist),
    (re.compile(r"风险.*(?:未关闭|未闭环|跟踪|high|High|高)|高风险"), _risk_open),
    (re.compile(r"问题.*(?:分布|按.*(?:优先级|严重|状态|类型|分类))"), _issue_dist),
    (re.compile(r"缺陷.*(?:分布|按.*(?:严重|等级|状态|类型|根因))"), _defect_dist),
    (re.compile(r"(?:成员|人员|人|资源|人力|团队).*(?:负载|工作量|任务数|饱和度|利用率|忙)"
                r"|(?:负载|利用率).*(?:成员|人员|人|资源|人力)"
                r"|(?:谁|哪个).*(?:任务|活).*(?:最多|最忙)|任务最多"), _member_load),
    # 工时：汇总类问法（计数类「多少条工时」仍走启发式，基准期望列名「数量」）
    (re.compile(r"(?:待审批|未审批|审核中|待审核|没审批).{0,4}工时"
                r"|工时.{0,4}(?:待审批|未审批|审核中|待审核|没审批)"), _timesheet_pending),
    (re.compile(r"工时.{0,8}(?:对象类型|对象属性|按对象|类型分布)|对象类型.{0,6}工时"), _timesheet_by_object),
    (re.compile(r"(?:每个|各|按)?\s*(?:成员|人员|填报人|人).*工时.*(?:汇总|合计|总|多少小时|时长)"
                r"|工时.*(?:按|每个)(?:人|成员|填报人)"), _timesheet_by_member),
    (re.compile(r"工时.*(?:按|每个).{0,4}项目|项目.*工时.*(?:汇总|合计|总)"
                r"|(?:每个|各)项目.{0,6}工时"), _timesheet_by_project),
    # 商务合同
    (re.compile(r"合同.*(?:台账|清单|一览|总览|列表)|(?:台账|清单).*合同"), _contract_ledger),
    (re.compile(r"(?:未结|未收|应收|欠款|到账).{0,6}合同|合同.{0,8}(?:收款|回款|已收|未结|应收|欠款|到账)"), _contract_collection),
    (re.compile(r"合同.*(?:按|分|各).{0,6}(?:币种|类别|类型|客户).*(?:汇总|合计|统计|多少|数量|金额)"
                r"|合同.*(?:币种|类别).*(?:汇总|合计)"), _contract_by_group),
    (re.compile(r"(?:每个|各)项目.*(?:任务|完成|逾期).*(?:情况|统计|数)|项目任务完成"), _project_load),
)


def match(pack: SchemaPack, question: str) -> tuple[str, str] | None:
    """按目录顺序试，命中即返回。"""
    for pattern, builder in CATALOG:
        if pattern.search(question):
            hit = builder(pack, question)
            if hit is not None:
                return hit
    return None
