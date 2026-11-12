"""中间模型：Excel 的一行 -> 一个对象；生成器只依赖这些模型。"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 工具函数


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(text or "").strip().lower())


def camel_to_snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def truthy(value: Any) -> bool:
    return str(value or "").strip().upper() in ("1", "Y", "YES", "TRUE", "是", "√", "T")


def parse_json(value: Any, where: str, errors: list[str]) -> dict:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        got = json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(f"{where}: 扩展参数不是合法 JSON（{exc.msg}）")
        return {}
    if not isinstance(got, dict):
        errors.append(f"{where}: 扩展参数必须是 JSON 对象")
        return {}
    return got


def split_list(value: Any) -> list[str]:
    text = str(value or "").replace("，", ",").replace("、", ",")
    return [x.strip() for x in text.split(",") if x.strip()]


# ---------------------------------------------------------------- 业务对象

# 类型参数来自 assets/fieldtypes.json（见 pp6kit/fieldtypes.py）
from . import fieldtypes as _ft  # noqa: E402

# 需要 objectType 的类型
NEED_OBJECT_TYPE = {"object", "dic"}

# 搭建规范：流程驱动的业务对象必须冗余这四个字段，流程流转时反写
PROCESS_FIELDS = [
    ("processNode", "流程节点", 1000),
    ("processHandler", "流程处理人", 200),
    ("processDefId", "流程定义", 200),
    ("processInstanceId", "流程实例", 200),
]

# 表单类型（顺序即标准搭建顺序）
FORM_CREATE = "create"
FORM_DETAIL = "detail"
FORM_CONDITION = "condition"
FORM_IMPORT = "import"
FORM_BUNDLE = "bundle"

FORM_KIND_LABEL = {
    "新建表单": FORM_CREATE,
    "详情表单": FORM_DETAIL,
    "条件专用表单": FORM_CONDITION,
    "导入专用表单": FORM_IMPORT,
    "标准全套": FORM_BUNDLE,
}

# 各类型的标签宽度（手册：新建 106，条件专用 160）
FORM_LABEL_WIDTH = {FORM_CREATE: 106, FORM_DETAIL: 106, FORM_CONDITION: 160, FORM_IMPORT: 106}


# 各类型表单的「样式套件」——来自 PP6/搭建规范手册.pdf「表单属性的配置」。
# 手册对此是硬性规定，每类表单的 customClass / 标签宽度 / 打开方式 / 尺寸 /
# 按钮位置都不一样。之前我们四类共用一套 extendAttr，这正是生成出来的表单
# 「不像系统里那些」的根因。
#
#   class                : 表单属性 > 自定义 Class
#   labelWidth           : 标签宽度(px)
#   openType             : 弹窗方式（drawer=抽屉 / fullscreen=全屏）
#   layoutMode           : 布局（紧凑 / 展开）—— 写进 extendAttr 的 compact
#   width                : 弹窗尺寸百分比
#   buttonPosition       : 按钮位置（bottomRight 右下角 / topRight 右上角）
#   singleLineEdit       : 是否开启「单行编辑模式」（详情表单要开）
#   withStandardButtons  : 是否输出「取消/保存」这类标准按钮
#
# 出处逐条对应手册 + **库里真实新建表单**（2026-10-29 查
# create_development_project / create_customer / create_business_opportunities
# 三个表单的 extend_attr_ 完全一致，这就是权威基线）：
#   新建表单       pp-drawer-add        106px  抽屉 90%   按钮右下 lowerRight
#   详情(紧凑)     pp-details-sideslip  106px  抽屉 90%   按钮右上 topRight + 单行编辑
#   详情(展开)     pp-drawer-details    120px  抽屉展开 90%   按钮右上 topRight + 单行编辑
#   条件专用       无独立 class，沿用新建样式（手册只规定命名与控件类型）
#   导入专用       同上
#
# 2026-11-10：面板宽度统一从 45% 改成 90%（用户要求）。
# 手册给的 45% 是抽屉默认值，实际填表时两列太挤，90% 才铺得开。
#
# 注意 buttonPosition 的取值是 **lowerRight / topRight**（小写 l），
# 不是 bottomRight —— 之前照官方导出包猜的 bottomRight 是错的，
# 库里真实数据里根本没有这个值。
#
# 2026-11-10：右下角那组浮动按钮（取消 / 预览数据 / 保存并继续 / 保存）是
# V6 新版本自带的功能，用户已确认与表单配置无关，因此**不做任何规避**，
# 保持官方 lowerRight 基线。库里 382 张表单的取值分布：不带该键 227 /
# lowerRight 83 / topRight 71 / center 1。
FORM_STYLE = {
    FORM_CREATE: {
        "customClass": "pp-drawer-add",
        "labelWidth": 106,
        "openType": "drawer",
        "layoutMode": "compact",
        "width": 90,
        "buttonPosition": "lowerRight",
        "singleLineEdit": False,
        "withStandardButtons": True,
    },
    FORM_DETAIL: {
        "customClass": "pp-details-sideslip",
        "labelWidth": 106,
        "openType": "drawer",
        "layoutMode": "compact",
        "width": 90,
        "buttonPosition": "topRight",
        "singleLineEdit": True,
        "withStandardButtons": True,
    },
    # 展开详情 / 流程详情：手册 pp-drawer-details + 120px + 90%，布局「展开」
    "detail_expand": {
        "customClass": "pp-drawer-details",
        "labelWidth": 120,
        "openType": "drawer",
        "layoutMode": "expand",
        "width": 90,
        "buttonPosition": "topRight",
        "singleLineEdit": True,
        "withStandardButtons": True,
    },
    FORM_CONDITION: {
        "customClass": "",
        "labelWidth": 160,
        "openType": "drawer",
        "layoutMode": "compact",
        "width": 90,
        "buttonPosition": "lowerRight",
        "singleLineEdit": False,
        "withStandardButtons": True,
    },
    FORM_IMPORT: {
        "customClass": "",
        "labelWidth": 106,
        "openType": "drawer",
        "layoutMode": "compact",
        "width": 90,
        "buttonPosition": "lowerRight",
        "singleLineEdit": False,
        "withStandardButtons": True,
    },
}


def form_style(kind: str) -> dict:
    """取该类型表单的样式套件，未知类型退回新建表单那套。"""
    return FORM_STYLE.get(kind) or FORM_STYLE[FORM_CREATE]


def table_name_for(code: str) -> str:
    """手册：物理表名 = pp_ 前缀 + 对象编码。

    编码本身已经带 pp_ 的原生对象，直接沿用：实测「设备」这个对象
    code = tblName = ``pp_equipment``，再拼一次就成了 ``pp_pp_equipment``，
    系统里根本没有这种表名。
    """
    if not str(code or "").strip():
        return ""
    base = re.sub(r"_+", "_", camel_to_snake(code)).strip("_")
    return base if base.lower().startswith("pp_") else "pp_" + base


@dataclass
class BoField:
    bo_code: str
    code: str
    name: str
    type_new: str = "varchar"
    datatype: str = ""
    object_type: str = ""
    length: int | None = None
    precision: int | None = None
    is_index: bool = False
    is_null: bool = True
    default_value: str = ""
    options: dict = field(default_factory=dict)
    desc: str = ""
    field_name: str = ""
    sn: int = 0
    is_system: bool = False
    readonly: bool = False          # 表单上只读展示（文档写「自动带出/只读文本」）
    # 选项值（dic 类型专用）：Excel 里逗号分隔填，生成时内嵌进 bo XML 的
    # dictionaryTypeList / dictionaryList，导入后系统里就有这些候选值
    choices: list[str] = field(default_factory=list)
    # 级联父字段（字段编码）。写在 attribute 的 options 里：
    #   cascadeEnabled=true / cascadeParentCode=<父字段 code>
    # 父级可以是 dic，也可以是关联对象 object（真实样例里「品种类别」的父级
    # 就是 object 类型的「隶属的项目类型 classificationId」）。
    # 下拉只是「听谁的」，具体哪些子项属于哪个父值仍存在系统侧字典里。
    cascade_parent: str = ""

    def resolved_field_name(self) -> str:
        if self.field_name:
            return self.field_name
        return camel_to_snake(self.code) + "_"

    def dict_key(self) -> str:
        """dic 字段的自定义字典 key：``对象编码_字段编码``（不带尾下划线）。

        依据用户导出的 PP6/业务对象/选项.zip（手动点"自定义"后导出）：
            对象 code = csxxz，字段 code = zheShiYiGeXuanXiang
            dictionaryType typeKey  = "csxxz_zheShiYiGeXuanXiang"
            attribute      objectType = "csxxz_zheShiYiGeXuanXiang"
        两处同一个值，靠它关联；**都不带尾下划线**。

        为什么**不看 object_type**：AI 生成时会把 objectType 填成字段编码本身
        （xiangMuLaiYuan），如果照抄就没有对象前缀，多个对象出现同名字段时
        字典会互相撞车，这正是线上一直出问题的原因。自定义字典的 key
        必须由对象编码参与，所以这里一律重新生成。
        """
        return f"{self.bo_code}_{self.code}" if self.bo_code else self.code

    def resolved(self) -> dict[str, Any]:
        """补齐缺省值，返回生成用的扁平参数。"""
        cfg = _ft.specs().get(self.type_new) or _ft.specs().get("varchar") or {
            "datatype": "varchar", "length": 200, "format": None,
        }
        datatype = self.datatype or cfg["datatype"]
        length = self.length if self.length is not None else cfg["length"]
        fmt = cfg["format"]
        if self.type_new == "clob":
            length, fmt = None, None
        if self.type_new == "datetime":
            length = None
        if self.is_system and self.type_new != "datetime":
            # 平台自建的系统字段（文本类）不带 format
            fmt = None
        opts = dict(self.options)
        if self.type_new == "datetime" and not self.is_system:
            # 线上实测（2026-11-12，逐个变量导入对照，13 行 / 14 行）：
            # 自定义 datetime 字段在「对象属性」页会**整条消失**，而
            # bo/def/manage 接口仍如实返回它，很难发现。触发条件是
            # options.isDefault = true —— 平台把它当成"系统自动填充的时间戳"
            # （createTime / updateTime 就是 isDefault=true 的 datetime，
            #  前端一律隐藏），自定义的日期字段就被连坐隐藏了。
            #   isDefault=false → 显示；=true → 消失，其余变量全部排除过。
            # 所以日期字段默认**不**自动填值；要自动填请显式给默认值。
            # options 同时按原生样子补全（原生业务字段都是这一套键）。
            for k, v in (
                ("isMultiple", False), ("isDefault", bool(self.default_value)),
                ("uniqueCheck", False), ("optionSource", "BO_ATTR_DIC_TYPE"),
                ("columns", []), ("identities", []), ("formula", None),
            ):
                opts.setdefault(k, v)
            opts["isDefault"] = bool(self.default_value)
            opts["format"] = "datetime-second" if (fmt or "").endswith(":ss") else "datetime-minute"
        if self.type_new in ("user", "department") and "isMultiple" in opts:
            if truthy(opts.get("isMultiple")) and not self.length:
                length = 1000

        # 选项字段的「单选 / 多选」（搭建视频里叫「选项类型」，界面是必选项）。
        #
        # 库里实测（iform_bo_attr，2026-10-29）：
        #   80 个 dic 字段的 options_ 全都带 isMultiple，其中 32 个是 true
        #   多选的 attr_length_ 一律是 1000（要存逗号分隔的多个 key）
        #   单选的通常是 32 / 50 / 64 / 255
        # 所以我们没显式给的时候默认单选（false），只有明确声明多选才放宽长度。
        if self.type_new == "dic":
            two = opts.get("isMultiple")
            multi = truthy(two) if two is not None else False
            opts["isMultiple"] = multi
            if multi and not self.length:
                length = 1000

        # 自动编号必须有编号规则，否则导入后这个字段**不会自动生成任何值**。
        # 库里真实 autoNumber 字段的 options 长这样（2026-10-29 实测）：
        #   {"isMultiple":false,"isDefault":false,"uniqueCheck":false,
        #    "optionSource":"BO_ATTR_DIC_TYPE","columns":[],
        #    "identities":[{"name":"自动编号","alias":"6881e46d",
        #                   "regulation":"{yyyy}{MM}{dd}{NO}","noLength":5,
        #                   "initValue":1,"step":1,
        #                   "duplicateType":"allow","duplicateScope":"",
        #                   "queryCondition":{"condition":"AND","rules":[]}}]}
        # regulation 占位符：{yyyy} 年 / {MM} 月 / {dd} 日 / {NO} 流水号
        if self.type_new == "autoNumber":
            opts.setdefault("isMultiple", False)
            opts.setdefault("isDefault", False)
            opts.setdefault("uniqueCheck", False)
            opts.setdefault("optionSource", "BO_ATTR_DIC_TYPE")
            opts.setdefault("columns", [])
            if not opts.get("identities"):
                opts["identities"] = [{
                    "name": self.name or "自动编号",
                    "alias": uuid.uuid4().hex[:8],
                    "regulation": "{yyyy}{MM}{dd}{NO}",
                    "noLength": 5,
                    "initValue": 1,
                    "step": 1,
                    "duplicateType": "allow",
                    "duplicateScope": "",
                    "queryCondition": {"condition": "AND", "rules": []},
                }]

        # dic 字段必须显式声明取数来源，否则界面上选项列表是空的。
        #
        # 两份真实样例定死了这个值（2026-10-29）：
        #   选项.zip（自定义，字段级字典）：
        #       optionSource="BO_ATTR_DIC_TYPE"  typeScope=BO_ATTR struType=0
        #       → 界面显示「选项来源=自定义」
        #   需求.zip（引用已有共享字典）：
        #       optionSource="DIC_TYPE"          typeScope=GLOBAL  struType=1
        #       → 界面显示「选项来源=全局选项」
        # 我们的字段都是从文档新建的独立字典，所以用 BO_ATTR_DIC_TYPE。
        object_type = self.object_type or None
        if self.type_new == "dic":
            # 自定义字典的 objectType 就是 dict_key（对象编码_字段编码），
            # 和 dictionaryType.typeKey 保持完全一致，不加下划线
            object_type = self.dict_key() or None
            opts.setdefault("optionSource", "BO_ATTR_DIC_TYPE")
        elif self.type_new == "object":
            opts.setdefault("optionSource", "BO_ATTR_DIC_TYPE")
            # 多选关联对象要存逗号分隔的多个 id，64 装不下。
            # 库里真实数据 project_completion.phase（多选关联「项目阶段」）
            # 的 attr_length_ 就是 1000，单选一律 64。用户显式给了长度就不动。
            if truthy(opts.get("isMultiple")) and not self.length:
                length = 1000

        # 级联：出处 PP6/业务对象/项目立项业务对象.zip（2026-10-31），
        # 5 个 attribute 的 options 多出这两个键，其余 dic 字段没有：
        #   productCategory        cascadeEnabled=true   parent=classificationId
        #   productClassification  cascadeEnabled=true   parent=productCategory
        #   strategyCategory       cascadeEnabled=false  parent=productCategory
        #   projectRegistrationCategory / projectCategoryCode 同理
        # 注意第三行：只留 parentCode、enabled 为 false 是合法状态（记着但不生效）。
        # 我们这边约定「填了父级就启用」，要关掉就把这一格清空。
        if self.cascade_parent:
            opts["cascadeEnabled"] = True
            opts["cascadeParentCode"] = self.cascade_parent

        # isDefault：库里 76% 的字段带这个开关（datetime 90% 是 true = 新建记录时
        # 自动填当前时间；user/number 也以 true 为主）。2026-11-11 结构 diff 发现
        # 生成的 XML 整个缺这个键——导入后新建记录时字段不带默认值，和真实系统
        # 行为不一致。Excel「默认值」填了值 → true；datetime 默认 true；
        # 其余保守给 false（不臆造 number=0 之类的语义）。
        opts.setdefault(
            "isDefault",
            bool(self.default_value) or self.type_new == "datetime")

        return {
            "code": self.code,
            "fieldName": self.resolved_field_name(),
            "name": self.name or self.code,
            "desc": self.desc or self.name or self.code,
            "type": "base",
            "datatype": datatype,
            "dataTypeNew": self.type_new,
            "objectType": object_type,
            "options": opts or None,
            "isnull": "Y" if self.is_null else "N",
            "isindex": "Y" if self.is_index else "N",
            "sn": self.sn,
            "defaultvalue": self.default_value or None,
            "format": fmt,
            "length": length,
            "precision": self.precision,
        }


@dataclass
class Bo:
    code: str
    name: str
    tbl_name: str
    bo_type: str = "object"
    module_id: str = ""
    type_id: str = ""
    pk: str = "id"
    desc: str = ""
    with_log: bool = True
    flow_driven: bool = False
    parent_obj: str = ""     # 父对象编码（Java 搭建说明：流程对象要挂到主对象下）
    parent_field: str = ""   # 指向父对象的关联字段编码
    rel_type: str = "PARENT"  # 关系类型：PARENT（父子）/ PARALLEL（平行关联）
    options: dict = field(default_factory=dict)
    fields: list[BoField] = field(default_factory=list)
    # 目标库里已存在同名对象时，出包必须写回它原来的 id，
    # 否则导入会「新增」一份而不是「更新」——判断依据就是 iform_bo_def.id
    exist_id: str = ""

    def field_by_code(self, code: str) -> BoField | None:
        for f in self.fields:
            if f.code == code:
                return f
        return None


# ---------------------------------------------------------------- 表单


@dataclass
class FormField:
    form_key: str
    name: str
    label: str
    field_type: str
    value_source: str = ""
    # 级联下拉的父级控件编码。系统里靠 fieldOptions 的 cascadeEnabled +
    # cascadeParentCode 两个键实现，父级选什么，本级只列它下面的选项。
    # 只对「选项来自字典（层级字典）」的控件有效。
    cascade_parent: str = ""
    placeholder: str = "请输入"
    required: bool = False
    readonly: bool = False
    full_row: bool = False
    newline: bool = False
    options: dict = field(default_factory=dict)
    desc: str = ""
    # 分区（表单布局 V1.0 的卡片/分组框标题）。同名的控件归到同一个容器，
    # 留空则整张表单只出一个容器（标题「基本信息」）。
    group: str = ""


# 条件隐藏：系统里叫「表单规则（formRules）」，存在 formDef 的 extendAttr 里。
# 一条规则 = 一个条件 + 一个动作（action，目前只支持 hidden）+ 一个目标控件。
# 线上真实表单（PP6/表单/项目表单包含级联关系…zip）里观察到的算子只有这四个，
# 其余算子（包含/大于…）没在样本里出现过，先不放出来，免得生成的规则系统不认。
HIDE_OPERATORS = ("equal", "not_equal", "is_empty", "is_not_empty")

OP_BY_LABEL = {
    "等于": "equal",
    "不等于": "not_equal",
    "为空": "is_empty",
    "不为空": "is_not_empty",
}
LABEL_BY_OP = {v: k for k, v in OP_BY_LABEL.items()}

# 不需要填值的算子
EMPTY_OPERATORS = {"is_empty", "is_not_empty"}


@dataclass
class HideRule:
    """条件隐藏规则：source 满足 operator/value 时，隐藏 target 控件。"""

    form_key: str
    target: str          # 被隐藏的控件编码
    source: str          # 条件依据的控件编码
    operator: str = "equal"
    value: str = ""
    value_label: str = ""    # 条件值的中文说明，只用于填写时核对，不参与逻辑
    clear_on_hide: bool = True   # 隐藏时是否清空已填内容（系统里是 clearOnHide）

    def condition_text(self) -> str:
        """给人看的条件描述，例如「战略类别 不等于 新品」。"""
        if self.operator in EMPTY_OPERATORS:
            return f"{self.source} {LABEL_BY_OP.get(self.operator, self.operator)}"
        val = self.value_label or self.value
        return f"{self.source} {LABEL_BY_OP.get(self.operator, self.operator)} {val}"


@dataclass
class Form:
    key: str
    name: str
    bo_code: str
    form_type: str = "form"
    layout: int = 2          # 1=一行一列, 2=一行两列
    type_id: str = ""
    status: str = "deploy"
    kind: str = FORM_CREATE
    # 弹窗宽度。**样式以 model.form_style(kind) 为准**——四类表单统一 90%。
    # 这里只在用户显式指定时作为逃生口，
    # 所以默认 None，不再默认 45 去盖掉样式表。
    width: int | None = None
    # 用户是否显式指定了宽度（Excel 里填了「弹窗宽度」列才为真）
    width_explicit: bool = False
    label_width: int = 0
    auto_hidden: bool = True
    fields: list[FormField] = field(default_factory=list)
    # 条件隐藏规则。同一控件可以有多条（系统里是并列的独立规则，
    # 满足任一即隐藏，等价于 OR），所以这里是列表而不是挂在字段上。
    hide_rules: list[HideRule] = field(default_factory=list)
    exist_key: str = ""    # 库里已有同 key 的表单模板时复用，不新建

    def resolved_label_width(self) -> int:
        return self.label_width or FORM_LABEL_WIDTH.get(self.kind, 106)

    def resolved_width(self) -> int:
        """面板宽度：用户显式填了就用，否则按类型样式（现在四类统一 90）。"""
        return self.width if self.width_explicit and self.width else form_style(self.kind)["width"]


# ---------------------------------------------------------------- 列表

@dataclass
class ListDef:
    key: str
    name: str
    bo_code: str
    views: list[str] = field(default_factory=lambda: ["grid"])
    columns: list[str] = field(default_factory=list)
    sort_field: str = ""
    sort_dir: str = "desc"
    tree: bool = False
    parent_field: str = ""
    title_field: str = ""
    board_group: str = ""      # 看板分列字段（与 parent_field 无关，别混用）
    gantt_start: str = ""
    gantt_end: str = ""
    gantt_progress: str = ""
    exist_key: str = ""    # 库里已有同 key 的列表模板时复用，不新建
    # 列表挂在 iform_cat_type 的哪个分类节点下（category_key_='DATA_TEMPLATE_TYPE'）。
    # 留空则导入后归到「未分类」，不指向错误模块；要精确指定就填该节点的 id。
    type_id: str = ""


# ---------------------------------------------------------------- 数据集

@dataclass
class DataSet:
    key: str
    name: str
    from_table: str
    ds_type: str = "valueSource"   # valueSource=值来源, default=列表数据集
    unique_field: str = "id_"
    id_field: str = "id_"
    name_field: str = "name_"


@dataclass
class Project:
    tenant_id: str = "-999"
    file_prefix: str = "001"
    module_id: str = ""
    type_id: str = ""
    bos: list[Bo] = field(default_factory=list)
    forms: list[Form] = field(default_factory=list)
    lists: list[ListDef] = field(default_factory=list)
    datasets: list[DataSet] = field(default_factory=list)
    form_scripts: dict[str, str] = field(default_factory=dict)  # 表单key -> JForm 脚本
    out_dir: str = "out"
    # 读取时自动补齐的明细（编码按名称生成、关联对象对到真实编码），供界面提示用
    auto_notes: list[str] = field(default_factory=list)

    def bo(self, code: str) -> Bo | None:
        for b in self.bos:
            if b.code == code:
                return b
        return None
