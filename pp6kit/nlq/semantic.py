"""业务语义层：把「用户嘴里的词」钉到「库里真实的表和列」。

为什么不靠模型自己猜
--------------------
模型看得到 schema，但不知道这家公司的口径：
  - 「项目状态」到底是 pp_project.state_id_，还是 phase_、progress_？
  - 「项目阶段」是多值 phase_ids_，join 要用 FIND_IN_SET，不是 =
  - 「任务责任人」是 assignee_ids_ 多值，不是单个 user id
这些规则一旦写错，SQL 能跑、结果却是错的，比报错更难发现。

这里只维护**能确定**的少量口径；每次按 pack 里实际存在的表过滤，
换库/换版本不会因为少一张表就整段失效。
"""
from __future__ import annotations

from .introspect import SchemaPack

# terms: 用户可能怎么说；tables: 规则依赖的表（缺表就不注入）
RULES: list[dict] = [
    {
        "terms": ("项目状态", "项目进展", "状态"),
        "tables": ("pp_project", "pp_project_state"),
        "text": "「项目状态」用 pp_project.state_id_ 关联 pp_project_state.name_；"
                "不要拿 phase_（阶段）或 progress_（进度）代替状态。",
    },
    {
        "terms": ("项目阶段", "当前阶段", "阶段"),
        "tables": ("pp_project", "pp_project_phase"),
        "text": "「项目阶段」是 pp_project.phase_ids_ 多值字段，"
                "关联 pp_project_phase 要用 FIND_IN_SET(pp_project_phase.id_, phase_ids_)，不能写 =。",
    },
    {
        "terms": ("项目负责人", "项目经理", "负责人"),
        "tables": ("pp_project",),
        "text": "「项目负责人/项目经理」是 pp_project.manager_ids_ 多值字段，"
                "按人筛选用 FIND_IN_SET(人员id, manager_ids_)；显示名要关联 iform_party_employee。",
    },
    {
        "terms": ("任务责任人", "任务负责人", "责任人", "执行人"),
        "tables": ("pp_task",),
        "text": "「任务责任人」是 pp_task.assignee_ids_ 多值字段，"
                "按人筛选用 FIND_IN_SET(人员id, assignee_ids_)。",
    },
    {
        "terms": ("项目类型", "项目分类", "类型"),
        "tables": ("pp_project", "pp_classification"),
        "text": "「项目类型」用 pp_project.classification_id_ 关联 pp_classification.name_。",
    },
    {
        "terms": ("项目级别", "项目分级", "级别"),
        "tables": ("pp_project", "pp_project_grade"),
        "text": "「项目级别」用 pp_project.grade_id_ 关联 pp_project_grade.name_。",
    },
    {
        "terms": ("所属部门", "隶属部门", "部门"),
        "tables": ("pp_project", "iform_party_org"),
        "text": "「所属部门」用 pp_project.org_id_ 关联 iform_party_org.name_。",
    },
    {
        "terms": ("合同金额", "合同额", "金额"),
        "tables": ("pp_contract",),
        "text": "「合同金额」是 pp_contract.amount_；不要用预算表或回款表代替。",
    },
    {
        "terms": ("项目成员", "团队成员", "项目团队", "角色"),
        "tables": ("pp_project_member", "pp_project_role"),
        "text": "「项目成员/团队」必须经 pp_project_member 关联 pp_project_role；"
                "只查 member 表会漏掉角色，历史错误答案就是这么来的。",
    },
]

_ALWAYS = "默认过滤删除数据：COALESCE(<主表>.deleted_, 'N') <> 'Y'；日期列用 create_time_。"


def active_rules(pack: SchemaPack) -> list[dict]:
    """只返回当前库确实有这些表的规则。"""
    out = []
    for r in RULES:
        if all(t in pack.tables for t in r["tables"]):
            out.append(r)
    return out


def prompt_block(pack: SchemaPack) -> str:
    rules = active_rules(pack)
    if not rules:
        return _ALWAYS
    lines = ["业务口径（必须遵守，优先级高于你自己的推断）："]
    lines += [f"- {r['text']}" for r in rules]
    lines.append(f"- {_ALWAYS}")
    return "\n".join(lines)


def glossary(pack: SchemaPack) -> list[dict]:
    """给前端/设置页展示「当前生效的口径」。"""
    return [{"terms": list(r["terms"]), "rule": r["text"]} for r in active_rules(pack)]
