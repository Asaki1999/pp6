"""列表 / 数据集 -> data_template/001-{key}.xml"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

from .ids import new_id
from .model import Bo, DataSet, ListDef
from .xmlw import DECL_STANDALONE, el, json_attr, wrap

_VIEW_LABEL = {"grid": "表格视图", "board": "看板视图", "gantt": "甘特视图", "chart": "图表视图"}

# 系统里的时间戳格式：2025-05-22T14:52:26+08:00（带时区偏移）
_CST = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")

_FIELD_TYPE = {"number": "number", "datetime": "date", "dic": "select"}


def _col_type(field) -> str:
    return _FIELD_TYPE.get(field.type_new, "text")


def _build_columns(bo: Bo, listdef: ListDef, snip_col: dict) -> tuple[list[dict], list[str]]:
    notices: list[str] = []
    wanted = listdef.columns or [f.code for f in sorted(bo.fields, key=lambda x: x.sn)]
    out: list[dict] = []
    for code in wanted:
        f = bo.field_by_code(code)
        if f is None:
            notices.append(f"列表 {listdef.key}: 显示字段 {code} 在对象 {bo.code} 中不存在，已跳过")
            continue
        col = copy.deepcopy(snip_col)
        col["origLabel"] = f.name
        col["label"] = f.name
        col["name"] = f.resolved_field_name()
        col["dataType"] = f.resolved()["datatype"]
        col["objName"] = bo.tbl_name
        col["objLabel"] = bo.name
        col["field_type"] = _col_type(f)
        col["fieldType"] = _col_type(f)
        col["align"] = "right" if f.type_new == "number" else "left"
        col["sortable"] = True
        out.append(col)
    return out, notices


def _patch_buttons(raw: str, bo: Bo, form_key: str) -> str:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    for btn in data.get("toolbar_buttons", []) or []:
        cfg = btn.get("cascade_configuration") or {}
        if cfg.get("type") == "form":
            cfg["formKey"] = form_key
            cfg["boKey"] = "id"
        btn["permission_object_type"] = bo.code
        if btn.get("button_type") == "custom":
            btn["permission_key"] = form_key
    for btn in data.get("function_buttons", []) or []:
        btn["permission_object_type"] = bo.code
        btn.pop("permission_key", None)
    return json_attr(data)


def render_list(listdef: ListDef, bo: Bo, form_key: str, snip: dict, tenant: str = "-999") -> tuple[str, list[str]]:
    notices: list[str] = []
    # 库里有同 key 的列表模板 → 沿用原 id（导入按更新处理）
    tpl_id = listdef.exist_key or new_id()
    if listdef.exist_key:
        notices.append(f"列表 {listdef.key}: 库里已有同 key，沿用模板 id {listdef.exist_key}")
    def_src = dict(snip["def"].get("default") or {})
    col_snip = snip.get("column") or {}

    attrs = json.loads(def_src.get("attrs") or "{}")
    attrs["boCode"] = bo.code
    attrs["form_key"] = form_key
    attrs["title"] = listdef.name
    attrs["script"] = ""
    attrs["query_columns"] = []
    attrs["filter_conditions"] = []
    attrs["views"] = [{"type": v, "label": _VIEW_LABEL.get(v, v)} for v in listdef.views]

    def_attrs = dict(def_src)
    def_attrs.update({
        "id": tpl_id,
        "datasetKey": bo.code,
        "name": listdef.name,
        "key": listdef.key,
        "type": "default",
        "showType": def_src.get("showType") or "dimension",
        "composeType": def_src.get("composeType") or "treeList",
        "unique": "id_",
        "uniqueField": "id_",
        "attrs": json_attr(attrs),
    })
    # typeId = 列表挂在哪个分类节点下（iform_cat_type 里
    # category_key_='DATA_TEMPLATE_TYPE' 的那棵树）。**不能留骨架里的旧值**——
    # def_src 是从某个具体样例抄来的，它的 typeId 指向那个样例的模块分类，
    # 带过来会让新列表挂到别人模块下。宁可留空（库里 419 个列表有 15 个是空的），
    # 也不能指向错的地方。要精确指定就用 listdef.type_id。
    def_attrs["typeId"] = listdef.type_id or ""
    # 这 4 个属性官方包里都有，缺了就成结构差异；但骨架里的是 2025 年的旧时间戳，
    # 原样搬过来会让新列表显示成一年前创建。所以保留键、换成当下的时间，
    # createBy/updateBy 交给系统按当前登录人填，这里置空。
    now = _now_iso()
    def_attrs["createTime"] = now
    def_attrs["updateTime"] = now
    def_attrs["createBy"] = ""
    def_attrs["updateBy"] = ""
    body = [wrap("dataTemplateDef", [el("tenantId", "", tenant, indent=3)], def_attrs, indent=2)]
    body.append(el("dataTemplateField", {
        "id": new_id(),
        "templateId": tpl_id,
        "name": bo.tbl_name,
        "label": listdef.name,
        "objName": bo.tbl_name,
    }, indent=2))

    columns, col_notices = _build_columns(bo, listdef, col_snip)
    notices.extend(col_notices)
    toolbar_form_key = form_key

    for view in listdef.views:
        src = snip["tpl"].get(view)
        if not src:
            notices.append(f"列表 {listdef.key}: 样例中没有 {view} 视图，已跳过")
            continue
        tpl_attrs = dict(src)
        tpl_attrs["id"] = new_id()
        tpl_attrs["templateId"] = tpl_id

        if view == "chart":
            tpl_attrs["attrs"] = json_attr({"form_info": {}, "form_rights": {}, "template_renderview_type": "chart"})
        else:
            a = json.loads(tpl_attrs.get("attrs") or "{}")
            a["template_renderview_type"] = view
            if isinstance(a.get("tree_config"), dict):
                a["tree_config"].update({
                    "pid_key": bo.field_by_code(listdef.parent_field).resolved_field_name()
                    if listdef.tree and listdef.parent_field and bo.field_by_code(listdef.parent_field) else "",
                    "expand_row_key": _title_field_name(bo, listdef),
                    "root_pid": "",
                    "root_script": "",
                })
            if "display_field" in a:
                a["display_field"] = _title_field_name(bo, listdef)
            if view == "board":
                a["titleField"] = _title_field_name(bo, listdef)
                # 看板必须有分组维度，否则卡片全堆一列没法用。
                # 优先用对象上的「选项/关联」字段（状态、类型这类天然适合分列），
                # 没有再退到 listdef.parent_field，最后才留空。
                groups = _board_group_fields(bo, listdef, col_snip)
                if not groups:
                    # 没有 dic 字段就不出看板——比碎成上百列强
                    notices.append(
                        f"列表 {listdef.key}: 没有「选项」类型的字段可做分列，看板视图已跳过")
                    continue
                a["groupField"] = [g["name"] for g in groups]
                a["groupFields"] = groups
            if view == "gantt":
                start, end = _gantt_range(bo, listdef)
                if start is None:
                    notices.append(f"列表 {listdef.key}: 没有 datetime 字段，甘特视图缺少开始/结束时间")
                    continue
                a["startDateField"] = start.resolved_field_name()
                a["endDateField"] = end.resolved_field_name()
                a["progressField"] = _first_of_type(bo, "number").resolved_field_name() \
                    if _first_of_type(bo, "number") else ""
                if start is end:
                    notices.append(
                        f"列表 {listdef.key}: 只有一个日期字段，甘特图开始/结束用了同一列，"
                        f"建议再加一个截止日期字段")
            tpl_attrs["attrs"] = json_attr(a)
            tpl_attrs["displayColumns"] = json_attr(columns)
            if "functionButtons" in tpl_attrs:
                tpl_attrs["functionButtons"] = _patch_buttons(tpl_attrs["functionButtons"], bo, toolbar_form_key)
            if listdef.sort_field and "sortColumns" in tpl_attrs:
                f = bo.field_by_code(listdef.sort_field)
                if f:
                    col = copy.deepcopy(col_snip)
                    col.update({
                        "origLabel": f.name,
                        "label": f.name,
                        "name": f.resolved_field_name(),
                        "dataType": f.resolved()["datatype"],
                        "objName": bo.tbl_name,
                        "objLabel": bo.name,
                        "direction": listdef.sort_dir or "desc",
                    })
                    col.pop("field_options", None)
                    tpl_attrs["sortColumns"] = json_attr([col])

        body.append(wrap("dataTemplateTpl", [el("tenantId", "", tenant, indent=3)], tpl_attrs, indent=2))

    if not columns:
        notices.append(f"列表 {listdef.key}: 没有任何可用显示字段")

    # 列表底层的 dataSet（系统导出中始终存在，缺失会导致列表取不到数）
    body.append(_dataset_element(snip, key=bo.code, name=bo.name, table=bo.tbl_name, tenant=tenant))

    xml = "\n".join([
        DECL_STANDALONE,
        "<dataTemplate>",
        "  <dataTemplateDefs>",
        *body,
        "  </dataTemplateDefs>",
        "</dataTemplate>",
    ]) + "\n"
    return xml, notices


def _dataset_element(snip: dict, key: str, name: str, table: str, tenant: str) -> str:
    """列表/值来源底层的 dataSet 节点（系统导出中始终存在）。"""
    ds_attrs = dict(snip.get("dataSet") or {})
    ds_attrs.update({
        "id": new_id(),
        "name": name,
        "key": key,
        "type": "table",
        "external": ds_attrs.get("external") or "N",
        "dsAlias": ds_attrs.get("dsAlias") or "dataSource_default",
        "from": table,
        "parentFrom": "",
        "sql": "",
        "isTree": "N",
        "isDefaultSort": "Y",
        "dataFrom": "singleTable",
        "dataTables": "",
        "selectFields": "",
        "joinFields": "",
    })
    return wrap("dataSet", [el("tenantId", "", tenant, indent=3)], ds_attrs, indent=2)


def _title_field_name(bo: Bo, listdef: ListDef) -> str:
    if listdef.title_field:
        f = bo.field_by_code(listdef.title_field)
        if f:
            return f.resolved_field_name()
    for f in bo.fields:
        if f.code in ("name", "title"):
            return f.resolved_field_name()
    return bo.fields[0].resolved_field_name() if bo.fields else ""


def _first_of_type(bo: Bo, type_new: str):
    for f in sorted(bo.fields, key=lambda x: x.sn):
        if f.type_new == type_new:
            return f
    return None


# 看板分列只认「选项」类型（状态/级别/紧急程度）。
# 人员、部门、关联对象一概不行——「按项目分列」会把看板碎成几十上百列。
# 和 ai_list._BOARDABLE 保持一致，两处规则不同会互相打脸。
_BOARD_GROUP_TYPES = ("dic",)


def _board_group_fields(bo: Bo, listdef: ListDef, snip_col: dict) -> list[dict]:
    """给看板挑分组维度。

    系统里 groupField 是物理列名数组，groupFields 是完整列定义，
    两个必须对上，否则看板拉不出分列。

    优先级（实测系统里的看板都按「状态 + 类型」分列）：
      1. 选项字段（dic）—— 状态、级别这类，天然适合做列
      2. 名字里带 state/status/type/level 的关联字段
      3. listdef.parent_field（树形列表的父字段，通常就是分类）
      4. 其余关联/人员字段
    最多两列，再多界面挤成一团。
    """
    def score(f):
        """按「做看板分列时的自然程度」打分，数字越小越优先。

        光看类型不够：实测样例里 `汇报类型`(dic) 会排在 `项目状态`(object) 前面，
        但项目列表的看板显然是按状态分的。所以先看名字语义，再看类型。
        """
        n = f.code.lower()
        if any(k in n for k in ("state", "status")):
            return 0            # 状态永远是最合适的分列
        if any(k in n for k in ("stage", "phase")):
            return 1            # 阶段
        if any(k in n for k in ("type", "category", "kind", "level", "grade")):
            return 2            # 类型/级别
        return 3                # 其它选项字段

    # AI 明确指定了分列字段就用它（ai_list 会给），否则按语义打分自动挑
    chosen = bo.field_by_code(listdef.board_group) if listdef.board_group else None
    if chosen is not None:
        rest = [f for f in bo.fields
                if f is not chosen and f.type_new in _BOARD_GROUP_TYPES]
        picks = [chosen] + sorted(rest, key=lambda f: (score(f), f.sn))[:1]
    else:
        pool = [f for f in bo.fields
                if f.type_new in _BOARD_GROUP_TYPES
                and f.code != (listdef.title_field or "")]
        picks = sorted(pool, key=lambda f: (score(f), f.sn))[:2]

    out = []
    for f in picks:
        col = copy.deepcopy(snip_col) if snip_col else {}
        col.update({
            "origLabel": f.name,
            "label": f.name,
            "name": f.resolved_field_name(),
            "dataType": f.resolved()["datatype"],
            "objName": bo.tbl_name,
            "objLabel": bo.name,
        })
        col.pop("field_options", None)
        out.append(col)
    return out


def _gantt_range(bo: Bo, listdef: ListDef):
    """甘特图取开始/结束两个日期。

    以前两个都填第一个 datetime，等于所有条都变成零长度，
    甘特图看着就是一条竖线。这里按「开始/结束」语义挑两个不同的列。
    """
    # AI 指定了起止就用它
    if listdef.gantt_start and listdef.gantt_end:
        s_ = bo.field_by_code(listdef.gantt_start)
        e_ = bo.field_by_code(listdef.gantt_end)
        if s_ is not None and e_ is not None and s_.type_new == "datetime" \
                and e_.type_new == "datetime":
            return s_, e_

    dates = [f for f in sorted(bo.fields, key=lambda x: x.sn) if f.type_new == "datetime"]
    if not dates:
        return None, None
    start_kw = ("start", "begin", "kickoff")
    end_kw = ("end", "due", "finish", "deadline", "close", "expire")

    def has(f, kws):
        return any(k in f.code.lower() for k in kws)

    start = next((f for f in dates if has(f, start_kw)), dates[0])
    end = next((f for f in dates if f is not start and has(f, end_kw)), None)
    if end is None:
        rest = [f for f in dates if f is not start]
        end = rest[0] if rest else start
    return start, end


def render_dataset(ds: DataSet, bo: Bo | None, snip: dict, tenant: str = "-999") -> tuple[str, list[str]]:
    """值来源 / 数据集：下拉框数据来源。"""
    notices: list[str] = []
    tpl_id = new_id()
    def_src = dict(snip["def"].get(ds.ds_type) or {})
    attrs = json.loads(def_src.get("attrs") or "{}")
    attrs.setdefault("uniqueType", "varchar")
    attrs.setdefault("uniqueFieldType", "varchar")

    def_attrs = dict(def_src)
    def_attrs.update({
        "id": tpl_id,
        "datasetKey": ds.key,
        "name": ds.name,
        "key": ds.key,
        "type": ds.ds_type,
        "showType": def_src.get("showType") or "list",
        "composeType": def_src.get("composeType") or "treeList",
        "unique": ds.unique_field,
        "uniqueField": ds.unique_field,
        "attrs": json_attr(attrs),
    })
    # 同 render_list：typeId / 时间戳不能继承骨架里的旧值（骨架来自某个具体样例，
    # 带过来会把新数据集挂到别人的模块分类下、显示成一年前创建）
    def_attrs["typeId"] = getattr(ds, "type_id", "") or ""
    now = _now_iso()
    def_attrs["createTime"] = now
    def_attrs["updateTime"] = now
    def_attrs["createBy"] = ""
    def_attrs["updateBy"] = ""
    body = [wrap("dataTemplateDef", [el("tenantId", "", tenant, indent=3)], def_attrs, indent=2)]
    body.append(el("dataTemplateField", {
        "id": new_id(),
        "templateId": tpl_id,
        "name": ds.from_table,
        "label": ds.name,
        "objName": ds.from_table,
    }, indent=2))

    result_columns = [
        {"origLabel": "主键", "label": "主键", "name": ds.id_field, "dataType": "varchar",
         "objName": ds.from_table, "objLabel": ds.name},
        {"origLabel": "名称", "label": "名称", "name": ds.name_field, "dataType": "varchar",
         "objName": ds.from_table, "objLabel": ds.name},
    ]
    body.append(wrap("dataTemplateTpl", [el("tenantId", "", tenant, indent=3)], {
        "id": new_id(),
        "templateId": tpl_id,
        "resultColumns": json_attr(result_columns),
        "attrs": json_attr({"sqlOptimize": True, "toRepeat": True}),
    }, indent=2))

    ds_attrs = dict(snip.get("dataSet") or {})
    body.append(_dataset_element(snip, key=ds.key, name=ds.name, table=ds.from_table, tenant=tenant))

    xml = "\n".join([
        DECL_STANDALONE,
        "<dataTemplate>",
        "  <dataTemplateDefs>",
        *body,
        "  </dataTemplateDefs>",
        "</dataTemplate>",
    ]) + "\n"
    return xml, notices
