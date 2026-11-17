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
        "流程分类ID": proj.wf_category_id,
        "输出目录": proj.out_dir,
    }
    return [(k, values.get(k, ""), note) for k, _default, note in spec.SETTING_ROWS]


def sheet_data(proj: Project) -> dict[str, list[list]]:
    """Project -> 各表的数据行（不含表头）。"""
    out: dict[str, list[list]] = {k: [] for k in spec.COLUMNS}
    out[spec.S_SETTINGS] = [list(r) for r in _settings_rows(proj)]

    for bo in proj.bos:
        # 值列表的位序必须与 spec.COLUMNS[S_BO] 逐位对齐：「关联项目」插在
        # 「流程驱动」之后（第 10 位）。
        # ⚠ 故意**不补** exist_id（架构 §0.3 F-A）：COLUMNS 有 15 列而这里只写
        # 14 个值，补上会让「确认版 Excel」多出一列值，破坏既有 diff 与回归。
        out[spec.S_BO].append([
            bo.code, bo.name, bo.tbl_name, bo.bo_type, bo.module_id, bo.type_id,
            bo.pk, "是" if bo.with_log else "否", "是" if bo.flow_driven else "否",
            "是" if bo.project_link else "否",
            bo.parent_obj, bo.parent_field, bo.rel_type or "PARENT", bo.desc,
        ])
        for f in sorted(bo.fields, key=lambda x: x.sn):
            # 类型列写系统界面的叫法（单行文本/数字/日期控件…），
            # 和下拉选项里看到的词一致，用户改起来不用翻译
            type_text = fieldtypes.ui_label(f.type_new)
            # 字段名/编码/物理列名都写**对齐后**的值：确认版 Excel 是用户的
            # 下一份底稿，必须与出包结果一致，否则下次上传又变回去。
            out[spec.S_FIELD].append([
                bo.code, f.sn, f.code, f.field_name, f.name, type_text, f.object_type,
                # 选项值：一行一个（Excel 里 Alt+Enter 换行），比逗号串好读好改
                f.choice_text(),
                f.cascade_parent,
                f.length, f.precision,
                "是" if f.is_index else "否", "是" if f.is_null else "否",
                # excel_options 带上 dict_ref/dict_name/dict_stru/dict_items 四个
                # 控制键：全局字典只有靠它们才能往返不丢（resolved() 会剥掉）
                f.default_value, _dump_json(f.excel_options()), f.desc,
            ])
            # 表格字段的子列：一行一列挂到「2.1_表格列」。
            # 列类型写中文名（和 2_字段 的「数据类型」一致），读回来 resolve_column_type 认。
            if fieldtypes.resolve(f.type_new) != "table":
                continue
            for i, col in enumerate(sorted(f.table_columns, key=lambda c: c.sn or 0), start=1):
                out[spec.S_TABLE_COL].append([
                    bo.code, f.code, col.sn or i, col.name,
                    fieldtypes.COLUMN_LABELS.get(col.input_type, col.input_type),
                    "\n".join(col.choices), col.dict_ref,
                    _num(col.width_percent), _ALIGN_TO_LABEL.get(col.align, ""),
                    "是" if col.required else "否",
                    _num(col.precision), "是" if col.multiple else "否",
                ])

    for form in proj.forms:
        out[spec.S_FORM].append([
            form.key, form.name, form.bo_code,
            _KIND_TO_LABEL.get(form.kind, form.kind),
            "一行一列" if form.layout == 1 else "一行两列",
            "拖拽布局V1.0" if form.layout_style == "flat" else "字段分组",
            form.type_id, form.status,
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

    # ---- 流程三张表
    # 反解一份流程 XML 后也要能原样落回这三张表（P2 的 inspect 反解会复用这里），
    # 所以列的写回顺序严格按 spec.COLUMNS 来，不要在这里调整。
    for wf in proj.workflows:
        out[spec.S_WF].append([
            wf.name, wf.bo_code, wf.workflow_type, wf.category_id, wf.title_format,
            wf.module_id, "是" if wf.is_active else "否", wf.exist_id, wf.desc,
        ])
        for n in wf.nodes:
            out[spec.S_WF_NODE].append([
                wf.name, n.id, n.name,
                _WF_KIND_TO_LABEL.get(n.kind, n.kind),
                n.handler_type, ",".join(n.handler_names), ",".join(n.handler_ids),
                n.expect_days, n.form_key, n.business_state,
                ",".join(n.go_back),
                "是" if n.pick_next else "否",
                "是" if n.allow_handover else "否",
                n.color, n.notes,
            ])
        for e in wf.edges:
            out[spec.S_WF_EDGE].append([
                wf.name, e.src, e.dst, e.name,
                "字段条件" if e.condition_field else "无条件",
                e.condition_field, e.condition_op, e.condition_value, e.value_label,
                e.order or "",
                "" if e.inform is None else ("是" if e.inform else "否"),
            ])
    return out


# XML 里的 State → 模板里给用户看的中文词（与 spec.S_WF_NODE 的下拉一致）
_WF_KIND_TO_LABEL = {
    "Start": "开始", "Ongoing": "审批", "Branch": "判断",
    "Passed": "通过", "Rejected": "驳回",
}

# 表格列的对齐：XML 写 left/center/right，模板里给人看左/中/右
_ALIGN_TO_LABEL = {"left": "左", "center": "中", "right": "右"}


def _num(value):
    """整数写整数（10 而不是 10.0），空值写空串——Excel 里好看，读回也不歧义。"""
    if value is None or value == "":
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    return int(f) if f == int(f) else f


def write_workflows(wb, proj: Project) -> None:
    """只重写 9_流程 / 10_流程节点 / 11_流程流转，其它 sheet 原样保留。

    为什么不一整本重建（build_workbook）：用户上传的 Excel 可能有自己的批注、
    额外列、甚至别的 sheet，AI 只改流程，不该把整本重排一遍。
    同理也不整表覆盖：先按**表头文字**把列对位，对不上就明确报错，
    免得把「节点名称」写进「节点类型」那一列。
    """
    data = sheet_data(proj)
    for name in (spec.S_WF, spec.S_WF_NODE, spec.S_WF_EDGE):
        cols = spec.COLUMNS[name]
        if name not in wb.sheetnames:
            ws = wb.create_sheet(name)
            # 表头必须自己 append：_style_sheet 只画样式，只有带 note 的列
            # 才顺手补一次表头值（历史行为，见 build_workbook 里也是先 append）
            ws.append([c.header for c in cols])
            _style_sheet(ws, cols)
            _write_note_row(ws, cols)
        ws = wb[name]

        header = [str(c.value or "").strip() for c in ws[1]]
        want = [c.header for c in cols]
        missing = [h for h in want if h not in header]
        if missing:
            raise ValueError(
                f"「{name}」这张表的表头不是标准模板（缺 {'、'.join(missing[:3])}），"
                f"没法安全写回。请用「普通模式」导出的 Excel 当底稿")
        index = [header.index(h) for h in want]

        # 数据从第几行开始：模板生成的表第 2 行是「# 填写说明」，
        # 用户自己整理的表可能第 2 行就是数据。按首列是不是 # 判断，
        # 一律按 3 删会把第一行业务数据留下来，读回时变成「流程重名」。
        second = str(ws.cell(row=2, column=1).value or "").strip()
        first = 3 if second.startswith("#") else 2
        if ws.max_row >= first:
            ws.delete_rows(first, ws.max_row - first + 1)
        for row in data[name]:
            vals = ["" for _ in header]
            for i, v in zip(index, row):
                vals[i] = v
            ws.append(vals)


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
    from .model import Bo, BoField, DataSet, Form, FormField, ListDef, Project, TableColumn

    bo = Bo(
        code="demo_purchase",
        name="采购申请",
        tbl_name="pp_demo_purchase",
        module_id="",
        type_id="",
        desc="示例业务对象",
        with_log=True,
        flow_driven=True,          # 示例演示默认三步流程：开始 → 部门审批 → 通过
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

    # 表格字段示例（子表）：列定义写在「2.1_表格列」，这里预置 5 列，
    # 用户照这一组改成自己的明细列即可。缩进/宽度/精度都演示一遍。
    bo.fields.append(BoField(
        bo_code=bo.code, code="lineItems", name="采购明细", type_new="table",
        sn=len(bo.fields) + 1,
        table_columns=[
            TableColumn("物料名称", "varchar", width_percent=20, align="left", sn=1),
            TableColumn("数量", "number", precision=0, width_percent=10, align="right", sn=2),
            TableColumn("单价", "number", precision=2, width_percent=15, align="right", sn=3),
            TableColumn("金额", "number", precision=2, width_percent=15, align="right", sn=4),
            TableColumn("是否急件", "dic", choices=["是", "否"], width_percent=10, sn=5),
        ],
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
        # 子表控件：字段类型=表格，控件自动出 tableField（这里显式写出来当范例）
        ("lineItems", "采购明细", "tableField", False, True, "", "采购明细"),
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
    # 示例流程：申请人 →（提交）→ 部门审批 →（同意）→ 通过。
    # 「11_流程流转」故意留空：留空时生成器按节点行序自动串成一条直线，
    # 用户照这个最小形态改字就能用，不会因为没连线而卡住。
    # 示例里**故意不写流程**：对象勾了「流程驱动」，生成时会自动补
    # 开始 → 部门审批 → 通过。这既是推荐用法（流程交给项目经理在系统里改），
    # 也让人一眼看到「不用填 9_流程 也能出包」。
    return Project(
        bos=[bo], forms=[form], lists=[lst], datasets=[ds],
        form_scripts={form.key: demo_script},
    )
