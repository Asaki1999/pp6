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

# 骨架里的按钮文案来自繁体界面的样例（關閉），生成包统一成简体——
# 不然同一张列表上「下一步」和「關閉」两种字形并存。
_LABEL_SIMPLE = {"關閉": "关闭"}


def _col_type(field) -> str:
    return _FIELD_TYPE.get(field.type_new, "text")


def _form_cascade(detail_form_key: str, source_field: str) -> dict:
    """「点这一列 / 这张卡片 → 全屏打开详情表单」的级联配置。

    逐键对照原生会议列表（config-sync-20261113002727）里那条能点开的配置：
    除了 type/formKey/casadeFields，还必须有 **inheritanceFormSetting** 和
    **pkValue**。少了这两个，导入时服务端会把 casadeFields 清空（实测导入后的
    test_tongbu_list 就是 `casadeFields: []`），点击虽然能弹表单，但拿不到
    行主键，详情页等于空壳。这是 2026-11-16 用户反馈「点了跳不到详情」的根因之一。

    id 交给服务端生成，这里不写（写了反而可能被当成另一颗控件）。
    """
    return {
        "type": "form",
        "openType": "fullscreen",
        "formKey": detail_form_key,
        "dataType": "edit",
        "boKey": "id",
        "pkValueType": "bo",
        "zoom": True,
        "close": "topRight",
        "bind_template_key": None,
        "templateId": None,
        "url": None,
        "bind_bpmn_key": None,
        "bpmnType": None,
        "bpmnValue": None,
        "required": False,
        "params": [],
        "casadeFields": [
            {"key": "id", "value": source_field, "valueType": "bo"}
        ],
        "inheritanceFormSetting": True,
        "pkValue": "id_",
    }


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


def _patch_menus(menus: list, bo: Bo, display_columns: list, form_keys: set[str],
                 import_form_key: str) -> list:
    """把「更多」下拉里的项目专属配置按本对象重写。

    样例来自项目列表，菜单里带着一堆本对象没有的东西：
      · 参数配置 → 打开 project_config_dev1 表单
      · 导入     → 校验表单 project_import_validation_form + 去重字段 code
      · 导出     → 预置 12 个 pp_project 的字段
    这些表/字段在生成包里都不存在，照抄导进去点开就是空表单或空导出。
    """
    kept: list = []
    for menu in menus or []:
        cfg = menu.get("cascade_configuration") or {}

        # 级联打开的表单必须在这个包里，否则这菜单点了没反应（或打开别人的表单）
        if cfg.get("type") == "form":
            if (cfg.get("formKey") or "") not in form_keys:
                continue
            menu["permission_object_type"] = bo.code
            menu["permission_key"] = ""

        kind = menu.get("button_type")

        if kind == "import":
            # 导入要靠「导入专用表单」做校验，包里没有就别留这个入口
            if not import_form_key:
                continue
            menu["importFormKey"] = import_form_key
            menu["importExcelFileName"] = f"导入{bo.name}模板"
            # 样例按 code 去重，新对象不一定有 code 字段；name 是系统必备字段
            menu["importUniqueFieldName"] = "name"
            menu["importUniqueFieldsName"] = "name"
            menu["permission_object_type"] = bo.code
            menu["permission_key"] = ""

        if kind == "export":
            ec = dict(menu.get("exportColumns") or {})
            ec["select_field"] = ec.get("select_field") or "N"
            ec["export_type"] = ec.get("export_type") or "page"
            # 导出列 = 当前列表的显示列，样例的 12 个项目字段全部作废
            ec["fields"] = copy.deepcopy(display_columns)
            menu["exportColumns"] = ec

        if isinstance(menu.get("label"), str):
            menu["label"] = _LABEL_SIMPLE.get(menu["label"], menu["label"])
        kept.append(menu)
    return kept


def _patch_buttons(raw: str, bo: Bo, form_key: str, new_label: str = "",
                   display_columns: list | None = None,
                   form_keys: set[str] | None = None,
                   import_form_key: str = "") -> str:
    """把样例里的按钮改成本对象的。

    骨架是从「项目」列表导出的，工具栏那个 custom 按钮写着「新建项目」、
    打开 create_project、挂着 project_create 权限项。以前只换了 formKey 和
    归属对象，**文案原样留着**——于是采购申请 / 合同 / 会议的列表上都挂着
    一个「新建项目」按钮（2026-11-16 用户指出）。按钮文案必须跟着
    「新建表单」的实际名字走，和点开后抽屉的标题对得上。

    「更多」下拉里的导入/导出/参数配置同理，见 _patch_menus。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    columns = display_columns or []
    keys = form_keys or set()
    kept_buttons: list = []
    for btn in data.get("toolbar_buttons", []) or []:
        cfg = btn.get("cascade_configuration") or {}
        if cfg.get("type") == "form":
            cfg["formKey"] = form_key
            cfg["boKey"] = "id"
        btn["permission_object_type"] = bo.code
        if btn.get("button_type") == "custom":
            # 只改「新建」那一颗：绑了表单级联的，或文案本来就是「新建…」的。
            # 将来骨架里多出「导入」「导出」之类自定义按钮，文案不能被覆盖。
            if new_label and (cfg.get("type") == "form"
                              or str(btn.get("label") or "").startswith("新建")):
                btn["label"] = new_label
            # 「新建」等自定义按钮默认**不绑权限项**（2026-11 反馈：新导入的列表
            # 一打开就多出一个权限项，用户得手动去删）。权限项留空后系统按
            # 「无权限控制」处理——按钮照样能点，只是不进权限树。
            # permission_object_type 仍写对象编码，那是按钮挂在哪个对象上的归属，
            # 与权限项不是一回事。要精确绑定时由用户在系统里自己选。
            btn["permission_key"] = ""
        if btn.get("button_type") == "more":
            btn["menus"] = _patch_menus(btn.get("menus") or [], bo, columns, keys,
                                        import_form_key)
            # 下拉里一项都不剩（甘特视图只有「参数配置」）→ 连「更多」一起收掉，
            # 留着就是个点了什么都不发生的空按钮
            if not btn["menus"]:
                continue
        kept_buttons.append(btn)
    if "toolbar_buttons" in data:
        data["toolbar_buttons"] = kept_buttons
    for btn in data.get("function_buttons", []) or []:
        btn["permission_object_type"] = bo.code
        btn.pop("permission_key", None)
    for btn in data.get("edit_buttons", []) or []:
        if isinstance(btn.get("label"), str):
            btn["label"] = _LABEL_SIMPLE.get(btn["label"], btn["label"])
    return json_attr(data)


def render_list(listdef: ListDef, bo: Bo, form_key: str, snip: dict, tenant: str = "-999",
                detail_form_key: str = "", new_button_label: str = "",
                form_keys: set[str] | None = None,
                import_form_key: str = "") -> tuple[str, list[str]]:
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
            # 甘特视图的 tooltip_field 是样例项目的 6 个字段（objName=pp_project），
            # 悬浮提示会去查别人的表 → 只留属于本对象的，滤空就整个去掉
            tf = a.get("tooltip_field")
            if isinstance(tf, list):
                tf = [x for x in tf if (x or {}).get("objName") in ("", bo.tbl_name)]
                if tf:
                    a["tooltip_field"] = tf
                else:
                    a.pop("tooltip_field", None)
            # 视图级「点行/卡片打开什么」：骨架是项目看板的 router
            # （/project/projectLayout/），只有「项目」对象才有这个页面。
            # 照抄会让新对象的看板卡片跳到别人的项目页（2026-11-16 用户反馈
            # 「在列表选了级联字段，导入后还是点不进详情」）。有详情表单就
            # 改成打开它；没有就去掉，别留一个指向别人页面的死链接。
            cc = a.get("cascade_configuration")
            if isinstance(cc, dict) and cc.get("type") != "form":
                if detail_form_key:
                    a["cascade_configuration"] = _form_cascade(
                        detail_form_key, _title_field_name(bo, listdef))
                else:
                    a.pop("cascade_configuration", None)
            tpl_attrs["attrs"] = json_attr(a)
            # 级联详情：给选中字段的列注入 cascade_configuration，
            # 点击该列 → 全屏打开详情表单（主键 bo 传过去）。
            # 格式照用户给的会议列表导出包逐键核对（config-sync-20261113002727）：
            # displayColumns[].cascade_configuration = {type:form, openType:fullscreen,
            #   formKey:详情表单, dataType:edit, boKey:id, pkValueType:bo, zoom:true,
            #   casadeFields:[{key:id, value:列name_, valueType:bo}], ...}
            if detail_form_key and listdef.detail_field:
                # 列的 name 是物理列名；detail_field 是字段编码，要换算
                f = bo.field_by_code(listdef.detail_field)
                if f is None:
                    notices.append(
                        f"列表 {listdef.key}: 级联详情字段 {listdef.detail_field} 不存在，已忽略")
                else:
                    col_name = f.resolved_field_name()
                    hit = next((c for c in columns if c.get("name") == col_name), None)
                    if hit is None:
                        # 选了「级联详情字段」但「显示字段」里没它：以前只记一条
                        # notice 就跳过，用户看到的是「明明选了级联，导入后点不动」。
                        # 级联必须有一列可点，这里直接把该列补到最前面。
                        hit = copy.deepcopy(col_snip)
                        hit.update({
                            "origLabel": f.name,
                            "label": f.name,
                            "name": col_name,
                            "dataType": f.resolved()["datatype"],
                            "objName": bo.tbl_name,
                            "objLabel": bo.name,
                            "field_type": _col_type(f),
                            "fieldType": _col_type(f),
                            "align": "right" if f.type_new == "number" else "left",
                            "sortable": True,
                        })
                        columns.insert(0, hit)
                        notices.append(
                            f"列表 {listdef.key}: 级联详情字段 {listdef.detail_field} "
                            f"不在显示字段里，已自动加到第一列")
                    hit["cascade_configuration"] = _form_cascade(detail_form_key, col_name)
                    # 原生那条能点的列还带这三个键（会议列表 name_ 列，
                    # 线上 23 列里只有这一列有）。只写 cascade_configuration
                    # 不带 enabledCascade/triggerEvent，部分前端版本不会把
                    # 单元格渲染成可点击的链接。
                    hit["enabledCascade"] = True
                    hit["triggerEvent"] = "font"
                    hit.setdefault("allowEdit", False)
                    notices.append(
                        f"列表 {listdef.key}: 点击「{f.name}」列将打开详情表单 {detail_form_key}")
            tpl_attrs["displayColumns"] = json_attr(columns)
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

        # 按钮改写在 if/else **外面**：chart 视图也带 functionButtons，
        # 原来只在 else 分支里改，图表视图的「新建」按钮就一直是样例的
        # create_project + 「新建项目」，点开是别人的表单。
        if "functionButtons" in tpl_attrs:
            tpl_attrs["functionButtons"] = _patch_buttons(
                tpl_attrs["functionButtons"], bo, toolbar_form_key, new_button_label,
                display_columns=columns, form_keys=form_keys,
                import_form_key=import_form_key)

        # 样例的「扩展列」是项目列表的 4 个组合列（项目类型/阶段/经理/部门），
        # 字段全在 pp_project 上；生成包只重建显示列，留着就是指向别人表的死列。
        # 按字段名过滤，一个都留不下就把整段去掉（board/gantt/chart 本来就没这个键）。
        if "extColumns" in tpl_attrs:
            try:
                ext_cols = json.loads(tpl_attrs["extColumns"] or "[]")
            except (TypeError, json.JSONDecodeError):
                ext_cols = []
            col_names = {c.get("name") for c in columns}
            ext_cols = [c for c in ext_cols if c.get("name") in col_names]
            if ext_cols:
                tpl_attrs["extColumns"] = json_attr(ext_cols)
            else:
                tpl_attrs.pop("extColumns", None)

        # 没指定排序字段时骨架会留着样例的排序列（objName=pp_project）。
        # 归属不改，别的列表就是按一个不属于自己的表在排序。
        if not listdef.sort_field and "sortColumns" in tpl_attrs:
            try:
                sort_cols = json.loads(tpl_attrs["sortColumns"] or "[]")
            except (TypeError, json.JSONDecodeError):
                sort_cols = []
            for c in sort_cols:
                c["objName"] = bo.tbl_name
                c["objLabel"] = bo.name
            if sort_cols:
                tpl_attrs["sortColumns"] = json_attr(sort_cols)

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
