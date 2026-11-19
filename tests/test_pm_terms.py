"""项目管理专业口径：AI 字段名/编码对齐 PowerProject 原生字段。"""
from __future__ import annotations

from pp6kit import ai_bo, pm_terms
from tests._harness import case


@case("PM01", "naming", "AI 字段按项目管理标准英文编码归一", "P0",
      ["项目预算金额→budget", "任务责任人→assignee",
       "风险等级→riskRating", "问题标题→issueTitle", "合同金额→contractAmount",
       "自定义字段原样保留"])
def _():
    assert pm_terms.family("project_risk", "项目风险") == "risk", "risk 必须先于 project 匹配"
    assert pm_terms.family("task_manage", "任务管理") == "task"

    assert pm_terms.PM_CODES["project"]["预算金额"] == "budget"
    assert pm_terms.PM_CODES["project"]["所属部门"] == "owningDepartment"
    assert pm_terms.PM_CODES["project"]["项目经理"] == "projectManager"
    assert pm_terms.PM_CODES["task"]["责任人"] == "assignee"
    assert pm_terms.PM_CODES["task"]["计划开始时间"] == "plannedStartDate"
    assert pm_terms.PM_CODES["risk"]["风险等级"] == "riskRating"
    assert pm_terms.PM_CODES["risk"]["应对措施"] == "mitigationPlan"
    assert pm_terms.PM_CODES["issue"]["问题名称"] == "issueTitle"
    assert pm_terms.PM_CODES["issue"]["问题责任人"] == "issueOwner"
    assert pm_terms.PM_CODES["contract"]["合同金额"] == "contractAmount"
    assert pm_terms.PM_CODES["project"].get("自定义评分") is None


@case("PM02", "naming", "build_bo 落库前完成 PM 标准编码归一，不把泛化编码带进包", "P0",
      ["项目名称→projectName", "预算金额→budget", "所属部门→owningDepartment",
       "责任人→projectManager", "返回一条告知，预览能看到改了什么"])
def _():
    bo, notices = ai_bo.build_bo({
        "name": "项目立项", "code": "project_create",
        "fields": [
            {"name": "项目名称", "type": "varchar"},
            {"name": "预算金额", "type": "number"},
            {"name": "所属部门", "type": "department"},
            {"name": "责任人", "type": "user"},
            {"name": "项目状态", "type": "object",
             "objectType": "project_state"},
        ],
    })
    by_name = {f.name: f for f in bo.fields}
    assert by_name["项目名称"].code == "projectName"
    assert by_name["预算金额"].code == "budget"
    assert by_name["所属部门"].code == "owningDepartment"
    assert by_name["项目经理"].code == "projectManager"
    assert by_name["项目状态"].code == "projectStatus"
    assert any("项目管理标准" in n for n in notices), notices
