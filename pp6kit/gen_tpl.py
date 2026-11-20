"""列表 / 数据集 -> data_template/001-{key}.xml"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

from .ids import new_id
from .model import Bo, DataSet, ListDef
from .xmlw import DECL_STANDALONE, el, json_attr, wrap

# 值来源的过滤/返回列推导只信 links 一处（rule_id 必须与表单 link_condition 同源）
from . import links as _links

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


def _object_code_of(target) -> tuple[str, str]:
    """值来源的 ``datasetKey`` / ``boCode``：**目标对象编码**（两处必须同值）。

    返回 ``(对象编码, 推导说明)``；推导说明非空时由调用方出 notice。

    为什么要单独抽出来：``datasetKey`` 以前写成了 ``ds.key``（这份值来源自己的
    标识），与线上样本对不上——``out/ref-20261119/data_template/005-zlyglxmlx.xml``
    是 ``datasetKey="project" key="zlyglxmlx" boCode="project"``，
    ``docs/线上抓取-20261112/selector_list.json`` 那 203 条选择器**没有一条**
    ``datasetKey == key``（如 ``key=pp_classification_value`` /
    ``datasetKey=classification``）。写成 key 等于让「数据集外键」指向自己。

    优先级与 ``links.LinkPlan.target`` 的解析链一致：
      ① 本包对象 / 系统清单 / 字段快照 → ``target.code`` 就是对象编码，直接用；
      ② 都没有（反解包、手写 Excel 只给了物理表名）→ ``target.code`` 是表名，
         按平台命名规则去掉 ``pp_`` 前缀当编码，**并在 notice 里说明是推导的**
         —— 宁可说出来，也不能静默把表名或 key 塞进 datasetKey。
    """
    code = str(getattr(target, "code", "") or "").strip()
    if str(getattr(target, "source", "") or "") != "offline":
        return code, ""
    if not code.startswith("pp_") or len(code) <= 3:
        return code, ""
    derived = code[3:]
    return derived, (f"datasetKey / boCode 由物理表名 {code} 推导为 {derived}"
                     f"（对象编码既不在本包，也没在系统清单 / 字段快照里找到）")


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


def _result_column(target, f) -> dict:
    """值来源的返回列（resultColumns 的一项）。

    逐键对照样本 005-zlyglxmlx.xml：rights/same/field_type/field_options 是
    「原始列、所有角色可见」的固定写法，objName 是**目标对象的表名**（不是本对象）。
    """
    label = f.label or f.code
    return {
        "rights": [{"type": "all"}],
        "same": "Y",
        "field_type": "orig",
        "field_options": {},
        "origLabel": label,
        "label": label,
        "name": f.column,
        "dataType": f.datatype or "varchar",
        "objName": target.table,
        "objLabel": target.label,
        "align": "center",
    }


def _filter_conditions(ds: DataSet, filt) -> list[dict]:
    """filterConditions：值来源按表单控件过滤。

    与表单 link_condition 共用同一个 rule_id（links.stable_code 派生），
    两侧一旦不同，系统就认不出「这个条件听表单哪个控件」——下拉里永远是全量，
    而表单上看着是配好的。
    """
    rule = {
        "id": filt.column,
        "field": filt.column,
        "label": filt.label,
        "type": filt.type,
        "input": filt.input,
        "operator": "equal",
        "source": "dynamic",
        "value": "text",
        "field_source": "formField",
        "field_date_type": "",
        "field_custom_format": "",
        "field_type": filt.type,
        "field_input": "text",
        "rule_id": filt.rule_id,
        "desc": filt.rule_id,
        "date_type": "",
        "custom_format": "",
        "origLabel": filt.label,
    }
    return [{
        "label": "默认条件",
        "key": _links.filter_key(f"{ds.key}:{filt.column}"),
        "type": "condition",
        "rights": [{"type": "all"}],
        "filter": {"condition": "AND", "rules": [rule]},
        "addType": {"isTrusted": True},
        "combinationName": "",
        "formFieldSource": "template",
    }]


def _rule_dict(rule) -> dict:
    """一条过滤规则 -> 线上 ``filterConditions[].filter.rules[]``（键序与老路径一致）。

    与 legacy ``_filter_conditions`` 的差别只有两处，且都有线上 333 条规则的统计
    依据（见 docs/架构-值来源过滤条件-20261120.md §5）：

      · ``field_type`` / ``field_input`` 恒 ``string`` / ``text``
        ——线上 333/333（含 type=number、input=number 的列）都是这两个值；
      · ``type`` / ``input`` 仍随列类型变（number 列 → number/number），
        它们才是决定算子集合的那一对。
    """
    label = rule.label or rule.field
    return {
        "id": rule.field,
        "field": rule.field,
        "label": label,
        "type": rule.type or "string",
        "input": rule.input or "text",
        "operator": rule.operator,
        "source": rule.source,
        "value": rule.resolved_value(),
        "field_source": rule.field_source,
        "field_date_type": "",
        "field_custom_format": "",
        "field_type": "string",
        "field_input": "text",
        "rule_id": rule.rule_id,
        "desc": rule.rule_id,
        "date_type": "",
        "custom_format": "",
        "origLabel": label,
    }


def _filter_conditions_rules(ds: DataSet, rules: list) -> list[dict]:
    """多条规则 -> 一个条件组（线上 263/266 份模板只有 1 个组）。

    组内 ``condition`` 取数据集上的 AND/OR（线上 AND=269 / OR=1，默认 AND）。
    组的 key 由 rule_id 一起派生：规则集不同就是另一个组，反复出包不变。
    """
    seed = f"{ds.key}:" + ",".join(str(r.rule_id) for r in rules)
    return [{
        "label": "默认条件",
        "key": _links.filter_key(seed),
        "type": "condition",
        "rights": [{"type": "all"}],
        "filter": {
            "condition": ds.filter_logic if ds.filter_logic in ("AND", "OR") else "AND",
            "rules": [_rule_dict(r) for r in rules],
        },
        "addType": {"isTrusted": True},
        "combinationName": "",
        "formFieldSource": "template",
    }]


def render_dataset(ds: DataSet, bo: Bo | None, snip: dict, tenant: str = "-999",
                   link=None) -> tuple[str, list[str]]:
    """值来源 / 数据集：下拉框数据来源。

    形状逐键对照 docs/参考-关联对象值来源-20261119.md §2：

      · dataTemplateDef   type=valueSource，unique/uniqueField **默认主键 id_**
      · dataTemplateField 首条是**表名**（无 fieldType），其后每个需要选择控件/
        候选值的列一条（带 fieldType/fieldOptions/sn）
      · dataTemplateTpl   filterConditions + resultColumns（JSON），
        resultColumns **强制包含主键**（没有主键值来源取不到值）

    ``link`` 是 links.LinkPlan：目标对象可能不在本包里（project/customer 之类
    的系统对象），要靠它去系统元数据/离线兜底里取字段清单。
    """
    notices: list[str] = []
    if link is not None and getattr(link, "project", None) is not None:
        target = link.target(ds.from_table) or (
            _links.target_from_bo(bo) if bo is not None else None)
    else:
        # 直接调（回归夹具/单测）没有 project 上下文：本包对象从 bo 拿，
        # 其余走离线兜底，保证不因为没有项目上下文就把值来源出成空列。
        target = (_links.target_from_bo(bo) if bo is not None else None) \
            or _links.offline_target(ds.from_table, ds.name)
    if target is None or not target.code:
        target = _links.offline_target(ds.from_table or ds.key, ds.name)
    # datasetKey / boCode 用同一个值：**目标对象编码**。解析链（本包 → 系统清单 →
    # 字段快照 → 离线兜底）在 links.LinkPlan 里，这里只处理「只剩物理表名」那一档。
    object_code, code_note = _object_code_of(target)
    if code_note:
        notices.append(f"值来源 {ds.key}: {code_note}")

    results, explicit = target.result_list(ds.result_fields,
                                           all_fields=ds.result_all)
    # 过滤条件要从「谁在用这份值来源」那一侧推导：ds 只记目标对象，
    # 源对象必须回查表单（见 links.LinkPlan.source_of）。显式配了
    # `过滤字段=目标列` 的，目标列直接定；只写源字段的按同名推导。
    src_bo, src_bf = link.source_of(ds.key) if link is not None else (None, None)
    # 返回列与表单侧 link_linkage 必须同一份（见 links.with_linked_columns）：
    # 自动联动到的列（专业/合同金额这种）也要真的出现在 resultColumns 里，
    # 否则联动的是一条值来源不返回的列，选了值带不过来。
    results = _links.with_linked_columns(target, src_bo, src_bf, results, explicit)
    # 过滤规则（2026-11-20）：归一化只在 links 一处做，值来源侧与表单侧读同一份。
    # rules is None → 老口径（继续走 filter_of，产物逐字不变）；
    # rules == []   → 明确「一条过滤都不要」（Excel 写 `无`）；
    # 非空          → 完全按规则，legacy 的过滤字段/过滤目标不再参与。
    if link is not None:
        rules, rule_notes = link.rules_of(ds, target, src_bo, src_bf)
    else:
        rules, rule_notes = _links.filter_rules_of(ds, target, src_bo, src_bf)
    notices.extend(rule_notes)
    filt = None if rules is not None else _links.filter_of(ds, target, src_bo, src_bf)
    if filt is not None and not ds.filter_target:
        # 回写目标列：gen_form 的 link_condition 与 dataTemplateField 的
        # 候选列都读这一个值，不回写就会两边各算一遍、有分叉风险。
        ds.filter_target = filt.column
    if filt is not None and filt.rebound_from:
        # 配置里写的过滤字段在出包时已经不存在（对齐改名 / 反解包换了编码），
        # 已按推导出的同名字段绑定。必须说出来：否则用户对着 Excel 里的
        # 「过滤字段=xiangMuLeiXing」看，会以为过滤条件是他当初配的那一列。
        notices.append(
            f"值来源 {ds.key}: 过滤字段 {filt.rebound_from} 已不存在"
            f"（可能被对齐改名），已按「{filt.source_label}」绑定")
    if rules is None and ds.filter_field and filt is None:
        notices.append(
            f"值来源 {ds.key}: 过滤字段 {ds.filter_field} 在目标对象 {target.code} 里"
            f"找不到对应的列，已按不过滤生成（请到 6.1_值来源 补「过滤目标」，或写「过滤字段=目标列」）")
    if explicit and not any(f.column == target.name_column for f in results):
        notices.append(f"值来源 {ds.key}: 名称列 {target.name_column} 已强制加入返回列"
                       f"（控件上显示的就是它）")

    # 值来源底层的物理表：dataSet.from 与首条 dataTemplateField 的 name/objName
    # 必须是同一个值（样本 005-zlyglxmlx.xml：<dataSet … from="pp_project"> 配
    # <dataTemplateField name="pp_project" objName="pp_project"/>）。ds.from_table
    # 里我们自己的行写的是**对象编码**（6_数据集 的「主表名」），反解来的包可能
    # 已经带 pp_ 前缀，两种都认；值来源最终是拿这张表取数的，写编码等于 FROM
    # 一张不存在的表。
    ds_table = str(ds.from_table or "")
    if not ds_table.startswith("pp_"):
        ds_table = target.table

    tpl_id = new_id()
    def_src = dict(snip["def"].get(ds.ds_type) or {})
    attrs = json.loads(def_src.get("attrs") or "{}")
    attrs.setdefault("uniqueType", "varchar")
    attrs.setdefault("uniqueFieldType", "varchar")
    # 样本 dataTemplateDef 的 attrs 里还有 title（= 值来源名称）：
    # {"title":"值来源_过滤项目类型","uniqueType":"varchar","uniqueFieldType":"varchar"}。
    # 缺了它，设计器里这份值来源的标题是空的，用户对着「值来源_xxx」的列表
    # 分不出哪份是自己刚建的。
    attrs.setdefault("title", ds.name)

    def_attrs = dict(def_src)
    def_attrs.update({
        "id": tpl_id,
        # boCode / dataAuthMode：值来源归属哪个对象、数据权限模式（0=按数据权限）。
        # 样本 005-zlyglxmlx.xml 两个键都在；缺 boCode 时设计器里看不出这份
        # 值来源是给哪个对象的，数据权限也会退回默认值。
        # ⚠ boCode 与 datasetKey **必须是同一个值**（目标对象编码）；只有 key 是
        # 这份值来源自己的标识。线上 203 条没有一条 datasetKey == key。
        "boCode": object_code,
        "dataAuthMode": def_src.get("dataAuthMode") or "0",
        # datasetKey = **目标对象的编码**，不是这份值来源自己的 key！
        # 依据（别再来回改）：
        #   · 线上 203 条选择器（docs/线上抓取-20261112/selector_list.json）全是
        #     datasetKey=对象编码、key=这份选择器自己的 slug；
        #   · 样本 out/ref-20261119/data_template/005-zlyglxmlx.xml：
        #     dataTemplateDef datasetKey="project" key="zlyglxmlx" boCode="project"，
        #     底下 <dataSet key="project" name="项目" from="pp_project">。
        # 以前写成 ds.key，等于让「数据集外键」指向自己、而底下那份 dataSet 的
        # key 又是另一个值——设计器里这份选择器挂不到真正的数据集上。
        # 列表那条（render_list）不动：样本 004-lllkk 的 datasetKey 是列表自己
        # 的 key（glxmd1111），与选择器是两套口径。
        "datasetKey": object_code,
        "name": ds.name,
        "key": ds.key,
        "type": ds.ds_type,
        "showType": def_src.get("showType") or "list",
        "composeType": def_src.get("composeType") or "treeList",
        "unique": ds.unique_field or target.id_column,
        "uniqueField": ds.unique_field or target.id_column,
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

    # 首条 = 表名（系统导出里每份 dataTemplate 都有一条 objName/name 同值的表行）。
    # name / objName 是**物理表名**（与 dataSet.from 同一个值），label 是**目标
    # 对象显示名**（样本是「项目」），不是值来源名——写成对象编码 + 值来源名的话，
    # 同一份 XML 里 dataSet 说 pp_project、这一行说 project，设计器按表名对数时
    # 对不上，界面上这份值来源就挂不到表上。
    body.append(el("dataTemplateField", {
        "id": new_id(),
        "templateId": tpl_id,
        "name": ds_table,
        "label": target.label or target.code,
        "objName": ds_table,
    }, indent=2))

    # 其后：需要「选择控件 + 候选值」的列各一条。过滤列也写一条——
    # 设计器的过滤条件面板就是按这些列选字段的，缺了它过滤条件选不出来。
    picker_cols: list = []
    if rules is not None:
        # 有规则时按**规则里的列**出 dataTemplateField：设计器的过滤条件面板
        # 就是按这些列选字段的，只看 ds.filter_target 的话规则里的列选不出来。
        for rule in rules:
            if rule.field_source != "formField":
                continue      # SQL 片段没有「目标列」，出不了候选值
            col = target.field(rule.field)
            if col is None or not col.needs_picker():
                continue
            if all(col.column != c.column for c in picker_cols):
                picker_cols.append(col)
    else:
        filter_field = target.field(ds.filter_target) if ds.filter_target else None
        if filter_field is not None and filter_field.needs_picker():
            picker_cols.append(filter_field)
    for col in results:
        if col.needs_picker() and all(col.column != c.column for c in picker_cols):
            picker_cols.append(col)
    for col in picker_cols:
        attr = {
            "id": new_id(),
            "templateId": tpl_id,
            "name": col.column,
            "label": col.label or col.code,
            "objName": ds_table,
            "fieldType": "select",
            "fieldOptions": json_attr(col.picker_options(target.code)),
        }
        if col.sn:
            attr["sn"] = col.sn
        body.append(el("dataTemplateField", attr, indent=2))

    tpl_attrs = {
        "id": new_id(),
        "templateId": tpl_id,
        "resultColumns": json_attr([_result_column(target, f) for f in results]),
        "attrs": json_attr({"sqlOptimize": True, "toRepeat": True}),
    }
    if rules:
        # 有规则（哪怕只有一条）：单条件组 + AND/OR。
        # 样本里 filterConditions 在 resultColumns 之前。
        tpl_attrs = {
            "id": tpl_attrs["id"],
            "templateId": tpl_id,
            "filterConditions": json_attr(_filter_conditions_rules(ds, rules)),
            "resultColumns": tpl_attrs["resultColumns"],
            "attrs": tpl_attrs["attrs"],
        }
    elif filt is not None:
        # 样本里 filterConditions 在 resultColumns 之前
        tpl_attrs = {
            "id": tpl_attrs["id"],
            "templateId": tpl_id,
            "filterConditions": json_attr(_filter_conditions(ds, filt)),
            "resultColumns": tpl_attrs["resultColumns"],
            "attrs": tpl_attrs["attrs"],
        }
    body.append(wrap("dataTemplateTpl", [el("tenantId", "", tenant, indent=3)],
                     tpl_attrs, indent=2))

    # 同一份 XML 里表名只有一个真相：dataSet.from、首条表名行、各列的 objName
    # 都用 ds_table（物理表），否则设计器按表名对数时会认为自己挂错表。
    # dataSet 的 key/name 跟着 datasetKey 走（= 目标对象编码 / 对象名），与
    # dataTemplateDef.datasetKey 是同一个值——样本 005：
    # <dataTemplateDef datasetKey="project"> + <dataSet key="project" name="项目" from="pp_project">。
    # 写成 ds.key/ds.name 的话，两条选择器会各自声明一份不同 key 的 dataSet，
    # 而外键指向的 project 谁也不认，导入后这份选择器取不到数。
    body.append(_dataset_element(snip, key=object_code,
                                 name=target.label or object_code,
                                 table=ds_table, tenant=tenant))

    xml = "\n".join([
        DECL_STANDALONE,
        "<dataTemplate>",
        "  <dataTemplateDefs>",
        *body,
        "  </dataTemplateDefs>",
        "</dataTemplate>",
    ]) + "\n"
    return xml, notices
