"""中间模型：Excel 的一行 -> 一个对象；生成器只依赖这些模型。"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import naming

# ---------------------------------------------------------------- 工具函数

# 数据集/值来源在 Excel 里表达「默认口径」的两个符号。
#
# 为什么需要它们：值来源有三档配置，光靠「留空 / 填值」表达不了——
#   过滤：留空 = 按同名自动推导；`无` = 一条过滤都不要（自身值来源、XX选择）
#   返回：留空 = 只有主键 + 名称；`*` = 主键 + 名称 + 全部自建字段
# 精确到「一个符号一档」，往返 Excel 才不会把两档悄悄合并成同一档
# （沿用 10_流程节点 里「退回节点=无」的写法，用户一看就懂）。
FILTER_NONE = "无"
RESULT_ALL = "*"
# 「只要主键 + 名称」必须**显式写出来**：`6.1_值来源` 的「返回字段」留空已经
# 表示「全部自建字段」（用户口径：返回结果默认 = 主键 + 全部自建字段），
# 留空与最小列不能是同一格，否则往返一次口径就翻。
RESULT_MIN = "id,name"


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


def value_source_key_base(bo_code: str, bo_name: str = "") -> str:
    """值来源 key 的词干：**中文对象名翻英文**优先，没有名字才退对象编码。

    为什么用 ``naming.object_code`` 而不是 ``naming.to_english``
    ------------------------------------------------------------
    ``to_english`` 是**拼音优先**的（专给字段编码 / 物理列名用，规则可预测），
    拿它翻「采购申请」得到 ``cai_gou_shen_qing``；而对象编码这一层早就是
    「英文词典优先、查不到才退拼音」（``naming.py:282`` 那段说明讲的就是
    为什么——对象编码会派生物理表名 ``pp_<code>``，拼音在系统里一张表都找不到）。
    选择器的 key 是给人看、给控件引用的机器标识，跟对象编码同一族最省心：
    ``采购申请 → purchase_apply`` → key ``purchase_apply_value``。

    对象的编码恰恰**可能是拼音**（``caiGou`` / ``cai_gou`` 这类历史编码），
    直接拿编码拼就是 ``caigou_value``——用户 2026-11-20 明确问「key 怎么是拼音」，
    这个函数就是那条口径的落点：**用名字翻英文，编码只做兜底**。
    """
    name = str(bo_name or "").strip()
    if name:
        got = naming.object_code(name)
        if got:
            return got
    return str(bo_code or "").strip().lower() or "bo"


def value_source_key(bo_code: str, taken=None, bo_name: str = "") -> str:
    """对象 → 它那份值来源的 key：``<对象名翻英文>_value``，第二份起 ``_2`` / ``_3``。

    **key 由我们生成，不让用户算**（用户 2026-11-20 口径）。三条约束合起来才是这个实现：

      1. 平台/校验要求 dataTemplate 的 key 小写开头、只含小写/数字/下划线
         （``validate.KEY_RE``）。词干走 ``naming.object_code``，出来的必然
         符合这个形状——哪怕对象编码是驼峰（``caiGou``）也一样，因为**编码
         只在不传名字时才用**，那时才需要 ``lower()``。
      2. 线上 203 条选择器的 key 全是小写英文 slug
         （``classification_value`` / ``customer_value`` …），``<英文名>_value``
         是它们的默认形态。
      3. 同一个对象可以有多份选择器（全部 / 按项目类型过滤 / …），key 不能撞：
         传 ``taken`` 进来就按出现顺序取第一个空位 ``_value`` → ``_2`` → ``_3``。

    ``bo_name`` 是**对象名称**（采购申请），``bo_code`` 是**对象编码**（caiGou）：
    两者是两回事，key 只跟名字走，``datasetKey`` / ``boCode`` 仍写编码
    （见 gen_tpl.render_dataset）。名字查不到时调用方应出一条提示（不静默）。
    """
    base = f"{value_source_key_base(bo_code, bo_name)}_value"
    used = set(taken or ())
    if base not in used:
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    return f"{base}_{n}"


def value_source_key_notes(key: str, bo_name: str) -> list[str]:
    """key 生成后的可查痕迹：词典缺词、退成拼音时说清楚是哪几个词。

    不静默的理由：``project_budget_kemu`` 这种 key 用户只会觉得怪，不知道
    该去补词；说出来（哪个中文词、退成什么拼音、去哪补）才是可修的提示。
    """
    parts = naming.pinyin_parts(bo_name)
    if not parts:
        return []
    shown = "、".join(f"「{cn}」（拼音 {py}）" for cn, py in parts)
    return [f"选择器 {key}: 名称里的{shown}未收录英文词典，已用拼音兜底"
            f"（可在 naming.WORDS 里补词表）"]


def value_source_missing_name_note(key: str, bo_code: str) -> str:
    """查不到对象名称时的提示原文（key 只能按编码生成）。

    单独一个函数是因为**调用方不同**：reader 从 Excel 只拿到编码、blueprint 从
    清单拿名字。文案在这里只有一份，界面 / CLI / 回归断言才对得上。
    """
    return (f"选择器 {key}: 找不到对象 {bo_code} 的名称，key 用对象编码生成"
            f"（如需英文请补对象名称）")


def object_name_of(project, code: str) -> str:
    """对象编码 → 对象名称：**本包**优先，其次系统清单，都没有就空串。

    值来源的 key 要按名字翻英文，所以每个调用点都得先拿到名字。四处调用点
    （blueprint / reader / webui）各写一遍「先查包内 BO、再查 catalog」迟早
    出现某一路径用编码生成 key 的分叉，收敛到这里一处。
    """
    text = str(code or "").strip()
    if not text:
        return ""
    bo = project.bo(text) if project is not None else None
    if bo is None and project is not None:
        bo = next((b for b in project.bos if b.tbl_name == text), None)
    if bo is not None and bo.name:
        return bo.name
    try:                       # 系统清单：离线时也有随包快照，查不到就返回空
        from . import objects as _objects
        for it in _objects.catalog():
            if it.get("code") == text:
                return str(it.get("name") or "")
    except Exception:          # noqa: BLE001
        return ""
    return ""


def value_source_name(obj_name: str, key: str, purpose: str = "", taken=None) -> str:
    """实例名称：``<对象名>值来源``，同对象多份时带用途后缀。

    用户口径是「名称用用途后缀区分」（``采购申请值来源_全部`` /
    ``采购申请值来源_过滤项目类型``）—— 一份叫「全部」的、一份被过滤的，
    在设计器的列表里一眼能分开；而 key 是机器标识（``purchase_apply_value_2``），
    不该让人从 key 去猜用途。

    ``purpose`` 为空时按``_2``/``_3`` 兜底（至少不重名）；重名时继续加序号。
    """
    base = f"{obj_name or ''}值来源"
    if key.endswith("_value"):
        return base                        # 每个对象的第一份（默认那份）
    tag = purpose.strip() or key.rsplit("_", 1)[-1]   # 没给用途就退成 2/3…
    text = f"{base}_{tag}"
    used = set(taken or ())
    if text not in used:
        return text
    n = 2
    while f"{text}_{n}" in used:
        n += 1
    return f"{text}_{n}"


def parse_linkage(value: Any) -> list[dict]:
    """Excel「联动字段」列 -> [{"name","field","label"}]。

    两种写法都收：
      · ``zhuan_ye_=zhuanYe,name_=`` （目标列=本控件，空=只回显）
      · ``[{"name":"zhuan_ye_","field":"zhuanYe","label":"专业"}]``
    留空 = 自动推导（见 links._linkage）。
    """
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("[") or text.startswith("{"):
        try:
            got = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(got, dict):
            got = [got]
        out: list[dict] = []
        for item in got if isinstance(got, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("column") or "").strip()
            if not name:
                continue
            out.append({"name": name,
                        "field": str(item.get("field") or "").strip(),
                        "label": str(item.get("label") or "").strip()})
        return out
    out = []
    for part in re.split(r"[,;，；]", text):
        if not part.strip():
            continue
        if "=" in part:
            name, _, fld = part.partition("=")
        else:
            name, fld = part, ""
        name = name.strip()
        if name:
            out.append({"name": name, "field": fld.strip(), "label": ""})
    return out


def parse_filter_field(value: Any) -> tuple[str, str]:
    """Excel「过滤字段」列 -> (本对象字段编码, 目标对象物理列)。

    写法：``xiangMuLeiXing`` 或 ``xiangMuLeiXing=classification_id_``。
    目标列留空时由生成阶段按「显示名相同」推导。

    特殊值 ``无`` = **一条过滤都不要**（自身值来源 / 「XX选择」链接字段的默认
    口径），返回 ``("", "")`` 且调用方要据此把 ``auto_filter`` 置 False：
    留空的含义是「按同名推导」，两者不是一回事。
    """
    text = str(value or "").strip()
    if not text or text == FILTER_NONE:
        return "", ""
    if "=" in text:
        left, _, right = text.partition("=")
        return left.strip(), right.strip()
    return text, ""


def wants_no_filter(value: Any) -> bool:
    """「过滤字段」列是否明确写了「无」（= 不要任何过滤，含不要自动推导）。"""
    return str(value or "").strip() == FILTER_NONE


def wants_all_results(value: Any) -> bool:
    """「返回字段」列是否写成了 ``*``（= 主键 + 名称 + 全部自建字段）。"""
    return str(value or "").strip() == RESULT_ALL


# ---------------------------------------------------------------- 业务对象

# 类型参数来自 assets/fieldtypes.json（见 pp6kit/fieldtypes.py）
from . import fieldtypes as _ft  # noqa: E402

# 需要 objectType 的类型
NEED_OBJECT_TYPE = {"object", "dic"}

# 流程驱动的业务对象必须冗余这 6 个字段，流程流转时反写。
# 逐键对照线上 pp_project_create（2026-11-17 只读查库 + 原生包双重确认）：
#   processDefId      varchar(200)
#   processInstanceId varchar(200)
#   processNode       dic(1000)，字典 = 流程节点名（isMultiple / isDefault）
#   processHandler    varchar(1000)
#   processName       varchar(255)
#   processCode       autoNumber(64)，WF-{yyyy}{MM}{dd}{NO}，noLength 3，全局去重
# processState 不在这里：它只在「本包里有流程」时才补，且要 4 个固定选项，
# 见 builder.ensure_workflow_fields（和流程里的 BusinessState 同源）。
PROCESS_FIELDS = [
    {"code": "processDefId", "name": "流程定义", "type_new": "varchar", "length": 200},
    {"code": "processInstanceId", "name": "流程实例", "type_new": "varchar", "length": 200},
    {"code": "processNode", "name": "流程节点", "type_new": "dic", "length": 1000,
     "options": {"isMultiple": True, "isDefault": True}},
    {"code": "processHandler", "name": "流程处理人", "type_new": "varchar", "length": 1000},
    {"code": "processName", "name": "流程名称", "type_new": "varchar", "length": 255},
    {"code": "processCode", "name": "流程编号", "type_new": "autoNumber", "length": 64},
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
    # 占位列标记：AI 没给出列定义时先顶一列，等 fill_table_columns 拿到真列后整批替换。
    # 不写进 options（option() 只挑键），只在内存里当状态用。
    placeholder: bool = False

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
    # 是不是本次出包的「XX选择」链接字段（关联项目→项目选择 / 关联其它对象→合同选择）。
    #
    # 为什么需要这个标记，而不是靠「object_type == 目标对象编码」现算：
    #   ① 预览字段列表与生成的三张表单都要求它**置顶**，而 `sn` 不能动
    #      （存量包铁律：sn 一变，线上已有的属性顺序/列顺序都要跟着改）；
    #   ② 值来源要按「全部业务字段 + 主键、不默认过滤」出，而普通关联字段
    #      （项目类型 → classification）必须保持老口径（主键+名称、按同名推导过滤）。
    #      两者都是 object 字段，只能靠这个标记区分。
    # 只由 builder.ensure_project_links / projlink.link_field 设置，用户 Excel
    # 里自己写的关联字段一律为 False。
    is_link: bool = False
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

    def choice_text(self) -> str:
        """候选值写回 Excel「选项值」列：分组字段要保留「父：子1、子2」。

        2026-11-17 用户反馈：AI 预览里明明有级联分组，下载 Excel 再出包，
        dependValues 全是空的。根因就是这里只写了扁平 `choices`，
        分组信息在 Excel 往返时丢了。
        """
        groups = {str(k): [str(x) for x in (v or []) if str(x).strip()]
                  for k, v in (self.choice_groups or {}).items() if str(k).strip()}
        if not groups:
            return "\n".join(self.choices)
        lines: list[str] = []
        covered: set[str] = set()
        for parent, kids in groups.items():
            if not kids:
                continue
            lines.append(f"{parent}：{'、'.join(kids)}")
            covered.update(kids)
        # 不在任何分组里的候选项原样补在后面，别丢
        lines.extend([c for c in self.choices if c not in covered])
        return "\n".join(lines)

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
        if type_new == "attachment":
            # 平台 BO 的附件类型叫 file（iform_bo_attr 实测），不是 attachment；
            # attachment 只是**表单控件**的 fieldType。写错会导入成空类型。
            # 多附件要存多个文件 id，长度按平台真实值 1000，不用 200。
            if self.length is None:
                length = 1000
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
        #
        # 系统字段例外：原生包里 id/tenantId/ip/createBy/createTime/updateBy/
        # updateTime/deleted/version/baselineVersion/isDraft/process* 一律
        # isDefault=true（2026-11-17 只读查库 labor_budget_rate / NEW01 /
        # test_case_base_category 三个对象、16 个默认字段全是 true）。
        # 我们原来只给 datetime 置 true，导入后系统「默认字段」页签里就只剩
        # 两三个，用户 19:16 反馈「你生成的包默认字段只有几个，系统里有 16 个」。
        if self.is_system:
            opts.setdefault("isMultiple", False)
            opts["isDefault"] = True
        else:
            opts.setdefault(
                "isDefault",
                bool(self.default_value) or type_new == "datetime")

        return {
            "code": self.code,
            "fieldName": self.resolved_field_name(),
            "name": self.name or self.code,
            "desc": self.desc or self.name or self.code,
            # 表格字段不落物理列，原生包里 type 一律是空串（不是 base）。
            # 实测两份生产包：table 全部 type=""，普通字段才是 base。
            # 写成 base 时系统按普通列处理，表格会被当成大文本导入，
            # 子表和 tableField 控件都挂不上（2026-11-16 用户反馈
            # 「生成的 zip 包表格类型没导入进去」）。
            "type": "" if type_new == "table" else "base",
            "datatype": datatype,
            # 附件：BO 侧 dataTypeNew=file，表单控件仍用 attachment
            "dataTypeNew": "file" if type_new == "attachment" else type_new,
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
    # 联动映射：值来源返回列 → 本表单控件（gen_form 写进 link_linkage）。
    # 每项 {"name": 目标对象物理列名, "field": 本表单控件编码, "label": 显示名}。
    # 空列表 = 自动推导（返回列的物理列名与本对象某字段的物理列名对上，见
    # links.LinkPlan）——样本里项目的 zhuan_ye_ 自动带到表单的 zhuanYe。
    linkage: list[dict] = field(default_factory=list)

    def linkage_text(self) -> str:
        """写回 Excel「联动字段」列。

        全部只有「目标列=控件」两段时写成紧凑串（人好读好改）；
        带自定义显示名时转成 JSON，否则标签会在 Excel <-> 模型之间丢掉。
        """
        items = [it for it in (self.linkage or [])
                 if isinstance(it, dict) and str(it.get("name") or "").strip()]
        if not items:
            return ""
        if any(str(it.get("label") or "").strip() for it in items):
            return json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        return ",".join(f"{it['name']}={it.get('field') or ''}" for it in items)


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
    # 表单布局样式：
    #   grouped = 字段分组（当前默认，每个分区一个 fieldset + 内部 grid_layout）
    #   flat    = 拖拽布局 V1.0（整张表单一个根 grid_layout，不套 fieldset 分组）
    layout_style: str = "grouped"
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
    # 是否在表单上补 id/version 隐藏控件。**默认不补**（2026-11-16 用户要求：
    # 生成器塞上去的隐藏字段在表单设计器里碍眼，默认不要）。
    # 需要时在 3_表单 的「补隐藏字段」填「是」；流程对象始终保留整套
    # （id/version + 5 个流程字段），流程任务提交要用，见 gen_form.render。
    auto_hidden: bool = False
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
    # ---- 以下是值来源（type=valueSource）的可选配置 ----------------
    # 过滤：本对象用哪个字段去过滤目标对象（filter_field 是**本对象字段编码**），
    # filter_target 是目标对象里被过滤的物理列（留空按同名推导）。
    # 两个都空 = 不加 filterConditions（值来源出全量）。
    # 参照 docs/参考-关联对象值来源-20261119.md §2.3。
    filter_field: str = ""
    filter_target: str = ""
    # 返回列：目标对象的字段编码（也接受物理列名 / 显示名）。
    # 空 = 默认「主键 + 名称」。**主键永远强制包含**——值来源没有主键取不到值，
    # 这是用户明确要求的硬约束。
    result_fields: list[str] = field(default_factory=list)
    # 「XX选择」链接字段专用：返回目标对象的**全部业务字段**（主键第一位 + 名称
    # + 其余非系统列），而不是默认的主键 + 名称。
    #
    # 为什么这必须与 auto_filter 一起记在数据集上，而不是各生成器自己算：
    # 表单侧 link_linkage 与值来源侧 resultColumns 必须引用**同一份**返回列。
    # 两边各推导一遍，联动就会指向一条值来源没返回的列——界面上看着配好了，
    # 选了值却带不出任何东西，线上极难查（见 links.with_linked_columns 的说明）。
    result_all: bool = False
    # 要不要按「本对象有同名关联字段指向同类对象」自动推导过滤条件。
    # 「XX选择」链接字段要 False：用户口径是**默认不加任何过滤**，等到 app 面板
    # 里自己选才加；普通关联字段（项目类型 → classification）保持 True。
    auto_filter: bool = True
    # 这份值来源是**系统按默认口径生成**的（Excel「6.1_值来源」的「自动」列=是），
    # 不是用户手加的。只在两个地方读：
    #   ① 出包时判断「要不要沿用默认口径」——自动生成的那份允许被 blueprint
    #      重新按默认口径生成（幂等），用户手加的不许动；
    #   ② 往返 Excel 时把「自动」列原样写回去，重复出包不再反复刷同一条提示。
    # 它不影响 XML：包里有没有这个键用户看不到。
    auto_generated: bool = False

    def filter_text(self) -> str:
        """写回 Excel「6_数据集」的「过滤字段」列：``本字段`` 或 ``本字段=目标列``。

        ``auto_filter=False`` 且没显式填过滤时写 ``无``：留空在 6_数据集 里
        是「按同名推导」，而链接字段 / 自身值来源要的是「一条过滤都不要」，
        两者必须能区分开（与「10_流程节点」的「退回节点=无」同一个路数）。
        """
        if not self.filter_field:
            return "" if self.auto_filter else FILTER_NONE
        if self.filter_target and self.filter_target != self.filter_field:
            return f"{self.filter_field}={self.filter_target}"
        return self.filter_field

    def result_text(self) -> str:
        """写回 Excel 的「返回字段」列。

        三档，必须能在 Excel 里区分并原样读回来：
          · 显式列了字段            → ``id,name,zhuanYe``
          · ``result_all``（全字段） → ``*``
          · 都不要（只剩主键+名称） → ``id,name``（= ``RESULT_MIN``）

        为什么最小列不能写空：**留空已经表示「全部自建字段」**（用户口径：
        返回结果默认包含主键 + 生成业务对象的全部自建字段）。两条口径抢同一格，
        往返一次就会翻——留空写回、读回变全字段，或者反过来。
        """
        if self.result_fields:
            return ",".join(self.result_fields)
        return RESULT_ALL if self.result_all else RESULT_MIN


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
    # 读到的原始工作表名。用来区分「标准模板但一个字没填」和「压根不是模板」——
    # 两者都是 0 个对象，前者该提示去 1_业务对象 填，后者该交给 AI 识别。
    sheet_names: list[str] = field(default_factory=list)
    # 本次解析/出包要对齐的目标业务对象（projlink.TargetObject）。
    # 用 Any 避免 model ↔ projlink 循环导入；None = 项目（保持老行为）。
    link_target: Any = None

    def bo(self, code: str) -> Bo | None:
        for b in self.bos:
            if b.code == code:
                return b
        return None
