"""生成 Excel 模板（也是 inspect 反解的输出载体）。"""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.formatting.rule import Rule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.styles.differential import DifferentialStyle
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import fieldtypes, spec
from .model import LABEL_BY_OP, Project

HEADER_FILL = PatternFill("solid", fgColor="2F5597")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=16, color="1F3864")
SECTION_FONT = Font(bold=True, size=11, color="2F5597")
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
NOTE_FONT = Font(size=9, color="7F7F7F")
PICK_FILL = PatternFill("solid", fgColor="FFF2CC")   # 浅黄：提示这里要填


def _settings_rows(proj: Project) -> list[tuple[str, str, str]]:
    values = {
        "租户ID": proj.tenant_id,
        "文件前缀": proj.file_prefix,
        "默认模块ID": proj.module_id,
        "默认分类ID": proj.type_id,
        "输出目录": proj.out_dir,
    }
    return [(k, values.get(k, ""), note) for k, _default, note in spec.SETTING_ROWS]


def sheet_data(proj: Project) -> dict[str, list[list]]:
    """Project -> 各表的数据行（不含表头）。"""
    out: dict[str, list[list]] = {k: [] for k in spec.COLUMNS}
    out[spec.S_SETTINGS] = [list(r) for r in _settings_rows(proj)]

    for bo in proj.bos:
        out[spec.S_BO].append([
            bo.code, bo.name, bo.tbl_name, bo.bo_type, bo.module_id, bo.type_id,
            bo.pk, "是" if bo.with_log else "否", "是" if bo.flow_driven else "否",
            bo.parent_obj, bo.parent_field, bo.rel_type or "PARENT", bo.desc,
        ])
        for f in sorted(bo.fields, key=lambda x: x.sn):
            # 类型列写系统界面的叫法（单行文本/数字/日期控件…），
            # 和下拉选项里看到的词一致，用户改起来不用翻译
            type_text = fieldtypes.ui_label(f.type_new)
            out[spec.S_FIELD].append([
                bo.code, f.sn, f.code, f.field_name, f.name, type_text, f.object_type,
                # 选项值：一行一个（Excel 里 Alt+Enter 换行），比逗号串好读好改
                "\n".join(f.choices),
                f.cascade_parent,
                f.length, f.precision,
                "是" if f.is_index else "否", "是" if f.is_null else "否",
                f.default_value, _dump_json(f.options), f.desc,
            ])

    for form in proj.forms:
        out[spec.S_FORM].append([
            form.key, form.name, form.bo_code,
            _KIND_TO_LABEL.get(form.kind, form.kind),
            "一行一列" if form.layout == 1 else "一行两列", form.type_id, form.status,
            form.width, form.label_width or "", "是" if form.auto_hidden else "否",
        ])
        for ff in form.fields:
            out[spec.S_FORMFIELD].append([
                ff.form_key, ff.name, ff.label, ff.field_type, ff.value_source, ff.placeholder,
                "是" if ff.required else "否", "是" if ff.readonly else "否",
                "是" if ff.full_row else "否", "是" if ff.newline else "否",
                ff.group, ff.cascade_parent, _dump_json(ff.options),
            ])

    for lst in proj.lists:
        out[spec.S_LIST].append([
            lst.key, lst.name, lst.bo_code, ",".join(lst.views), ",".join(lst.columns),
            lst.sort_field, lst.sort_dir, "是" if lst.tree else "否", lst.parent_field, lst.title_field,
        ])

    for ds in proj.datasets:
        out[spec.S_DATASET].append([
            ds.key, ds.name, ds.from_table, ds.ds_type, ds.unique_field, ds.id_field, ds.name_field,
        ])

    for fkey, script in proj.form_scripts.items():
        out[spec.S_SCRIPT].append([fkey, "", script])

    # 条件隐藏规则：一行一条，「满足任一即隐藏」靠写多行表达
    for form in proj.forms:
        for rule in form.hide_rules:
            out[spec.S_HIDE].append([
                form.key, rule.target, rule.source,
                LABEL_BY_OP.get(rule.operator, rule.operator),
                rule.value, rule.value_label,
                "是" if rule.clear_on_hide else "否",
            ])
    return out


_KIND_TO_LABEL = {
    "create": "新建表单", "detail": "详情表单", "condition": "条件专用表单",
    "import": "导入专用表单", "bundle": "标准全套",
}


def _dump_json(value: dict) -> str:
    if not value:
        return ""
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _style_sheet(ws, cols: list[spec.Col]) -> list[int]:
    """画表头 + 加下拉。返回「需要换行对齐」的列号，由调用方在写完数据后统一刷。"""
    wrap_idx: list[int] = []
    for idx, col in enumerate(cols, start=1):
        letter = get_column_letter(idx)
        ws.column_dimensions[letter].width = col.width
        cell = ws.cell(row=1, column=idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
        if col.note:
            cell.comment = None
            ws.cell(row=1, column=idx).value = col.header
        if col.choices:
            dv = DataValidation(
                type="list",
                formula1='"' + ",".join(col.choices) + '"',
                allow_blank=True,
                showDropDown=False,
            )
            ws.add_data_validation(dv)
            dv.add(f"{letter}2:{letter}500")
        if col.wrap:
            wrap_idx.append(idx)
    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}1"
    return wrap_idx


def _apply_wrap(ws, wrap_idx: list[int]) -> None:
    """数据写完后再刷换行列的样式，避免提前创建空单元格撑大 max_row。"""
    if not wrap_idx or ws.max_row < 3:
        return
    for idx in wrap_idx:
        for row in range(3, ws.max_row + 1):
            c = ws.cell(row=row, column=idx)
            c.alignment = Alignment(vertical="top", wrap_text=True)


def _link_option_col(ws, cols: list[spec.Col]) -> None:
    """「数据类型」选了选项类时，让「选项值」列变黄提示必填。

    只做视觉提示，不做硬校验——用户可能想先填类型后补选项值，
    直接弹错误框会打断填写。真正漏填由 check 阶段报出来。
    """
    names = {c.key: get_column_letter(i) for i, c in enumerate(cols, start=1)}
    type_col = names.get("type_new")
    opt_col = names.get("options_text")
    if not type_col or not opt_col:
        return

    # 选项类类型的下拉文案（与 fieldtypes.choices() 一致，字段层只有「选项」）
    dic_labels = ["选项（dic）", "选项", "下拉框（object）", "下拉框"]

    for label in dic_labels:
        dxf = DifferentialStyle(fill=PatternFill(bgColor="FFF2CC"))
        rule = Rule(
            type="expression",
            dxf=dxf,
            # 类型列等于该选项 且 选项值列是空的 → 高亮
            formula=[f'AND(${type_col}2="{label}",LEN(TRIM(${opt_col}2))=0)'],
        )
        ws.conditional_formatting.add(f"{opt_col}2:{opt_col}500", rule)



def _write_note_row(ws, cols: list[spec.Col]) -> None:
    """第 2 行写填写说明，读取时会被识别为注释行（首列以 # 开头）而跳过。

    必须用 append 而不是 cell(row=2)，否则 openpyxl 的写入游标不前进，
    后续数据行会把这一行覆盖掉。
    """
    row = [f"# {c.note}" if c.note else None for c in cols]
    if not any(row):
        return
    ws.append(row)
    written = ws.max_row
    for idx in range(1, len(cols) + 1):
        cell = ws.cell(row=written, column=idx)
        cell.font = NOTE_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=False)


def _build_help(wb: Workbook) -> None:
    ws = wb.create_sheet(spec.S_HELP)
    ws.column_dimensions["A"].width = 110
    ws.sheet_view.showGridLines = False
    row = 1
    for text, kind in spec.HELP_TEXT:
        cell = ws.cell(row=row, column=1, value=text)
        if kind == "title":
            cell.font = TITLE_FONT
            ws.row_dimensions[row].height = 30
        elif kind == "h":
            cell.font = SECTION_FONT
            ws.row_dimensions[row].height = 22
        else:
            cell.font = Font(size=10, color="333333")
        cell.alignment = Alignment(vertical="center", wrap_text=False)
        row += 1


def build_workbook(proj: Project, data: dict[str, list[list]] | None = None) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    payload = data if data is not None else sheet_data(proj)

    _build_help(wb)
    for name in spec.SHEET_ORDER[1:]:
        ws = wb.create_sheet(name)
        cols = spec.COLUMNS[name]
        ws.append([c.header for c in cols])
        wrap_idx = _style_sheet(ws, cols)
        # 字段表的「选项值」列跟着「数据类型」联动：选了选项类就高亮提示要填
        if name == spec.S_FIELD:
            _link_option_col(ws, cols)
        # 说明行必须紧跟表头：放最后会让人误以为数据从第 2 行开始
        if name != spec.S_SETTINGS:
            _write_note_row(ws, cols)
        for row in payload.get(name, []):
            ws.append(row)
        # 数据写完再刷换行列样式（提前刷会创建空单元格、撑大 max_row）
        _apply_wrap(ws, wrap_idx)
    return wb


def demo_project() -> Project:
    """给模板预置一套最小可用的示例（采购申请），用户可直接改名改字。"""
    from .model import Bo, BoField, DataSet, Form, FormField, ListDef, Project

    bo = Bo(
        code="demo_purchase",
        name="采购申请",
        tbl_name="pp_demo_purchase",
        module_id="",
        type_id="",
        desc="示例业务对象",
        with_log=True,
    )
    base = [
        ("title", "申请标题", "varchar", 200, None, []),
        ("applyReason", "申请事由", "clob", None, None, []),
        ("amount", "申请金额", "number", 20, 2, []),
        ("applyDate", "申请日期", "datetime", None, None, []),
        ("applyUser", "申请人", "user", 64, None, []),
        ("departmentId", "申请部门", "department", 64, None, []),
        # 选项类型：这里演示「选项值」列怎么填（逗号分隔，顺序即界面顺序）
        ("urgency", "紧急程度", "dic", 32, None, ["普通", "紧急", "特急"]),
        ("attachments", "附件列表", "attachment", None, None, []),
        ("totalScore", "评分结果", "formula", 255, None, []),
    ]
    for sn, (code, name, t, length, prec, choices) in enumerate(base, start=1):
        bo.fields.append(BoField(
            bo_code=bo.code, code=code, name=name, type_new=t, length=length,
            precision=prec, object_type="urgency" if t == "dic" else "", sn=sn,
            choices=choices,
        ))

    form = Form(key="create_demo_purchase", name="新建采购申请", bo_code=bo.code,
                layout=2, kind="bundle")
    # 控件类型要和业务对象字段对得上（列在 _CONTROL_BY_TYPE 里）：
    # 这里以前手写成 text/editor，和展开出来的详情表单（number/textarea/datePicker）
    # 对不上，表单体检会报「同一字段控件类型不一致」，而且示例本身就是错的。
    # 「紧急程度」也统一改回字典（对象里是 dic，字典随包走；原来的 grade 在对象里不存在）。
    # 区间（布局 V1.0 的分区）：示例直接分成三块，用户照着填「分区」列即可
    for name, label, ftype, required, full, vs, group in [
        ("title", "申请标题", "text", True, False, "", "基础信息"),
        ("applyUser", "申请人", "dropSelector", False, False, "", "基础信息"),
        ("departmentId", "申请部门", "dropSelectOrg", False, False, "", "基础信息"),
        ("amount", "申请金额", "number", False, False, "", "金额与时间"),
        ("urgency", "紧急程度", "dictionary", False, False, "demo_purchase_urgency", "金额与时间"),
        ("applyDate", "申请日期", "datePicker", False, False, "", "金额与时间"),
        ("applyReason", "申请事由", "textarea", False, True, "", "补充说明"),
    ]:
        form.fields.append(FormField(
            form_key=form.key, name=name, label=label, field_type=ftype,
            required=required, full_row=full, value_source=vs, group=group,
        ))

    lst = ListDef(
        key="demo_purchase_list", name="采购申请列表", bo_code=bo.code,
        views=["grid", "board"], columns=["title", "applyUser", "amount", "applyDate"],
        sort_field="createTime", title_field="title",
    )
    ds = DataSet(key="demo_urgency_value", name="紧急程度值来源", from_table="pp_demo_urgency")

    demo_script = (
        "Object.assign(JForm, {\n"
        "  // 加载时把申请人默认填成当前登录人\n"
        "  onLoad: function (form) {\n"
        "    const user = form.$store.getters.userInfo.user;\n"
        "    form.setData('applyUser', user.id);\n"
        "  },\n"
        "});"
    )
    return Project(
        bos=[bo], forms=[form], lists=[lst], datasets=[ds],
        form_scripts={form.key: demo_script},
    )
