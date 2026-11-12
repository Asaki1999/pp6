"""inspect：把系统导出的 zip 反解成 Excel，作为「起稿」入口。

比自己从空白模板填更靠谱——直接拿一份线上跑通的配置当基线，改字段即可。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .model import Bo, BoField, DataSet, Form, FormField, HideRule, ListDef, Project

_SYSTEM_CODES = {"id", "tenantId", "ip", "deleted", "createBy", "createTime",
                 "updateTime", "version", "updateBy"}
_CONTAINER = {"grid", "grid_layout", "tabs", "card", "fieldset", "tablelayout", "label"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _iter_xml(path: Path):
    """遍历目录下所有 zip（或单个 zip），产出 (kind, key, root)。"""
    targets = [path] if path.is_file() else sorted(path.rglob("*.zip"))
    for zp in targets:
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                if not name.endswith(".xml") or name.startswith("__MACOSX"):
                    continue
                kind = name.split("/")[0]
                key = Path(name).stem.split("-", 1)[-1]
                try:
                    yield kind, key, ET.fromstring(z.read(name)), zp.name
                except ET.ParseError:
                    continue


def _parse_bo(root) -> Bo | None:
    ent = _child(root, "bodefentity")
    if ent is None:
        return None
    bo = Bo(
        code=ent.attrib.get("code", ""),
        name=ent.attrib.get("name", ""),
        # 有些导出包不带 tblName（用户 2026-10-29 导的 requirement_category 就是这样），
        # 空着会让 check 报「物理表名为空」并中止出包。
        # 按手册规范补 PP_ + 编码，和 build 阶段补的是同一套规则。
        tbl_name=ent.attrib.get("tblName", "") or "",
        bo_type=ent.attrib.get("boType", "object"),
        module_id=ent.attrib.get("moduleId", ""),
        type_id=ent.attrib.get("typeId", ""),
        pk=ent.attrib.get("pk", "id"),
        desc=ent.attrib.get("desc", ""),
        # 反解即显式：包里的对象 id 原样带回 exist_id，出包时导入仍按「更新」
        # 处理（XML id 不变）。以前反解不带 id，靠 bind() 的名称匹配静默补——
        # 那条路现已封死（新建同名对象会被误判成更新），反解场景必须自带 id。
        exist_id=ent.attrib.get("id", "") or "",
    )
    if not bo.tbl_name and bo.code:
        from .model import table_name_for
        bo.tbl_name = table_name_for(bo.code)
    logs = _child(root, "boLogTemplateList")
    bo.with_log = bool(logs is not None and _children(logs, "boLogTemplate"))

    for sn, attr in enumerate(_children(ent, "attribute"), start=1):
        code = attr.attrib.get("code", "")
        if code in _SYSTEM_CODES:
            continue
        opts = {}
        if attr.attrib.get("options"):
            try:
                opts = json.loads(attr.attrib["options"])
            except json.JSONDecodeError:
                opts = {}
        # 级联写在 options 里。这里把它提到 cascade_parent 单一来源，并从 opts 删掉：
        # 否则用户在 Excel 里清空「级联父字段」，opts 里的旧值还是会被原样带出去，
        # 级联就关不掉了。
        # 只认 cascadeEnabled=true 的。样例里 strategyCategory 是 false 但留着
        # parentCode（记着父级却没启用），这种按「不级联」反解，语义等价。
        _on = opts.pop("cascadeEnabled", False)
        _parent = str(opts.pop("cascadeParentCode", "") or "").strip()
        cascade_parent = _parent if _parent and (_on is True or str(_on).lower() == "true") else ""
        bo.fields.append(BoField(
            bo_code=bo.code,
            code=code,
            name=attr.attrib.get("name", code),
            type_new=attr.attrib.get("dataTypeNew", "varchar"),
            datatype=attr.attrib.get("datatype", ""),
            object_type=attr.attrib.get("objectType", ""),
            length=int(attr.attrib["length"]) if attr.attrib.get("length", "").isdigit() else None,
            precision=int(attr.attrib["precision"]) if attr.attrib.get("precision", "").isdigit() else None,
            is_index=attr.attrib.get("isindex") == "Y",
            is_null=attr.attrib.get("isnull") != "N",
            default_value=attr.attrib.get("defaultvalue", ""),
            options=opts,
            desc=attr.attrib.get("desc", ""),
            field_name=attr.attrib.get("fieldName", ""),
            sn=int(attr.attrib.get("sn", sn)) if attr.attrib.get("sn", "").isdigit() else sn,
            cascade_parent=cascade_parent,
        ))

    # 父子/平行关系：只取「本对象是子对象」的那条（src == 本对象），
    # 反解回 Excel 的「父对象编码 / 关联字段 / 关系类型」三列，保证往返不丢。
    rel_list = _child(root, "objRelationshipList")
    for rel in _children(rel_list, "objRelationship") if rel_list is not None else []:
        if rel.attrib.get("srcObjCode") == bo.code and not bo.parent_obj:
            bo.parent_obj = rel.attrib.get("dstObjCode", "")
            bo.parent_field = rel.attrib.get("relField", "")
            bo.rel_type = rel.attrib.get("relType", "PARENT")
    return bo


def _parse_form(root) -> tuple[Form, list[FormField]] | None:
    defs = _child(root, "formDefs")
    if defs is None:
        return None
    fdef = _child(defs, "formDef")
    if fdef is None:
        return None
    bo_el = _child(defs, "formBo")
    try:
        ext = json.loads(fdef.attrib.get("extendAttr", "{}"))
    except json.JSONDecodeError:
        ext = {}

    fields_el = _children(defs, "formField")
    parents = {f.attrib.get("id"): f.attrib.get("parentId") for f in fields_el}
    by_id = {f.attrib.get("id"): f for f in fields_el}

    # ---- 布局：两种写法都要能反解
    #   布局 V1.0：grid_layout 的 rows[].columns[].span（24 制，24=整行，12=半宽）
    #   旧写法  ：每行一个 grid 容器，columns 的个数就是这一行放几个控件
    span_by_name: dict[str, int] = {}
    all_spans: list[int] = []
    for f in fields_el:
        ft = f.attrib.get("fieldType")
        if ft not in ("grid", "grid_layout"):
            continue
        try:
            opts = json.loads(f.attrib.get("fieldOptions", "{}"))
        except json.JSONDecodeError:
            continue
        if ft == "grid_layout":
            rows = opts.get("rows") or []
            for row in rows:
                for cell in row.get("columns") or []:
                    sp = int(cell.get("span") or 24)
                    all_spans.append(sp)
                    for w in cell.get("fields") or []:
                        nm = (w or {}).get("name")
                        if nm:
                            span_by_name[nm] = sp
            if not rows:
                # 只有 columns、没有 rows 的包（手工改过），按列推
                all_spans.extend(int(c.get("span") or 12) for c in opts.get("columns") or [])
        elif f.attrib.get("parentId") is not None:
            all_spans.extend([24] * len(opts.get("columns") or []))
    layout = 1 if all_spans and all(s >= 24 for s in all_spans) else 2

    # 控件所在的分区（card / fieldset 的标题）
    def _group_of(fid: str) -> str:
        pid = parents.get(fid)
        seen: set[str] = set()
        while pid and pid not in seen:
            seen.add(pid)
            p = by_id.get(pid)
            if p is None:
                return ""
            if p.attrib.get("fieldType") in ("card", "fieldset"):
                return p.attrib.get("label", "")
            pid = parents.get(pid)
        return ""

    fname = fdef.attrib.get("name", "")
    if fname.startswith("条件专用_"):
        kind = "condition"
    elif fname.startswith("导入专用_"):
        kind = "import"
    elif "详情" in fname:
        kind = "detail"
    else:
        kind = "create"

    form = Form(
        key=fdef.attrib.get("key", ""),
        name=fname,
        kind=kind,
        bo_code=(bo_el.attrib.get("boCode", "") if bo_el is not None else ""),
        form_type=fdef.attrib.get("formType", "form"),
        layout=layout,
        type_id=fdef.attrib.get("typeId", ""),
        status=fdef.attrib.get("status", "deploy"),
        width=int(ext.get("width", 90) or 90),
        label_width=int(ext.get("labelWidth", 0) or 0),
    )

    out: list[FormField] = []
    for f in fields_el:
        ftype = f.attrib.get("fieldType", "")
        name = f.attrib.get("name", "")
        if ftype in _CONTAINER or ftype == "hidden" or name.startswith("grid_"):
            continue
        if name in ("id", "version", "pinyin"):
            continue
        try:
            opts = json.loads(f.attrib.get("fieldOptions", "{}"))
        except json.JSONDecodeError:
            opts = {}
        # 占满整行：布局 V1.0 看单元格 span>=24；旧写法看行容器是否只有一列
        pid = parents.get(f.attrib.get("id"))
        full_row = span_by_name.get(name, 0) >= 24
        if not full_row:
            parent = by_id.get(pid)
            if parent is not None and parent.attrib.get("fieldType") == "grid":
                try:
                    popts = json.loads(parent.attrib.get("fieldOptions", "{}"))
                    full_row = len(popts.get("columns") or []) == 1
                except json.JSONDecodeError:
                    pass
        out.append(FormField(
            form_key=form.key,
            name=name,
            label=f.attrib.get("label", name),
            field_type=ftype,
            # 明细表控件（dataTemplate）引用的是列表模板 key，存在
            # bind_template_key 里，不是 value_source/linkdata——
            # 漏了这一句，反解再出包时子表指向空列表，校验会报错
            value_source=(opts.get("value_source") or opts.get("linkdata")
                          or opts.get("bind_template_key") or ""),
            # 级联：系统只在打开 cascadeEnabled 时才认 cascadeParentCode
            cascade_parent=(opts.get("cascadeParentCode") or "") if opts.get("cascadeEnabled") else "",
            placeholder=opts.get("placeholder", "请输入"),
            required=str(opts.get("required")) == "true",
            readonly=bool(opts.get("disabled")),
            full_row=full_row,
            group=_group_of(f.attrib.get("id")),
            desc=f.attrib.get("desc", ""),
        ))
        form.fields.append(out[-1])

    _parse_hide_rules(form, ext)
    return form, out


def _parse_hide_rules(form: Form, ext: dict) -> None:
    """反解 extendAttr.formRules → HideRule 列表。

    一条记录一个条件一个动作；一条记录里塞了多个条件时（系统设计器也这么存），
    每个条件拆成一条 HideRule，导回去是等效的（都是并列 OR）。
    """
    for raw in ext.get("formRules") or []:
        if raw.get("action") != "hidden":
            continue
        clear = raw.get("clearOnHide") != "none"
        for cond in ((raw.get("condition") or {}).get("rules") or []):
            field = cond.get("field") or {}
            value = cond.get("value")
            form.hide_rules.append(HideRule(
                form_key=form.key,
                target=raw.get("controlName", ""),
                source=field.get("name", ""),
                operator=cond.get("operator") or "equal",
                value="" if value is None else str(value),
                value_label=cond.get("value_label") or "",
                clear_on_hide=clear,
            ))


def _parse_template(root) -> tuple[ListDef | None, DataSet | None]:
    defs = _child(root, "dataTemplateDefs")
    if defs is None:
        return None, None
    d = _child(defs, "dataTemplateDef")
    if d is None:
        return None, None
    key = d.attrib.get("key", "")
    name = d.attrib.get("name", key)
    dtype = d.attrib.get("type", "default")
    try:
        attrs = json.loads(d.attrib.get("attrs", "{}"))
    except json.JSONDecodeError:
        attrs = {}
    ds_field = _child(defs, "dataTemplateField")
    tbl = ds_field.attrib.get("objName", "") if ds_field is not None else ""

    if dtype == "default":
        views: list[str] = []
        columns: list[str] = []
        for tpl in _children(defs, "dataTemplateTpl"):
            try:
                a = json.loads(tpl.attrib.get("attrs", "{}"))
            except json.JSONDecodeError:
                a = {}
            v = a.get("template_renderview_type")
            if v and v not in views:
                views.append(v)
            if not columns and tpl.attrib.get("displayColumns"):
                try:
                    cols = json.loads(tpl.attrib["displayColumns"])
                except json.JSONDecodeError:
                    cols = []
                columns = [c.get("name", "") for c in cols]
        lst = ListDef(
            key=key, name=name, bo_code=attrs.get("boCode", ""),
            views=views or ["grid"], columns=columns,
            tree=bool((attrs.get("tree_config") or {}).get("pid_key")),
            parent_field="",
            title_field="",
        )
        return lst, None

    ds = DataSet(
        key=key, name=name, from_table=d.attrib.get("datasetKey", "") or tbl,
        ds_type=dtype, unique_field=d.attrib.get("uniqueField", "id_"),
    )
    return None, ds


def inspect_path(path: Path) -> tuple[Project, list[str]]:
    proj = Project()
    notes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for kind, key, root, src in _iter_xml(path):
        # 样例目录里常有「xx_副本」，同一 key 只取第一份
        if (kind, key) in seen:
            notes.append(f"{src}: {kind}/{key} 重复，已忽略")
            continue
        seen.add((kind, key))
        try:
            if kind == "bo":
                bo = _parse_bo(root)
                if bo:
                    proj.bos.append(bo)
            elif kind == "form":
                got = _parse_form(root)
                if got:
                    form, _ = got
                    proj.forms.append(form)
            elif kind == "data_template":
                lst, ds = _parse_template(root)
                if lst:
                    proj.lists.append(lst)
                if ds:
                    proj.datasets.append(ds)
            else:
                notes.append(f"{src}: 跳过未知目录 {kind}")
        except Exception as exc:  # 单个文件失败不影响整体
            notes.append(f"{src}: 解析 {key} 失败（{exc}）")

    # 列表/数据集的对象编码：用 attr 中的 boCode，缺失时按表名反查
    tbl_to_code = {b.tbl_name: b.code for b in proj.bos}
    for lst in proj.lists:
        if not lst.bo_code:
            lst.bo_code = next((c for t, c in tbl_to_code.items() if t), "")

    # 列表列在系统里存的是物理列名，转回字段编码；系统内置字段没有对应编码，直接丢弃
    for lst in proj.lists:
        bo = proj.bo(lst.bo_code)
        if bo is None:
            continue
        by_col = {f.resolved_field_name(): f.code for f in bo.fields}
        mapped: list[str] = []
        dropped: list[str] = []
        for name in lst.columns:
            if name in by_col:
                mapped.append(by_col[name])
            else:
                dropped.append(name)
        lst.columns = mapped
        if dropped:
            notes.append(
                f"列表 {lst.key}: 列 {', '.join(dropped)} 对应系统内置字段或已删除字段，已省略"
            )
    proj.module_id = next((b.module_id for b in proj.bos if b.module_id), "")
    proj.type_id = next((b.type_id for b in proj.bos if b.type_id), "")
    return proj, notes
