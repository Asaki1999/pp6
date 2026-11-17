"""表单 -> form/001-{key}.xml

布局：**「布局 V1.0」**（线上真实表单的写法，2026-11-10 对包确认）

    分区容器 (card / fieldset)
      └── 表格布局V1.0 (grid_layout)
            └── 控件

  - 按控件的「分区」分组，一个分区一个容器 + 一个表格布局；
    分区留空则整张表单一个容器，标题「基本信息」。
  - 详情类表单用 card（详情页就是这个形态，卡片自带标题栏）；
    新建/条件/导入用 fieldset（立项表单的形态，抽屉里更轻）。
  - 表格布局的 fieldOptions **写两份**：columns（扁平单元格引用）
    + rows（每格内嵌完整控件 JSON），控件的 parentId 指向单元格 id。
  - 一行最多 2 个控件（span=12）；富文本/长文本/附件等占满整行（span=24）；
    「另起一行」强制换行；「一行一列」时全部 span=24。
"""
from __future__ import annotations

import copy
import json

from .ids import new_id, short_id
from .model import (
    EMPTY_OPERATORS,
    FORM_CREATE,
    FORM_DETAIL,
    HIDE_OPERATORS,
    LABEL_BY_OP,
    Bo,
    Form,
    FormField,
    form_style,
)
from .xmlw import DECL_STANDALONE, el, json_attr, wrap

# 这些类型默认独占一行
_FULL_ROW_TYPES = {"editor", "textarea", "tablelayout", "tabs", "card", "attachment",
                   # 明细表（子表）控件横向要铺满，跟原生表单一致：
                   # 项目汇报表里 4 个 dataTemplate 全在独立的 fieldset 里各占一行
                   "dataTemplate",
                   # 表格字段（子表）控件同理，而且它连 grid_layout 都不进
                   "tableField"}

# 直挂容器的控件类型：一个分区只有一个、且是这两种时，容器 columns 直接引用控件 id，
# 中间不加 grid_layout。依据：真实表单里 dataTemplate 与 tableField 都是这个形态
# （项目汇报表的明细表、project_create 的 3 个表格字段），塞进 grid_layout 的 rows
# 里系统不渲染。
_DIRECT_TYPES = {"dataTemplate", "tableField"}


def _is_direct_row(row: list[FormField]) -> bool:
    return len(row) == 1 and row[0].field_type in _DIRECT_TYPES

# 控件图标：从项目详情表单 / 立项表单两个真实包里逐个字段统计来的，
# 缺了会让设计器里的控件图标变成问号。
_ICONS = {
    "text": "SingleText",
    "textarea": "Texts",
    "editor": "Texts",
    "number": "ListNumbers",
    "datePicker": "Date",
    "date": "Date",
    "select": "DropDown",
    "dictionary": "DropDown",
    "selectObject": "CustomControl",
    "dropSelector": "CustomControl",
    "dropSelectOrg": "CustomControl",
    "dropSelectGroup": "CustomControl",
    "linkdata": "CustomControl",
    "attachment": "Enclosure",
    "autoNumber": "SingleText",
    "tablelayout": "Table",
    "tableField": "Table",
    # 明细表控件：原生表单里这个控件没有 icon 键（它不在 grid_layout 的
    # rows 里，只被容器的 columns 引用），但我们的 rows 会带上它，
    # 给一个 Table 图标，设计器里不至于显示成问号。
    "dataTemplate": "Table",
    "label": "Textss",
}

# 分区容器：详情页用卡片（能挂工具栏、有标题栏），其余用分组框。
_CONTAINER_BY_KIND = {FORM_DETAIL: "card", "detail_expand": "card"}

# 分区留空时的标题
_DEFAULT_GROUP = "基本信息"


def _max_per_row(form: Form) -> int:
    """一行放几个控件：**固定 2 个**。

    这是用户 2026-10-29 明确要求的，也符合线上所有真实表单（新建/详情
    都是两列），以及手册的抽屉宽度。

    不按表单宽度自适应的原因：
      - 手册的抽屉 45% 下，2 列是唯一舒服的选择（每列 432px，
        扣 labelWidth 106 后输入框还有 310px）
      - 3 列就只剩 166px，「请选择」要截断
      - 4 列不到 80px，直接塌成竖条
      - 展开详情虽然是 90%，但一行 3 个仍比 2 个挤；一致性更重要，
        用户看起来也整齐（所有字段对齐成两列网格）

    独占一行的类型（多行文本/附件/子表单等）不受这个限制。
    """
    return 2

_LINK_CONFIG_LIST = {"id": "id_", "text": "name_", "typeById": "varchar", "type": "first", "structure": "list"}

# 系统内置隐藏字段
# 只补 id / version 两个系统必需的主键与版本控件。
# 以前还会补一个「拼音」隐藏控件，但线上业务表并没有这一列（老表单才有），
# 生成后表单上多出来一列反而要去删，所以不再生成。
# 流程冗余字段/流程状态：对象里必须有（流程反写），但表单上不能给用户看。
# 只在表单里落成 fieldType=hidden；不放进任何分区。
_PROCESS_HIDDEN = (
    ("processNode", "流程节点"),
    ("processHandler", "流程处理人"),
    ("processDefId", "流程定义"),
    ("processInstanceId", "流程实例"),
    ("processState", "流程状态"),
)
_PROCESS_CODES = {code for code, _label in _PROCESS_HIDDEN}
_HIDDEN_FIELDS = (("id", "主键"), ("version", "版本")) + _PROCESS_HIDDEN


class Snippets:
    """样例骨架访问器。"""

    def __init__(self, data: dict) -> None:
        self.data = data

    def field_options(self, field_type: str) -> tuple[dict, bool]:
        """返回 (骨架, 是否用了兜底)。"""
        raw = self.data["fieldOptions"].get(field_type)
        if raw:
            return json.loads(raw), False
        return json.loads(self.data["fieldOptions"].get("text", "{}")), True

    def hidden_options(self, which: str) -> dict:
        raw = self.data["fieldOptions"].get(f"_hidden_{which}") or self.data["fieldOptions"].get("_hidden", "{}")
        return json.loads(raw)

    def grid(self, kind: str) -> dict:
        return json.loads(self.data["grid"][kind])

    def layout(self, which: str) -> dict:
        """分区容器 / 表格布局的外层参数骨架（card / fieldset / grid_layout）。"""
        raw = (self.data.get("layout") or {}).get(which)
        if not raw:
            return {}
        return copy.deepcopy(raw)

    @property
    def extend_attr(self) -> dict:
        raw = self.data.get("formDefExtendAttr") or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}


def _span_of(field: FormField, form: Form, alone: bool = False) -> int:
    """单元格宽度：24 制。

    线上真实表单里只有两种格子：整行 24、两列各 12。
    一行里只放一个控件时必须给 24，否则右半边整片空白。
    """
    if alone or form.layout == 1 or field.full_row or field.field_type in _FULL_ROW_TYPES:
        return 24
    return 12


def _widget_json(field: FormField, ftype: str, options: dict, code: str,
                 cell_id: str, field_name: str = "") -> dict:
    """表格布局 rows 里那份「内嵌完整控件 JSON」。

    线上真实表单把它和 formField 元素写了两份，缺一份导入后该格就空着。
    这里的 id 是 7 位短 id，跟 formField 元素的 19 位 id 不是一个东西。
    """
    out = {
        "field_type": ftype,
        "name": field.name,
        "label": field.label,
        "icon": _ICONS.get(ftype, "SingleText"),
        "field_options": options,
        "code": code,
        "parentId": cell_id,
        "isSub": False,
        "modelKey": field.name,
        "id": short_id(),
    }
    if field.desc:
        out["desc"] = field.desc
    if field_name:
        out["field_name"] = field_name
    return out


# 「数据字典」控件是**树形选择**（骨架里的 select_mode=leaf / display_mode=path /
# split="/" 就是树的参数），它本身按字典层级展开，没有「听哪个控件」的概念。
# 线上样本（项目表单包含级联关系…zip）里，品种类别/品种分类用的是
# fieldType=select + datasource=dictionary + options_source=bo_attr + init_linkage。
# 所以 dic 字段一旦配了级联父级，要按 select 那种形态出包，否则级联键白写。
_CASCADE_DROP_TYPES = {"dictionary"}


def _cascade_parent_names(form: Form) -> set[str]:
    """被别的控件当作级联父级的控件名。"""
    return {f.cascade_parent for f in form.fields if f.cascade_parent}


def _inherit_cascade(form: Form, bo: Bo | None, notices: list[str]) -> None:
    """业务对象字段配了级联、表单控件这层没填 → 表单自动继承。

    线上样本（项目立项业务对象.zip + 项目表单包）两层是同一个关系：
    属性上写一遍、控件上再写一遍。让用户在两处填同一件事，漏一处就变成
    「属性面板里有、表单里不生效」，所以这里以业务对象为准补全。
    """
    if bo is None:
        return
    by_code = {bf.code: bf for bf in bo.fields}
    present = {f.name for f in form.fields}
    for ff in form.fields:
        if ff.cascade_parent:
            continue
        bf = by_code.get(ff.name)
        if bf is None or not bf.cascade_parent:
            continue
        if bf.cascade_parent not in present:
            notices.append(
                f"控件 {ff.name}（{ff.label}）：业务对象里配了级联父级 {bf.cascade_parent}，"
                f"但表单里没有这个控件，已跳过"
            )
            continue
        ff.cascade_parent = bf.cascade_parent
        notices.append(
            f"控件 {ff.name}（{ff.label}）：级联父级 {bf.cascade_parent} 取自业务对象字段"
        )


def _inherit_table_columns(form: Form, bo: Bo | None, notices: list[str]) -> None:
    """表格字段 -> 表格控件 的两处对齐。

    1. 控件类型：字段类型是「表格」就一定要出 tableField。用户在 4_表单控件 里
       手填 text / textarea / dataTemplate（历史习惯）时自动纠正并提示——
       填 dataTemplate 的话，子表会去找一个同名列表，找不到就是一片空白。
    2. 列快照：Excel 手写的控件行不带列，从对象字段那里补；
       预览/流程节点权限都靠这份快照，缺了会生成没有列的空子表。
    """
    if bo is None:
        return
    by_code = {bf.code: bf for bf in bo.fields}
    for ff in form.fields:
        bf = by_code.get(ff.name)
        if bf is not None and bf.type_new == "table":
            if not ff.table_columns:
                ff.table_columns = list(bf.table_columns or [])
            if ff.field_type != "tableField":
                notices.append(
                    f"控件 {ff.name}: 字段类型是「表格」，控件类型 {ff.field_type} "
                    f"已按系统规范改成 tableField")
                ff.field_type = "tableField"
            ff.full_row = True
        elif ff.field_type == "tableField":
            # 反过来：控件填了 tableField 但字段不是表格（从别处复制来的行）
            notices.append(
                f"控件 {ff.name}: 对象里没有对应字段或字段不是「表格」，"
                f"tableField 已退回单行文本")
            ff.field_type = "text"

    # 3. 4_表单控件 里漏写了表格字段：文档写的是「字段类型选『表格』会自动出」，
    # 但之前只对齐已有控件，没写控件行时表单上压根没有这张子表——
    # 用户看到的就是「表格类型没导入进去」（2026-11-16 反馈）。
    # 只补新建/详情这类全量表单；条件专用/导入专用只放用户挑的控件，不插队。
    if form.kind in (FORM_CREATE, FORM_DETAIL, "detail_expand"):
        have = {ff.name for ff in form.fields}
        for bf in sorted(bo.fields, key=lambda x: x.sn):
            if bf.type_new != "table" or bf.code in have:
                continue
            form.fields.append(FormField(
                form_key=form.key,
                name=bf.code,
                label=bf.name,
                field_type="tableField",
                table_columns=list(bf.table_columns or []),
                readonly=form.kind != FORM_CREATE,
                full_row=True,
                group="",
            ))
            notices.append(
                f"表单 {form.key}: 表格字段 {bf.code} 没在「4_表单控件」里，"
                f"已自动补 tableField（不需要就去那行删掉）")


def _is_cascade_control(field: FormField, parents: set[str] | None = None) -> bool:
    """这个控件在不在级联链上（自己是子级，或者自己是别人的父级）。

    父级也要算：样本里 productCategory 和 productClassification 都是
    select + 数据字典。父级若是树形字典控件，子级取到的值形态可能不一致。
    """
    if field.field_type not in _CASCADE_DROP_TYPES:
        return False
    return bool(field.cascade_parent) or field.name in (parents or set())


def _effective_type(field: FormField, parents: set[str] | None = None) -> str:
    """真正写进 XML 的控件类型（可能要跟模型里的类型不一样）。"""
    return "select" if _is_cascade_control(field, parents) else field.field_type


def _dict_cascade_shape(out: dict, field: FormField) -> None:
    """把字典控件改成线上样本里「下拉 + 数据字典 + 级联」的形态。"""
    out.pop("select_mode", None)
    out.pop("display_mode", None)
    out.pop("split", None)
    for k in ("value_source", "link_attr", "link_condition", "link_linkage"):
        out.pop(k, None)
    out["datasource"] = "dictionary"
    out["options_source"] = "bo_attr"
    out["dictionary"] = field.value_source
    # 下拉控件的占位符不该是「请输入」
    out["placeholder"] = "请选择" if field.placeholder in ("", "请输入") else field.placeholder
    # 样本里这三个键都在，缺了会让级联初始化不生效
    out["init_linkage"] = True
    out["selection"] = True
    # 样本里级联下拉都是可搜索的（选项动辄十几二十条，不能靠滚动）
    out["filterable"] = True
    out.setdefault("cascadeEnabled", False)   # 根节点显式 false，与样本一致
    out.setdefault("link_config", None)
    out.setdefault("options", [])


# 表格列（表格字段的子列）的 inputType → 表格控件里那一列的控件类型。
# 依据真实表单 project_create 详情（planExcel / budgetExcel / riskExcel）：
#   varchar / clob → type=text    date → type=date
#   number         → type=number  dic  → type=select
# user / department 在真实样本里**没有出现在表单列上**（riskExcel 的「风险责任人」
# 只写在对象定义里），表格单元格目前没有人员选择器；这里降级成 text 保证能渲染，
# 出包时的提示里会说明可以到设计器改。
_TABLE_CELL_TYPE = {
    "varchar": ("text", "text"),
    "clob": ("text", "text"),
    "number": ("number", "number"),
    "date": ("date", "date"),
    "datetime": ("date", "date"),
    "dic": ("select", "select"),
    "user": ("text", "text"),
    "department": ("text", "text"),
}


def _table_cell(col, index: int, scope: str) -> dict:
    """把一列落成 tableField 控件 options.columns 里的一项。

    真实样本里「类型 / 显示类型 / inputType」是分开的三层：type 是单元格控件
    （text/date/select…），displayType 是只读态的呈现方式，inputType 才是对象
    字段的存储类型（clob/varchar/dic…）。少写 inputType，「编辑列」里就认不出原类型。
    """
    typ = col.input_type
    wtype, display = _TABLE_CELL_TYPE.get(typ, ("text", "text"))
    cell: dict = {
        "id": col.col_id(scope, index),
        "name": "",
        "label": col.name,
        "type": wtype,
        "displayType": display,
        "inputType": typ,
        "align": col.align or "left",
        "fixed": "",
        "editable": True,
        "isTree": False,
        "main": False,
        "nameEn": col.name_en or "",
        "required": bool(col.required),
    }
    if col.width_percent is not None:
        w = float(col.width_percent)
        cell["widthPercent"] = int(w) if w == int(w) else w
    if typ == "number":
        cell["precision"] = col.precision if col.precision is not None else 0
    if typ == "datetime":
        cell["datefmt"] = "yyyy-MM-dd HH:mm:ss"
    if typ in ("dic", "user", "department"):
        cell["multiple"] = bool(col.multiple)
    if typ == "dic":
        if col.dict_ref:
            cell["optionSource"] = "DIC_TYPE"
            cell["objectType"] = col.dict_ref
        else:
            cell["optionSource"] = "BO_ATTR_DIC_TYPE"
            cell["objectType"] = None
            if col.choices:
                cell["selectOptions"] = [
                    {"value": c, "label": c, "text": c, "disabled": 0,
                     "dependValues": [], "data": {}} for c in col.choices
                ]
        cell["cascadeEnabled"] = False
        cell["cascadeParentCode"] = ""
    return cell


def _table_configurable(cell: dict) -> dict:
    """「可配置列」那一份：只留 id/标题/类型 + 四个开关。

    真实样本里它与 columns 一一对应、id 相同，但键不同（title/type/forbidConfig*）。
    type 写的是 **inputType**（clob/varchar/dic…），不是单元格控件类型。
    """
    return {
        "id": cell["id"],
        "title": cell["label"],
        "type": cell["inputType"],
        "forbidConfigHidden": False,
        "forbidConfigEditable": False,
        "forbidConfigRequired": False,
        "forbidConfigFilter": False,
    }


def _apply_options(base: dict, field: FormField, parents: set[str] | None = None,
                   bo_code: str = "") -> dict:
    out = copy.deepcopy(base)
    t = field.field_type

    out["required"] = "true" if field.required else "false"
    if field.placeholder:
        out["placeholder"] = field.placeholder
    if field.readonly:
        out["disabled"] = True

    if t == "select":
        if field.value_source:
            out["value_source"] = field.value_source
            out["datasource"] = "valuesource"
        else:
            # 没给来源就清掉骨架里的示例值，否则会残留 project_grade_value 这种
            # 别的对象的数据源——导入后下拉框能点，但列出来的是项目级别
            out.pop("value_source", None)
            out["datasource"] = ""
        out.setdefault("link_config", dict(_LINK_CONFIG_LIST))
        out.setdefault("options", [])
        out.setdefault("multiple", False)
    elif t == "dictionary":
        # 字典控件：字典 key 直接放进 dictionary 键（线上真实结构），
        # 不是 value_source——dictionary 是选项字典，不是值来源数据集
        if field.value_source:
            out["dictionary"] = field.value_source
        out.setdefault("multiple", False)
    elif t == "linkdata":
        if field.value_source:
            out["linkdata"] = field.value_source
        out.setdefault("link_config", dict(_LINK_CONFIG_LIST))
        out.setdefault("multiple", "N")
    elif t == "dropSelector":
        out.setdefault("selector_type", "user")
        out.setdefault("multiple", False)
    elif t == "dropSelectOrg":
        out.setdefault("multiple", False)
    elif t == "dataTemplate":
        # 明细表：把已建好的列表模板嵌进表单（一对多子表）。
        #
        # 键集照抄原生包（项目汇报表 4 个、新建模板测试用例 1 个逐键比对）：
        #   bind_template_key  指向 data_template/*.xml 里的 key
        #   name               再写一遍显示名，缺了子表标题是空的
        #   hide_label/hide_border 恒为 true（子表自带表头，不要外面再套一圈）
        #   heightMode=fixed + heightUnit=px  固定高度模式
        #   mappingFields/casadeFields 没有也要写空数组——省掉导入后子表不渲染
        #   notInputCondition=conditionIsNone 不参与查询条件
        out["bind_template_key"] = field.value_source or field.name
        out["name"] = field.label
        out["hide_label"] = True
        out["hide_border"] = True
        out["hide_border_bottom"] = True
        out["heightMode"] = "fixed"
        out["heightUnit"] = "px"
        out["mappingFields"] = []
        out["casadeFields"] = []
        out["isAlldata"] = False
        out["notInputCondition"] = "conditionIsNone"
        # 内嵌在页面里的形态（原生「未关闭的问题」那一块）：上下留 10px、
        # 不计入外边距、可折叠。这三个键原生里时有时无，写了更贴近页面形态。
        out.setdefault("autoCollapse", True)
        out.setdefault("top_margin", 10)
        out.setdefault("bottom_margin", 10)
        out.setdefault("is_margin", False)
        # 明细表没有占位提示，留骨架里的「请输入」会在子表上方飘一行灰字
        out.pop("placeholder", None)
    elif t == "tableField":
        # 表格字段（子表）：两套列定义都从字段的列快照生成，id 用同一批，
        # 设计器里「编辑列」改的列才认得回哪一列。
        # scope 用「对象编码/字段编码」——与 model.TableColumn.option 算的完全一样，
        # 两处不一致的话对象里列 A、表单里列 B，改一次列就多出一列。
        cols = list(field.table_columns or [])
        scope = f"{bo_code}/{field.name}" if bo_code else field.name
        cells = [_table_cell(c, i, scope) for i, c in enumerate(cols, 1)]
        out["columns"] = cells
        out["configurableColumns"] = [_table_configurable(c) for c in cells]
        out.setdefault("dbFieldType", "clob")
        out.setdefault("operation",
                       {"addEnabled": True, "deleteEnabled": True, "dragEnabled": True})
        out.setdefault("allowConfigRowRight", True)
        out.setdefault("hide_label", True)
        out.setdefault("default_value_type", "fixed")
        out.setdefault("multiSummaryEnabled", False)
        out.setdefault("multiSummaryColumns", [])
        # 注意这里**不要** pop placeholder：原生表格控件的 fieldOptions 里就有
        # "placeholder": "请输入"（planExcel 逐键比对），照抄更稳。


    # 级联下拉：系统只认这两个键。
    # 线上样本（项目表单包含级联关系…zip）里，根节点是 cascadeEnabled=false +
    # 无 cascadeParentCode；只有子节点写 true + 父控件编码。所以这里只在
    # 填了父字段时才打开开关，不填就保持骨架里的 false，避免脏数据。
    if field.cascade_parent:
        out["cascadeEnabled"] = True
        out["cascadeParentCode"] = field.cascade_parent
    if _is_cascade_control(field, parents):
        _dict_cascade_shape(out, field)

    # 显式覆盖优先级最高
    out.update(field.options or {})
    return out


def node_field_meta(form: Form, field: FormField, snip: "Snippets") -> tuple[str, dict, bool]:
    """给「流程节点字段权限」用的控件快照：(真正写进 XML 的控件类型, fieldOptions, 是否兜底)。

    为什么必须由本模块提供、而不是让 gen_workflow 自己拼：
    ``nodePermissions`` 里的 field_type / field_options 要与 form 里那个控件**逐键一致**，
    而「级联父级集合 → 有效控件类型 → 取哪份骨架 → 应用级联形态」是一条链
    （render() 里第 742/755/760/766 行就是这条链）。拆到别的模块去复制一遍，
    将来 gen_form 改了签名不会报错，只会静默生成与表单不一致的 field_options ——
    而「节点上字段全消失」的成因正是这个（docs/流程-分析.md §2 第 6 行）。

    返回的第三个值表示「骨架用了 text 兜底」，调用方据此提示用户核对。
    """
    parents = _cascade_parent_names(form)
    ftype = _effective_type(field, parents)
    base, fallback = snip.field_options(ftype)
    return ftype, _apply_options(base, field, parents, form.bo_code), fallback


def _is_dic_field(field: FormField) -> bool:
    """这个下拉的选项是不是来自字典（层级字典才能级联）。"""
    if field.field_type == "dictionary":
        return True
    return (field.options or {}).get("datasource") == "dictionary"


# 规则里引用的「条件字段」是控件的快照，系统靠它显示「战略类别 不等于 新品」
# 这种人话。类型/图标对不上会让条件显示成一串编码，所以给一份映射表。
_RULE_TYPES: dict[str, tuple[str, str, str]] = {
    "text": ("text", "string", "iform-icon-font"),
    "textarea": ("textarea", "string", "iform-icon-font"),
    "editor": ("editor", "text", "iform-icon-font"),
    "number": ("number", "number", "iform-icon-number"),
    "datePicker": ("datePicker", "date", "iform-icon-calendar"),
    "date": ("datePicker", "date", "iform-icon-calendar"),
    "select": ("select", "string", "iform-icon-radio"),
    "dictionary": ("dictionary", "dic", "iform-icon-radio"),
    "dropSelector": ("dropSelector", "string", "iform-icon-user"),
    "dropSelectOrg": ("dropSelectOrg", "string", "iform-icon-user"),
    "dropSelectGroup": ("dropSelectGroup", "string", "iform-icon-user"),
    "selectObject": ("selectObject", "object", "iform-icon-link"),
    "linkdata": ("linkdata", "object", "iform-icon-link"),
    "autoNumber": ("autoNumber", "string", "iform-icon-font"),
    # 表格字段（子表）作为条件隐藏的对象时，快照里必须是 tableField，
    # 否则设计器把「隐藏 里程碑计划表」显示成一个普通文本框规则。
    # field_data_type 给 string：子表本身是 clob 存的 JSON，可用的算子只有为空/不为空。
    "tableField": ("tableField", "string", "iform-icon-font"),
}


def _rule_field_snapshot(field: FormField) -> dict:
    """生成 formRules[].condition.rules[].field —— 条件字段的完整快照。"""
    dic = _is_dic_field(field)
    ftype, fdata, icon = _RULE_TYPES.get(field.field_type, ("text", "string", "iform-icon-font"))
    if dic:
        ftype, fdata, icon = "dictionary", "dic", "iform-icon-radio"

    opts: dict = {
        "isMultiple": False,
        "isDefault": False,
        "uniqueCheck": False,
        "columns": [],
        "identities": [],
        "format": None,
        "formula": None,
        "cascadeEnabled": bool(field.cascade_parent),
        "cascadeParentCode": field.cascade_parent,
    }
    if dic:
        opts["optionSource"] = "BO_ATTR_DIC_TYPE"
        opts["objectType"] = field.value_source
        opts["dictionary"] = field.value_source
    return {
        "name": field.name,
        "label": field.label,
        "field_type": ftype,
        "field_options": opts,
        "field_data_type": fdata,
        "field_object_type": field.value_source or "",
        "icon": icon,
        "form_rule_source": "object",
        "form_display_label": field.label,
    }


def _resolve_rule_value(bo: Bo | None, source: str, value: str, form: Form,
                        notices: list[str]) -> str:
    """条件值允许填中文选项名，出包时换算成系统里的选项值。

    我们生成的字典项，选项值是 md5 短码（630ddeea 这种），用户填不出来也记不住。
    所以 Excel 里可以写「是」，这里换算成对应短码；换算不了就原样保留
    （比如引用的是线上已有的共享字典，值本来就是 QT/XP 这种人工码）。
    """
    if not value or bo is None:
        return value
    from .gen_bo import dict_choice_codes

    codes = dict_choice_codes(bo, source)
    if not codes:
        return value
    if value in codes:
        return codes[value]
    if value not in codes.values():
        notices.append(
            f"表单 {form.key}: 隐藏规则「{source}」的条件值 {value} 既不是选项名也不是选项值，"
            f"已按原样写入，条件可能永远不成立（可选选项名：{'、'.join(list(codes)[:6])}）"
        )
    return value


def build_rules(form: Form, notices: list[str], bo: Bo | None = None) -> list[dict]:
    """把 HideRule 列表落成 extendAttr.formRules。

    线上表单里，同一个控件挂多条规则时是**并列的多条独立记录**（不是一条里塞
    多个条件），效果是「满足任一即隐藏」，也就是 OR。所以这里一条 HideRule
    对应一条 formRules 记录，需要 OR 就写多条。
    """
    by_name = {f.name: f for f in form.fields}
    parents = _cascade_parent_names(form)
    out: list[dict] = []
    for rule in form.hide_rules:
        target = by_name.get(rule.target)
        source = by_name.get(rule.source)
        if target is None or source is None:
            missing = rule.target if target is None else rule.source
            notices.append(
                f"表单 {form.key}: 隐藏规则引用了不存在的控件 {missing}，"
                f"已跳过「隐藏 {rule.target}」这条"
            )
            continue
        op = rule.operator or "equal"
        if op not in HIDE_OPERATORS:
            notices.append(
                f"表单 {form.key}: 隐藏 {rule.target} 的条件算子 {op} 不认识，已按「等于」处理"
            )
            op = "equal"
        is_empty_op = op in EMPTY_OPERATORS
        resolved = "" if is_empty_op else _resolve_rule_value(
            bo, source.name, rule.value, form, notices)
        out.append({
            "id": short_id(),
            "controlName": target.name,
            "controlLabel": target.label,
            # 目标控件用真正写出去的类型（dic 配了级联会出成 select），
            # 条件字段的快照仍用字典语义（线上样本就是这么存的）
            "controlType": _effective_type(target, parents),
            "action": "hidden",
            "condition": {
                "type": "group",
                "rules": [{
                    "field": _rule_field_snapshot(source),
                    # 为空/不为空 不需要值，线上样本里就是 null
                    "value": None if is_empty_op else (resolved or None),
                    "operator": op,
                    "rule_condition": "and",
                    "rule_id": short_id(),
                    "operator_label": LABEL_BY_OP.get(op, op),
                    "value_label": "" if is_empty_op else (rule.value_label or rule.value),
                }],
            },
            "value": "true",
            "clearOnHide": "global" if rule.clear_on_hide else "none",
        })
    return out


def _rows_within(fields: list[FormField], form: Form) -> list[list[FormField]]:
    """一组字段切成行，**每行最多 2 个**（见 ``_max_per_row``）。

    规则：
      - 普通控件两两一行
      - 多行文本/附件/子表单等（``_FULL_ROW_TYPES``）独占一行
      - 标了 ``newline`` 的字段强制起新行
      - ``layout == 1`` 时每个字段各占一行
    """
    limit = _max_per_row(form)
    rows: list[list[FormField]] = []
    cur: list[FormField] = []
    for f in fields:
        full = f.full_row or f.field_type in _FULL_ROW_TYPES
        if f.newline and cur:
            rows.append(cur)
            cur = []
        if form.layout == 1 or full:
            if cur:
                rows.append(cur)
                cur = []
            rows.append([f])
            continue
        cur.append(f)
        if len(cur) >= limit:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    return rows


def _plan_groups(form: Form) -> list[tuple[str, list[list[FormField]]]]:
    """按「分区」把控件分组，每组再切行。

    分区取控件的 ``group``，同名合并到同一组、顺序按首次出现；
    全空则整张表单只有一个分区，标题「基本信息」。
    返回 [(分区标题, 行列表)]。
    """
    order: list[str] = []
    buckets: dict[str, list[FormField]] = {}
    for f in form.fields:
        # 先落成标题再分桶：分区留空的并到「基本信息」里，
        # 否则会出两个都叫基本信息、却分在两处的容器
        g = (f.group or "").strip() or _DEFAULT_GROUP
        if g not in buckets:
            buckets[g] = []
            order.append(g)
        buckets[g].append(f)
    out: list[tuple[str, list[list[FormField]]]] = []
    for g in order:
        out.append((g, _rows_within(buckets[g], form)))
    return out


def _plan_rows(form: Form) -> list[list[FormField]]:
    """兼容旧调用：拍平所有分区的行（不看分区边界的行合并）。"""
    return [row for _, rows in _plan_groups(form) for row in rows]


def render(form: Form, bo: Bo | None, bo_id: str, snip: Snippets,
           tenant: str = "-999", extend_patch: dict | None = None,
           script: str = "") -> tuple[str, list[str]]:
    """返回 (xml 文本, 提示列表)。"""
    notices: list[str] = []
    # 流程节点/处理人/定义/实例/状态只用于流程反写，不渲染成可见控件；
    # 下面 _HIDDEN_FIELDS 会补 hidden 控件（不改对象字段本身）。
    form.fields = [f for f in form.fields if f.name not in _PROCESS_CODES]
    _inherit_cascade(form, bo, notices)
    _inherit_table_columns(form, bo, notices)
    # 库里有同 key 的模板 → 沿用它的 id，导入才是「更新」而不是又建一个同名表单
    form_id = form.exist_key or new_id()

    ext = snip.extend_attr
    # 骨架来自某个具体表单，其中的自定义脚本/校验规则指向它的字段，必须剔除
    for risky in ("script", "formRules", "verifys"):
        ext.pop(risky, None)
    # 骨架里的 moreBtn 是「悬浮更多按钮」——原生**详情表单**才用它
    # （顶部按钮收进一颗悬浮按钮里，悬停才展开）。新建表单的原生导出包
    # 都没有这个键，只有 buttons 那一组底部按钮。
    # 我们在下面保留了完整 buttons，再留着 moreBtn 就会在右下角多出一块
    # 空白热区，鼠标移上去展开「取消 / 保存并继续 / 保存」，和底部按钮重复
    # （2026-11-16 用户在 8112 测试-sjz 新建页看到的现象）。
    ext.pop("moreBtn", None)

    # 条件隐藏：extendAttr.formRules。没规则就写空数组（跟线上一致），
    # 有规则才打开 clearOnHide 开关。
    ext["formRules"] = build_rules(form, notices, bo)
    if form.hide_rules:
        ext["formRuleClearOnHide"] = any(r.clear_on_hide for r in form.hide_rules)
    # 隐藏字段的计数口径，线上五个真实表单都是 count，照抄
    ext.setdefault("setHiddenField", "count")

    # 手册：按钮位置右下角，仅「保存」用主要-蓝色，其余次要-灰色；
    # 只保留 取消 / 保存并继续 / 保存 三个标准按钮
    kept = []
    for btn in ext.get("buttons") or []:
        if btn.get("alias") not in ("close", "saveAndContinue", "save"):
            continue
        btn = dict(btn)
        btn["style"] = "primary" if btn.get("alias") == "save" else "default"
        attrs_ = btn.get("attrs")
        if isinstance(attrs_, dict):
            if btn["style"] == "primary":
                attrs_.update({"backgroundColor": "#3388FF", "border_color": "#3388FF", "color": "#ffffff"})
            else:
                attrs_.update({"backgroundColor": "#ffffff00", "border_color": "#DDE1EB", "color": "#223355"})
        kept.append(btn)
    if kept:
        ext["buttons"] = kept

    # 手册：标签宽度按表单类型取（新建 106 / 条件专用 160），弹窗为百分比宽度
    ext["labelWidth"] = form.resolved_label_width()
    ext["openType"] = ext.get("openType") or "drawer"

    # ---- 按表单类型套样式（PP6/搭建规范手册.pdf「表单属性的配置」）
    # 四类表单的 class / 尺寸 / 按钮位置都不同，不能共用一套 extendAttr。
    style = form_style(form.kind)
    ext["customClass"] = style["customClass"]
    ext["labelWidth"] = style["labelWidth"]
    ext["openType"] = style["openType"]
    ext["width"] = style["width"]
    ext["widthUnit"] = "%"
    ext["buttonPosition"] = style["buttonPosition"]
    # 布局：紧凑=compact / 展开=expand（手册详情页有这两种）
    ext["compact"] = style["layoutMode"] == "compact"
    # 单行编辑模式（详情表单要开）
    ext["form_item_eidt_mode"] = bool(style["singleLineEdit"])

    # 表单脚本：脚本方案 PDF 说明 JForm 必须挂在 extendAttr.script 上
    if script:
        ext["script"] = script
    ext.update(extend_patch or {})

    form_def = wrap(
        "formDef",
        [el("tenantId", "", tenant, indent=3)],
        {
            "id": form_id,
            "name": form.name,
            "key": form.key,
            "mode": "bo",
            "status": form.status or "deploy",
            "typeId": form.type_id,
            "formType": form.form_type or "form",
            "shared": "Y",
            "isMain": "Y",
            "version": "1",
            "extendAttr": json_attr(ext),
        },
        indent=2,
    )

    groups = _plan_groups(form)

    # 明细表控件从所在分区里摘出来，各自单独成一个分区。
    # 原生表单就是这么排的：项目汇报表里 7 个 fieldset 装普通控件、
    # 4 个明细表各自成块；新建模板测试用例的明细表也不跟别的字段同框。
    # 不拆开的话，「明细表 + 普通控件」会共用一层 grid_layout，
    # 而 grid_layout 里放 dataTemplate 系统不渲染（见下面 direct 分支注释）。
    _split: list[tuple[str, list[list[FormField]]]] = []
    for _title, _rows in groups:
        _plain = [r for r in _rows if not _is_direct_row(r)]
        _dms = [r for r in _rows if _is_direct_row(r)]
        if _plain:
            _split.append((_title, _plain))
        for _r in _dms:
            # 分区标题用控件自己的标签，跟原生一致（明细表那一块就叫控件名）
            _split.append((_r[0].label or _title, [_r]))
    groups = _split

    # 拖拽布局 V1.0：不按业务分组套 fieldset，普通控件全部进**一个根 grid_layout**；
    # 明细表/表格字段仍各自一个容器（它们不能进 grid_layout，会被系统忽略）。
    flat = form.layout_style == "flat"
    if flat:
        _plain_all = [r for _t, _rows in groups for r in _rows if not _is_direct_row(r)]
        _direct_all = [(_r[0].label or _t, [_r])
                       for _t, _rows in groups for _r in _rows if _is_direct_row(_r)]
        groups = ([("", _plain_all)] if _plain_all else []) + _direct_all

    container_type = _CONTAINER_BY_KIND.get(form.kind, "fieldset")
    bo_code = bo.code if bo else form.bo_code
    bo_fields = {bf.code: bf for bf in (bo.fields if bo else [])}

    body: list[str] = [form_def]
    sn = 1

    # 隐藏字段挂在根上（与线上一致：id / version 不在任何分区里）
    hidden_names = []
    # 只有显式「补隐藏字段=是」才补。流程字段也**不再**自动塞进表单：
    # 线上 pp_project_create 的两张表单里一个 process* 控件都没有（2026-11-17
    # 核对 config-sync-20261113150255），流程值由服务端按对象字段反写。
    need_hidden = form.auto_hidden
    for which, label in _HIDDEN_FIELDS:
        if not need_hidden:
            continue
        if any(f.name == which for f in form.fields):
            continue
        hidden_names.append(which)
        body.append(el("formField", {
            "id": new_id(),
            "formId": form_id,
            "name": which,
            "label": label,
            "fieldType": "hidden",
            "fieldOptions": json_attr(snip.hidden_options(which)),
            "sn": sn,
        }, indent=2))
        sn += 1

    parents = _cascade_parent_names(form)

    for gi, (title, rows) in enumerate(groups, start=1):
        # flat 模式下第一组（普通控件）直接挂根 grid_layout，不套 fieldset
        no_container = flat and gi == 1 and not title
        # 明细表（dataTemplate）/ 表格字段（tableField）单独成容器：原生表单里它们
        # **不经过** grid_layout，直接挂在容器的 columns 上（项目汇报表里 4 个明细表、
        # project_create 详情里 3 个表格字段，容器 columns 引用的就是控件 id 本身）。
        # 塞进 grid_layout 的 rows 里系统不认，子表整块不渲染——
        # 这跟普通控件「两条链路都要写」正相反。
        direct = len(rows) == 1 and _is_direct_row(rows[0])

        container_id, grid_id = new_id(), new_id()
        # 直挂模式下控件 id 要先定：容器的 columns 要引用它
        dm_cid = new_id() if direct else ""

        # ---- 分区容器：card（详情）/ fieldset（新建等）
        if not no_container:
            c_opts = snip.layout(container_type)
            c_opts.setdefault("clearable", True)
            c_opts.setdefault("controls", True)
            c_opts.setdefault("selection", True)
            c_opts.setdefault("mobile", True)
            c_opts.setdefault("autoCollapse", True)
            if container_type == "fieldset":
                c_opts.setdefault("border_style", "none")
                c_opts.setdefault("position", "left")
                c_opts.setdefault("type", "default")
                # 线上立项表单：首个分组不加间距，其余 top_margin=20
                if gi > 1:
                    c_opts["is_margin"] = True
                    c_opts["top_margin"] = 20
            c_opts["columns"] = [{
                "name": None, "label": None,
                "fields": [dm_cid if direct else grid_id], "span": "",
            }]
            body.append(el("formField", {
                "id": container_id,
                "formId": form_id,
                "name": ("card_" if container_type == "card" else "fieldset_") + short_id(),
                "label": title,
                "fieldType": container_type,
                "fieldOptions": json_attr(c_opts),
                "sn": sn,
            }, indent=2))
        sn += 1

        if direct:
            # 明细表 / 表格字段：容器下只有一个控件，没有布局层
            f = rows[0][0]
            dft = _effective_type(f, parents)
            opts, fallback = snip.field_options(dft)
            if fallback:
                notices.append(
                    f"控件 {f.name}: 样例中没有 {dft} 类型骨架，已用 text 兜底，导入后请核对"
                )
            if dft == "tableField" and not (f.table_columns or []):
                notices.append(
                    f"控件 {f.name}: 表格字段一列都没有，导入后子表是空白——"
                    f"请到「2.1_表格列」补列定义")
            body.append(el("formField", {
                "id": dm_cid,
                "formId": form_id,
                "parentId": container_id,
                "name": f.name,
                "label": f.label,
                "desc": f.desc or "",
                "fieldType": dft,
                "fieldOptions": json_attr(_apply_options(opts, f, parents, bo_code)),
                "sn": 1,
            }, indent=2))
            continue

        # ---- 表格布局V1.0：columns（扁平引用）+ rows（内嵌完整控件 JSON）
        flat_cols: list[dict] = []
        out_rows: list[dict] = []
        leaf: list[tuple[FormField, str, dict]] = []   # (字段, 控件id, fieldOptions)

        for row in rows:
            row_id = short_id()
            cells: list[dict] = []
            for f in row:
                cell_id = short_id()
                span = _span_of(f, form, alone=(len(row) == 1))
                ftype = _effective_type(f, parents)
                if ftype != f.field_type:
                    # 链上的父级自己没填父级，只说「配了级联父级」会显示成空的
                    if f.cascade_parent:
                        why = f"配了级联父级 {f.cascade_parent}"
                    else:
                        kids = [x.name for x in form.fields if x.cascade_parent == f.name]
                        why = f"是 {'、'.join(kids)} 的级联父级" if kids else "在级联链上"
                    notices.append(
                        f"控件 {f.name}: {why}，"
                        f"控件类型已按线上样本从「{f.field_type}」出成「select + 选项来源=数据字典」"
                        f"（树形字典控件没有级联概念）"
                    )
                opts, fallback = snip.field_options(ftype)
                if fallback:
                    notices.append(
                        f"控件 {f.name}: 样例中没有 {ftype} 类型骨架，已用 text 兜底，导入后请核对"
                    )
                fo = _apply_options(opts, f, parents, bo_code)
                bf = bo_fields.get(f.name)
                cell_id_el = new_id()
                cells.append({
                    "id": cell_id,
                    "span": span,
                    "colSpan": 1,
                    "fields": [_widget_json(f, ftype, fo, bo_code, cell_id,
                                            bf.resolved_field_name() if bf else "")],
                })
                flat_cols.append({
                    "name": None, "label": "单元格",
                    "fields": [cell_id_el], "span": span,
                })
                leaf.append((f, cell_id_el, fo, ftype))
            out_rows.append({"id": row_id, "columns": cells})

        g_opts = snip.layout("grid_layout")
        g_opts.setdefault("clearable", True)
        g_opts.setdefault("controls", True)
        g_opts.setdefault("selection", True)
        g_opts.setdefault("mobile", True)
        g_opts.setdefault("autoCollapse", True)
        g_opts["columns"] = flat_cols
        g_opts["rows"] = out_rows
        body.append(el("formField", {
            "id": grid_id,
            "formId": form_id,
            # flat：根 grid_layout 直接挂 formDef，parentId 不写
            "parentId": None if no_container else container_id,
            "name": "gridLayout_" + short_id(),
            "label": "表格布局V1.0",
            "desc": "",
            "fieldType": "grid_layout",
            "fieldOptions": json_attr(g_opts),
            "sn": 1,
        }, indent=2))

        # ---- 控件本体：parentId 指向布局控件，sn 在布局内 1..N
        for leaf_sn, (f, cid, fo, ftype) in enumerate(leaf, start=1):
            body.append(el("formField", {
                "id": cid,
                "formId": form_id,
                "parentId": grid_id,
                "name": f.name,
                "label": f.label,
                "desc": f.desc or "",
                "fieldType": ftype,
                "fieldOptions": json_attr(fo),
                "sn": leaf_sn,
            }, indent=2))

    if bo is not None:
        body.append(el("formBo", {
            "id": new_id(), "formId": form_id, "boId": bo_id, "boCode": bo.code,
        }, indent=2))
    else:
        notices.append(f"表单 {form.key}: 未找到绑定对象 {form.bo_code}，已跳过 formBo")

    def rights(name: str, label: str, field_type: str | None) -> str:
        perm = {
            "field_type": field_type,
            "label": label,
            "read": [{"type": "none", "rightsId": "", "rightsName": ""}],
            "edit": [{"type": "all", "rightsId": "", "rightsName": ""}],
            "required": [{"type": "none"}],
            "hide": [{"type": "none"}],
        }
        return wrap("formRights", [el("tenantId", "", tenant, indent=3)], {
            "id": new_id(),
            "formKey": form.key,
            "code": bo.code if bo else form.bo_code,
            "name": name,
            "permission": json_attr(perm),
            "type": "field",
            "rightsType": "form",
            "flowKey": "",
            "nodeId": "",
            "parentFlowKey": "",
        }, indent=2)

    body.append(rights("_p_title", "表单标题", None))
    for which in hidden_names:
        body.append(rights(which, which, "hidden"))
    for _, rows in groups:
        for group in rows:
            for f in group:
                # 权限里的 field_type 要和真正写出去的控件类型一致，否则设计器里
                # 打开字段权限会显示成另一种控件
                body.append(rights(f.name, f.label, _effective_type(f, parents)))

    xml = "\n".join([
        DECL_STANDALONE,
        "<form>",
        "  <formDefs>",
        *body,
        "  </formDefs>",
        "</form>",
    ]) + "\n"
    return xml, notices
