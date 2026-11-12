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
    Bo,
    BoField,
    DataSet,
    Form,
    FormField,
    HideRule,
    ListDef,
    Project,
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
    """
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    # 中文逗号、英文逗号、分号、竖线、换行都当分隔符
    parts = re.split(r"[,，;；|\n\r]+", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


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
                    parent_obj=str(get("parent_obj") or "").strip(),
                    parent_field=str(get("parent_field") or "").strip(),
                    # 关系类型缺省 PARENT（父子）；空值时若填了父对象也按 PARENT 走
                    rel_type=(str(get("rel_type") or "").strip().upper() or "PARENT"),
                    exist_id=str(get("exist_id") or "").strip(),
                )
            )

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
                options=parse_json(get("options"), f"[{spec.S_FIELD}] 第{row_no}行", errors),
                desc=str(get("desc") or "").strip(),
                field_name=str(get("field_name") or "").strip(),
                sn=sn or (len(bo.fields) + 1),
                choices=_parse_choices(get("options_text")),
                cascade_parent=str(get("cascade_parent") or "").strip(),
            )
            bo.fields.append(f)

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

    stats = {
        "业务对象": len(proj.bos),
        "字段": sum(len(b.fields) for b in proj.bos),
        "表单": len(proj.forms),
        "表单控件": sum(len(f.fields) for f in proj.forms),
        "列表": len(proj.lists),
        "数据集": len(proj.datasets),
        "脚本": len(proj.form_scripts),
        "隐藏规则": sum(len(f.hide_rules) for f in proj.forms),
    }
    # 自动补齐的东西：只放数量进 stats（stats 会被拼成一行打印，放 list 会很难看），
    # 明细挂在 proj.auto_notes 上供 UI 展示。除了自动生成的编码，
    # 现在还包含关联对象归一的提示（「项目」→ project）。
    if auto_notes:
        stats["自动补齐"] = len(auto_notes)
        proj.auto_notes = auto_notes
    return proj, errors, stats

