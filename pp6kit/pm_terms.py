"""项目管理专业口径：把 AI 的泛化字段名/编码对齐到 PowerProject 原生字段。

为什么要这一层
--------------
模型能生成「预算金额 / planStartTime / ownerId」这种**通用**命名，但
PowerProject 的原生对象有自己的口径（iform_bo_attr 实测）：

    项目：totalBudget / start / finish / managerIds / stateId
    任务：assigneeIds / start / finish / state / priority
    风险：rating / memberIds / mitigation / closeTime
    问题：title / causerId / handlerIds / status
    合同：amount / signTime / planInspectDate / state

通用名不是错，但导入后和系统已有对象对不齐：报表、流程、列表按原生编码取数，
对不上就是空列。这里只处理**能确定**的标准概念，用户自定义字段原样保留。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Term:
    code: str
    label: str
    type_new: str = ""


# 先匹配更具体的业务族；project_risk 必须归到 risk，不能归 project
_FAMILIES: list[tuple[str, tuple[str, ...]]] = [
    ("risk", ("风险",)),
    ("issue", ("问题", "缺陷")),
    ("task", ("任务",)),
    ("contract", ("合同",)),
    ("requirement", ("需求",)),
    ("project", ("项目", "立项")),
]

_MAPS: dict[str, dict[str, Term]] = {
    "project": {
        "项目名称": Term("name", "项目名称"),
        "名称": Term("name", "项目名称"),
        "项目简称": Term("shortName", "项目简称"),
        "项目编号": Term("code", "项目编号"),
        "项目编码": Term("code", "项目编号"),
        "编号": Term("code", "项目编号"),
        "项目类型": Term("classificationId", "项目类型"),
        "项目分类": Term("classificationId", "项目类型"),
        "项目级别": Term("gradeId", "项目级别"),
        "项目状态": Term("stateId", "项目状态"),
        "状态": Term("stateId", "项目状态"),
        "项目阶段": Term("phaseIds", "项目阶段"),
        "项目经理": Term("managerIds", "项目经理"),
        "项目负责人": Term("managerIds", "项目经理"),
        "负责人": Term("managerIds", "项目经理"),
        "责任人": Term("managerIds", "项目经理"),
        "所属部门": Term("orgId", "隶属部门"),
        "隶属部门": Term("orgId", "隶属部门"),
        "计划开始时间": Term("start", "计划开始时间"),
        "计划开始日期": Term("start", "计划开始时间"),
        "开始时间": Term("start", "计划开始时间"),
        "计划完成时间": Term("finish", "计划完成时间"),
        "计划完成日期": Term("finish", "计划完成时间"),
        "完成时间": Term("finish", "计划完成时间"),
        "结束时间": Term("finish", "计划完成时间"),
        "实际开始时间": Term("actualStart", "实际开始时间"),
        "实际完成时间": Term("actualFinish", "实际结束时间"),
        "实际结束时间": Term("actualFinish", "实际结束时间"),
        "总预算": Term("totalBudget", "总预算"),
        "预算": Term("totalBudget", "总预算"),
        "预算金额": Term("totalBudget", "总预算"),
        "项目预算": Term("totalBudget", "总预算"),
        "总成本": Term("totalCost", "总成本"),
        "成本": Term("totalCost", "总成本"),
        "项目成本": Term("totalCost", "总成本"),
        "实际进度": Term("progress", "实际进度"),
        "进度": Term("progress", "实际进度"),
        "计划进度": Term("plannedProgress", "计划进度"),
        "项目工期": Term("duration", "项目工期(单位:分钟)"),
        "工期": Term("duration", "项目工期(单位:分钟)"),
        "项目标准工时": Term("standardHours", "项目标准工时"),
        "标准工时": Term("standardHours", "项目标准工时"),
    },
    "task": {
        "任务名称": Term("name", "任务名称"),
        "名称": Term("name", "任务名称"),
        "任务编号": Term("code", "任务编号"),
        "编号": Term("code", "任务编号"),
        "所属项目": Term("projectId", "所属项目"),
        "关联项目": Term("projectId", "所属项目"),
        "任务责任人": Term("assigneeIds", "任务责任人"),
        "责任人": Term("assigneeIds", "任务责任人"),
        "负责人": Term("assigneeIds", "任务责任人"),
        "任务状态": Term("state", "任务状态"),
        "状态": Term("state", "任务状态"),
        "任务类别": Term("category", "任务类别", "varchar"),
        "任务类型": Term("category", "任务类别", "varchar"),
        "类别": Term("category", "任务类别", "varchar"),
        "计划开始时间": Term("start", "计划开始时间"),
        "开始时间": Term("start", "计划开始时间"),
        "计划完成时间": Term("finish", "计划完成时间"),
        "完成时间": Term("finish", "计划完成时间"),
        "结束时间": Term("finish", "计划完成时间"),
        "实际开始时间": Term("actualStart", "实际开始时间"),
        "实际完成时间": Term("actualFinish", "实际完成时间"),
        "实际结束时间": Term("actualFinish", "实际完成时间"),
        "标准工时": Term("standardHours", "标准工时"),
        "工期": Term("duration", "工期"),
        "进度": Term("progress", "进度"),
        "优先级": Term("priority", "任务重要性"),
        "任务重要性": Term("priority", "任务重要性"),
        "任务说明": Term("notes", "任务说明"),
        "备注": Term("notes", "任务说明"),
        "是否里程碑": Term("isMilestone", "是否里程碑"),
        "里程碑": Term("isMilestone", "是否里程碑"),
    },
    "risk": {
        "风险名称": Term("name", "风险名称"),
        "名称": Term("name", "风险名称"),
        "风险描述": Term("description", "风险描述"),
        "描述": Term("description", "风险描述"),
        "发生概率": Term("probability", "发生概率"),
        "概率": Term("probability", "发生概率"),
        "影响程度": Term("impact", "影响"),
        "影响": Term("impact", "影响"),
        "风险等级": Term("rating", "风险等级"),
        "等级": Term("rating", "风险等级"),
        "严重程度": Term("severity", "严重程度"),
        "缓解或应对措施": Term("mitigation", "缓解或应对措施"),
        "应对措施": Term("mitigation", "缓解或应对措施"),
        "缓解措施": Term("mitigation", "缓解或应对措施"),
        "应对计划": Term("strategyPlan", "应对计划"),
        "责任人": Term("memberIds", "责任人"),
        "负责人": Term("memberIds", "责任人"),
        "状态": Term("state", "状态"),
        "关闭时间": Term("closeTime", "关闭时间"),
        "关闭日期": Term("closeTime", "关闭时间"),
        "开始时间": Term("startTime", "开始时间"),
        "到期时间": Term("dueTime", "到期时间"),
        "风险类别": Term("category", "风险类别"),
        "类别": Term("category", "风险类别"),
        "风险来源": Term("source", "来源"),
        "来源": Term("source", "来源"),
        "优先级": Term("priority", "优先级"),
    },
    "issue": {
        "问题标题": Term("title", "问题标题"),
        "问题名称": Term("title", "问题标题"),
        "标题": Term("title", "问题标题"),
        "问题编号": Term("code", "问题编号"),
        "编号": Term("code", "问题编号"),
        "问题描述": Term("description", "描述"),
        "描述": Term("description", "描述"),
        "问题责任人": Term("causerId", "问题责任人"),
        "责任人": Term("causerId", "问题责任人"),
        "当前处理人": Term("handlerIds", "当前处理人"),
        "处理人": Term("handlerIds", "当前处理人"),
        "状态": Term("status", "状态"),
        "优先级": Term("priority", "优先级"),
        "问题关闭时间": Term("closeTime", "问题关闭时间"),
        "关闭时间": Term("closeTime", "问题关闭时间"),
        "关闭日期": Term("closeTime", "问题关闭时间"),
        "问题来源": Term("source", "问题来源"),
        "来源": Term("source", "问题来源"),
        "问题小类": Term("category", "问题小类"),
        "类别": Term("category", "问题小类"),
        "解决过程": Term("progress", "问题解决过程"),
        "解决方案": Term("solution", "问题解决方案"),
        "所属部门": Term("orgId", "所属部门"),
        "关联项目": Term("projectId", "关联项目"),
        "关联任务": Term("taskId", "关联任务"),
    },
    "contract": {
        "合同名称": Term("name", "合同名称"),
        "名称": Term("name", "合同名称"),
        "外部合同编号": Term("code", "外部合同编号"),
        "合同编号": Term("code", "外部合同编号"),
        "编号": Term("code", "外部合同编号"),
        "合同金额": Term("amount", "合同金额"),
        "金额": Term("amount", "合同金额"),
        "客户": Term("customerId", "客户"),
        "供应商": Term("vendorId", "供应商"),
        "签署日期": Term("signTime", "签署日期"),
        "签署时间": Term("signTime", "签署日期"),
        "计划验收日期": Term("planInspectDate", "计划验收日期"),
        "实际验收日期": Term("actualInspectDate", "实际验收日期"),
        "合同状态": Term("state", "合同状态"),
        "状态": Term("state", "合同状态"),
        "合同类别": Term("category", "合同类别"),
        "类别": Term("category", "合同类别"),
        "关联项目": Term("projectIds", "关联项目"),
        "附件信息": Term("documentIds", "附件信息"),
        "附件": Term("documentIds", "附件信息"),
        "所属部门": Term("departmentId", "所属部门"),
        "优先级": Term("priority", "优先级"),
    },
    "requirement": {
        "需求标题": Term("title", "标题"),
        "需求名称": Term("title", "标题"),
        "标题": Term("title", "标题"),
        "需求描述": Term("description", "需求描述"),
        "描述": Term("description", "需求描述"),
        "提出人": Term("submitterId", "提出人"),
        "提交人": Term("submitterId", "提出人"),
        "提出部门": Term("submitDepartmentId", "提出部门"),
        "紧急程度": Term("urgency", "紧急程度"),
        "希望完成日期": Term("planFinishDate", "希望完成日期"),
        "计划完成日期": Term("planFinishDate", "希望完成日期"),
        "需求负责人": Term("assigneeId", "需求负责人"),
        "责任人": Term("assigneeId", "需求负责人"),
        "关联项目": Term("projectId", "关联项目"),
        "关联产品": Term("productId", "关联产品"),
        "附件": Term("documentIds", "附件"),
    },
}

_PUNCT = re.compile(r"[\s（）()：:、,，。.·\-]+")


def normalize(name: str) -> str:
    return _PUNCT.sub("", (name or "").strip())


def family(bo_code: str, bo_name: str = "") -> str | None:
    hay = f"{bo_code or ''} {bo_name or ''}".lower()
    for key, words in _FAMILIES:
        if any(w in hay for w in words):
            return key
    return None


def lookup(fam: str | None, name: str) -> Term | None:
    if not fam:
        return None
    return _MAPS.get(fam, {}).get(normalize(name))


def apply(bo) -> list[str]:
    """就地改名/改编码；返回一条给用户看的说明。"""
    fam = family(bo.code, bo.name)
    if not fam:
        return []
    used = {f.code for f in bo.fields}
    code_map: dict[str, str] = {}
    changed: list[str] = []
    for f in bo.fields:
        term = lookup(fam, f.name)
        if term is None:
            continue
        old_name, old_code = f.name, f.code
        if term.code != f.code:
            # 编码冲突：同名对象里已经有这个原生字段，保留原编码，只统一中文名
            if term.code not in used or term.code == f.code:
                used.discard(f.code)
                code_map[old_code] = term.code
                f.code = term.code
                used.add(term.code)
                if f.type_new == "dic" and f.object_type:
                    # 字典 key 跟着编码走，否则导入后取值来源指向旧 key
                    if f.object_type == f"{bo.code}_{old_code}":
                        f.object_type = f"{bo.code}_{term.code}"
        f.name = term.label
        if term.type_new and f.type_new != term.type_new:
            f.type_new = term.type_new
            # 标准化成文本/数字后，旧的 object/dic 配置不能留着
            if term.type_new not in ("object", "dic"):
                f.object_type = ""
                f.choices = []
                f.choice_groups = {}
        if (old_name, old_code) != (f.name, f.code):
            changed.append(f"{old_name}→{f.name}（{f.code}）")
    # 级联父级引用的是编码：字段改名后要把引用一起改，否则校验会报父级不存在
    if code_map:
        for f in bo.fields:
            if f.cascade_parent in code_map:
                f.cascade_parent = code_map[f.cascade_parent]
    if changed:
        return ["按 PowerProject 原生口径统一字段：" + "、".join(changed[:10])]
    return []


PROMPT = """=== 字段英文编码：按项目管理专业术语（不是通用 CRUD，也不是照抄数据库）===
你是项目管理专家（PMP / PRINCE2 / 敏捷），生成字段时先想清楚它在项目管理里的标准英文，再落成小驼峰编码。

硬性约束：
1. 编码必须是小驼峰、全英文、语义化、24 字符以内；禁止拼音（xiangMuMingCheng）、
   禁止占位名（field1/data1/value1/item1）、禁止中英混杂（MAHChiYouRen）。
2. 用项目管理通行词，而不是泛词：
   项目：projectName / projectCode / projectType / projectLevel / projectStatus /
         projectPhase / projectManager / projectSponsor / owningDepartment /
         plannedStartDate / plannedFinishDate / actualStartDate / actualFinishDate /
         budget / actualCost / percentComplete / plannedPercentComplete /
         wbsCode / milestone / deliverable / acceptanceCriteria / projectObjective / projectScope
   任务：taskName / taskCode / projectId / assignee / taskStatus / taskCategory /
         plannedStartDate / plannedFinishDate / actualStartDate / actualFinishDate /
         effortHours / duration / percentComplete / priority / milestone / deliverable
   风险：riskName / riskDescription / riskProbability / riskImpact / riskRating /
         riskSeverity / mitigationPlan / contingencyPlan / riskOwner / riskStatus /
         closureDate / riskCategory / riskSource
   问题：issueTitle / issueCode / issueDescription / issueOwner / assignee / issueStatus /
         priority / closureDate / issueSource / issueCategory / resolution
   合同：contractName / contractCode / contractAmount / customer / vendor / counterparty /
         signDate / plannedAcceptanceDate / actualAcceptanceDate / contractStatus /
         contractType / relatedProjects / attachments / effectiveDate / expiryDate
3. 编码要和字段中文名一一对应；不要为了“像系统”而缩成 name/code/status 这种泛词。
4. 输出前自检：每个编码单独拿出来，能不能让一个项目经理一眼知道是什么字段？
   不能就重命名，不要交差。"""


PM_CODES: dict[str, dict[str, str]] = {
    "project": {
        "项目名称": "projectName", "名称": "projectName",
        "项目编号": "projectCode", "项目编码": "projectCode", "编号": "projectCode",
        "项目类型": "projectType", "项目分类": "projectType",
        "项目级别": "projectLevel", "项目状态": "projectStatus", "状态": "projectStatus",
        "项目阶段": "projectPhase", "项目目标": "projectObjective",
        "项目范围": "projectScope", "项目背景": "projectBackground",
        "项目经理": "projectManager", "项目负责人": "projectManager", "负责人": "projectManager",
        "责任人": "projectManager", "发起人": "projectSponsor", "项目发起人": "projectSponsor",
        "所属部门": "owningDepartment", "隶属部门": "owningDepartment", "部门": "owningDepartment",
        "计划开始时间": "plannedStartDate", "计划开始日期": "plannedStartDate",
        "开始时间": "plannedStartDate", "计划完成时间": "plannedFinishDate",
        "计划完成日期": "plannedFinishDate", "完成时间": "plannedFinishDate",
        "结束时间": "plannedFinishDate", "实际开始时间": "actualStartDate",
        "实际完成时间": "actualFinishDate", "实际结束时间": "actualFinishDate",
        "总预算": "budget", "预算": "budget", "预算金额": "budget", "项目预算": "budget",
        "总成本": "actualCost", "成本": "actualCost", "项目成本": "actualCost",
        "实际成本": "actualCost", "实际进度": "percentComplete", "进度": "percentComplete",
        "计划进度": "plannedPercentComplete", "项目工期": "duration", "工期": "duration",
        "项目标准工时": "effortHours", "标准工时": "effortHours",
        "WBS": "wbsCode", "WBS编码": "wbsCode", "里程碑": "milestone",
        "交付物": "deliverable", "验收标准": "acceptanceCriteria",
        "优先级": "priority", "项目优先级": "priority", "标签": "tags",
        "备注": "notes", "描述": "description", "项目描述": "description",
    },
    "task": {
        "任务名称": "taskName", "名称": "taskName",
        "任务编号": "taskCode", "编号": "taskCode",
        "所属项目": "projectId", "关联项目": "projectId",
        "任务责任人": "assignee", "责任人": "assignee", "负责人": "assignee",
        "任务状态": "taskStatus", "状态": "taskStatus",
        "任务类别": "taskCategory", "任务类型": "taskCategory", "类别": "taskCategory",
        "计划开始时间": "plannedStartDate", "开始时间": "plannedStartDate",
        "计划完成时间": "plannedFinishDate", "完成时间": "plannedFinishDate",
        "结束时间": "plannedFinishDate", "实际开始时间": "actualStartDate",
        "实际完成时间": "actualFinishDate", "实际结束时间": "actualFinishDate",
        "标准工时": "effortHours", "实际工时": "actualEffortHours", "工时": "effortHours",
        "工期": "duration", "进度": "percentComplete",
        "优先级": "priority", "任务重要性": "priority",
        "里程碑": "milestone", "是否里程碑": "isMilestone",
        "任务说明": "notes", "备注": "notes", "交付物": "deliverable",
    },
    "risk": {
        "风险名称": "riskName", "名称": "riskName",
        "风险描述": "riskDescription", "描述": "riskDescription",
        "发生概率": "riskProbability", "概率": "riskProbability",
        "影响程度": "riskImpact", "影响": "riskImpact",
        "风险等级": "riskRating", "等级": "riskRating", "风险评级": "riskRating",
        "严重程度": "riskSeverity", "风险类别": "riskCategory", "类别": "riskCategory",
        "风险来源": "riskSource", "来源": "riskSource",
        "缓解或应对措施": "mitigationPlan", "应对措施": "mitigationPlan",
        "缓解措施": "mitigationPlan", "应对计划": "contingencyPlan",
        "责任人": "riskOwner", "负责人": "riskOwner", "风险责任人": "riskOwner",
        "状态": "riskStatus", "关闭时间": "closureDate", "关闭日期": "closureDate",
        "开始时间": "startDate", "到期时间": "dueDate",
        "优先级": "priority", "进展反馈": "progressUpdate",
    },
    "issue": {
        "问题标题": "issueTitle", "问题名称": "issueTitle", "标题": "issueTitle",
        "问题编号": "issueCode", "编号": "issueCode",
        "问题描述": "issueDescription", "描述": "issueDescription",
        "问题责任人": "issueOwner", "责任人": "issueOwner", "负责人": "issueOwner",
        "当前处理人": "assignee", "处理人": "assignee",
        "状态": "issueStatus", "问题状态": "issueStatus", "优先级": "priority",
        "问题关闭时间": "closureDate", "关闭时间": "closureDate", "关闭日期": "closureDate",
        "问题来源": "issueSource", "来源": "issueSource",
        "问题小类": "issueCategory", "类别": "issueCategory",
        "解决过程": "resolutionProgress", "解决方案": "resolution",
        "所属部门": "owningDepartment", "关联项目": "projectId", "关联任务": "taskId",
    },
    "contract": {
        "合同名称": "contractName", "名称": "contractName",
        "外部合同编号": "contractCode", "合同编号": "contractCode", "编号": "contractCode",
        "合同金额": "contractAmount", "金额": "contractAmount",
        "客户": "customer", "供应商": "vendor", "对方单位": "counterparty",
        "签署日期": "signDate", "签署时间": "signDate", "签订日期": "signDate",
        "计划验收日期": "plannedAcceptanceDate", "实际验收日期": "actualAcceptanceDate",
        "合同状态": "contractStatus", "状态": "contractStatus",
        "合同类别": "contractType", "合同类型": "contractType", "类别": "contractType",
        "关联项目": "relatedProjects", "附件信息": "attachments", "附件": "attachments",
        "所属部门": "owningDepartment", "优先级": "priority",
        "生效日期": "effectiveDate", "到期日期": "expiryDate",
        "承办部门": "owningDepartment", "经办人": "contractOwner",
    },
    "requirement": {
        "需求标题": "requirementTitle", "需求名称": "requirementTitle", "标题": "requirementTitle",
        "需求描述": "requirementDescription", "描述": "requirementDescription",
        "提出人": "requester", "提交人": "requester",
        "提出部门": "requestingDepartment", "紧急程度": "urgency",
        "希望完成日期": "plannedFinishDate", "计划完成日期": "plannedFinishDate",
        "需求负责人": "requirementOwner", "责任人": "requirementOwner",
        "关联项目": "projectId", "关联产品": "productId",
        "附件": "attachments", "验收标准": "acceptanceCriteria",
    },
}

PM_TYPES: dict[str, str] = {
    # 任务类别在 PM 里是文本分类，不是关联对象；生成 object 会得到空下拉
    "taskCategory": "varchar",
}

PM_LABELS: dict[tuple[str, str], str] = {
    ("project", "状态"): "项目状态",
    ("project", "责任人"): "项目经理",
    ("project", "负责人"): "项目经理",
    ("task", "状态"): "任务状态",
    ("task", "责任人"): "任务责任人",
    ("task", "负责人"): "任务责任人",
    ("task", "优先级"): "任务优先级",
    ("risk", "状态"): "风险状态",
    ("risk", "责任人"): "风险责任人",
    ("risk", "负责人"): "风险责任人",
    ("issue", "状态"): "问题状态",
    ("issue", "责任人"): "问题责任人",
    ("issue", "负责人"): "问题责任人",
    ("contract", "状态"): "合同状态",
}

_GENERIC = re.compile(r"^(field|data|value|item|col|column|test|tmp|a|b|c)\d*$", re.I)


def _is_pinyin(code: str, name: str) -> bool:
    from . import naming
    py = naming.to_english(name).lower()
    return bool(py) and code.lower() == py


def _looks_generic(code: str, name: str) -> bool:
    return bool(_GENERIC.match(code or "")) or _is_pinyin(code, name)


def apply(bo) -> list[str]:
    """按项目管理标准英文统一编码；只动标准概念，自定义字段原样保留。

    与旧版（照抄数据库）的区别：这里用 PMP/PRINCE2 的英文词，
    例如项目「预算」→ budget、「项目名称」→ projectName。
    """
    fam = family(bo.code, bo.name)
    if not fam:
        return []
    from . import naming

    mapping = PM_CODES.get(fam, {})
    used = {f.code for f in bo.fields}
    code_map: dict[str, str] = {}
    changed: list[str] = []
    for f in bo.fields:
        key = normalize(f.name)
        target = mapping.get(key)
        if target is None and _looks_generic(f.code, f.name):
            target = naming.semantic_code(f.name)
        if not target or target == f.code:
            continue
        if target in used and target != f.code:
            continue
        old_code = f.code
        used.discard(old_code)
        code_map[old_code] = target
        f.code = target
        used.add(target)
        ftype = PM_TYPES.get(target)
        if ftype and f.type_new != ftype:
            f.type_new = ftype
            if ftype not in ("object", "dic"):
                f.object_type = ""
                f.choices = []
                f.choice_groups = {}
        if f.type_new == "dic" and f.object_type == f"{bo.code}_{old_code}":
            f.object_type = f"{bo.code}_{target}"
        label = PM_LABELS.get((fam, key))
        if label:
            f.name = label
        changed.append(f"{f.name}→{target}")
    if code_map:
        for f in bo.fields:
            if f.cascade_parent in code_map:
                f.cascade_parent = code_map[f.cascade_parent]
    if changed:
        return ["按项目管理标准统一英文编码：" + "、".join(changed[:10])]
    return []
