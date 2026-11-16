"""打包：zip 目录结构、条目编号、包名、模块过滤。

包结构错了导入时直接报「缺少业务类型目录」，所以这一层断言最死：
顶层只能是 bo/ form/ data_template/ 这类英文目录，且每条都是裸 XML。
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime

from pp6kit import builder, spec as S, webui
from tests._harness import (case, field, pick, texts, unzip, xlsx, xml_ok)

ALLOWED_TOP = {"bo", "form", "data_template", "workflow", "filter"}


def build(sheets: dict, codes: list[str] | None = None):
    return webui.build_zip(xlsx(sheets), codes=codes)


def one_form_package() -> dict:
    return {
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
        S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                    "对象编码": "contract", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_contract", "字段编码": "mingCheng",
                         "显示标签": "名称", "控件类型": "text"}],
    }


@case("BLD01", "builder", "包顶层是英文业务类型目录", "P0",
      ["bo/ form/ data_template/", "中文目录会被服务端当成同步类型而失败"])
def _():
    body, _ = build(one_form_package())
    tops = {n.split("/")[0] for n in unzip(body)}
    assert tops <= ALLOWED_TOP, tops
    assert "bo" in tops


@case("BLD02", "builder", "条目名按 001/002 连续编号", "P0",
      ["服务端按序号排序导入", "跳号不影响但会让人以为漏了"])
def _():
    body, _ = build(one_form_package())
    names = sorted(unzip(body))
    nums = [int(m.group(1)) for n in names
            if (m := re.match(r"\w+/(\d+)-", n))]
    assert nums == list(range(1, len(nums) + 1)), names


@case("BLD03", "builder", "包内每个 XML 都能解析", "P0",
      ["坏一个整包导入失败"])
def _():
    body, _ = build(one_form_package())
    for name, text in texts(body).items():
        xml_ok(text)


@case("BLD04", "builder", "包名带业务名与日期", "P1",
      ["合同-导入包-20261112.zip", "一天多个包也能分辨"])
def _():
    _, name = build(one_form_package())
    assert name.endswith(".zip")
    assert "合同" in name
    assert datetime.now().strftime("%Y%m%d") in name


@case("BLD05", "builder", "只选业务对象时不带表单", "P0",
      ["用户想分步导入", "codes 过滤必须生效"])
def _():
    body, _ = build(one_form_package(), codes=["bo"])
    names = list(unzip(body))
    assert names == ["bo/001-contract.xml"], names


@case("BLD06", "builder", "只选表单时不带业务对象，但新建+详情两张都要出", "P0",
      ["codes=form", "单新建表单的包自动补详情表单"])
def _():
    body, _ = build(one_form_package(), codes=["form"])
    names = list(unzip(body))
    assert all(n.startswith("form/") for n in names), names
    assert any("create" in n for n in names), names
    assert any("details" in n for n in names), names


@case("BLD07", "builder", "列表进 data_template 目录", "P0",
      ["目录名决定导入到哪个模块"])
def _():
    body, _ = build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
        S.S_LIST: [{"列表key": "contract_list", "列表名称": "合同列表",
                    "对象编码": "contract", "显示字段": "mingCheng"}],
    })
    names = list(unzip(body))
    assert any(n.startswith("data_template/") and "contract_list" in n for n in names), names


@case("BLD08", "builder", "数据集进 data_template 目录", "P1",
      ["下拉取数来源"])
def _():
    body, _ = build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
        S.S_DATASET: [{"数据集key": "ds_contract", "数据集名称": "取合同",
                       "主表名": "pp_contract", "类型": "valueSource"}],
    })
    names = list(unzip(body))
    assert any(n.startswith("data_template/") for n in names), names


@case("BLD09", "builder", "同一对象的多个表单不撞条目名", "P1",
      ["create/detail 两张表", "撞名会覆盖丢文件"])
def _():
    body, _ = build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
        S.S_FORM: [
            {"表单key": "create_contract", "表单名称": "新建合同",
             "对象编码": "contract", "表单类型": "新建表单"},
            {"表单key": "detail_contract", "表单名称": "合同详情",
             "对象编码": "contract", "表单类型": "详情表单"},
        ],
        S.S_FORMFIELD: [
            {"表单key": "create_contract", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
            {"表单key": "detail_contract", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
        ],
    })
    names = [n for n in unzip(body) if n.startswith("form/")]
    assert len(names) == 2, names
    assert len(set(names)) == 2


@case("BLD10", "builder", "包名过滤掉路径分隔符等非法字符", "P2",
      ["对象名里有 / 时不能把目录撑开"])
def _():
    assert "/" not in builder.safe_filename("项目/合同")
    assert "\\" not in builder.safe_filename("项目\\合同")


@case("BLD11", "builder", "生成报告列出包内清单", "P1",
      ["告诉用户先导 bo 再导 form", "报告进 X-Build-Report"])
def _():
    from pp6kit.webui import _zip_report

    body, name = build(one_form_package())
    report = _zip_report(body)
    assert report["counts"]["bo"] == 1, report["counts"]
    assert any(e["name"].startswith("bo/") for e in report.get("entries", [])), report.get("entries")


@case("BLD12", "builder", "存在校验错误时不出包", "P0",
      ["宁可不给包，也别给一份导不进去的包"])
def _():
    try:
        webui.build_zip(xlsx({
            S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "PP_Contract"}],
            S.S_FIELD: [field("contract", "名称", "字符串")],
        }))
    except ValueError as exc:
        assert "校验错误" in str(exc), exc
    else:
        raise AssertionError("校验有错还出了包")


@case("BLD13", "builder", "完全没有内容时不出包", "P1",
      ["空包导入会报无业务类型"])
def _():
    try:
        webui.build_zip(xlsx({S.S_BO: []}))
    except ValueError as exc:
        assert "没有" in str(exc) or "校验" in str(exc), exc
    else:
        raise AssertionError("空内容还出了包")


@case("BLD14", "builder", "包内没有空目录", "P2",
      ["空目录会被当成「有这类但没内容」"])
def _():
    body, _ = build(one_form_package())
    names = list(unzip(body))
    tops = {n.split("/")[0] for n in names}
    for top in tops:
        assert any(n.startswith(top + "/") for n in names), f"{top}/ 是空的"


@case("BLD15", "builder", "条目名只用小写英文与下划线", "P2",
      ["中文条目名在部分解压工具里乱码"])
def _():
    body, _ = build(one_form_package())
    for name in unzip(body):
        base = name.split("/")[-1].rsplit(".", 1)[0]
        stem = re.sub(r"^\d+-", "", base)
        assert re.match(r"^[a-z0-9_]+$", stem), name


@case("BLD16", "builder", "同步包只出普通对象，流程对象单独一包", "P0",
      ['同步包不含 mode="workflow" 的对象',
       "流程对象包只含流程对象",
       "两包各自带自己的表单，包名能区分"])
def _():
    """用户 2026-11-14 的要求：流程对象从同步包挪到「流程定义」工作区。

    在此之前所有对象混在一个包里，流程对象要额外配流程 XML，
    导起来得分两步找东西；分两包后各自成套。
    """
    sheets = {
        S.S_BO: [
            {"对象名称": "合同", "对象编码": "contract",
             "物理表名": "pp_contract", "流程驱动": "是"},
            {"对象名称": "供应商", "对象编码": "supplier",
             "物理表名": "pp_supplier", "流程驱动": "否"},
        ],
        S.S_FIELD: [field("contract", "名称", "字符串"),
                    field("supplier", "名称", "字符串")],
        S.S_FORM: [
            {"表单key": "create_contract", "表单名称": "新建合同",
             "对象编码": "contract", "表单类型": "新建表单"},
            {"表单key": "create_supplier", "表单名称": "新建供应商",
             "对象编码": "supplier", "表单类型": "新建表单"},
        ],
        S.S_FORMFIELD: [
            {"表单key": "create_contract", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
            {"表单key": "create_supplier", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
        ],
    }
    data = xlsx(sheets)

    plain, plain_name = webui.build_zip(data, ["bo", "form"], object_scope="plain")
    wf, wf_name = webui.build_zip(data, ["bo", "form"], object_scope="workflow")

    plain_bos = {n: t for n, t in texts(plain).items() if n.startswith("bo/")}
    wf_bos = {n: t for n, t in texts(wf).items() if n.startswith("bo/")}
    assert len(plain_bos) == 1 and "code=\"supplier\"" in next(iter(plain_bos.values()))
    assert 'mode="normal"' in next(iter(plain_bos.values()))
    assert len(wf_bos) == 1 and "code=\"contract\"" in next(iter(wf_bos.values()))
    assert 'mode="workflow"' in next(iter(wf_bos.values()))

    # 表单跟着对象走：两包都不该出现对方的表单
    plain_forms = [n for n in unzip(plain) if n.startswith("form/")]
    wf_forms = [n for n in unzip(wf) if n.startswith("form/")]
    assert plain_forms and all("supplier" in n for n in plain_forms), plain_forms
    assert wf_forms and all("contract" in n for n in wf_forms), wf_forms

    assert "流程对象" in wf_name and "流程对象" not in plain_name, (plain_name, wf_name)


@case("BLD17", "builder", "只有流程对象时同步包给出明确指引而不是空包", "P1",
      ["400 说明去「流程定义」导出", "同一份表走 workflow 能出包"])
def _():
    sheets = {
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract",
                  "物理表名": "pp_contract", "流程驱动": "是"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
    }
    try:
        webui.build_zip(xlsx(sheets), ["bo"], object_scope="plain")
    except ValueError as exc:
        assert "流程模式" in str(exc), exc
    else:
        raise AssertionError("全是流程对象时同步包不该出包")

    body, _ = webui.build_zip(xlsx(sheets), ["bo"], object_scope="workflow")
    assert pick(body, "bo/").count("<tns:bodefentity") == 1


def _renamed_package() -> dict:
    """反解「项目立项信息」包后只改了对象名/编码，表单列表仍是旧 key。"""
    return {
        S.S_BO: [{"对象名称": "项目测试 1115", "对象编码": "project_test_1115",
                  "物理表名": "pp_project_test_1115"}],
        S.S_FIELD: [field("project_test_1115", "名称", "字符串")],
        S.S_FORM: [{"表单key": "create_project_create", "表单名称": "新建项目立项信息",
                    "对象编码": "project_test_1115", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_project_create", "字段编码": "mingCheng",
                         "显示标签": "名称", "控件类型": "text"}],
        S.S_LIST: [{"列表key": "project_create_list", "列表名称": "项目立项信息列表",
                    "对象编码": "project_test_1115", "显示字段": "mingCheng"}],
    }


@case("BLD18", "builder", "反解改编码后表单列表 key 归位到新对象", "P0",
      ["真实库实测：旧 key 会覆盖线上同名模板", "列表页按新对象取模板报未找到"])
def _():
    body, _ = build(_renamed_package())
    names = list(unzip(body))
    assert any(n.startswith("form/") and "create_project_test_1115" in n for n in names), names
    assert any(n.startswith("data_template/") and "project_test_1115_list" in n for n in names), names
    all_text = "\n".join(texts(body).values())
    assert 'key="create_project_test_1115"' in all_text
    assert 'key="project_test_1115_list"' in all_text
    assert 'name="新建项目测试 1115"' in all_text
    assert 'name="项目测试 1115列表"' in all_text
    assert "create_project_create" not in all_text, "表单旧 key 还留在 XML 里"
    assert "project_create_list" not in all_text, "列表旧 key 还留在 XML 里"
    assert "项目立项信息" not in all_text, "骨架对象的显示名还留在包里"


@case("BLD19", "builder", "key 归位不误伤同对象修订与自定义短 key", "P0",
      ["BO25 的原地更新语义不能被打破", "cs / fxcs 这类短 key 一律不动"])
def _():
    from pp6kit import model as M

    normal, _ = build(one_form_package())
    assert any("create_contract" in n for n in unzip(normal)), list(unzip(normal))

    bo = M.Bo(code="contract", name="合同", tbl_name="pp_contract")
    f = M.Form(key="create_contract", name="新建合同", bo_code="contract")
    short = M.ListDef(key="fxcs", name="测试", bo_code="contract")
    proj = M.Project(bos=[bo], forms=[f], lists=[short])
    assert builder.normalize_derived_keys(proj) == []
    assert (f.key, short.key) == ("create_contract", "fxcs")

    # 多对象包：key 指向的编码是本包另一个对象，不算骨架残留
    a = M.Bo(code="project_create", name="项目立项信息", tbl_name="pp_project_create")
    b = M.Bo(code="supplier", name="供应商", tbl_name="pp_supplier")
    lst = M.ListDef(key="project_create_list", name="项目立项信息列表", bo_code="project_create")
    proj2 = M.Project(bos=[a, b], lists=[lst])
    assert builder.normalize_derived_keys(proj2) == []
    assert lst.key == "project_create_list"


@case("BLD20", "builder", "key 归位时所有外键引用与 exist 标记同步", "P0",
      ["漏改 form_key 会让控件/规则找不到表单", "留着 exist_id 会覆盖线上旧对象"])
def _():
    from pp6kit import model as M

    bo = M.Bo(code="project_test_1115", name="项目测试 1115",
              tbl_name="pp_project_test_1115", exist_id="1444049045910720512",
              options={"formKey": "create_project_create",
                       "tree": '{"formKey":"create_project_create","idKey":"id"}'})
    f = M.Form(key="create_project_create", name="新建项目立项信息",
              bo_code=bo.code, kind=M.FORM_CREATE, exist_key="old-id",
              fields=[M.FormField(form_key="create_project_create", name="mingCheng",
                                 label="名称", field_type="text")],
              hide_rules=[M.HideRule(form_key="create_project_create",
                                     target="mingCheng", source="zhuangTai")])
    lst = M.ListDef(key="project_create_list", name="项目立项信息列表",
                    bo_code=bo.code, exist_key="old-id")
    proj = M.Project(
        bos=[bo], forms=[f], lists=[lst],
        form_scripts={"create_project_create": "return 1;"},
        workflows=[M.Workflow(name="立项流程", bo_code=bo.code,
                              nodes=[M.WfNode(wf="立项流程", name="开始",
                                              form_key="create_project_create")])],
    )
    notes = builder.normalize_derived_keys(proj)
    assert notes and any("project_create" in n for n in notes)
    new_key = "create_project_test_1115"
    assert f.key == new_key and f.name == "新建项目测试 1115"
    assert lst.key == "project_test_1115_list" and lst.name == "项目测试 1115列表"
    assert f.fields[0].form_key == new_key
    assert f.hide_rules[0].form_key == new_key
    assert proj.form_scripts == {new_key: "return 1;"}
    assert bo.options["formKey"] == new_key
    assert "create_project_create" not in bo.options["tree"]
    assert proj.workflows[0].nodes[0].form_key == new_key
    assert bo.exist_id == "" and f.exist_key == "" and lst.exist_key == ""


@case("BLD21", "builder", "列表的「新建」按钮默认不绑权限项", "P0",
      ["permission_key 留空（否则导入后系统里凭空多一个权限项）",
       "cascade_configuration.formKey 仍指向新表单",
       "permission_object_type 仍是对象编码"])
def _():
    import json

    from pp6kit import gen_tpl, model as M

    bo = M.Bo(code="project_test_1115", name="项目测试 1115",
              tbl_name="pp_project_test_1115")
    form_key = "create_project_test_1115"
    # 骨架里「新建」按钮的样子：custom + 绑了一个权限项 + 级联打开某个表单。
    # 两个都写死成旧模板的值，模拟「照抄样例」的场景。
    raw = json.dumps({
        "function_buttons": [
            {"button_type": "export", "label": "导出", "code": "export",
             "permission_object_type": "old_obj", "permission_key": "old_export"},
        ],
        "toolbar_buttons": [
            {"button_type": "custom", "label": "新建项目", "code": "custom",
             "permission_object_type": "old_obj", "permission_key": "project_create",
             "enabledCascade": True,
             "cascade_configuration": {"type": "form", "formKey": "create_project",
                                       "boKey": ""}},
            {"button_type": "batchModify", "label": "批量编辑", "code": "batchModify"},
        ],
    }, ensure_ascii=False)

    out = json.loads(gen_tpl._patch_buttons(raw, bo, form_key))

    create_btn = out["toolbar_buttons"][0]
    assert create_btn["permission_key"] == "", \
        "「新建」按钮还绑着权限项：导入后系统里会凭空多出一个权限项"
    assert create_btn["permission_object_type"] == bo.code, \
        "按钮挂哪个对象被改坏了（与权限项是两件事，必须继续写对象编码）"
    cfg = create_btn["cascade_configuration"]
    assert cfg["formKey"] == form_key, "「新建」按钮打开的表单没跟着换成新 key"
    assert cfg["boKey"] == "id", "级联打开表单的 boKey 不再固定为 id"

    # 系统内置按钮（非 custom）不该被塞权限项，也不该被清成空串
    assert "permission_key" not in out["toolbar_buttons"][1]
    # 功能按钮：同样只写归属、不带权限项（原有行为不能回退）
    fn_btn = out["function_buttons"][0]
    assert fn_btn["permission_object_type"] == bo.code
    assert "permission_key" not in fn_btn, "功能按钮又被塞回了 permission_key"


@case("BLD22", "builder", "列表「新建」按钮的文案跟着对象走，不沿用样例的「新建项目」", "P0",
      ["骨架来自项目列表，照抄会让合同/采购/会议的列表都挂着「新建项目」",
       "按钮文案 = 新建表单的名字（新建采购申请）",
       "grid / board / gantt / chart 四个视图都要改，缺一个就露馅"])
def _():
    import json
    import xml.etree.ElementTree as ET

    body, _ = build({
        S.S_BO: [{"对象名称": "采购申请", "对象编码": "purchase",
                  "物理表名": "pp_purchase"}],
        S.S_FIELD: [
            field("purchase", "名称", "字符串"),
            field("purchase", "状态", "选项", 选项值="待审批,已通过,已驳回"),
            field("purchase", "开始日期", "日期"),
            field("purchase", "截止日期", "日期"),
        ],
        S.S_FORM: [{"表单key": "create_purchase", "表单名称": "新建采购申请",
                    "对象编码": "purchase", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [
            {"表单key": "create_purchase", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
        ],
        S.S_LIST: [{"列表key": "purchase_list", "列表名称": "采购申请列表",
                    "对象编码": "purchase", "视图类型": "grid,board,gantt,chart"}],
    })

    labels: list[str] = []
    for name, text in texts(body).items():
        if not name.startswith("data_template/"):
            continue
        for node in ET.fromstring(text).iter():
            raw = node.get("functionButtons")
            if not raw:
                continue
            for btn in json.loads(raw).get("toolbar_buttons", []):
                if btn.get("button_type") == "custom":
                    labels.append(btn.get("label") or "")

    assert labels, "列表里一个工具栏「新建」按钮都没生成"
    assert set(labels) == {"新建采购申请"}, \
        f"「新建」按钮文案没跟着对象走，实际：{sorted(set(labels))}"
    assert "新建项目" not in "".join(texts(body).values()), \
        "产物里还残留着样例的项目文案"


@case("BLD23", "builder", "列表「更多」菜单与扩展列不再照抄项目样例", "P0",
      ["扩展列（项目类型/阶段/经理/部门）指向 pp_project，必须去掉",
       "参数配置指向 project_config_dev1、导入指向 project_import_validation_form，包里没有就该消失",
       "导出列 = 当前显示列，不是样例的 12 个项目字段",
       "edit_buttons 的繁体「關閉」统一成「关闭」"])
def _():
    import json
    import xml.etree.ElementTree as ET

    body, _ = build({
        S.S_BO: [{"对象名称": "采购申请", "对象编码": "purchase",
                  "物理表名": "pp_purchase"}],
        S.S_FIELD: [
            field("purchase", "名称", "字符串"),
            field("purchase", "状态", "选项", 选项值="待审批,已通过"),
            field("purchase", "开始日期", "日期"),
            field("purchase", "截止日期", "日期"),
        ],
        S.S_FORM: [{"表单key": "create_purchase", "表单名称": "新建采购申请",
                    "对象编码": "purchase", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [
            {"表单key": "create_purchase", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
        ],
        S.S_LIST: [{"列表key": "purchase_list", "列表名称": "采购申请列表",
                    "对象编码": "purchase", "视图类型": "grid,board,gantt,chart"}],
    })

    all_xml = "".join(texts(body).values())
    for junk in ("pp_project", "專案", "project_config_dev1",
                 "project_import_validation_form", "项目类型", "项目经理"):
        assert junk not in all_xml, f"产物里还残留样例的 {junk}"

    tpl_nodes = [n for name, text in texts(body).items()
                 if name.startswith("data_template/")
                 for n in ET.fromstring(text).iter("dataTemplateTpl")]
    assert tpl_nodes, "包里没有列表模板"

    saw_export = saw_more = False
    for node in tpl_nodes:
        assert not node.get("extColumns"), "扩展列还留着（指向别人的表）"
        attrs = json.loads(node.get("attrs") or "{}")
        assert "tooltip_field" not in attrs, "甘特悬浮提示还挂着项目字段"
        cfg = json.loads(node.get("functionButtons") or "{}")
        assert "關閉" not in [b.get("label") for b in cfg.get("edit_buttons", [])], \
            "编辑态按钮还是繁体「關閉」"
        for btn in cfg.get("toolbar_buttons", []):
            if btn.get("button_type") != "more":
                continue
            saw_more = True
            menus = btn.get("menus", [])
            assert menus, "「更多」一项菜单都不剩了，按钮本身也该收掉"
            kinds = {m.get("button_type") for m in menus}
            labels = {m.get("label") for m in menus}
            assert "参数配置" not in labels, "「参数配置」还指向样例的配置表单"
            assert "import" not in kinds, "包里没有导入专用表单，导入菜单不该留"
            for m in menus:
                if m.get("button_type") == "export":
                    saw_export = True
                    fields = (m.get("exportColumns") or {}).get("fields") or []
                    names = {f.get("objName") for f in fields}
                    assert names <= {"pp_purchase"}, f"导出列指向别的表：{names}"
                    assert fields, "导出列被清空了"

    assert saw_more and saw_export, "更多菜单/导出按钮没生成"


@case("BLD24", "builder", "有导入专用表单时，「导入」菜单接到本对象上", "P1",
      ["importFormKey 指向本包的导入专用表单",
       "导入模板文件名跟对象名走",
       "去重字段用系统必备的 name（样例是 code，新对象不一定有）"])
def _():
    import json
    import xml.etree.ElementTree as ET

    body, _ = build({
        S.S_BO: [{"对象名称": "采购申请", "对象编码": "purchase",
                  "物理表名": "pp_purchase"}],
        S.S_FIELD: [field("purchase", "名称", "字符串")],
        S.S_FORM: [
            {"表单key": "create_purchase", "表单名称": "新建采购申请",
             "对象编码": "purchase", "表单类型": "新建表单"},
            {"表单key": "import_purchase", "表单名称": "导入专用_采购申请",
             "对象编码": "purchase", "表单类型": "导入专用表单"},
        ],
        S.S_FORMFIELD: [
            {"表单key": "create_purchase", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
            {"表单key": "import_purchase", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
        ],
        S.S_LIST: [{"列表key": "purchase_list", "列表名称": "采购申请列表",
                    "对象编码": "purchase", "视图类型": "grid"}],
    })

    imp = None
    for name, text in texts(body).items():
        if not name.startswith("data_template/"):
            continue
        for node in ET.fromstring(text).iter("dataTemplateTpl"):
            for btn in json.loads(node.get("functionButtons") or "{}").get("toolbar_buttons", []):
                for menu in btn.get("menus", []) or []:
                    if menu.get("button_type") == "import":
                        imp = menu
    assert imp is not None, "包里明明有导入专用表单，导入菜单却没了"
    assert imp["importFormKey"] == "import_purchase", "导入校验表单没接到本对象"
    assert imp["importExcelFileName"] == "导入采购申请模板"
    assert imp["importUniqueFieldsName"] == "name", "去重字段还是样例的 code"
    assert imp["permission_object_type"] == "purchase"


@case("BLD25", "builder", "级联详情真的能点开：列级联补齐原生键，看板不再跳项目页", "P0",
      ["原生可点击列带 enabledCascade / triggerEvent，缺了有的前端不渲染成链接",
       "cascade_configuration 少了 inheritanceFormSetting / pkValue，导入后 casadeFields 会被清空",
       "看板模板级 cascade_configuration 原样照抄项目看板的 /project/projectLayout/，点卡片跳到别人的项目页",
       "表单的 moreBtn 是悬浮「更多」按钮，和底部按钮重复，原生新建表单没有它"])
def _():
    import json
    import xml.etree.ElementTree as ET

    body, _ = build({
        S.S_BO: [{"对象名称": "采购申请", "对象编码": "purchase",
                  "物理表名": "pp_purchase"}],
        S.S_FIELD: [
            field("purchase", "名称", "字符串"),
            field("purchase", "状态", "选项", 选项值="待审批,已通过"),
            field("purchase", "开始日期", "日期"),
            field("purchase", "截止日期", "日期"),
        ],
        S.S_FORM: [{"表单key": "create_purchase", "表单名称": "新建采购申请",
                    "对象编码": "purchase", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [
            {"表单key": "create_purchase", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
        ],
        S.S_LIST: [{"列表key": "purchase_list", "列表名称": "采购申请列表",
                    "对象编码": "purchase", "视图类型": "grid,board",
                    "级联详情字段": "name"}],
    })

    all_xml = "".join(texts(body).values())
    assert "/project/projectLayout/" not in all_xml, \
        "看板还挂着样例项目的页面地址"
    assert '"moreBtn"' not in all_xml, "表单还留着悬浮「更多」按钮（和底部按钮重复）"

    saw_col = saw_board = False
    for name, text in texts(body).items():
        if name.startswith("data_template/"):
            for node in ET.fromstring(text).iter("dataTemplateTpl"):
                attrs = json.loads(node.get("attrs") or "{}")
                view = attrs.get("template_renderview_type")
                casc = attrs.get("cascade_configuration") or {}
                if view == "board":
                    assert casc.get("type") == "form", "看板卡片没有绑详情表单"
                    assert casc.get("formKey") == "purchase_details"
                    assert casc.get("casadeFields"), "看板级联没带行主键映射"
                    saw_board = True
                for col in json.loads(node.get("displayColumns") or "[]"):
                    cfg = col.get("cascade_configuration")
                    if not cfg:
                        continue
                    saw_col = True
                    assert col.get("enabledCascade") is True, \
                        "级联列缺 enabledCascade，可能渲染不出可点的链接"
                    assert col.get("triggerEvent") == "font"
                    assert cfg["formKey"] == "purchase_details"
                    assert cfg["inheritanceFormSetting"] is True, \
                        "缺 inheritanceFormSetting，导入后 casadeFields 会被清空"
                    assert cfg["pkValue"] == "id_"
                    assert cfg["casadeFields"] == [
                        {"key": "id", "value": "name_", "valueType": "bo"}]

    assert saw_col and saw_board, "级联详情/看板配置没生成"


@case("BLD26", "builder", "级联字段不在显示字段里时自动补一列，不让「选了却不生效」", "P1",
      ["用户只填了「级联详情字段」、没把它写进「显示字段」，以前只记 notice 就跳过",
       "级联必须有一列可点：自动补到第一列并带上 cascade_configuration",
       "补出来的列不能丢 label/objName，否则列表显示空白表头"])
def _():
    import json
    import xml.etree.ElementTree as ET

    body, _ = build({
        S.S_BO: [{"对象名称": "采购申请", "对象编码": "purchase",
                  "物理表名": "pp_purchase"}],
        S.S_FIELD: [
            field("purchase", "名称", "字符串"),
            field("purchase", "状态", "选项", 选项值="待审批,已通过"),
            field("purchase", "开始日期", "日期"),
            field("purchase", "截止日期", "日期"),
        ],
        S.S_FORM: [{"表单key": "create_purchase", "表单名称": "新建采购申请",
                    "对象编码": "purchase", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [
            {"表单key": "create_purchase", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
        ],
        S.S_LIST: [{"列表key": "purchase_list", "列表名称": "采购申请列表",
                    "对象编码": "purchase", "视图类型": "grid",
                    "显示字段": "状态,开始日期", "级联详情字段": "name"}],
    })

    first = None
    for name, text in texts(body).items():
        if name.startswith("data_template/"):
            node = next(ET.fromstring(text).iter("dataTemplateTpl"))
            cols = json.loads(node.get("displayColumns") or "[]")
            first = cols[0]
    assert first is not None, "没有列表模板"
    assert first["name"] == "name_", "级联列没补到第一列"
    assert first["label"] == "名称" and first["objName"] == "pp_purchase"
    assert (first.get("cascade_configuration") or {}).get("formKey") == "purchase_details"
