"""Excel -> Project 模型。"""
from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook

from . import fieldtypes, naming, spec
from .model import (
    FORM_BUNDLE,
    FORM_KIND_LABEL,
    OP_BY_LABEL,
    TABLE_COLUMN_TYPES,
    WF_KIND_BY_LABEL,
    WF_ONGOING,
    Bo,
    BoField,
    DataSet,
    Form,
    FormField,
    HideRule,
    ListDef,
    Project,
    TableColumn,
    WfEdge,
    WfNode,
    Workflow,
    parse_json,
    split_list,
    table_name_for,
    truthy,
)


# 旧模板沿用 form/detail/list 三种取值，映射到新语义
_LEGACY_KIND = {"form": "create", "detail": "detail", "list": "create", "": "create"}


class SheetReader:
    """按表头名取值，容忍列顺序变化与尾部空行。"""

    def __init__(self, ws, columns: list[spec.Col], errors: list[str], name: str) -> None:
        self.ws = ws
        self.errors = errors
        self.name = name
        self.cols = columns
        header = {}
        for idx, cell in enumerate(next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())):
            if cell is None:
                continue
            header[str(cell).strip()] = idx
        self.map = {}
        for col in columns:
            if col.header in header:
                self.map[col.key] = header[col.header]
        self.missing = [c.header for c in columns if c.required and c.header not in header]
        if self.missing:
            errors.append(f"[{name}] 缺少必需列: {', '.join(self.missing)}")

    def rows(self):
        for row_no, row in enumerate(self.ws.iter_rows(min_row=2, values_only=True), start=2):
            if not any(v is not None and str(v).strip() for v in row):
                continue
            first = next((str(v).strip() for v in row if v is not None), "")
            if first.startswith("#"):
                continue
            yield row_no, self.getter(row)

    def getter(self, row):
        def get(key: str):
            idx = self.map.get(key)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        return get


def _int(value, default=None):
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(str(value).strip()))
    except ValueError:
        return default


def _float(value, default=None):
    """解析「宽度%」这类小数格：允许 10 / 10.5 / 10% 。"""
    text = str(value or "").strip().rstrip("%")
    try:
        return float(text) if text else default
    except ValueError:
        return default


def _resolve_bo_code(proj, code: str, where: str, auto_notes: list[str]) -> str:
    """把引用里的对象编码归一。只有唯一对象时，错误的引用自动归位。"""
    if proj.bo(code) is not None:
        return code
    if len(proj.bos) == 1:
        bo = proj.bos[0]
        note = f"{where}引用的对象编码「{code}」不存在，已自动归到唯一对象「{bo.name}」({bo.code})"
        if note not in auto_notes:
            auto_notes.append(note)
        return bo.code
    return code


def _fix_columns(bo, columns: list[str], where: str, auto_notes: list[str]) -> list[str]:
    """列引用纠错：编码找不到时，按字段名或旧编码两种方式反查。

    典型场景：列表里写了 `title`，但字段编码被自动生成成了 `applyTitle`。
    两种反查：
      1. 按字段名（中文）精确匹配
      2. 按「旧编码的英文语义」反查——title 和 applyTitle 都含 title 语义
    """
    if bo is None:
        return columns
    fixed: list[str] = []
    for c in columns:
        if bo.field_by_code(c) is not None:
            fixed.append(c)
            continue
        # 1. 按字段名找
        hit = next((f for f in bo.fields if f.name == c), None)
        if hit is not None:
            note = f"{where}的列「{c}」按字段名匹配到了 {hit.code}"
            if note not in auto_notes:
                auto_notes.append(note)
            fixed.append(hit.code)
            continue
        # 2. 按旧编码的英文语义找：把 c 转成「中文再转回编码」，或直接比结尾
        cand = [f for f in bo.fields
                if f.code.lower() == c.lower()
                or f.code.lower().endswith(c.lower())
                or c.lower().endswith(f.code.lower())]
        # 排除自动补的内置字段干扰，取最短匹配（最贴近原意）
        cand.sort(key=lambda f: len(f.code))
        if len(cand) == 1:
            note = f"{where}的列「{c}」按编码语义匹配到了 {cand[0].code}"
            if note not in auto_notes:
                auto_notes.append(note)
            fixed.append(cand[0].code)
        else:
            fixed.append(c)      # 留给校验层报错
    return fixed


def _parse_choices(value) -> list[str]:
    """解析「选项值」列：逗号分隔，中英文逗号都认。

    支持 `月报,季报,周报` / `月报，季报` / 换行分隔。
    去重保序（重复项导入后会变成两个同名选项）。

    **分组写法**（级联字段用）：一行一个父选项，父与子用「：」分隔，子项逗号分隔——
        ```
        中药：OTC、RX、双跨、其他
        化药：OTC、RX、转增加
        大健康：大健康
        ```
    这种写法下，这里只返回「所有父选项 + 所有子选项」的并集（用于告诉用户
    这字段有多少候选值），真正的分组结构由 :func:`_parse_choice_groups` 给出。
    """
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    out: list[str] = []

    def _add(x: str) -> None:
        x = x.strip()
        if x and x not in out:
            out.append(x)

    # 先按行切：分组写法里「父：子、子」是一行，不能被逗号拆散
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _GROUP_SEP.search(line):
            parent, kids = _GROUP_SEP.split(line, 1)
            _add(parent)
            for k in re.split(r"[,，;；|、/]+", kids):
                _add(k)
            continue
        for p in re.split(r"[,，;；|\n\r]+", line):
            _add(p)
    return out


# 分组分隔符：「父：子1、子2」/「父: 子1,子2」/「父>子」
_GROUP_SEP = re.compile(r"\s*[：:>＞]\s*")


def _parse_choice_groups(value) -> dict[str, list[str]]:
    """把「父：子1、子2」这种分组写法解析成 {父: [子...]}。

    不是分组写法的返回空 dict（调用方退回扁平处理）。
    允许同一父选项写多行（追加子项），也允许分组里出现重复子项（去重保序）。
    """
    if value is None:
        return {}
    text = str(value).strip()
    if not text or not _GROUP_SEP.search(text):
        return {}
    groups: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or not _GROUP_SEP.search(line):
            continue
        parent, kids = _GROUP_SEP.split(line, 1)
        parent = parent.strip()
        if not parent:
            continue
        bucket = groups.setdefault(parent, [])
        for k in re.split(r"[,，;；|、/]+", kids):
            k = k.strip()
            if k and k not in bucket:
                bucket.append(k)
    return groups


def _node_ref(raw, name_by_id: dict[str, str]) -> str:
    """把「节点序号」换成「节点名称」；已经是名字或认不出来的原样返回。

    Excel 里「退回节点」「起点节点」「终点节点」三列用户既可能写序号也可能写名字
    （样本 node2 的退回是 [1]，界面上显示「申请人」）。生成器只按名字连边，
    所以读表时就统一形态；认不出来**不在这里报错**——留给 validate 汇总报
    「引用了不存在的节点」，用户跑一次就能看到全部问题。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    return name_by_id.get(text, text)


def _align_table_controls(proj: Project, auto_notes: list[str]) -> None:
    """把「表格」字段对应的控件统一成 tableField，并从对象字段补上列快照。

    为什么要在这里做（而不是只在 gen_form 里）：
      · 用户在 4_表单控件 里常常把表格字段写成 text / textarea，
        更糟的是写成 dataTemplate——那会让子表去找一个**同名列表**，
        找不到就是一片空白，导入后还看不出原因；
      · 界面预览读的是 reader 的产物，这里不改的话预览显示成文本框，
        用户会以为「表格字段没生效」。

    gen_form._inherit_table_columns 里还有同样一份（AI/blueprint 那条路不经过这里），
    两处都是幂等的，重复跑不会改坏。
    """
    by_bo: dict[str, dict[str, BoField]] = {
        b.code: {f.code: f for f in b.fields} for b in proj.bos}

    for form in proj.forms:
        fields = by_bo.get(form.bo_code) or {}
        for ff in form.fields:
            bf = fields.get(ff.name)
            is_table = bf is not None and fieldtypes.resolve(bf.type_new) == "table"
            if is_table:
                if not ff.table_columns:
                    ff.table_columns = list(bf.table_columns or [])
                if ff.field_type != "tableField":
                    auto_notes.append(
                        f"控件 {ff.name}（{ff.label}）：字段类型是「表格」，"
                        f"控件类型 {ff.field_type} 已改成 tableField")
                    ff.field_type = "tableField"
                ff.full_row = True
            elif ff.field_type == "tableField":
                auto_notes.append(
                    f"控件 {ff.name}（{ff.label}）：对象里没有对应的「表格」字段，"
                    f"表格控件已退回单行文本")
                ff.field_type = "text"


def read_workbook(path: Path) -> tuple[Project, list[str], dict]:
    """返回 (项目, 错误列表, 统计信息)。"""
    errors: list[str] = []
    auto_notes: list[str] = []      # 自动生成的编码提示（不是错误，是告知）
    wb = load_workbook(path, data_only=True)
    proj = Project()

    # ---- 项目设置
    if spec.S_SETTINGS in wb.sheetnames:
        ws = wb[spec.S_SETTINGS]
        for _, get in SheetReader(ws, spec.COLUMNS[spec.S_SETTINGS], errors, spec.S_SETTINGS).rows():
            key = str(get("key") or "").strip()
            val = get("value")
            if not key:
                continue
            if key == "租户ID":
                proj.tenant_id = str(val or "-999").strip()
            elif key == "文件前缀":
                proj.file_prefix = str(val or "001").strip().rstrip("-")
            elif key == "默认模块ID":
                proj.module_id = str(val or "").strip()
            elif key == "默认分类ID":
                proj.type_id = str(val or "").strip()
            elif key == "流程分类ID":
                # 流程分类与对象分类是两套 ID，不能互相顶替（见 model.Project）
                proj.wf_category_id = str(val or "").strip()
            elif key == "输出目录":
                proj.out_dir = str(val or "out").strip()

    # ---- 业务对象
    if spec.S_BO in wb.sheetnames:
        ws = wb[spec.S_BO]
        taken_codes: set[str] = set()
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_BO], errors, spec.S_BO).rows():
            code = str(get("code") or "").strip()
            name = str(get("name") or "").strip()
            # 编码留空时按对象名自动生成（英文小写下划线，对齐系统真实对象：
            # 项目→project / 项目立项→project_create，而不是拼音 xiang_mu）
            if not code:
                if not name:
                    # 名称也没有，这行是空的，跳过
                    continue
                code = naming.object_code(name)
                if not code:
                    errors.append(f"[{spec.S_BO}] 第{row_no}行: 对象名「{name}」无法自动生成编码，请手动填写")
                    continue
                code = naming.unique(code, taken_codes)
                auto_notes.append(f"业务对象「{name}」未填编码，已自动生成 {code}")
            taken_codes.add(code)
            tbl = str(get("tbl_name") or "").strip()
            # 物理表名留空就按规范补：pp_ + 编码。表名是线上建库时真正用的名字，
            # 让用户手填既容易拼错，也和系统里 pp_project / pp_project_create 对不上。
            if not tbl:
                tbl = table_name_for(code)
                auto_notes.append(f"业务对象「{name or code}」未填物理表名，已按规范取 {tbl}")
            proj.bos.append(
                Bo(
                    code=code,
                    name=name or code,
                    tbl_name=tbl,
                    bo_type=str(get("bo_type") or "object").strip(),
                    module_id=str(get("module_id") or "").strip(),
                    type_id=str(get("type_id") or "").strip(),
                    pk=str(get("pk") or "id").strip(),
                    desc=str(get("desc") or "").strip(),
                    with_log=bool(get("with_log") is None or truthy(get("with_log"))),
                    flow_driven=truthy(get("flow_driven")),
                    # 老模板没有「关联项目」这一列 → get() 返回 None → False，
                    # 产物与改造前逐字节一致（回归 PL12 守的就是这条）
                    project_link=truthy(get("link_project")),
                    parent_obj=str(get("parent_obj") or "").strip(),
                    parent_field=str(get("parent_field") or "").strip(),
                    # 关系类型缺省 PARENT（父子）；空值时若填了父对象也按 PARENT 走
                    rel_type=(str(get("rel_type") or "").strip().upper() or "PARENT"),
                    exist_id=str(get("exist_id") or "").strip(),
                )
            )
            # 原始填法留一份：非法值（如「可能」）由 validate 报「按否处理」，
            # 而不是在 reader 里静默吞掉——用户得知道自己填的那一格没生效。
            proj.bos[-1].project_link_raw = str(get("link_project") or "").strip()

    # ---- 字段
    if spec.S_FIELD in wb.sheetnames:
        ws = wb[spec.S_FIELD]
        # 每个对象已用的字段编码，用于自动生成时避重
        used_by_bo: dict[str, set[str]] = {}
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_FIELD], errors, spec.S_FIELD).rows():
            bo_code = str(get("bo_code") or "").strip()
            code = str(get("code") or "").strip()
            fname = str(get("name") or "").strip()
            if not bo_code:
                continue
            bo = proj.bo(bo_code)
            if bo is None:
                # 常见情况：对象编码留空被自动生成了，但字段表里还写着用户原来随手填的名字。
                # 只有一个对象时直接归位，不让用户为了改编码跑回来改字段表。
                if len(proj.bos) == 1:
                    bo = proj.bos[0]
                    note = (f"字段表引用的对象编码「{bo_code}」不存在，"
                            f"已自动归到唯一对象「{bo.name}」({bo.code})")
                    if note not in auto_notes:      # 同样的提示只记一次
                        auto_notes.append(note)
                    bo_code = bo.code
                else:
                    errors.append(f"[{spec.S_FIELD}] 第{row_no}行: 对象编码 {bo_code} 在 1_业务对象 中不存在")
                    continue
            taken = used_by_bo.setdefault(bo_code, {f.code for f in bo.fields})
            # 字段编码留空时按字段名自动生成（用户只填名称也能出包）
            if not code:
                if not fname:
                    continue      # 名称也没填，空行
                code = naming.to_english(fname, camel=True)
                if not code:
                    errors.append(f"[{spec.S_FIELD}] 第{row_no}行: 字段名「{fname}」无法自动生成编码，请手动填写")
                    continue
                code = naming.unique(code, taken)
                auto_notes.append(f"字段「{fname}」未填编码，已自动生成 {code}")
            taken.add(code)
            sn = _int(get("sn"), None)
            # 「扩展参数」里除了 options 本体，还可能带四个全局字典控制键
            # （dict_ref/dict_name/dict_stru/dict_items）——确认版 Excel 靠它们
            # 往返不丢字典引用。读进模型后就从 options 里摘出来，避免污染 XML。
            raw_opts = parse_json(get("options"), f"[{spec.S_FIELD}] 第{row_no}行", errors)
            ctrl = {k: raw_opts.pop(k) for k in
                    ("dict_ref", "dict_name", "dict_stru", "dict_items") if k in raw_opts}
            f = BoField(
                bo_code=bo_code,
                code=code,
                name=fname or code,
                type_new=fieldtypes.resolve(get("type_new")),
                datatype="",
                object_type=str(get("object_type") or "").strip(),
                length=_int(get("length"), None),
                precision=_int(get("precision"), None),
                is_index=truthy(get("is_index")),
                is_null=True if get("is_null") is None else truthy(get("is_null")),
                default_value=str(get("default_value") or "").strip(),
                options=raw_opts,
                desc=str(get("desc") or "").strip(),
                field_name=str(get("field_name") or "").strip(),
                sn=sn or (len(bo.fields) + 1),
                choices=_parse_choices(get("options_text")),
                choice_groups=_parse_choice_groups(get("options_text")),
                cascade_parent=str(get("cascade_parent") or "").strip(),
                dict_ref=str(ctrl.get("dict_ref") or "").strip(),
                dict_name=str(ctrl.get("dict_name") or "").strip(),
                dict_stru=str(ctrl.get("dict_stru") or "").strip(),
                dict_items=(list(ctrl.get("dict_items") or [])
                            if isinstance(ctrl.get("dict_items"), list) else []),
            )
            bo.fields.append(f)

    # ---- 表格字段的子列
    # 单独一张表，按「对象编码 + 字段编码」挂回 2_字段 里那张表格。
    # 必须在字段全部读完后再挂（用户完全可以把列写在前、字段写在后），
    # 所以这段放在字段块之后、表单块之前。
    if spec.S_TABLE_COL in wb.sheetnames:
        ws = wb[spec.S_TABLE_COL]
        bucket: dict[tuple[str, str], list[TableColumn]] = {}
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_TABLE_COL], errors,
                                       spec.S_TABLE_COL).rows():
            bo_code = str(get("bo_code") or "").strip()
            field_code = str(get("field_code") or "").strip()
            name = str(get("name") or "").strip()
            if not (bo_code or field_code or name):
                continue
            if not bo_code or not field_code:
                errors.append(f"[{spec.S_TABLE_COL}] 第{row_no}行: 对象编码与字段编码都要填")
                continue
            if not name:
                errors.append(f"[{spec.S_TABLE_COL}] 第{row_no}行: 列名不能为空")
                continue
            raw_type = str(get("input_type") or "").strip()
            input_type = fieldtypes.resolve_column_type(raw_type)
            if input_type not in TABLE_COLUMN_TYPES:
                errors.append(
                    f"[{spec.S_TABLE_COL}] 第{row_no}行: 列类型「{raw_type}」不认识，"
                    f"可选：{'、'.join(fieldtypes.column_choices())}")
                input_type = "varchar"
            align_raw = str(get("align") or "").strip()
            align = {"左": "left", "中": "center", "右": "right",
                     "left": "left", "center": "center", "right": "right"}.get(align_raw, "")
            bucket.setdefault((bo_code, field_code), []).append(TableColumn(
                name=name,
                input_type=input_type,
                choices=_parse_choices(get("options_text")),
                dict_ref=str(get("dict_ref") or "").strip(),
                width_percent=_float(get("width_percent"), None),
                align=align,
                required=truthy(get("required")),
                precision=_int(get("precision"), None),
                multiple=truthy(get("multiple")),
                sn=_int(get("sn"), 0) or 0,
            ))
        used: set[tuple[str, str]] = set()
        for (bo_code, field_code), cols in bucket.items():
            bo = proj.bo(bo_code)
            if bo is None:
                # 对象编码写错（或留空被自动改过）时别静默丢列，报出来让用户改
                errors.append(
                    f"[{spec.S_TABLE_COL}] 对象编码 {bo_code} 在 1_业务对象 中不存在，"
                    f"字段 {field_code} 的 {len(cols)} 列没有挂上")
                continue
            field = bo.field_by_code(field_code)
            if field is None:
                errors.append(
                    f"[{spec.S_TABLE_COL}] 对象 {bo_code} 里没有字段 {field_code}，"
                    f"这 {len(cols)} 列没有挂上（字段编码要填 2_字段 里那张表格的编码）")
                continue
            if fieldtypes.resolve(field.type_new) != "table":
                errors.append(
                    f"[{spec.S_TABLE_COL}] 字段 {bo_code}.{field_code} 的数据类型是"
                    f"「{fieldtypes.ui_label(field.type_new)}」，不是「表格」——"
                    f"请先在 2_字段 把类型改成「表格」，这些列才会生效")
                continue
            # 用户可能没写序号：按行序补齐，再按序号稳定排序
            for i, col in enumerate(cols, start=1):
                if not col.sn:
                    col.sn = i
            field.table_columns = sorted(cols, key=lambda c: c.sn)
            used.add((bo_code, field_code))

    # 关联对象字段归一：这一列用户会填「项目」「关联客户」「pp_project」，
    # 系统只认对象编码（iform_bo_def.code_），填错导入后是空下拉框。
    # 读入时就对一次，网页预览里看到的直接是 project，而不是导入才报错的「项目」。
    # 字段已全部读完才做，因为要拿整包对象（含本包新建的）当候选池。
    try:
        from . import objects as _objects
        for note in _objects.resolve_object_fields(proj):
            if note not in auto_notes:      # 同样的提示只记一次
                auto_notes.append(note)
    except Exception:
        pass          # 归一失败不影响读表，出包前 builder 还会再试一次

    # ---- 表单
    if spec.S_FORM in wb.sheetnames:
        ws = wb[spec.S_FORM]
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_FORM], errors, spec.S_FORM).rows():
            key = str(get("key") or "").strip()
            if not key:
                continue
            layout_raw = str(get("layout") or "一行两列").strip()
            kind_raw = str(get("kind") or "标准全套").strip()
            kind = (
                FORM_KIND_LABEL.get(kind_raw)          # 中文标签
                or (_LEGACY_KIND.get(kind_raw))        # 旧模板的 form/detail/list
                or (kind_raw if kind_raw in FORM_KIND_LABEL.values() else None)
                or FORM_BUNDLE
            )
            proj.forms.append(
                Form(
                    key=key,
                    name=str(get("name") or key).strip(),
                    bo_code=_resolve_bo_code(proj, str(get("bo_code") or "").strip(), "表单", auto_notes),
                    form_type=str(get("form_type") or "form").strip(),
                    layout=1 if layout_raw in ("一行一列", "1") else 2,
                    type_id=str(get("type_id") or "").strip(),
                    status=str(get("status") or "deploy").strip(),
                    # 面板宽度读进来只用于回写 Excel / 预览显示；
                    # **出包时一律以 model.form_style(kind) 为准（现为 90%）**，
                    # 不让历史工作簿里残留的 45 把新默认值盖回去。
                    width=_int(get("width"), 90),
                    kind=kind,
                    label_width=_int(get("label_width"), 0),
                    auto_hidden=True if get("auto_hidden") is None else truthy(get("auto_hidden")),
                )
            )

    # ---- 表单控件
    if spec.S_FORMFIELD in wb.sheetnames:
        ws = wb[spec.S_FORMFIELD]
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_FORMFIELD], errors, spec.S_FORMFIELD).rows():
            fkey = str(get("form_key") or "").strip()
            name = str(get("name") or "").strip()
            if not fkey or not name:
                continue
            form = next((x for x in proj.forms if x.key == fkey), None)
            if form is None:
                errors.append(f"[{spec.S_FORMFIELD}] 第{row_no}行: 表单key {fkey} 在 3_表单 中不存在")
                continue
            form.fields.append(
                FormField(
                    form_key=fkey,
                    name=name,
                    label=str(get("label") or name).strip(),
                    field_type=str(get("field_type") or "text").strip(),
                    value_source=str(get("value_source") or "").strip(),
                    placeholder=str(get("placeholder") or "请输入").strip(),
                    required=truthy(get("required")),
                    readonly=truthy(get("readonly")),
                    full_row=truthy(get("full_row")),
                    newline=truthy(get("newline")),
                    group=str(get("group") or "").strip(),
                    cascade_parent=str(get("cascade_parent") or "").strip(),
                    options=parse_json(get("options"), f"[{spec.S_FORMFIELD}] 第{row_no}行", errors),
                )
            )

    # ---- 列表
    if spec.S_LIST in wb.sheetnames:
        ws = wb[spec.S_LIST]
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_LIST], errors, spec.S_LIST).rows():
            key = str(get("key") or "").strip()
            if not key:
                continue
            views = [v.lower() for v in split_list(get("views"))] or ["grid"]
            lst_bo_code = _resolve_bo_code(proj, str(get("bo_code") or "").strip(), "列表", auto_notes)
            lst_bo = proj.bo(lst_bo_code)
            raw_cols = split_list(get("columns"))
            fixed_cols = _fix_columns(lst_bo, raw_cols, f"列表 {key}", auto_notes)
            # 排序字段、标题字段同样纠错
            sort_f = str(get("sort_field") or "").strip()
            title_f = str(get("title_field") or "").strip()
            fixed_sort = _fix_columns(lst_bo, [sort_f], f"列表 {key}(排序)", auto_notes)[0] if sort_f else ""
            fixed_title = _fix_columns(lst_bo, [title_f], f"列表 {key}(标题)", auto_notes)[0] if title_f else ""
            proj.lists.append(
                ListDef(
                    key=key,
                    name=str(get("name") or key).strip(),
                    bo_code=lst_bo_code,
                    views=views,
                    columns=fixed_cols,
                    sort_field=fixed_sort,
                    sort_dir=str(get("sort_dir") or "desc").strip(),
                    tree=truthy(get("tree")),
                    parent_field=str(get("parent_field") or "").strip(),
                    title_field=fixed_title,
                    # 级联详情字段同样纠错：填了不存在的字段编码按没填处理
                    detail_field=_fix_columns(lst_bo, [str(get("detail_field") or "").strip()],
                                              f"列表 {key}(级联详情)", auto_notes)[0],
                )
            )

    # ---- 数据集
    if spec.S_DATASET in wb.sheetnames:
        ws = wb[spec.S_DATASET]
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_DATASET], errors, spec.S_DATASET).rows():
            key = str(get("key") or "").strip()
            if not key:
                continue
            proj.datasets.append(
                DataSet(
                    key=key,
                    name=str(get("name") or key).strip(),
                    from_table=str(get("from_table") or "").strip(),
                    ds_type=str(get("ds_type") or "valueSource").strip(),
                    unique_field=str(get("unique_field") or "id_").strip(),
                    id_field=str(get("id_field") or "id_").strip(),
                    name_field=str(get("name_field") or "name_").strip(),
                )
            )

    # ---- 表单脚本
    if spec.S_SCRIPT in wb.sheetnames:
        ws = wb[spec.S_SCRIPT]
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_SCRIPT], errors, spec.S_SCRIPT).rows():
            fkey = str(get("form_key") or "").strip()
            script = str(get("script") or "").strip()
            if not fkey or not script:
                continue
            if not any(f.key == fkey for f in proj.forms):
                errors.append(f"[{spec.S_SCRIPT}] 第{row_no}行: 表单key {fkey} 不存在")
                continue
            proj.form_scripts[fkey] = script

    # ---- 条件隐藏规则
    if spec.S_HIDE in wb.sheetnames:
        ws = wb[spec.S_HIDE]
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_HIDE], errors, spec.S_HIDE).rows():
            fkey = str(get("form_key") or "").strip()
            target = str(get("target") or "").strip()
            if not fkey or not target:
                continue
            form = next((x for x in proj.forms if x.key == fkey), None)
            if form is None:
                errors.append(f"[{spec.S_HIDE}] 第{row_no}行: 表单key {fkey} 不存在")
                continue
            op_raw = str(get("operator") or "等于").strip()
            operator = OP_BY_LABEL.get(op_raw, op_raw)
            form.hide_rules.append(
                HideRule(
                    form_key=fkey,
                    target=target,
                    source=str(get("source") or "").strip(),
                    operator=operator,
                    value=str(get("value") or "").strip(),
                    value_label=str(get("value_label") or "").strip(),
                    clear_on_hide=True if get("clear_on_hide") is None else truthy(get("clear_on_hide")),
                )
            )

    # ---- 流程头（9_流程）
    #
    # 三张表互相引用：节点挂在流程名下，流转指向节点名或序号。读取顺序必须是
    # 流程 → 节点 → 流转，因为「序号 → 节点名」这张对照表要等节点读完才建得全。
    #
    # 归一失败的**不在 reader 报错**（除了流程名不存在这种硬错），而是原样留下，
    # 交给 validate 统一报「引用了不存在的节点」——一次能看到全部问题，
    # 而不是修一个错再跑一遍又冒出下一个。
    if spec.S_WF in wb.sheetnames:
        ws = wb[spec.S_WF]
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_WF], errors, spec.S_WF).rows():
            name = str(get("name") or "").strip()
            if not name:
                continue
            if any(w.name == name for w in proj.workflows):
                errors.append(
                    f"[{spec.S_WF}] 第{row_no}行: 流程名称「{name}」重复。"
                    f"节点和流转都按名字关联流程，重名会串到同一条流程上，请改掉一个")
                continue
            wf = Workflow(
                name=name,
                # 与表单/列表一致：只有一个对象时把写错的对象编码自动归位
                bo_code=_resolve_bo_code(proj, str(get("bo_code") or "").strip(), "流程", auto_notes),
                workflow_type=str(get("workflow_type") or "normal").strip() or "normal",
                category_id=str(get("category_id") or "").strip(),
                module_id=str(get("module_id") or "").strip(),
                is_active=True if get("is_active") is None else truthy(get("is_active")),
                desc=str(get("desc") or "").strip(),
                exist_id=str(get("exist_id") or "").strip(),
            )
            # 标题格式留空要保留 dataclass 里的默认模板，不能被空串覆盖
            title_format = str(get("title_format") or "").strip()
            if title_format:
                wf.title_format = title_format
            proj.workflows.append(wf)

    # ---- 流程节点（10_流程节点）
    if spec.S_WF_NODE in wb.sheetnames:
        ws = wb[spec.S_WF_NODE]
        # 每个流程下一个「下一个可用序号」，用来给「节点序号」留空的行补号
        next_id: dict[str, int] = {}
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_WF_NODE], errors,
                                       spec.S_WF_NODE).rows():
            wf_name = str(get("wf") or "").strip()
            node_name = str(get("name") or "").strip()
            if not wf_name or not node_name:
                continue
            wf = next((w for w in proj.workflows if w.name == wf_name), None)
            if wf is None:
                errors.append(
                    f"[{spec.S_WF_NODE}] 第{row_no}行: 流程名称「{wf_name}」在 {spec.S_WF} 里不存在。"
                    f"先在 9_流程 里加一行这个流程")
                continue
            if wf.node_by_name(node_name):
                errors.append(
                    f"[{spec.S_WF_NODE}] 第{row_no}行: 流程「{wf_name}」里节点名称「{node_name}」重复。"
                    f"流转按名字找节点，重名会连错线，请改成能区分的名字")
                continue
            kind_raw = str(get("kind") or "审批").strip()
            kind = WF_KIND_BY_LABEL.get(kind_raw)
            if kind is None:
                # 认不出的类型不猜「判断」这种会改变图结构的，按最普通的审批处理
                kind = WF_ONGOING
                note = (f"流程「{wf_name}」节点「{node_name}」的类型「{kind_raw}」认不出，"
                        f"已按「审批」处理（可选：开始 / 审批 / 判断 / 通过 / 驳回）")
                if note not in auto_notes:
                    auto_notes.append(note)
            handler_type = str(get("handler_type") or "角色").strip()
            if handler_type not in ("角色", "人员", "无"):
                note = (f"流程「{wf_name}」节点「{node_name}」的处理人类型「{handler_type}」认不出，"
                        f"已按「角色」处理（可选：角色 / 人员 / 无）")
                if note not in auto_notes:
                    auto_notes.append(note)
                handler_type = "角色"
            # 序号：留空/写 0 就按该流程内的行序补号（判断节点也占号，与样本
            # v30 的 13 个 activity Id 一致）
            nid = _int(get("id"), 0) or 0
            if nid <= 0:
                nid = next_id.get(wf_name, 1)
            next_id[wf_name] = max(next_id.get(wf_name, 1), nid + 1)
            wf.nodes.append(WfNode(
                wf=wf_name,
                name=node_name,
                kind=kind,
                id=nid,
                handler_type=handler_type,
                handler_names=split_list(get("handler_names")),
                handler_ids=split_list(get("handler_ids")),
                expect_days=_int(get("expect_days"), 2),
                form_key=str(get("form_key") or "").strip(),
                business_state=str(get("business_state") or "").strip(),
                go_back=split_list(get("go_back")),
                pick_next=truthy(get("pick_next")),
                allow_handover=truthy(get("allow_handover")),
                color=str(get("color") or "").strip(),
                notes=str(get("notes") or "").strip(),
            ))

    # ---- 节点名 ↔ 序号归一
    #
    # 用户的写法很随意：「退回节点」和流转的「起点/终点」可能写 1、2，也可能写
    # 「部门审批」。生成器只按**节点名**连边，所以这里统一换成名字；
    # 认不出来的原样保留（既不报错也不丢弃），validate 会报「引用了不存在的节点」。
    for wf in proj.workflows:
        name_by_id = {str(n.id): n.name for n in wf.nodes}
        for node in wf.nodes:
            node.go_back = [_node_ref(x, name_by_id) for x in node.go_back]

    # ---- 流程流转（11_流程流转）
    if spec.S_WF_EDGE in wb.sheetnames:
        ws = wb[spec.S_WF_EDGE]
        for row_no, get in SheetReader(ws, spec.COLUMNS[spec.S_WF_EDGE], errors,
                                       spec.S_WF_EDGE).rows():
            wf_name = str(get("wf") or "").strip()
            src_raw = str(get("src") or "").strip()
            dst_raw = str(get("dst") or "").strip()
            if not wf_name or not src_raw or not dst_raw:
                continue
            wf = next((w for w in proj.workflows if w.name == wf_name), None)
            if wf is None:
                errors.append(
                    f"[{spec.S_WF_EDGE}] 第{row_no}行: 流程名称「{wf_name}」在 {spec.S_WF} 里不存在")
                continue
            name_by_id = {str(n.id): n.name for n in wf.nodes}
            src = _node_ref(src_raw, name_by_id)
            dst = _node_ref(dst_raw, name_by_id)
            cond_raw = str(get("condition") or "无条件").strip()
            is_cond = cond_raw not in ("", "无条件")
            field_raw = str(get("condition_field") or "").strip()
            if is_cond and field_raw:
                # 条件字段允许写中文名，按列表那套反查对成字段编码
                bo = proj.bo(wf.bo_code)
                field_raw = _fix_columns(bo, [field_raw], f"流程 {wf_name}", auto_notes)[0]
            inform_raw = get("inform")
            wf.edges.append(WfEdge(
                wf=wf_name,
                src=src,
                dst=dst,
                name=str(get("name") or "").strip(),
                condition_field=field_raw if is_cond else "",
                condition_op=str(get("condition_op") or "").strip() if is_cond else "",
                condition_value=str(get("condition_value") or "").strip() if is_cond else "",
                value_label=str(get("value_label") or "").strip(),
                order=_int(get("order"), 0) or 0,
                # 留空 = 交给生成器按源节点种类推导（判断节点否，其余是）
                inform=None if (inform_raw is None or str(inform_raw).strip() == "")
                else truthy(inform_raw),
            ))

    # 表格字段的最后一道对齐：把「字段类型=表格」的控件统一成 tableField，
    # 并把列快照从对象字段带过来。放在读表阶段而不是只放在出包阶段，
    # 是因为界面预览读的是这里的结果——不然预览里那张子表显示成文本框，
    # 用户以为没生效，直到出包才发现其实已经是 tableField。
    _align_table_controls(proj, auto_notes)

    stats = {
        "业务对象": len(proj.bos),
        "字段": sum(len(b.fields) for b in proj.bos),
        "表单": len(proj.forms),
        "表单控件": sum(len(f.fields) for f in proj.forms),
        "列表": len(proj.lists),
        "数据集": len(proj.datasets),
        "脚本": len(proj.form_scripts),
        "隐藏规则": sum(len(f.hide_rules) for f in proj.forms),
        "流程": len(proj.workflows),
        "流程节点": sum(len(w.nodes) for w in proj.workflows),
    }
    # 自动补齐的东西：只放数量进 stats（stats 会被拼成一行打印，放 list 会很难看），
    # 明细挂在 proj.auto_notes 上供 UI 展示。除了自动生成的编码，
    # 现在还包含关联对象归一的提示（「项目」→ project）。
    if auto_notes:
        stats["自动补齐"] = len(auto_notes)
        proj.auto_notes = auto_notes
    return proj, errors, stats

