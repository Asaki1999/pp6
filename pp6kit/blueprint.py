"""搭建蓝图：按《搭建规范手册》把一个业务对象展开成标准表单组。

手册定义的搭建顺序：
    业务对象 → 新建表单 → 详情表单 → 条件专用表单 → 导入专用表单（按需）→ 多维表格

其中「条件专用表单」有明确的生成规则：
    该表单预先配置好对象所有字段的显示类型，供自动化等场景直接取用。
      · 日期字段      → 日期控件
      · 人员字段      → 选择器-人员
      · 组织字段      → 选择器-部门
      · 关联/字典字段 → 下拉框（父子关系的关联对象用关联数据）
      · 其余          → 单行文本
    命名固定为「条件专用_{对象名称}」。
"""
from __future__ import annotations

from dataclasses import replace

from .model import (
    FORM_BUNDLE,
    FORM_CONDITION,
    FORM_CREATE,
    FORM_DETAIL,
    FORM_IMPORT,
    Bo,
    DataSet,
    Form,
    FormField,
    Project,
)
from .modules import DS_VALUE

# 条件专用表单的控件映射（对照线上系统 Version 6.0 的真实控件名）
# 之前 datetime→date、dic→select 是错的：线上真实控件名是 datePicker / dictionary。
# 2026-10 对照线上系统 iform_form_field 表 68 种控件校正。
_CONTROL_BY_TYPE = {
    "datetime": "datePicker",       # 线上是 datePicker，不是 date
    "user": "dropSelector",
    "department": "dropSelectOrg",
    "object": "select",             # 关联对象用下拉（树形的在 control_for 里改 linkdata）
    "dic": "dictionary",            # 选项/字典用 dictionary，不是 select
    "number": "number",
    "attachment": "attachment",
    "autoNumber": "autoNumber",
    "clob": "textarea",             # 大文本用多行文本，不是单行 text
    # 以下类型没有专用控件，退化为单行文本
    "i18n": "text",
    "formula": "text",
    # 表格 = 子表（明细行）。真实控件名是 **tableField**，不是 dataTemplate。
    # 依据：project_create 的 3 个表格字段（planExcel / budgetExcel / riskExcel）
    # 在 form/002-project_create_detail_.xml 里全是 fieldType="tableField"，
    # 控件自己带 columns/configurableColumns 两套列定义。
    # dataTemplate 是「把 5_列表 的列表模板嵌进表单」，和表格字段无关——
    # 以前把 table 映射成 dataTemplate，结果子表挂到一个不相干的列表上，整块是空的。
    "table": "tableField",
}

# 流程冗余字段 / 流程状态是系统反写用的，不在表单上给用户填。
PROCESS_FIELD_CODES = {"processNode", "processHandler", "processDefId",
                       "processInstanceId", "processState"}

_NAME_TEMPLATE = {
    FORM_CREATE: "新建{obj}",
    FORM_DETAIL: "{obj}详情",
    FORM_CONDITION: "条件专用_{obj}",
    FORM_IMPORT: "导入专用_{obj}",
}

_KEY_TEMPLATE = {
    FORM_CREATE: "create_{code}",
    FORM_DETAIL: "detail_{code}",
    # 平台惯例：业务对象 options.formKey 指向这张表，样例里是 fetch_basic_project_information
    FORM_CONDITION: "fetch_basic_{code}_information",
    FORM_IMPORT: "import_{code}",
}


def control_for(field) -> str:
    """按字段类型挑控件；标记为树形的关联对象用关联数据。"""
    if field.type_new == "object" and field.options.get("tree") is True:
        return "linkdata"
    return _CONTROL_BY_TYPE.get(field.type_new, "text")


def fields_from_bo(bo: Bo, kind: str, form_key: str = "") -> list[FormField]:
    """从对象字段直接生成控件。

    控件类型一律走 control_for（按字段类型映射）：
    以前写成 `control_for(f) if kind == FORM_CONDITION else "text"`，
    只有条件专用表单是对的，新建/详情/导入表单全被硬编码成 text——
    后果就是选项字段在界面里是个**文本框**（用户截图里「选项值」显示中文、
    下拉不出来、也没法选），日期字段也是.text，整张表单等于废的。

    2026-10-29 修正：所有 kind 都用 control_for。
    """
    out: list[FormField] = []
    for f in sorted(bo.fields, key=lambda x: x.sn):
        if f.code in PROCESS_FIELD_CODES:
            continue
        out.append(
            FormField(
                form_key=form_key,
                name=f.code,
                label=f.name,
                field_type=control_for(f),
                # 子表横向铺满：真实表单里表格控件独占一行（连 grid_layout 都不进）
                full_row=f.type_new in ("clob", "table"),
                # 取值来源只有 BoField.value_source() 一处真相：
                # 全局字典（dict_ref）与自定义字典（dict_key）在这里分流，
                # 三处各写一遍「dic → dict_key()」时全局字典必然指向空下拉。
                value_source=f.value_source(),
                # 表格字段的列随控件走：Excel/界面里改的列定义要在表单上体现
                table_columns=list(f.table_columns or []),
            )
        )
    return out


def _make(bo: Bo, kind: str, src: Form | None, group_src: Form | None = None) -> Form:
    key = _KEY_TEMPLATE[kind].format(code=bo.code)
    form = Form(
        key=key,
        name=_NAME_TEMPLATE[kind].format(obj=bo.name),
        bo_code=bo.code,
        form_type="form",
        layout=src.layout if src else 2,
        type_id=src.type_id if src else "",
        status=src.status if src else "deploy",
        width=src.width if src else 90,
        kind=kind,
        auto_hidden=src.auto_hidden if src else True,
    )
    if kind == FORM_CONDITION:
        form.fields = fields_from_bo(bo, FORM_CONDITION, key)
    elif src is not None and src.fields:
        form.fields = [replace(f, form_key=key) for f in src.fields]
    else:
        form.fields = fields_from_bo(bo, FORM_DETAIL, key)

    # 新建表单里填的「分区」按字段编码带到详情/条件专用表单，
    # 这样一套表单的分组名字是一致的，不用在三个地方各填一遍。
    # 顺带按分区分组排序：详情表单的字段是按对象 sn 出的，分组会交错，
    # 排一下才和新建表单看起来是一套东西。
    if group_src is not None and group_src.fields:
        by_name = {f.name: f.group for f in group_src.fields if f.group}
        for f in form.fields:
            if not f.group:
                f.group = by_name.get(f.name, "")
        order: dict[str, int] = {}
        for f in group_src.fields:
            order.setdefault(f.group or "", len(order))
        form.fields.sort(key=lambda f: order.get(f.group, len(order)))
    return form


def expand(project: Project) -> list[str]:
    """把「标准全套」展开成新建/详情/条件专用三张表单。"""
    notices: list[str] = []
    existing = {(f.bo_code, f.kind) for f in project.forms if f.kind != FORM_BUNDLE}
    added: list[Form] = []

    for form in list(project.forms):
        if form.kind != FORM_BUNDLE:
            continue
        bo = project.bo(form.bo_code)
        if bo is None:
            notices.append(f"表单 {form.key}: 标准全套找不到对象 {form.bo_code}，已跳过")
            continue
        for kind in (FORM_CREATE, FORM_DETAIL, FORM_CONDITION):
            if (bo.code, kind) in existing:
                notices.append(f"对象 {bo.code}: 已存在 {kind} 表单，跳过自动生成")
                continue
            added.append(_make(bo, kind, form if kind == FORM_CREATE else None,
                               group_src=form))
            existing.add((bo.code, kind))
        notices.append(f"对象 {bo.code}: 已按规范展开为标准全套（新建 / 详情 / 条件专用）")

    project.forms = [f for f in project.forms if f.kind != FORM_BUNDLE] + added
    notices.extend(_ensure_value_sources(project))
    return notices


def _ensure_value_sources(project: Project) -> list[str]:
    """为 object 字段自动建「值来源」数据集，并把下拉控件接到它上面。

    为什么必须有这一步（2026-11-11 查 crsanjiu 库得出的结论）：
    真实系统 select 控件的取数 97% 来自 valueSource 数据集——控件的
    field_options.value_source 存的是数据集 key（如 project_grade_value），
    而不是对象编码；每个被引用的对象都有配套的值来源数据集（库里 154 个）。
    app 之前把控件 value_source 填成对象编码（fields_from_bo），却没有任何
    数据集支撑，导入后下拉引用悬空、取不到数。

    规则：
    - 同一个目标对象只建一个数据集（多个字段共享，真实库 `project` 被 39 个控件共用）
    - 控件的 value_source 只有「恰好等于目标对象编码」的才替换（那是我们自己的
      默认填法）；用户手填或反解来的数据集 key 不动
    - 目标对象不存在（既不在本包也不在系统清单）不建，避免悬空数据集
    """
    from . import objects as _objects

    notices: list[str] = []
    ds_keys = {d.key for d in project.datasets}
    pkg_codes = {b.code for b in project.bos}
    names = {it["code"]: (it.get("name") or it["code"]) for it in _objects.catalog()}
    for b in project.bos:
        names[b.code] = b.name

    # 目标对象 → 首个引用它的字段（用来算 key 和提示）
    refs: dict[str, Bo] = {}          # dst -> 首个引用字段
    ordered: list[tuple[Bo, object]] = []
    for bo in project.bos:
        for f in bo.fields:
            if f.type_new != "object" or not f.object_type or f.object_type == bo.code:
                continue              # 自关联（上级项目）下拉走对象自身，不另建
            if f.object_type not in pkg_codes and f.object_type not in names:
                continue              # 归一失败的目标，建了也是悬空
            refs.setdefault(f.object_type, bo)
            ordered.append((bo, f))
    if not ordered:
        return notices

    # 系统库里已有的数据集 key（refresh 时拉的 iform_data_dataset.key_）。
    # 真实系统自带 97 个 <对象>_value 数据集（project/customer/contract/... 全都有）
    # ——引用这些对象时**根本不该再建**，控件直接指向系统已有的数据集即可，
    # 否则导入撞 key 报错。离线/无清单时为空，保持本包内查重。
    sys_ds_keys: set[str] = set()
    try:
        from . import objects as _objects
        sys_ds_keys = set(_objects.extra().get("datasets") or [])
    except Exception:
        sys_ds_keys = set()

    # 已经指向某个已存在数据集的控件：这些字段不再动（反解来的包、用户手配的）。
    # 本包与系统库的 key 都算「已存在」——反解真实包的控件指向的是系统数据集。
    wired: set[str] = set()
    all_known = ds_keys | sys_ds_keys
    for form in project.forms:
        bo = project.bo(form.bo_code)
        if bo is None:
            continue
        for ff in form.fields:
            if not ff.value_source:
                continue
            bf = next((x for x in bo.fields if x.code == ff.name or x.name == ff.name), None)
            if bf is None or bf.type_new != "object" or not bf.object_type:
                continue
            if ff.value_source in all_known and ff.value_source != bf.object_type:
                # 已指向真实数据集 key（≠对象编码默认填法）→ 用户配置/反解来的，不动。
                # value_source 恰好等于对象编码的仍走下面的 key_of 流程：
                # 那是 blueprint 的默认填法，要按「本包/系统是否已有数据集」决定
                # 是引用还是替换。
                wired.add(bf.object_type)

    def _key_for(dst: str) -> str:
        # 已有现成数据集（本包 key、系统 key、对象编码本身）→ 直接复用，不重复建
        for candidate in (f"{dst}_value", dst):
            if candidate in ds_keys or candidate in sys_ds_keys:
                return candidate
        return f"{dst}_value"          # 新建（到此为止它必然不存在，不会冲突）

    key_of: dict[str, str] = {}
    created: list[str] = []
    reused: list[str] = []
    for dst in sorted(refs):
        if dst in wired:
            continue              # 控件已经指向数据集了，不要再建/再接
        key = _key_for(dst)
        if key in ds_keys:
            key_of[dst] = key     # 本包已有，复用
        elif key in sys_ds_keys:
            key_of[dst] = key     # 系统库里已有（真实系统自带 <obj>_value），直接引用不建
            reused.append(key)
        else:
            ds = DataSet(key=key, name=f"{names.get(dst, dst)}值来源",
                         from_table=dst, ds_type=DS_VALUE)
            project.datasets.append(ds)
            ds_keys.add(key)
            created.append(f"{ds.name}({key})")
        key_of[dst] = key
    if created:
        notices.append(
            "已自动创建值来源数据集 " + "、".join(created)
            + "：关联对象的下拉框要靠它取数（真实系统的做法）")
    if reused:
        notices.append(
            "引用系统已有数据集 " + "、".join(sorted(set(reused)))
            + "（系统自带值来源，不重复创建）")

    # 接线：控件 value_source 恰好等于目标对象编码（blueprint 的默认填法）→ 指向数据集。
    # 用户手填的数据集 key、反解来的 key 都不等于对象编码，天然不受影响。
    for form in project.forms:
        bo = project.bo(form.bo_code)
        if bo is None:
            continue
        for ff in form.fields:
            if ff.field_type not in ("select", "linkdata") or not ff.value_source:
                continue
            bf = next((x for x in bo.fields if x.code == ff.name or x.name == ff.name), None)
            if bf is None or bf.type_new != "object" or not bf.object_type:
                continue
            if ff.value_source == bf.object_type and bf.object_type in key_of:
                ff.value_source = key_of[bf.object_type]
    return notices
