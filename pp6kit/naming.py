"""中文名 → 英文编码：用户不填编码时自动生成。

规则来源
--------
1. 对照线上系统 4125 组「中文字段名 → 字段编码」真实数据统计得出
   （见 docs/线上系统对照.md 第十三节）
2. 系统的编码规律：小写下划线（对象）/ 小驼峰（字段），按**英文语义**命名，
   不是拼音（如 任务→task、风险→risk，不是 renwu/fengxian）

设计原则
--------
- 词典优先整词匹配（「项目汇报」→ projectReport，不是 project + report 硬拼）
- 匹配不到就退**拼音**：至少能生成合法编码，用户可自行改
- 永远保证输出合法：只用字母数字，首字符是字母
"""
from __future__ import annotations

import re

# 中文词 → 英文。按词长从长到短匹配，长词优先（避免「项目汇报」被拆成「项目」+「汇报」）
# 覆盖业务系统高频词汇，回测系统真实数据准确率 86%
WORDS: dict[str, str] = {
    # —— 基础信息 ——
    "名称": "name", "标题": "title", "编号": "code", "编码": "code",
    "描述": "description", "备注": "notes", "说明": "remark", "内容": "content",
    "类型": "type", "类别": "category", "分类": "classification",
    "状态": "status", "级别": "grade", "等级": "level", "阶段": "phase",
    "序号": "order", "排序": "order", "排序号": "order", "版本": "version",
    "拼音": "pinyin", "简称": "shortName", "全称": "fullName",
    "英文名称": "nameEn", "中文名称": "nameCn", "别名": "alias",
    "主键": "id", "标识": "key", "键": "key", "值": "value", "值域": "valueRange",
    # —— 时间 ——
    "创建时间": "createTime", "更新时间": "updateTime", "创建日期": "createDate",
    "日期": "date", "时间": "time", "年月": "yearMonth", "年份": "year", "月份": "month",
    "创建": "create", "更新": "update", "修改": "update",
    "开始": "start", "结束": "finish", "截止": "deadline", "完成": "complete",
    "计划": "plan", "实际": "actual", "工期": "duration", "耗时": "elapsed",
    "开始时间": "startTime", "结束时间": "endTime", "开始日期": "startDate", "结束日期": "endDate",
    "截止日期": "deadline", "完成时间": "finishTime", "完成日期": "finishDate",
    "计划开始": "planStart", "计划结束": "planFinish", "实际开始": "actualStart",
    "实际结束": "actualFinish", "时长": "duration", "天数": "days",
    # —— 人员组织 ——
    "人员": "user", "用户": "user", "负责人": "owner", "经理": "manager",
    "创建人": "createBy", "更新人": "updateBy", "修改人": "updateBy",
    "部门": "org", "组织": "org", "角色": "role", "岗位": "position",
    "成员": "member", "团队": "team", "员工": "employee", "用户组": "userGroup",
    "客户": "customer", "联系人": "contact", "供应商": "vendor", "签名": "signature",
    "审批人": "approver", "处理人": "handler", "参与人": "participant",
    "上级": "parent", "下级": "child", "父级": "parent", "子级": "child",
    # —— 业务主干 ——
    "项目": "project", "任务": "task", "需求": "requirement", "缺陷": "defect",
    "问题": "issue", "风险": "risk", "合同": "contract", "预算": "budget",
    "成本": "cost", "费用": "expense", "收入": "income", "利润": "profit",
    "金额": "amount", "价格": "price", "单价": "unitPrice", "总价": "totalPrice",
    "数量": "quantity", "单位": "unit", "税率": "taxRate", "币种": "currency",
    "发票": "invoice", "付款": "payment", "收款": "receipt", "结算": "settlement",
    "工时": "workHours", "进度": "progress", "里程碑": "milestone",
    "评审": "review", "审批": "approval", "流程": "process", "节点": "node",
    "文档": "document", "附件": "attachment", "文件": "file", "目录": "directory",
    "会议": "meeting", "行动": "action", "汇报": "report", "报表": "reportForm",
    "产品": "product", "物料": "material", "库存": "stock", "设备": "equipment",
    "质量": "quality", "安全": "security", "权限": "permission", "菜单": "menu",
    "字典": "dict", "参数": "param", "配置": "config", "模板": "template",
    "商机": "opportunity", "线索": "lead", "报价": "quotation", "订单": "order",
    "交付": "delivery", "验收": "acceptance", "质保": "warranty", "资质": "qualification",
    "基线": "baseline", "变更": "change", "变更集": "changeset",
    "项目集": "program", "项目组合": "portfolio", "工作台": "workbench",
    "看板": "board", "甘特": "gantt", "日历": "calendar", "日志": "log",
    "消息": "message", "通知": "notification", "提醒": "reminder",
    "统计": "statistics", "汇总": "summary", "明细": "detail", "清单": "list",
    "指标": "metric", "权重": "weight", "分数": "score",
    # —— 修饰 ——
    "是否": "is", "有效": "enable", "启用": "enabled", "禁用": "disabled",
    "删除": "deleted", "草稿": "draft", "主": "main", "子": "sub",
    "关联": "related", "所属": "belong", "来源": "source", "结果": "result",
    "原因": "reason", "优先级": "priority", "紧急": "urgency", "重要": "important",
    "颜色": "color", "图标": "icon", "地址": "address", "电话": "phone",
    "邮箱": "email", "网址": "url", "邮编": "zipCode", "传真": "fax",
    "扩展": "ext", "序号号": "order", "备注信息": "remark",
    "是否有效": "isValid", "是否删除": "isDeleted", "是否启用": "isEnabled",
    "是否默认": "isDefault", "是否内置": "isBuiltin",
    "创建者": "creator", "更新者": "updater",
    "审批状态": "approvalStatus", "流程状态": "processStatus",
    "开始日期时间": "startDateTime", "结束日期时间": "endDateTime",
    "父级编号": "parentCode", "父级名称": "parentName",
    "排序编号": "sortOrder", "显示名称": "displayName", "显示": "display",
    # —— 补充：常见但上面漏掉的 ——
    # 别写"申请单 → applyForm"：它会把「立项申请单位」的前三字吞掉，
    # 剩下一个「位」孤零零地走拼音，结果是 liXiangApplyFormWei
    "申请": "apply", "申请人": "applicant", "申请单号": "applyNo",
    "采购": "purchase", "采购单": "purchaseOrder", "销售": "sales",
    "入库": "stockIn", "出库": "stockOut", "盘点": "inventory",
    "程度": "level", "满意度": "satisfaction", "评价": "evaluation",
    "得分": "score", "排名": "ranking", "比率": "ratio", "占比": "proportion",
    "完成率": "completionRate", "使用率": "usage", "增长率": "growthRate",
    "申请日期": "applyDate", "申请时间": "applyTime", "申请事由": "applyReason",
    "事由": "reason", "依据": "basis", "参考": "reference",
    "组织架构": "orgStructure", "公司": "company", "企业": "enterprise",
    "品牌": "brand", "型号": "model", "规格": "spec", "材质": "material",
    "颜色": "color", "尺寸": "size", "重量": "weight", "体积": "volume",
    "包装": "packaging", "仓储": "warehouse", "物流": "logistics",
    "运输": "transport", "收货": "receive", "发货": "ship",
    "退货": "return", "换货": "exchange", "售后": "afterSales",
    "服务": "service", "反馈": "feedback", "投诉": "complaint",
    "建议": "suggestion", "意见": "opinion", "附件信息": "attachmentInfo",
    "图片": "image", "照片": "photo", "视频": "video", "音频": "audio",
    "链接": "link", "二维码": "qrcode", "条形码": "barcode",
    "里程碑": "milestone", "任务清单": "taskList", "待办": "todo",
    "已办": "done", "抄送": "cc", "转办": "transfer", "催办": "urge",
    "归档": "archive", "回收站": "recycleBin", "导入": "import", "导出": "export",
    "批量": "batch", "单个": "single", "全部": "all", "部分": "partial",
    "默认": "default", "自定义": "custom", "系统": "system", "内置": "builtin",
    "排序值": "sortValue", "层级": "depth", "是否叶子": "isLeaf",
    "父级": "parent", "子级": "child", "路径": "path", "深度": "depth",
    "数据": "data", "数据集": "dataset", "数据源": "dataSource",
    "列表": "list", "结果": "result", "评分": "score", "打分": "score",
    "分数": "score", "等级": "level", "列表页": "listPage",
    "工作": "work", "台账": "ledger", "档案": "archive", "履历": "resume",
    "经历": "experience", "环节": "step", "步骤": "step", "事项": "matter",
    "条目": "entry", "明细项": "detailItem", "子项": "subItem",
    # —— 业务对象后缀/动作用词（对齐系统真实对象名：
    #    project_create / requirement_category / test_case_base_category）——
    "立项": "create", "申请": "apply", "登记": "register", "跟踪": "track",
    "填报": "fill", "报销": "reimburse", "变更": "change",
    "纪要": "minutes", "周报": "weeklyReport", "日报": "dailyReport",
    "月报": "monthlyReport", "大类": "category", "小类": "subCategory",
    "用例": "testCase", "题库": "questionBank", "规则": "rule",
    "模板": "template", "看板": "board", "门户": "portal", "基线": "baseline",
    "测试": "test", "测试用例": "testCase", "库": "library", "持有人": "holder",
    "管理": "manage", "分析": "analysis", "统计": "statistics", "执行": "execute",
    "评价": "evaluate", "考核": "assessment", "培训": "training", "知识": "knowledge",
    "申请表": "apply", "台账表": "ledger",
}

# 去掉这些词再匹配（无实际语义的尾巴）
_NOISE = ("表", "单据", "记录", "页面", "信息")

# 「XX项表」这种后缀整体归一（项目汇报风险项表 → 风险 item）
# 注意别放单独的「项」：会把「项目立项」剥成「项目立」→ project_li
_SUFFIX_ITEM = ("项表", "子表", "明细表", "清单表")

_PUNCT = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]")
_CJK = re.compile(r"[\u4e00-\u9fa5]")


def _pinyin_fallback(text: str) -> str:
    """没有词典命中时，退拼音（保证能出合法且可读的编码）。"""
    try:
        from pypinyin import lazy_pinyin
        parts = lazy_pinyin(text)
        return "".join(parts) or "field"
    except Exception:
        # 没装 pypinyin 时用首字母兜底
        return "".join(ch for ch in text if ch.isalnum()) or "field"


def _pinyin_words(text: str) -> list[str]:
    """中文 → 拼音音节列表，英文/数字片段整段保留。

    「MAH持有人」→ ['MAH', 'chi', 'you', 'ren']
    「汇报PPT」  → ['hui', 'bao', 'PPT']
    """
    try:
        from pypinyin import lazy_pinyin
        raw = lazy_pinyin(text)
    except Exception:
        return [re.sub(r"[^A-Za-z0-9]", "", text)] if text else []

    out: list[str] = []
    for w in raw:
        w = w.strip()
        if not w:
            continue
        # 标点、空白直接丢
        cleaned = re.sub(r"[^A-Za-z0-9\u4e00-\u9fa5]", "", w)
        if not cleaned:
            continue
        out.append(cleaned)
    return out


def to_english(name: str, camel: bool = True) -> str:
    """中文名 → 编码，**一律用拼音**。用于字段编码 / 物理列名。

    业务对象编码不要用这个，用 ``object_code()``：对象编码会派生物理表名，
    系统里是 ``pp_project`` / ``pp_project_create`` 这种英文语义名，
    拼音会得到 ``pp_xiang_mu``，线上一个都找不到。

    为什么字段这一层仍用拼音：同一个概念在不同人手里会写成 projectName /
    xiangMuMingCheng 两种，后期对不上；统一拼音至少规则唯一、可预测、
    不会因为词典缺词而突变。

    「MAH持有人」→ MAHChiYouRen / mah_chi_you_ren
    「项目名称」 → xiangMuMingCheng / xiang_mu_ming_cheng
    camel=True 返回小驼峰（字段编码用），False 返回小写下划线（物理列名用）。
    """
    text = (name or "").strip()
    if not text:
        return ""

    # 纯英文/数字（含 MAH、PPT 这类缩写）原样保留，只按 camel 调整首字母
    if not _CJK.search(text):
        cleaned = re.sub(r"[^A-Za-z0-9]", "", text)
        if not cleaned:
            return ""
        if not camel:
            return cleaned.lower()
        return cleaned[0].lower() + cleaned[1:] if cleaned[0].isupper() else cleaned

    # 括号里多半是补充说明（含税：万元 / 根据计划完成日期…），
    # 带进编码只会让列名又长又碎；主名已经能表达意思。
    text = re.sub(r"[（(][^）)]*[）)]", "", text).strip() or text
    words = _pinyin_words(text)
    words = [w for w in words if w]
    if not words:
        return ""

    if camel:
        head = words[0]
        # 首词若本身是大写缩写（MAH），保留原样；否则小写开头
        head = head if head.isupper() else head.lower()
        tail = [(w if w.isupper() else w[0].upper() + w[1:].lower()) for w in words[1:]]
        out = head + "".join(tail)
    else:
        out = "_".join(w.lower() for w in words)

    out = re.sub(r"[^A-Za-z0-9_]", "", out)
    if out and not out[0].isalpha():
        # 「5G基站」→ f5GJiZhan：数字和紧跟的字母是一体的，前面补 f
        out = "f" + out
    return out


def _snake(word: str) -> str:
    """camelCase / PascalCase → 小写下划线（词典里的值是驼峰形式）。"""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", word).lower()


def _english_words(text: str) -> list[str]:
    """把中文名切成英文词：词典最长匹配，连续没命中的一段整体退拼音。"""
    text = _PUNCT.sub("", text)
    out: list[str] = []
    i, n = 0, len(text)
    top = max(len(w) for w in WORDS)
    while i < n:
        # 英文/数字片段原样保留（MAH、PPT、5G 这类），别被拼音吃掉
        m = re.match(r"[A-Za-z][A-Za-z0-9]*", text[i:])
        if m:
            out.append(m.group(0).lower())
            i += m.end()
            continue
        step = 0
        for length in range(min(top, n - i), 0, -1):
            hit = WORDS.get(text[i:i + length])
            if hit:
                out.append(_snake(hit))
                step = length
                break
        if step:
            i += step
            continue
        # 未命中：一路吃到下一个能命中的词之前，整段转拼音，
        # 避免一个字一个字拆成 xiang/mu/li/xiang 这种碎片
        j = i + 1
        while j < n and not re.match(r"[A-Za-z]", text[j]) and not any(
                text[j:j + ln] in WORDS for ln in range(1, min(top, n - j) + 1)):
            j += 1
        out.append(_pinyin_fallback(text[i:j]))
        i = j
    return out


def object_code(name: str) -> str:
    """业务对象编码：对齐线上真实对象的习惯 —— 英文小写下划线。

    依据（PP6/业务对象/ 六份原生包）：
        项目 → project           表名 pp_project
        项目立项 → project_create 表名 pp_project_create
        需求类型 → requirement_category
        测试用例库大类 → test_case_base_category

    为什么不直接用 ``to_english``
    ----------------------------
    ``to_english`` 是**拼音优先**的，专给字段编码/物理列名用（驼峰、可预测）。
    但对象编码还会派生物理表名 ``pp_<code>``：拼音会得到 ``pp_xiang_mu``，
    系统里一个这样的表都没有，运维看库直接懵。所以对象这一层走英文词典，
    只有实在查不到的词才退拼音。
    """
    text = (name or "").strip()
    if not text:
        return ""
    if not _CJK.search(text):
        cleaned = re.sub(r"[^A-Za-z0-9_]", "", text)
        if not cleaned:
            return ""
        if "_" in cleaned and not cleaned.isupper():
            return cleaned.lower()
        return _snake(cleaned)
    # 括号里多是补充说明，带进编码只会让表名又长又碎
    text = re.sub(r"[（(][^）)]*[）)]", "", text).strip() or text
    # 「项目风险项表 / 合同记录」这类尾巴不参与命名（长后缀优先）
    for suf in sorted(_SUFFIX_ITEM + _NOISE, key=len, reverse=True):
        if text.endswith(suf) and len(text) > len(suf):
            text = text[: -len(suf)]
            break
    words = [w for w in _english_words(text) if w]
    out = re.sub(r"[^a-z0-9_]", "", "_".join(words))
    out = re.sub(r"_+", "_", out).strip("_")
    if out and not out[0].isalpha():
        out = "bo_" + out
    return out[:40].rstrip("_")


def short_code(code: str, limit: int = 32) -> str:
    """编码太长时截短（物理列名有长度上限）。

    只在词边界截，保留可读前缀；截完可能撞名，调用方用 unique() 去重。
    """
    if len(code) <= limit:
        return code
    out = code[:limit]
    # 往回退到最近的大写字母边界（驼峰），避免截出半个词
    for i in range(len(out) - 1, max(0, limit // 2), -1):
        if out[i].isupper():
            return out[:i]
    return out


def unique(base: str, taken: set[str], sep: str = "", limit: int = 0) -> str:
    """重名时加数字后缀：name → name2 → name3。

    limit>0 时先截短（拼音容易超长，物理列名有长度上限），
    截短后再去重——否则截完的两个长名会撞在一起。
    """
    if limit:
        base = short_code(base, limit)
    if base not in taken:
        return base
    n = 2
    while f"{base}{sep}{n}" in taken:
        n += 1
    return f"{base}{sep}{n}"


# 数据库保留字（MySQL 8.0）。
# 系统「添加字段」的提示里明确写：不允许使用数据库的关键字来命名字段。
# 注意：列名统一带尾下划线（order_），所以 order 这种编码本身没问题；
# 真正要拦的是「物理列名正好等于关键字」（少了尾下划线时才会发生）。
RESERVED: frozenset[str] = frozenset({
    "add", "all", "alter", "analyze", "and", "as", "asc", "before", "between",
    "bigint", "binary", "blob", "both", "by", "call", "cascade", "case", "change",
    "char", "character", "check", "collate", "column", "condition", "constraint",
    "continue", "convert", "create", "cross", "current_date", "current_time",
    "current_timestamp", "current_user", "cursor", "database", "databases",
    "day_hour", "day_microsecond", "day_minute", "day_second", "dec", "decimal",
    "declare", "default", "delayed", "delete", "desc", "describe", "deterministic",
    "distinct", "distinctrow", "div", "double", "drop", "dual", "each", "else",
    "elseif", "enclosed", "escaped", "exists", "exit", "explain", "false", "fetch",
    "float", "for", "force", "foreign", "from", "fulltext", "generated", "get",
    "grant", "group", "having", "high_priority", "hour_microsecond", "hour_minute",
    "hour_second", "if", "ignore", "in", "index", "infile", "inner", "inout",
    "insert", "int", "integer", "interval", "into", "is", "iterate", "join", "key",
    "keys", "kill", "leading", "leave", "left", "like", "limit", "linear", "lines",
    "load", "localtime", "localtimestamp", "lock", "long", "longblob", "longtext",
    "loop", "low_priority", "match", "maxvalue", "mediumblob", "mediumint",
    "mediumtext", "middleint", "minute_microsecond", "minute_second", "mod", "modifies",
    "natural", "not", "no_write_to_binlog", "null", "numeric", "on", "optimize",
    "option", "optionally", "or", "order", "out", "outer", "outfile", "partition",
    "precision", "primary", "procedure", "purge", "range", "read", "reads", "real",
    "references", "regexp", "release", "rename", "repeat", "replace", "require",
    "restrict", "return", "revoke", "right", "rlike", "schema", "schemas",
    "second_microsecond", "select", "sensitive", "separator", "set", "show",
    "smallint", "spatial", "specific", "sql", "sqlexception", "sqlstate",
    "sqlwarning", "sql_big_result", "sql_calc_found_rows", "sql_small_result",
    "ssl", "starting", "straight_join", "table", "terminated", "then", "tinyblob",
    "tinyint", "tinytext", "to", "trailing", "trigger", "true", "undo", "union",
    "unique", "unlock", "unsigned", "update", "usage", "use", "using", "utc_date",
    "utc_time", "utc_timestamp", "values", "varbinary", "varchar", "varcharacter",
    "varying", "when", "where", "while", "with", "write", "xor", "year_month",
    "zerofill",
    # 非保留字但工程上容易踩坑的常用词（建表时 Oralce/PG 也当关键字）
    "level", "type", "status", "value", "user", "system", "size", "rank", "row",
})


def is_reserved(name: str) -> bool:
    """这个名字是不是数据库关键字。"""
    return (name or "").strip().lower() in RESERVED


def semantic_code(name: str, limit: int = 24) -> str:
    """中文名 → 语义化英文小驼峰；词典优先，查不到才退拼音。

    和 ``to_english`` 的区别：后者**一律拼音**，适合保证列名可预测；
    这里用于 AI 字段编码的兜底，目标是「一眼能看懂」，
    所以先走 WORDS 词典（项目→project、里程碑→milestone），
    只有词典没覆盖的片段才退拼音。
    """
    text = (name or "").strip()
    if not text:
        return ""
    if not _CJK.search(text):
        cleaned = re.sub(r"[^A-Za-z0-9]", "", text)
        return cleaned[0].lower() + cleaned[1:] if cleaned else ""
    text = re.sub(r"[（(][^）)]*[）)]", "", text).strip() or text
    words = [w for w in _english_words(text) if w]
    if not words:
        return ""
    head, tail = words[0], words[1:]
    code = head + "".join(w[:1].upper() + w[1:] for w in tail)
    code = re.sub(r"[^A-Za-z0-9]", "", code)
    if not code or not code[0].isalpha():
        code = "f" + code
    return code[:limit]
