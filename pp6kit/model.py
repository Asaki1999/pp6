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


# 表格字段（dataTypeNew=table）的子列。
#
# 真实结构出处（2026-11-15 核对两份生产包）：
#   PP6/业务对象/项目立项业务对象.zip        planExcel 里程碑计划表 / budgetExcel 预算编辑表格
#   PP6/流程/config-sync-20261113150255.zip  planExcel / budgetExcel / riskExcel 主要风险控制
# 结论：表格字段本身是 ``datatype="clob" dataTypeNew="table"``，子列不在物理表里，
# 而是塞进 attribute 的 options.columns 数组，一行一个 JSON：
#   {id, name, nameEn, inputType, required, [precision | multiple, optionSource,
#    objectType, cascadeEnabled, cascadeParentCode | align, widthPercent]}
# 表单侧对应的控件是 fieldType="tableField"，它自己也带一份 columns（见 gen_form）。
#
# id 用 uuid5 按「对象编码/字段编码/列名/序号」算：同一个 Excel 反复出包得到同一批
# id，导入时才是「更新这几列」；换随机 id 会让同一列在系统里被当成新列，旧数据丢。
_TABLE_COL_NS = uuid.UUID("6f1c7e2a-5b93-4d1e-9c3a-8f2d4b6a1e07")

# 子列能用的类型（对应系统「表格」字段编辑列时的下拉）。
# 只放真实样本里出现过的 + 字段层同一套同义类型：
#   varchar 字符串 / number 数字 / date 日期 / datetime 日期时间 /
#   clob 大文本 / dic 选项 / user 用户 / department 部门
TABLE_COLUMN_TYPES = ("varchar", "number", "date", "datetime", "clob", "dic",
                      "user", "department")


@dataclass
class TableColumn:
    """表格字段里的一列。"""

    name: str
    input_type: str = "varchar"
    choices: list[str] = field(default_factory=list)   # dic 列的候选值（中文名）
    dict_ref: str = ""                # 引用全局字典时填 typeKey（DIC_TYPE 分支）
    width_percent: float | None = None
    align: str = ""                   # left / center / right，留空不写
    required: bool = False
    precision: int | None = None      # number 列的小数位
    multiple: bool = False            # dic / user / department 是否多选
    name_en: str = ""                 # 英文名，样本里都是空串
    exist_id: str = ""                # 反解已有包时原样保留列 id
    sn: int = 0                       # 列顺序（Excel 里的「序号」/ 反解按原序）

    def col_id(self, scope: str, index: int) -> str:
        if self.exist_id:
            return self.exist_id
        return str(uuid.uuid5(_TABLE_COL_NS, f"{scope}/{index}/{self.name}"))

    def option(self, scope: str, index: int) -> dict:
        """落成 options.columns 里的一项。

        键序照生产包：基础四键 → required → 各类型专属键。系统按 inputType 取用，
        多写的键会被忽略，所以 dic 的四个键只在 dic 列出现，不铺给别的类型。
        """
        typ = self.input_type if self.input_type in TABLE_COLUMN_TYPES else "varchar"
        out: dict[str, Any] = {
            "id": self.col_id(scope, index),
            "name": self.name,
            "nameEn": self.name_en or "",
            "inputType": typ,
            "required": bool(self.required),
        }
        if typ == "number":
            out["precision"] = self.precision if self.precision is not None else 0
        if typ in ("dic", "user", "department"):
            out["multiple"] = bool(self.multiple)
        if typ == "dic":
            if self.dict_ref:
                # 引用线上已有共享字典（对应 riskExcel 的「风险等级」：
                # optionSource=DIC_TYPE + objectType=risk_rating）
                out["optionSource"] = "DIC_TYPE"
                out["objectType"] = self.dict_ref
            else:
                out["optionSource"] = "BO_ATTR_DIC_TYPE"
                out["objectType"] = None
                if self.choices:
                    # 行内候选值：value/label/text 都用中文，不依赖字典表
                    # （真实表单里「是否里程碑」就是这么写的）
                    out["selectOptions"] = [
                        {"value": c, "label": c, "text": c, "disabled": 0,
                         "dependValues": [], "data": {}}
                        for c in self.choices
                    ]
            out["cascadeEnabled"] = False
            out["cascadeParentCode"] = ""
        if self.align:
            out["align"] = self.align
        if self.width_percent is not None:
            # 整数写整数：真实包里是 10 而不是 10.0，导出/导入往返才不会假差异
            w = float(self.width_percent)
            out["widthPercent"] = int(w) if w == int(w) else w
        return out


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
    # 选项分组（仅级联字段用）：{父选项名: [子选项名, ...]}。
    #
    # 为什么必须有这一层：系统级联是「父值 → 过滤子字典」。而《项目基本信息
    # 表单-尹姣定.xlsx》里品种分类的候选值是**分组**给出的：
    #     中药：OTC、RX、双跨、其他
    #     化药：OTC、RX、双跨、其他
    # 同一个 OTC 在多个父级下各出现一次。如果按扁平去重成 6 项，分组关系就
    # 丢了，导入后父级选「中药」子级仍全量显示——用户看到的就是「级联没用」。
    # 解析时保留这份映射，出包时据此给子字典项挂依赖。
    choice_groups: dict[str, list[str]] = field(default_factory=dict)
    # 全局字典引用（P1-3 + 项目对齐 R4）。空 = 自定义字典（BO_ATTR），走 dict_key()。
    #
    # 为什么不再复用 object_type 判断：resolved() 对 dic **强制**把 object_type
    # 改写成 dict_key()，反解/对齐带进来的全局 typeKey 会被悄悄打回
    # 「对象编码_字段编码」，界面上就是一个不存在的自定义字典（空下拉）。
    # 所以必须有独立的四个字段承载它，且 options 原样照抄项目侧，不合成 optionSource。
    dict_ref: str = ""                   # dictionaryType.typeKey（如 "report_type"）
    dict_name: str = ""                  # dictionaryType.name（"项目汇报类型"）
    dict_stru: str = ""                  # dictionaryType.struType（GLOBAL 实测 "1"）
    dict_items: list[dict] = field(default_factory=list)
    #   [{"key":"MonthlyReportModules","name":"月报","sn":146}, ...]
    #   ↑ 候选值 key 是**固定业务码**，不可按 md5 重算，只能原样照抄
    # 表格字段（type_new=table）的子列定义。来源：Excel「2.1_表格列」，
    # 或反解已有包的 options.columns，或「扩展参数」里手写的 columns。
    table_columns: list[TableColumn] = field(default_factory=list)

    def sub_choices(self) -> list[str]:
        """去重后的全部子选项（分组映射为空时退回 choices）。"""
        if not self.choice_groups:
            return list(self.choices)
        out: list[str] = []
        for items in self.choice_groups.values():
            for it in items:
                if it not in out:
                    out.append(it)
        return out

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

    def value_source(self) -> str:
        """表单控件/详情表单取值来源的**唯一真相**（替代三处 f.dict_key() 复制）。

        以前 blueprint.fields_from_bo / builder 详情表单 / patch._project_with_fields
        各自写了一遍「dic → dict_key()」，一旦出现全局字典（dict_ref 非空）就会
        指向一个不存在的自定义 key，界面上是空下拉。收敛到这里，只改一处。

            dic    → dict_ref or dict_key()
            object → object_type
            其余   → ""
        """
        if self.type_new == "dic":
            return self.dict_ref or self.dict_key()
        if self.type_new == "object":
            return self.object_type or ""
        return ""

    def excel_options(self) -> dict:
        """写回 Excel「扩展参数」列用：options + 四个控制键。

        控制键只在模型/Excel 之间往返，``resolved()`` 写出 XML 前会剥掉，
        所以 XML 零污染（架构 D5）。不新开一列「字典类型」的原因：
        S_FIELD 加列会牵动 sheet_data 位序与用户旧模板，风险大于收益。
        """
        opts = dict(self.options)
        if self.dict_ref:
            opts["dict_ref"] = self.dict_ref
            opts["dict_name"] = self.dict_name
            opts["dict_stru"] = self.dict_stru
            opts["dict_items"] = [dict(i) for i in self.dict_items]
        return opts

    def resolved(self) -> dict[str, Any]:
        """补齐缺省值，返回生成用的扁平参数。"""
        # reader 已把「日期时间（datetime）」「选项」这类写法归一过，
        # 但手写系统里不存在的叫法（「日期」）会原样留下来。
        # XML 的 dataTypeNew 必须是系统认得的类型：认不出就按字符串落库，
        # 且与 datatype 保持一致——否则会写出 dataTypeNew="日期" 这种脏类型，
        # 导入后字段在界面上既不是日期也改不了。
        type_new = self.type_new if _ft.is_known(self.type_new) else "varchar"
        cfg = _ft.specs().get(type_new) or _ft.specs().get("varchar") or {
            "datatype": "varchar", "length": 200, "format": None,
        }
        datatype = self.datatype or cfg["datatype"]
        length = self.length if self.length is not None else cfg["length"]
        fmt = cfg["format"]
        if type_new in ("clob", "table"):
            # 表格字段和「大文本」一样按 clob 落库（子列只存在于 options.columns，
            # 不建物理列），所以长度/格式一律不写，否则导入端会当成 varchar 建表。
            length, fmt = None, None
        if type_new == "datetime":
            length = None
        if self.is_system and type_new != "datetime":
            # 平台自建的系统字段（文本类）不带 format
            fmt = None
        opts = dict(self.options)
        # ① 先剥控制键（架构 D5）：dict_ref/dict_name/dict_stru/dict_items 只活在
        # 模型与 Excel 的「扩展参数」列里，绝不进 XML。它们不在 gen_bo.ATTR_ORDER
        # 里，剥掉后产物与线上原生包逐键一致。
        ctrl = {k: opts.pop(k, None)
                for k in ("dict_ref", "dict_name", "dict_stru", "dict_items")}
        if ctrl["dict_ref"] and not self.dict_ref:
            self.dict_ref = str(ctrl["dict_ref"])
            self.dict_name = str(ctrl["dict_name"] or "")
            self.dict_stru = str(ctrl["dict_stru"] or "")
            items = ctrl["dict_items"]
            self.dict_items = list(items) if isinstance(items, list) else []
        if type_new == "datetime" and not self.is_system:
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
        if type_new in ("user", "department") and "isMultiple" in opts:
            if truthy(opts.get("isMultiple")) and not self.length:
                length = 1000

        # 选项字段的「单选 / 多选」（搭建视频里叫「选项类型」，界面是必选项）。
        #
        # 库里实测（iform_bo_attr，2026-10-29）：
        #   80 个 dic 字段的 options_ 全都带 isMultiple，其中 32 个是 true
        #   多选的 attr_length_ 一律是 1000（要存逗号分隔的多个 key）
        #   单选的通常是 32 / 50 / 64 / 255
        # 所以我们没显式给的时候默认单选（false），只有明确声明多选才放宽长度。
        if type_new == "dic":
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
        if type_new == "autoNumber":
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

        # 表格字段：子列写在 options.columns 里，其余六个键照真实包铺满。
        #
        # 出处（PP6/流程/config-sync-20261113150255.zip › bo/001-project_create.xml）：
        #   planExcel / budgetExcel / riskExcel 三个表格字段的 options 全是这 8 个键，
        #   键序 isMultiple, isDefault, uniqueCheck, optionSource, columns,
        #   identities, format, formula —— 与 gen_bo._OPTION_KEY_ORDER_FULL 一致。
        # 没有 columns 的表格导入后是一片空白子表，所以「有列」这件事由 validate 报错拦住。
        if type_new == "table":
            for k, v in (
                ("isMultiple", False), ("isDefault", False), ("uniqueCheck", False),
                ("optionSource", "BO_ATTR_DIC_TYPE"),
            ):
                opts.setdefault(k, v)
            generated = [c.option(f"{self.bo_code}/{self.code}", i)
                         for i, c in enumerate(self.table_columns, 1)]
            # 优先级：2.1_表格列 / 反解出来的 table_columns > 扩展参数里手写的 columns。
            # 后者是逃生口（老工作簿把整块 JSON 塞在「扩展参数」里），有列就原样保留。
            if generated or not isinstance(opts.get("columns"), list):
                opts["columns"] = generated
            opts.setdefault("identities", [])
            opts.setdefault("format", None)
            opts.setdefault("formula", None)

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
        if type_new == "dic":
            if self.dict_ref:
                # ② GLOBAL 分支（架构 R4 / F-C）：引用项目侧的共享字典，
                # objectType 就是那个 typeKey（线上实测 project.huiBaoLeiXing
                # 的 objectType="report_type"），且 options 里**没有**
                # optionSource——合成它会造出一个线上不存在的形态。
                object_type = self.dict_ref
                opts.pop("optionSource", None)
            else:
                # 自定义字典的 objectType 就是 dict_key（对象编码_字段编码），
                # 和 dictionaryType.typeKey 保持完全一致，不加下划线
                object_type = self.dict_key() or None
                opts.setdefault("optionSource", "BO_ATTR_DIC_TYPE")
            # 级联字段的 options 有两种「宽度」，2026-11-12 逐包实测：
            #
            #   A. 完整 10 键（用户显式配过级联的那个字段，如 productClass）：
            #      isMultiple, isDefault, uniqueCheck, optionSource, columns,
            #      identities, format:null, formula:null,
            #      cascadeEnabled, cascadeParentCode
            #   B. 精简 5 键（同一条级联链上其余字段，如 strategyCategory /
            #      registerCategory / categoryCode）：
            #      isMultiple, optionSource, cascadeEnabled,
            #      cascadeParentCode, isDefault
            #
            # 判据不是「有没有级联」，而是「调用方给没给整块 options」：
            # 系统在设计器里手动配级联时会把整套空键一起写进去，程序化设置的
            # 只写两个级联键。所以这里以「self.options 里是否已带 uniqueCheck
            # 或 columns 这类配套键」为准——保留了手工配置，新填的走精简版。
            _explicit_full = any(
                k in (self.options or {})
                for k in ("uniqueCheck", "columns", "identities", "format", "formula"))
            if self.cascade_parent and _explicit_full:
                for k, v in (
                    ("uniqueCheck", False), ("columns", []),
                    ("identities", []), ("format", None), ("formula", None),
                ):
                    opts.setdefault(k, v)
        elif type_new == "object":
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
            bool(self.default_value) or type_new == "datetime")

        return {
            "code": self.code,
            "fieldName": self.resolved_field_name(),
            "name": self.name or self.code,
            "desc": self.desc or self.name or self.code,
            "type": "base",
            "datatype": datatype,
            "dataTypeNew": type_new,
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
    # `1_业务对象`›「关联项目」= 是。勾上后 builder 会补「所属项目」链接字段，
    # 并把与项目对象同名/同族的字段对齐成项目侧定义。
    # **不自动推断**（架构 D10）：反解出来的包默认「否」，否则「拆了重造」
    # 会静默多出链接字段与一堆对齐改动。
    project_link: bool = False

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
    # 表格控件（tableField）带的列快照。来源两处：
    #   ① blueprint.fields_from_bo 从 BoField.table_columns 复制；
    #   ② gen_form 里按对象字段补齐（Excel 手填 4_表单控件 时不带这个）。
    # 表单 XML 里同一个表格要写两份 columns（控件一份、configurableColumns 一份），
    # 都从这里来；流程 nodePermissions 的控件快照也用它，保证三处一致。
    table_columns: list[TableColumn] = field(default_factory=list)


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
    # 级联详情字段：列表里点击该列 → 全屏打开详情表单（对象的所有字段只读）。
    # 留空 = 不开详情联动。出处：用户给的会议列表导出包（config-sync-20261113002727），
    # 「会议名称」列的 cascade_configuration 指向 meeting_details 详情表单。
    detail_field: str = ""


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


# ---------------------------------------------------------------- 流程
#
# 流程定义（workflowDefinition）是**裸 XML**，不进同步包，导入位置是
# 管理 › 流程设计 › 导入。结构与取值全部以真实样本为准：
#   PP6/流程/项目立项_20261113145550.xml  的 **v30** 那一版
#   （DefaultFlowChartVersion="30"，用户线上正在跑的那版图）
#   + 配套 PP6/流程/config-sync-20261113150255.zip（bo + 2 张 form）
# v1–v10 只有 5 个节点的旧图，只用来判断「某个键是必需还是可选」。

WF_START, WF_ONGOING, WF_BRANCH, WF_PASSED, WF_REJECTED = (
    "Start", "Ongoing", "Branch", "Passed", "Rejected")

# Excel 里填的中文 → XML 里的 State。
# 「不通过」也映射到 Rejected：样本 v1-v10 那张旧图里该节点就叫「不通过」。
WF_KIND_BY_LABEL = {"开始": WF_START, "审批": WF_ONGOING, "判断": WF_BRANCH,
                    "通过": WF_PASSED, "驳回": WF_REJECTED, "不通过": WF_REJECTED}

# 业务状态：固定 4 个，名字与对象 processState 字段的选项**逐字相同**
# （短码靠 gen_bo.dict_choice_codes 换算，差一个字就换不出来 → 节点状态为空）。
WF_STATE_BY_KIND = {WF_START: "待发起", WF_ONGOING: "进行中",
                    WF_PASSED: "已完成", WF_REJECTED: "已失败"}

# processState 字段的选项值（顺序即字典里的 sn，短码用它算，**不能改序**）。
# builder.ensure_workflow_fields() 与 gen_workflow.state_codes() 都以这里为唯一来源：
# 顺序一变，同一份 Excel 的 BusinessState 短码就会与业务对象里的字典项对不上。
WF_STATE_OPTIONS = ["待发起", "进行中", "已完成", "已失败"]

# 流程状态（processState）的选项短码是**系统常量**，不是按对象算出来的。
# 2026-11-13 在 8112 真实库 powerproject6_release 上核对：
#   iform_cat_dic 里 20+ 个 `*_processState` 字典，四个选项的 key_ 完全一致：
#     待发起 f9cd5a6d / 进行中 d432d037 / 已完成 78782698 / 已失败 3184e2aa
#   对象导出 XML 与流程 XML 的 BusinessState 用的也是这同一组值。
# 之前按 `{对象编码}_processState` 做 scope 算 md5，导致每个对象的短码都不同，
# 与平台约定对不上（自洽但与全系统不一致，列表筛选/看板按码过滤就会漏数据）。
PROCESS_STATE_CODES = {
    "待发起": "f9cd5a6d",
    "进行中": "d432d037",
    "已完成": "78782698",
    "已失败": "3184e2aa",
}

# 各 State 的默认画布颜色（样本 v30 实测：Ongoing=1 / Branch=2 / Passed=3 / Rejected=8）。
# ⚠ Start 故意不在表里：v30 的开始节点**没有** ColorCode
# （只有 v1–v10 的旧图写了 7）。要改回「写 7」只需在这里加一行 WF_START: "7"。
WF_COLOR = {WF_ONGOING: "1", WF_BRANCH: "2", WF_PASSED: "3", WF_REJECTED: "8"}

# 判断节点出边的条件运算符：中文 → XML 里的 operator。
# 「属于 / 为空 / 不为空」在样本里**没有实例**（属于无证据实现，见架构文档待明确第 9 条）。
WF_COND_LABEL = {"等于": "equal", "不等于": "not_equal", "大于": "greater",
                 "大于等于": "greater_or_equal", "小于": "less", "小于等于": "less_or_equal",
                 "属于": "in", "为空": "is_empty", "不为空": "is_not_empty"}

# 不需要填「条件值」的算子
WF_EMPTY_OPS = {"is_empty", "is_not_empty"}


@dataclass
class WfNode:
    """流程里的一个节点。

    三种落库形态（样本 v30 逐字）：
      Start/Ongoing/Passed/Rejected → ``<NormalActivity State="…">``
      Branch                        → ``<BranchActivity>``（没有 State、没有表单）
    """

    wf: str
    name: str
    kind: str = WF_ONGOING
    id: int = 0                       # 0 = 按行序自动 1..N（判断节点也占号，与样本一致）
    handler_type: str = "角色"        # 角色 / 人员 / 无
    handler_names: list[str] = field(default_factory=list)
    handler_ids: list[str] = field(default_factory=list)
    expect_days: int = 2              # → ExpectHandleDays（只有 Ongoing 带）
    form_key: str = ""                # 空 = 自动（开始→新建表单，其余→详情表单）
    business_state: str = ""          # 空 = 按 kind 推导（见 WF_STATE_BY_KIND）
    go_back: list[str] = field(default_factory=list)   # ["无"] = 不允许退回
    pick_next: bool = False           # → NextHandler 走人工选择
    allow_handover: bool = False      # → AllowHandover
    color: str = ""                   # 空 = 按 kind 取 WF_COLOR
    notes: str = ""                   # 仅备注，不写入 XML


@dataclass
class WfEdge:
    """一条出边（Transition）。

    同一 src/dst 的多行会在生成时合并成**一条出边的多个 rules**（AND），
    这样 Excel 不用加「条件组」列。
    """

    wf: str
    src: str
    dst: str
    name: str = ""
    condition_field: str = ""
    condition_op: str = ""
    condition_value: str = ""
    value_label: str = ""
    order: int = 0                    # 判断节点出边 1..N；普通节点恒 0
    inform: bool | None = None        # None = 按源节点种类推导（判断→否，其余→是）


@dataclass
class Workflow:
    """一条流程的定义头 + 节点 + 流转。"""

    name: str
    bo_code: str
    workflow_type: str = "normal"
    category_id: str = ""             # → categoryId（流程分类，不是对象的 typeId）
    module_id: str = ""
    title_format: str = "${name}-${bo.name}-${bo.createBy}-${bo.createTime}"
    is_active: bool = True            # → FlowDefinition IsActive
    desc: str = ""
    exist_id: str = ""                # 更新线上已有流程时填它原来的 UUID
    # 自动生成的「兜底流程」（开始→审批→通过）。用户没填 9_流程 时补一条，
    # 让流程驱动对象至少能跑通；validate 对它的审批人留空只警告不报错，
    # 因为真正的审批人要项目经理到系统「流程设计」里指定。
    auto: bool = False
    nodes: list[WfNode] = field(default_factory=list)
    edges: list[WfEdge] = field(default_factory=list)

    def node_by_name(self, name: str) -> WfNode | None:
        for n in self.nodes:
            if n.name == name:
                return n
        return None


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
    workflows: list["Workflow"] = field(default_factory=list)
    # 流程分类ID（0_项目设置 ›「流程分类ID」）。与 type_id 是**两套**分类体系：
    # 样本 项目立项_20261113145550.xml 的 categoryId 1559242949219319808
    # 与对象分类 ID 完全不同，复用一个值会让流程落进错的分类节点。
    wf_category_id: str = ""
    out_dir: str = "out"
    # 读取时自动补齐的明细（编码按名称生成、关联对象对到真实编码），供界面提示用
    auto_notes: list[str] = field(default_factory=list)

    def bo(self, code: str) -> Bo | None:
        for b in self.bos:
            if b.code == code:
                return b
        return None
