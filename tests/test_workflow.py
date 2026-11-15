"""流程生成（workflowDefinition）：从三张 Excel 表到一份可导入的裸 XML。

格式基准是 `PP6/流程/项目立项_20261113145550.xml` 的 **v30** 那一版
（`DefaultFlowChartVersion="30"`，用户线上正在跑的那张图）。
每条用例都基于**真实产出**断言（生成 XML → 解析 / json 反解），
不看中间变量；流程这块最容易出的问题是「XML 能生成但导入端不认」。
"""
from __future__ import annotations

import io
import json
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from pp6kit import builder, gen_workflow, spec as S, webui
from tests._harness import case, field, project_of, xlsx, xml_ok

BO = "project_create"


# ---------------------------------------------------------------- 样本构造

def sheets(nodes: list[dict], edges: list[dict] | None = None,
           extra: dict | None = None) -> dict:
    """一份带流程的最小填写表（1 对象 2 字段 1 个「标准全套」表单 + 流程）。"""
    data = {
        S.S_SETTINGS: [{"配置项": "流程分类ID", "值": "1559242949219319808"}],
        S.S_BO: [{"对象名称": "项目立项", "对象编码": BO, "流程驱动": "是"}],
        S.S_FIELD: [field(BO, "名称", "字符串"), field(BO, "预算金额", "数字")],
        S.S_FORM: [{"表单key": "create_project_create", "表单名称": "新建项目立项",
                    "对象编码": BO, "表单类型": "标准全套"}],
        S.S_WF: [{"流程名称": "项目立项", "对象编码": BO}],
        S.S_WF_NODE: nodes,
    }
    if edges is not None:
        data[S.S_WF_EDGE] = edges
    data.update(extra or {})
    return data


START = {"流程名称": "项目立项", "节点名称": "申请人", "节点类型": "开始"}
APPROVE = {"流程名称": "项目立项", "节点序号": 2, "节点名称": "部门审批", "节点类型": "审批",
           "处理人类型": "角色", "处理人": "部门负责人", "处理人ID": "1567950168366514176"}
PASS = {"流程名称": "项目立项", "节点名称": "通过", "节点类型": "通过"}
REJECT = {"流程名称": "项目立项", "节点名称": "驳回", "节点类型": "驳回"}
BRANCH = {"流程名称": "项目立项", "节点名称": "判断_预算", "节点类型": "判断"}


def wf_xml(data: dict) -> str:
    """只出流程模块 → 返回落盘后的 XML 原文（走真实的 builder 链路）。"""
    proj, errors, _ = project_of(xlsx(data))
    assert not errors, errors
    with tempfile.TemporaryDirectory() as tmp:
        res = builder.build(proj, webui._snippets(), Path(tmp), codes=["workflow"])
        files = [d for d in res.deliverables if d.module == "workflow"]
        assert files, [d.filename for d in res.deliverables]
        return files[0].path.read_text(encoding="utf-8")


def wf_build(data: dict):
    """同上，但同时返回 BuildResult（要看 notices / artifacts 时用）。"""
    proj, errors, _ = project_of(xlsx(data))
    assert not errors, errors
    tmp = tempfile.mkdtemp()
    res = builder.build(proj, webui._snippets(), Path(tmp), codes=["workflow"])
    return res, proj


def props_of(root) -> dict[str, dict[str, str]]:
    """activity Id → 它的 Properties（子元素的 文本）。"""
    out: dict[str, dict[str, str]] = {}
    for act in root.iter():
        if act.tag not in ("NormalActivity", "BranchActivity"):
            continue
        props = act.find("Properties")
        out[act.get("Id")] = {} if props is None else {
            ch.tag: (ch.text or "") for ch in props}
    return out


def transitions_of(root) -> list:
    return list(root.iter("Transition"))


def permission_codes(root) -> dict[str, list[str]]:
    """activity Id → nodePermissions.field 里的控件 name 列表。"""
    perms = json.loads(root.findtext("nodePermissions"))
    return {k: [x["name"] for x in v["field"]] for k, v in perms.items()}


# ---------------------------------------------------------------- 用例

@case("WF01", "workflow", "最小三节点流程能出可导入的 XML", "P0",
      ["首行就是 <workflowDefinition>（没有 <?xml 声明）", "ET 能解析"])
def _():
    xml = wf_xml(sheets([START, APPROVE, PASS]))
    xml_ok(xml)
    assert xml.splitlines()[0] == "<workflowDefinition>", xml.splitlines()[0]
    assert "<?xml" not in xml
    root = ET.fromstring(xml)
    states = [a.get("State") for a in root.iter("NormalActivity")]
    assert states == ["Start", "Ongoing", "Passed"], states
    assert root.findtext("businessType") == BO
    assert root.findtext("workflowType") == "normal"
    # 没填流转表 → 自动串直线，两条出边
    assert len(transitions_of(root)) == 2


@case("WF02", "workflow", "节点 id 连续、流转 id 独立序列、Target 指向正确", "P0",
      ["activity Id 1..N", "Transition Id 1..M（与 activity 不同源）", "Target 指目标 activity"])
def _():
    xml = wf_xml(sheets([START, APPROVE, BRANCH, PASS, REJECT], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "部门审批"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "判断_预算"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "通过"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "驳回"},
    ]))
    root = ET.fromstring(xml)
    ids = [int(a.get("Id")) for a in root.iter()
           if a.tag in ("NormalActivity", "BranchActivity")]
    assert ids == list(range(1, 6)), ids
    ts = transitions_of(root)
    assert [t.get("Id") for t in ts] == [str(i) for i in range(1, len(ts) + 1)]
    targets = sorted(int(t.get("Target")) for t in ts)
    assert targets == [2, 3, 4, 5], targets


@case("WF03", "workflow", "nodePermissions 覆盖表单控件，开始可编辑、审批只读", "P0",
      ["_p_title 在第一项", "判断节点没有权限条目", "容器不进权限"])
def _():
    xml = wf_xml(sheets([START, APPROVE, BRANCH, PASS]))
    root = ET.fromstring(xml)
    perms = json.loads(root.findtext("nodePermissions"))
    assert set(perms) == {"1", "2", "4"}, list(perms)      # 3 是判断节点，不出现
    for key, block in perms.items():
        names = [x["name"] for x in block["field"]]
        assert names[0] == "_p_title", names
        assert "mingCheng" in names and "yuSuanJinE" in names, names
        assert not any(n.startswith(("fieldset_", "gridLayout_", "card_")) for n in names), names
        assert block["column"] == [] and block["button"] == []
    start_edit = perms["1"]["field"][0]["edit"]
    assert start_edit[0]["type"] == "BpmRole", start_edit
    assert start_edit[0]["rightsId"] == "NodeTodoHandler,BpmCreator"
    approve_edit = perms["2"]["field"][0]["edit"]
    assert approve_edit == [{"type": "none"}], approve_edit
    # field_options 必须与表单控件同源（逐键一致），否则节点上字段会消失
    title = next(x for x in perms["1"]["field"] if x["name"] == "mingCheng")
    assert title["field_type"] == "text"
    assert "placeholder" in title["field_options"], title["field_options"]


@case("WF04", "workflow", "BusinessState 与对象 processState 字典短码一致且可重现", "P0",
      ["待发起/进行中/已完成/已失败", "同输入同输出"])
def _():
    data = sheets([START, APPROVE, PASS, REJECT])
    proj, errors, _ = project_of(xlsx(data))
    assert not errors, errors
    with tempfile.TemporaryDirectory() as tmp:
        # 同时出 bo 与流程：bo XML 里的字典 key 应与流程里的 BusinessState 完全相同
        res = builder.build(proj, webui._snippets(), Path(tmp), codes=["bo", "workflow"])
        bo_artifact = next(a for a in res.artifacts if a.module == "bo")
        bo_codes = {}
        for item in ET.fromstring(bo_artifact.xml).iter():
            if item.tag.endswith("dictionary") and item.get("typeKey", "").endswith("_processState"):
                bo_codes[item.get("name")] = item.get("key")
        assert len(bo_codes) == 4, bo_codes
        wf_file = next(d for d in res.deliverables if d.module == "workflow")
        root = ET.fromstring(wf_file.path.read_text(encoding="utf-8"))
        by_state = {a.get("State"): a.find("Properties").findtext("BusinessState")
                    for a in root.iter("NormalActivity")}
        assert by_state["Start"] == bo_codes["待发起"], (by_state, bo_codes)
        assert by_state["Ongoing"] == bo_codes["进行中"]
        assert by_state["Passed"] == bo_codes["已完成"]
        assert by_state["Rejected"] == bo_codes["已失败"]

        # 再跑一次：UUID / 短码 / 规则 id / 坐标全部一致
        res2 = builder.build(project_of(xlsx(data))[0], webui._snippets(),
                             Path(tmp), codes=["workflow"])
        first = wf_file.path.read_text(encoding="utf-8")
        second = next(d for d in res2.deliverables
                      if d.module == "workflow").path.read_text(encoding="utf-8")
        assert first == second


@case("WF05", "workflow", "条件流转是双层 JSON，能被反解", "P0",
      ["外层 $type + conditionJson", "内层 condition/AND/rules"])
def _():
    xml = wf_xml(sheets([START, APPROVE, BRANCH, PASS, REJECT], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "部门审批"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "判断_预算"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "通过",
         "流转条件": "字段条件", "条件字段": "预算金额", "条件运算": "大于", "条件值": "300"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "驳回"},
    ]))
    root = ET.fromstring(xml)
    conds = [(t.get("Target"), t.findtext("Condition")) for t in transitions_of(root)]
    outer_text = next(c for target, c in conds if target == "4")
    outer = json.loads(outer_text)
    assert outer["$type"] == "WorkflowConditionTreeTransitionCondition"
    tree = json.loads(outer["conditionJson"])
    assert tree["condition"] == "AND"
    rule = tree["rules"][0]
    assert rule["field"] == "yuSuanJinE", rule["field"]
    assert rule["operator"] == "greater" and rule["operator_label"] == "大于"
    # 数字字段的值是 JSON number，不是字符串 "300"
    assert rule["value"] == 300 and isinstance(rule["value"], int), rule["value"]
    assert rule["type"] == "number" and rule["field_data_type"] == "number"
    # 无条件那条仍然是 True
    assert any(c == "True" for _, c in conds), conds


@case("WF06", "workflow", "判断节点出边 Order 递增且不通知", "P1",
      ["Order 1/2", "Inform=false", "普通节点恒 Order=0"])
def _():
    xml = wf_xml(sheets([START, APPROVE, BRANCH, PASS, REJECT], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "部门审批"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "判断_预算"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "通过",
         "流转条件": "字段条件", "条件字段": "预算金额", "条件运算": "小于", "条件值": "300"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "驳回"},
    ]))
    root = ET.fromstring(xml)
    branch = next(a for a in root.iter("BranchActivity"))
    outs = branch.findall("Transition")
    assert [t.get("Order") for t in outs] == ["1", "2"], [t.get("Order") for t in outs]
    assert all(t.get("Inform") == "false" for t in outs)
    # 判断节点的出边没有 IfHideAttachment / NotAllowManualTransform
    first_props = {ch.tag for ch in outs[0].find("Properties")}
    assert "IfHideAttachment" not in first_props, first_props
    assert "NotAllowManualTransform" not in first_props, first_props
    # 开始节点的出边：有 IfHideAttachment、**没有** NotAllowManualTransform
    # （30 个版本里 30/30 个 Start 出边都没有它，只有 Ongoing 的 215/215 才有）
    start = next(a for a in root.iter("NormalActivity") if a.get("State") == "Start")
    t0 = start.find("Transition")
    assert t0.get("Order") == "0"
    start_props = {ch.tag for ch in t0.find("Properties")}
    assert "IfHideAttachment" in start_props, start_props
    assert "NotAllowManualTransform" not in start_props, start_props
    # 审批节点的出边：两个键都在，且 NotAllowManualTransform 紧跟 IfHideCC
    approve = next(a for a in root.iter("NormalActivity") if a.get("State") == "Ongoing")
    approve_tags = [ch.tag for ch in approve.find("Transition").find("Properties")]
    assert "NotAllowManualTransform" in approve_tags, approve_tags
    assert approve_tags.index("NotAllowManualTransform") == approve_tags.index("IfHideCC") + 1, \
        approve_tags


@case("WF07", "workflow", "Handler 链格式正确（角色/人员/多人）", "P1",
      ["Chain:[...];SelectMode=Referenced", "多 id 时 AllowMultiple=true"])
def _():
    xml = wf_xml(sheets([
        START,
        {"流程名称": "项目立项", "节点名称": "部门审批", "节点类型": "审批",
         "处理人类型": "角色", "处理人": "部门负责人", "处理人ID": "1567950168366514176"},
        {"流程名称": "项目立项", "节点名称": "领导会签", "节点类型": "审批",
         "处理人类型": "人员", "处理人": "张三,李四",
         "处理人ID": "1001,1002"},
        PASS,
    ]))
    root = ET.fromstring(xml)
    handlers = [a.find("Properties").findtext("Handler")
                for a in root.iter("NormalActivity") if a.get("State") == "Ongoing"]
    one, two = handlers
    assert one.startswith("Chain:[") and ";SelectMode=Referenced" in one, one
    head = json.loads(one[len("Chain:["):one.index("];")])["header"]
    assert head["type"] == "RoleSelection" and head["ids"] == ["1567950168366514176"], head
    assert head["texts"] == ["部门负责人"]
    assert one.endswith("AllowMultiple=false"), one
    head2 = json.loads(two[len("Chain:["):two.index("];")])["header"]
    assert head2["type"] == "UserSelection" and head2["ids"] == ["1001", "1002"], head2
    assert two.endswith("AllowMultiple=true"), two


@case("WF08", "workflow", "流转表留空时自动串直线并跳过判断节点", "P1",
      ["用户不填流转也能出包", "判断节点不参与串线"])
def _():
    res, proj = wf_build(sheets([START, APPROVE, BRANCH, PASS]))
    xml = next(d for d in res.deliverables
               if d.module == "workflow").path.read_text(encoding="utf-8")
    root = ET.fromstring(xml)
    # 两条出边：申请人→部门审批→通过；判断节点（Id=3）孤立，不参与串线
    acts = {a.get("Id"): a.get("Name") for a in root.iter()
            if a.tag in ("NormalActivity", "BranchActivity")}
    targets = {t.get("Target") for t in transitions_of(root)}
    assert targets == {"2", "4"}, (targets, acts)
    assert any("自动串成一条直线" in n for n in res.notices), res.notices


@case("WF09", "workflow", "校验拦住缺开始/缺通过/重名/陌生节点", "P0",
      ["错误必须在出包前暴露", "一条都不能静默生成"])
def _():
    from tests._harness import project_issues

    errs, _warns, _infos = project_issues(sheets([APPROVE, PASS]))
    assert any("开始" in e for e in errs), errs

    errs, _warns, _infos = project_issues(sheets([START, APPROVE]))
    assert any("终点" in e for e in errs), errs

    dup = [START, dict(APPROVE, 节点名称="申请人"), PASS]
    errs, _warns, _infos = project_issues(sheets(dup))
    assert any("重复" in e for e in errs), errs

    errs, _warns, _infos = project_issues(sheets([START, APPROVE, PASS], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "查无此节点"},
    ]))
    assert any("不存在" in e for e in errs), errs

    # 审批节点没处理人 / 名字与 id 数量不一致 / 判断节点填了表单
    errs, _warns, _infos = project_issues(sheets([
        START, {"流程名称": "项目立项", "节点名称": "部门审批", "节点类型": "审批"}, PASS,
    ]))
    assert any("处理人" in e for e in errs), errs
    errs, _warns, _infos = project_issues(sheets([
        START, dict(APPROVE, 处理人="甲,乙"), PASS,
    ]))
    assert any("一一对应" in e for e in errs), errs
    errs, _warns, _infos = project_issues(sheets([
        START, dict(BRANCH, 表单="create_project_create"), PASS,
    ]))
    assert any("判断节点不能填表单" in e for e in errs), errs


@case("WF10", "workflow", "layout 可反解且与节点/出边数量一致", "P1",
      ["node key 与 link key 是两套 -1..-N", "points 是 4 个数字"])
def _():
    xml = wf_xml(sheets([START, APPROVE, BRANCH, PASS, REJECT], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "部门审批"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "判断_预算"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "通过"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "驳回"},
    ]))
    root = ET.fromstring(xml)
    layout = json.loads(root.findtext("layout"))
    assert isinstance(layout, list) and len(layout) == 1, layout
    chart = layout[0]
    assert chart["version"] == 1
    assert len(chart["nodeDataArray"]) == 5
    assert len(chart["linkDataArray"]) == 4
    assert [n["key"] for n in chart["nodeDataArray"]] == [-1, -2, -3, -4, -5]
    assert [x["key"] for x in chart["linkDataArray"]] == [-1, -2, -3, -4]
    for link in chart["linkDataArray"]:
        assert len(link["points"]) == 4, link
        assert all(isinstance(v, (int, float)) for v in link["points"]), link
    cats = {n["category"] for n in chart["nodeDataArray"]}
    assert cats == {"Start", "Ongoing", "Branch", "Passed", "Rejected"}, cats
    # loc 是 "x y" 字符串
    assert all(len(n["loc"].split()) == 2 for n in chart["nodeDataArray"])


@case("WF11", "workflow", "同起终点两行合并成一条出边的两个条件（AND）", "P1",
      ["Excel 不用加「条件组」列", "两个条件都要进 rules"])
def _():
    xml = wf_xml(sheets([START, APPROVE, BRANCH, PASS, REJECT], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "部门审批"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "判断_预算"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "通过",
         "流转条件": "字段条件", "条件字段": "预算金额", "条件运算": "大于", "条件值": "300"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "通过",
         "流转条件": "字段条件", "条件字段": "名称", "条件运算": "不为空", "条件值": ""},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "驳回"},
    ]))
    root = ET.fromstring(xml)
    branch = next(a for a in root.iter("BranchActivity"))
    to_pass = [t for t in branch.findall("Transition") if t.get("Target") == "4"]
    assert len(to_pass) == 1, [t.get("Target") for t in branch.findall("Transition")]
    tree = json.loads(json.loads(to_pass[0].findtext("Condition"))["conditionJson"])
    assert tree["condition"] == "AND"
    assert [r["field"] for r in tree["rules"]] == ["yuSuanJinE", "mingCheng"], tree["rules"]
    assert tree["rules"][1]["operator"] == "is_not_empty"
    assert tree["rules"][1]["value"] is None


@case("WF12", "workflow", "GoBackActivityIds 含开始节点、跳过判断；键按字符串排序", "P1",
      ["样本 node2=[1] / node10=[1,2,3,8]", "commentRequired 键序是字符串字典序"])
def _():
    xml = wf_xml(sheets([
        START, APPROVE,
        {"流程名称": "项目立项", "节点名称": "判断_预算", "节点类型": "判断"},
        {"流程名称": "项目立项", "节点名称": "财务审批", "节点类型": "审批",
         "处理人类型": "角色", "处理人": "财务负责人", "处理人ID": "2002"},
        PASS,
    ], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "部门审批"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "判断_预算"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "财务审批"},
        {"流程名称": "项目立项", "起点节点": "财务审批", "终点节点": "通过"},
    ]))
    root = ET.fromstring(xml)
    props = props_of(root)
    assert props["2"]["GoBackActivityIds"] == "[1]", props["2"]      # 只有开始节点
    assert props["4"]["GoBackActivityIds"] == "[1,2]", props["4"]    # 跳过判断节点 3
    # commentRequired 的键按字符串排序：1、12、2…（这里只有 1、2，验证形态即可）
    cfg = json.loads(props["4"]["GoBackConfig"])
    assert cfg["name"] == "退回" and cfg["commentRequired"] == {"1": True, "2": True}, cfg
    # 显式写「无」→ 空数组且不出 GoBackConfig / ReviewBusinessParams 之外不加键
    xml2 = wf_xml(sheets([
        START, dict(APPROVE, 退回节点="无"), PASS,
    ]))
    props2 = props_of(ET.fromstring(xml2))
    assert props2["2"]["GoBackActivityIds"] == "[]", props2["2"]
    assert "GoBackConfig" not in props2["2"], props2["2"]


@case("WF13", "workflow", "只选流程时导出 zip 给出中文错误而不是 BadZipFile", "P1",
      ["流程模块不再单独导出", "错误指向流程对象包"])
def _():
    data = xlsx(sheets([START, APPROVE, PASS]))
    try:
        webui.build_zip(data, ["workflow"])
    except zipfile.BadZipFile:                       # noqa: PERF203
        raise AssertionError("抛了 BadZipFile：流程 XML 被当成 zip 打开了") from None
    except ValueError as exc:
        assert "不再单独导出" in str(exc) and "流程对象包" in str(exc), str(exc)
    else:
        raise AssertionError("只选流程时不应该成功返回 zip")
    # /api/import/workflow 同样要守住（否则会把 .xml 当 zip 发出去）
    try:
        webui.import_module(data, "workflow")
    except ValueError as exc:
        assert "不再单独导出" in str(exc) or "不生成流程 XML" in str(exc), str(exc)
    else:
        raise AssertionError("import_module 不应该给出流程的 zip")


@case("WF14", "workflow", "流程 id 确定性：uuid5 且已有 id 优先", "P1",
      ["同输入同 id", "exist_id 覆盖"])
def _():
    proj, errors, _ = project_of(xlsx(sheets([START, APPROVE, PASS])))
    assert not errors, errors
    wf = proj.workflows[0]
    bo = proj.bo(BO)
    first = gen_workflow.wf_id(wf, bo)
    assert first == gen_workflow.wf_id(wf, bo), "两次调用必须一致"
    assert len(first) == 36 and first.count("-") == 4, first        # UUID 形态
    wf.exist_id = "8eea2a6d-f67a-4da8-8d6d-b2f13c21fd22"
    assert gen_workflow.wf_id(wf, bo) == wf.exist_id
    # 换了流程名 → 换了 id（否则两条流程会互相覆盖）
    wf.exist_id = ""
    wf.name = "项目立项V2"
    assert gen_workflow.wf_id(wf, bo) != first


@case("WF15", "workflow", "只选流程时不产出空 zip", "P1",
      ["0 条目的 zip 会被用户当成「生成成功」", "_pack_all 必须在没写条目时返回 None"])
def _():
    proj, errors, _ = project_of(xlsx(sheets([START, APPROVE, PASS])))
    assert not errors, errors
    with tempfile.TemporaryDirectory() as tmp:
        res = builder.build(proj, webui._snippets(), Path(tmp), codes=["workflow"])
        assert not any(d.module == "all" for d in res.deliverables), \
            [d.filename for d in res.deliverables]
        # 整包目录里不该躺着一个空 zip
        for path in (Path(tmp) / "整包").glob("*.zip"):
            with zipfile.ZipFile(path) as z:
                assert z.namelist(), f"{path.name} 是空 zip"


@case("WF16", "workflow", "流程产物是裸 .xml，且进不了同步包 zip", "P1",
      ["文件名 = 流程名.xml", "菜单指向 管理 › 流程设计 › 导入"])
def _():
    proj, errors, _ = project_of(xlsx(sheets([START, APPROVE, PASS])))
    assert not errors, errors
    with tempfile.TemporaryDirectory() as tmp:
        res = builder.build(proj, webui._snippets(), Path(tmp), codes=["bo", "workflow"])
        wf_file = next(d for d in res.deliverables if d.module == "workflow")
        assert wf_file.filename == "项目立项.xml", wf_file.filename
        assert wf_file.path.suffix == ".xml" and wf_file.path.is_file()
        assert "流程设计" in wf_file.menu, wf_file.menu
        # 同步包（整包）里不允许出现流程条目
        allp = next(d for d in res.deliverables if d.module == "all")
        with zipfile.ZipFile(allp.path) as z:
            names = z.namelist()
        assert names and all(not n.startswith("workflow/") for n in names), names
    # 包注释里要说明不再生成流程 XML，否则用户会以为包里漏了流程
    body, _name = webui.build_zip(xlsx(sheets([START, APPROVE, PASS])), ["bo", "workflow"])
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        comment = (z.comment or b"").decode("utf-8", "ignore")
    assert "不再生成流程 XML" in comment, comment[-300:]


@case("WF17", "workflow", "五类节点的 Properties 键序与样本 v30 逐字一致（更正 #1 / #2）", "P0",
      ["Start 没有 ColorCode、有 IfShowButtonOfConsult",
       "Rejected 有 IfShowButtonOfAddReader、没有 IfShowButtonOfSaveForm",
       "Passed / Branch 同样本"])
def _():
    """样本证据：v30 的 13 个节点 + v1–v10 的 10 个 Rejected 节点全量解析后取的键序。

    这几条最容易「照设计文档写反」（架构文档第二节第 1、2 条），
    而只断言「有 IfShowButtonOfSaveForm 这个字符串」是测不出顺序和有无的。
    """
    xml = wf_xml(sheets([START, APPROVE, BRANCH, PASS, REJECT], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "部门审批"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "判断_预算"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "通过"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "驳回"},
    ]))
    props = props_of(ET.fromstring(xml))
    # Id：1 开始 / 2 审批 / 3 判断 / 4 通过 / 5 驳回
    assert list(props["1"]) == [
        "BeforeCompleteEvents", "IfShowButtonOfConsult", "AfterEnterEvents",
        "IfShowButtonOfSaveForm", "BusinessState", "FormKey",
        "BeforeEnterEvents", "AfterCompleteEvents"], list(props["1"])
    assert props["1"]["IfShowButtonOfConsult"] == "false"
    assert props["1"]["IfShowButtonOfSaveForm"] == "true"
    # 更正 #2：Rejected 与 Passed **不一样**
    assert list(props["5"]) == [
        "BeforeCompleteEvents", "AfterEnterEvents", "BusinessState", "FormKey",
        "ColorCode", "BeforeEnterEvents", "AfterCompleteEvents",
        "IfShowButtonOfAddReader"], list(props["5"])
    assert props["5"]["ColorCode"] == "8"
    assert list(props["4"]) == [
        "BeforeCompleteEvents", "AfterEnterEvents", "IfShowButtonOfSaveForm",
        "BusinessState", "FormKey", "ColorCode", "BeforeEnterEvents",
        "AfterCompleteEvents", "IfShowButtonOfAddReader"], list(props["4"])
    assert props["4"]["ColorCode"] == "3"
    # 更正 #1：开始节点没有 ColorCode（v1–v10 旧图有 7，v11–v30 都没有）
    assert "ColorCode" not in props["1"]
    # 判断节点只有一个 ColorCode，没有 State/ExpectHandleDays
    branch = next(a for a in ET.fromstring(xml).iter("BranchActivity"))
    assert branch.get("State") is None and branch.get("ExpectHandleDays") is None
    assert list(props["3"]) == ["ColorCode"] and props["3"]["ColorCode"] == "2"


@case("WF18", "workflow", "GoBackConfig 的键是字符串字典序，不是数字序", "P1",
      ["样本 node14 实证 {\"1\",\"12\",\"2\",\"3\",\"6\"}",
       "退回目标 ≥ 10 个才证得出：少于此数两种序完全相同，等于没测"])
def _():
    """WF12 只断言了 {"1","2"}，字符串序与数字序在那组数据上无法区分。

    这里串 11 个审批节点，让退回目标跨过 10，两种序必然不同。
    """
    nodes = [START]
    for i in range(2, 13):
        nodes.append({"流程名称": "项目立项", "节点序号": i,
                      "节点名称": f"审批{i}", "节点类型": "审批",
                      "处理人类型": "角色", "处理人": f"R{i}",
                      "处理人ID": str(1000 + i)})
    nodes.append(PASS)
    props = props_of(ET.fromstring(wf_xml(sheets(nodes))))
    ids = json.loads(props["12"]["GoBackActivityIds"])
    assert ids == list(range(1, 12)), ids          # 含开始节点、按数字升序
    keys = list(json.loads(props["12"]["GoBackConfig"])["commentRequired"])
    assert keys == sorted(keys, key=str), keys     # 字符串字典序
    assert keys != sorted(keys, key=int), \
        f"键序恰好与数字序相同，这条用例证不出差别：{keys}"


@case("WF19", "workflow", "nodePermissions 的 field_options 与表单 fieldOptions 逐控件相等", "P0",
      ["样本 57 项实测完全相等（form/002-project_create_detail_.xml）",
       "只断言「有 placeholder」测不出取值不同"])
def _():
    """节点上的控件行为是从 nodePermissions 来的，表单里的从 fieldOptions 来的。

    两边不一致 = 导入后「表单能填的字段在节点上是空的」或反之。
    这是流程与表单的接缝，必须逐键比，不能只看键存在。
    """
    data = sheets([START, APPROVE, PASS])
    proj, errors, _ = project_of(xlsx(data))
    assert not errors, errors
    with tempfile.TemporaryDirectory() as tmp:
        res = builder.build(proj, webui._snippets(), Path(tmp),
                            codes=["bo", "form", "workflow"])
        # 表单 XML 里每个控件的 fieldOptions（同一控件名可能出现在两张表里）
        form_opts: dict[str, list[dict]] = {}
        for art in res.artifacts:
            if art.module != "form" or not art.xml:
                continue
            for ff in ET.fromstring(art.xml).iter("formField"):
                raw = ff.get("fieldOptions")
                form_opts.setdefault(ff.get("name"), []).append(
                    json.loads(raw) if raw else {})
        assert form_opts, [a.key for a in res.artifacts]
        wf = next(d for d in res.deliverables if d.module == "workflow")
        perms = json.loads(ET.fromstring(
            wf.path.read_text(encoding="utf-8")).findtext("nodePermissions"))
        checked = 0
        for node, block in perms.items():
            for item in block["field"]:
                if item["name"] == "_p_title":
                    continue
                assert item["name"] in form_opts, \
                    f"节点 {node} 的控件 {item['name']} 在表单里不存在"
                assert item["field_options"] in form_opts[item["name"]], (
                    f"节点 {node} 的控件 {item['name']} field_options 与表单不一致\n"
                    f"  权限侧：{item['field_options']}\n"
                    f"  表单侧：{form_opts[item['name']]}")
                checked += 1
        assert checked >= 4, f"只比了 {checked} 个控件，样本量不足"


@case("WF20", "workflow", "出边顺序重复只对判断节点报，普通节点不误报", "P1",
      ["普通节点 Order 恒 0（更正 #3），两个方向都要断言",
       "误报会给出一条永远改不掉的建议：用户填 1、2 也会被改回 0"])
def _():
    # 方向一：普通节点（审批）两条出边 —— 不得报「出边顺序重复」
    res, _proj = wf_build(sheets([START, APPROVE, PASS, REJECT], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "部门审批"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "通过"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "驳回"},
    ]))
    bogus = [n for n in res.notices if "出边顺序重复" in n]
    assert not bogus, f"非判断节点被当成判断节点报了重复：{bogus}"
    xml = next(d for d in res.deliverables
               if d.module == "workflow").path.read_text(encoding="utf-8")
    approve = next(a for a in ET.fromstring(xml).iter("NormalActivity")
                   if a.get("State") == "Ongoing")
    orders = [t.get("Order") for t in approve.findall("Transition")]
    assert orders == ["0", "0"], orders

    # 方向二：判断节点两条出边「顺序」都填 1 —— 必须报出来（否则两条出边抢同一个位置）
    res2, _ = wf_build(sheets([START, APPROVE, BRANCH, PASS, REJECT], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "部门审批"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "判断_预算"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "通过", "顺序": 1},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "驳回", "顺序": 1},
    ]))
    assert any("出边顺序重复" in n for n in res2.notices), res2.notices


@case("WF21", "workflow", "流转 Properties 键集按源节点种类精确对齐样本", "P1",
      ["源=Start 无 NotAllowManualTransform", "源=Ongoing 有，且在 IfHideCC 之后",
       "源=Branch 两个键都没有（30 个版本 365 条出边的统计结论）"])
def _():
    res, _ = wf_build(sheets([START, APPROVE, BRANCH, PASS, REJECT], [
        {"流程名称": "项目立项", "起点节点": "申请人", "终点节点": "部门审批"},
        {"流程名称": "项目立项", "起点节点": "部门审批", "终点节点": "判断_预算"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "通过"},
        {"流程名称": "项目立项", "起点节点": "判断_预算", "终点节点": "驳回"},
    ]))
    xml = next(d for d in res.deliverables
               if d.module == "workflow").path.read_text(encoding="utf-8")
    root = ET.fromstring(xml)

    def tags_of(state: str) -> list[str]:
        act = next(a for a in root.iter()
                   if a.get("State") == state and a.tag == "NormalActivity")
        return [ch.tag for ch in act.find("Transition").find("Properties")]

    branch = next(a for a in root.iter("BranchActivity"))
    branch_tags = [ch.tag for ch in branch.find("Transition").find("Properties")]
    start_tags = tags_of("Start")
    ongoing_tags = tags_of("Ongoing")

    assert start_tags == ["IfHideComment", "PreTransformEvents", "IfHideAttachment",
                          "IfHideCC", "PostTransformEvents"], start_tags
    assert ongoing_tags == ["IfHideComment", "PreTransformEvents", "IfHideAttachment",
                            "IfHideCC", "NotAllowManualTransform",
                            "PostTransformEvents"], ongoing_tags
    assert branch_tags == ["IfHideComment", "PreTransformEvents",
                           "IfHideCC", "PostTransformEvents"], branch_tags
    # Ongoing：NotAllowManualTransform 必须夹在 IfHideCC 与 PostTransformEvents 之间
    assert (ongoing_tags.index("IfHideCC") < ongoing_tags.index("NotAllowManualTransform")
            < ongoing_tags.index("PostTransformEvents")), ongoing_tags
    # Start 的那条出边**不得**出现这个键（本次唯一与样本对不上的结构差异）
    assert "NotAllowManualTransform" not in start_tags
    assert "NotAllowManualTransform" not in branch_tags


@case("WF22", "workflow", "级联字段的父选项名不会被换成字典里不存在的短码", "P1",
      ["字典块只写子选项（sub_choices），换算表必须同源",
       "父选项名换算不出就保留原文 + 提示，不能静默指向坏 value"])
def _():
    from pp6kit import gen_bo

    # 「选项值」填分组写法（父：子、子），reader 会解析出 choice_groups
    grouped = field("pc", "品种分类", "选项", 选项值="中药：OTC、RX\n化类：跨类")
    data = {
        S.S_BO: [{"对象名称": "项目", "对象编码": "pc"}],
        S.S_FIELD: [grouped],
        S.S_FORM: [{"表单key": "create_pc", "表单名称": "新建项目", "对象编码": "pc",
                    "表单类型": "标准全套"}],
        S.S_WF: [{"流程名称": "P", "对象编码": "pc"}],
        S.S_WF_NODE: [
            {"流程名称": "P", "节点名称": "开始", "节点类型": "开始"},
            {"流程名称": "P", "节点名称": "判断_品种", "节点类型": "判断"},
            {"流程名称": "P", "节点名称": "通过", "节点类型": "通过"},
        ],
        S.S_WF_EDGE: [
            {"流程名称": "P", "起点节点": "开始", "终点节点": "判断_品种"},
            {"流程名称": "P", "起点节点": "判断_品种", "终点节点": "通过",
             "流转条件": "字段条件", "条件字段": "品种分类", "条件运算": "等于",
             "条件值": "OTC"},
            # 父选项名：字典块里没有这个 key，不能算出一个假短码
            {"流程名称": "P", "起点节点": "判断_品种", "终点节点": "开始",
             "流转条件": "字段条件", "条件字段": "品种分类", "条件运算": "等于",
             "条件值": "中药"},
        ],
    }
    # 同时出 bo 与流程：要和 bo XML 里的字典块逐项对照
    proj, errors, _ = project_of(xlsx(data))
    assert not errors, errors
    with tempfile.TemporaryDirectory() as tmp:
        res = builder.build(proj, webui._snippets(), Path(tmp), codes=["bo", "workflow"])
        bo = proj.bo("pc")
        assert bo is not None, "对象 pc 没读出来"
        assert bo.field_by_code("pinZhongFenLei") is not None, [f.code for f in bo.fields]
        codes = gen_bo.dict_choice_codes(bo, "pinZhongFenLei")
        # 父选项名不该出现在换算表里（字典块写的是 sub_choices，只有子选项）
        assert "中药" not in codes, codes
        assert {"OTC", "RX", "跨类"} <= set(codes), codes
        # 与 bo XML 的字典块逐项一致
        bo_xml = next(a for a in res.artifacts if a.module == "bo").xml
        dic = {i.get("name"): i.get("key") for i in ET.fromstring(bo_xml).iter()
               if i.tag.endswith("dictionary")}
        assert all(dic.get(k) == v for k, v in codes.items()), (codes, dic)

        xml = next(d for d in res.deliverables
                   if d.module == "workflow").path.read_text(encoding="utf-8")

    branch = next(a for a in ET.fromstring(xml).iter("BranchActivity"))
    values = {}
    for t in branch.findall("Transition"):
        tree = json.loads(json.loads(t.findtext("Condition"))["conditionJson"])
        rule = tree["rules"][0]
        values[rule["value_label"]] = rule["value"]
    # 子选项正常换码（= bo 字典里那个 key）
    assert values["OTC"] == codes["OTC"], values
    # 父选项名不再是「字典里查不到的码」，而是保留原文 + 明确提示
    assert values["中药"] == "中药", values
    assert values["中药"] not in dic.values(), f"父选项名仍被换成了死码：{values}"
    assert any("不在字段" in n and "中药" in n for n in res.notices), res.notices


@case("WF23", "workflow", "8_隐藏规则 也走同一套选项值换算（父选项名不出死码）", "P1",
      ["dict_choice_codes 有两条调用路径：流程条件 + 隐藏规则",
       "gen_bo 的修复同时影响隐藏规则，这里守住第二条"])
def _():
    from pp6kit import gen_bo

    grouped = field("pc", "品种分类", "选项", 选项值="中药：OTC、RX\n化类：跨类")
    data = {
        S.S_BO: [{"对象名称": "项目", "对象编码": "pc"}],
        S.S_FIELD: [grouped, field("pc", "名称", "字符串"), field("pc", "备注", "多行文本")],
        S.S_FORM: [{"表单key": "create_pc", "表单名称": "新建项目", "对象编码": "pc",
                    "表单类型": "新建表单"}],
        S.S_FORMFIELD: [
            {"表单key": "create_pc", "字段编码": "pinZhongFenLei", "显示标签": "品种分类",
             "控件类型": "dictionary", "取值来源": "pc_pinZhongFenLei"},
            {"表单key": "create_pc", "字段编码": "mingCheng", "显示标签": "名称",
             "控件类型": "text"},
            {"表单key": "create_pc", "字段编码": "beiZhu", "显示标签": "备注",
             "控件类型": "textarea"},
        ],
        # 两条规则：父选项名（中药）+ 子选项名（OTC），分开落在两个目标控件上
        S.S_HIDE: [
            {"表单key": "create_pc", "隐藏控件": "mingCheng", "依赖控件": "pinZhongFenLei",
             "判断条件": "不等于", "条件值": "中药"},
            {"表单key": "create_pc", "隐藏控件": "beiZhu", "依赖控件": "pinZhongFenLei",
             "判断条件": "等于", "条件值": "OTC"},
        ],
    }
    proj, errors, _ = project_of(xlsx(data))
    assert not errors, errors
    with tempfile.TemporaryDirectory() as tmp:
        res = builder.build(proj, webui._snippets(), Path(tmp), codes=["bo", "form"])
        bo = proj.bo("pc")
        codes = gen_bo.dict_choice_codes(bo, "pinZhongFenLei")
        assert "中药" not in codes, codes

        bo_xml = next(a for a in res.artifacts if a.module == "bo").xml
        dic = {i.get("name"): i.get("key") for i in ET.fromstring(bo_xml).iter()
               if i.tag.endswith("dictionary")}
        form_xml = next(a for a in res.artifacts if a.module == "form").xml

    form_def = next(iter(ET.fromstring(form_xml).iter("formDef")))
    ext = json.loads(form_def.get("extendAttr"))
    rules = {r["controlName"]: r["condition"]["rules"][0] for r in ext["formRules"]}
    assert set(rules) == {"mingCheng", "beiZhu"}, list(rules)

    # ① 父选项名：保留原文，不再是一个字典里查不到的假短码
    assert rules["mingCheng"]["value"] == "中药", rules["mingCheng"]
    assert rules["mingCheng"]["value"] not in dic.values(), \
        f"父选项名被换成了字典里不存在的码：{rules['mingCheng']['value']}"
    # ② 提示里列出的「可选选项名」必须是子选项，不能把父选项名又列一遍
    hint = [n for n in res.notices if "既不是选项名也不是选项值" in n]
    assert hint, res.notices
    listed = hint[0].split("可选选项名：")[1].rstrip("）")
    assert "OTC" in listed and "RX" in listed, listed
    assert "中药" not in listed and "化类" not in listed, listed
    # ③ 子选项：值与 bo 字典里的 key 完全相同（与 WF22 的流程条件对齐）
    assert rules["beiZhu"]["value"] == dic["OTC"], (rules["beiZhu"], dic)
    assert rules["beiZhu"]["value"] == codes["OTC"]


@case("WF24", "workflow", "流程驱动对象不填流程表也能出三步兜底流程", "P0",
      ["自动生成 开始→部门审批→通过", "审批人留空只警告不拦截", "Handler 回落人工选择"])
def _():
    """这个 app 的主业是出对象和表单，不逼人配流程。

    勾了「流程驱动」但三张流程表一个字没填时，必须自动给一条能导入的最小流程：
    开始 → 部门审批 → 通过。审批人允许留空（项目经理到系统里指定），
    所以 validate 只能警告，不能拦出包——拦了就等于又逼人回来填表。
    """
    from pp6kit.validate import validate

    res, proj = wf_build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract",
                  "物理表名": "pp_contract", "流程驱动": "是"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
    })
    files = [d for d in res.deliverables if d.module == "workflow"]
    assert len(files) == 1, [d.filename for d in res.deliverables]

    root = ET.fromstring(files[0].path.read_bytes())
    chart = root.find("designer/FlowDefinition/FlowCharts/FlowChart")
    acts = list(chart)
    assert [a.get("Name") for a in acts] == ["开始", "部门审批", "通过"], [a.get("Name") for a in acts]
    assert [a.get("State") for a in acts] == ["Start", "Ongoing", "Passed"], acts
    # 直线：开始 → 部门审批 → 通过
    assert [t.get("Target") for t in acts[0].findall("Transition")] == ["2"]
    assert [t.get("Target") for t in acts[1].findall("Transition")] == ["3"]
    assert acts[2].findall("Transition") == []
    # 审批人留空 → Handler 回落「提交时人工选择」，不能写成空串
    handler = acts[1].find("Properties/Handler").text or ""
    assert handler and "SelectMode=Referenced" in handler, handler

    issues = validate(proj)
    err = [i.message for i in issues if i.level == "error"]
    assert not err, f"默认流程不该有 error：{err}"
    warn = [i.message for i in issues if i.level == "warn"]
    assert any("默认流程的审批人留空" in m for m in warn), warn


@case("WF25", "workflow", "自己填了流程就不再加兜底流程", "P0",
      ["不覆盖用户配置", "只出一条"])
def _():
    res, _proj = wf_build(sheets([START, APPROVE, PASS]))
    names = [d.filename for d in res.deliverables if d.module == "workflow"]
    assert names == ["项目立项.xml"], names
    assert not any("自动生成默认流程" in n for n in res.notices), res.notices


@case("WF26", "workflow", "没填流程表时「下载流程 XML」也能下到兜底流程", "P0",
      ["/api/workflow 不误报 9_流程 是空的", "返回 application/xml"])
def _():
    """界面上的下载按钮走 workflow_files()，它自己读一遍 Excel。

    这条守的是「兜底流程必须对**所有**入口生效」——build 补了、parse 补了，
    下载入口漏补就会出现：预览里有流程，点下载却说「9_流程 是空的」。
    """
    data = xlsx({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract",
                  "物理表名": "pp_contract", "流程驱动": "是"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
    })
    # 反复调用两次：_cache_proj 有缓存，兜底逻辑必须幂等，不能越调越多流程
    body1, name1, ctype1 = webui.workflow_files(data)
    body2, name2, ctype2 = webui.workflow_files(data)

    assert ctype1 == "application/xml; charset=utf-8", ctype1
    assert name1.endswith(".xml") and name2 == name1, (name1, name2)
    assert body1 == body2, "同一份 Excel 两次下载应完全一致"
    root = ET.fromstring(body1)
    acts = list(root.find("designer/FlowDefinition/FlowCharts/FlowChart"))
    assert [a.get("Name") for a in acts] == ["开始", "部门审批", "通过"], acts


@case("WF27", "workflow", "processState 四个短码是平台系统常量", "P0",
      ["待发起 f9cd5a6d", "进行中 d432d037", "已完成 78782698", "已失败 3184e2aa"])
def _():
    """2026-11-13 在 8112 真实库（powerproject6_release）上核对出来的硬事实：

        SELECT name_, key_ FROM iform_cat_dic WHERE name_ IN ('待发起','进行中','已完成','已失败')

    20+ 个 `*_processState` 字典类型，四个选项的 key_ **完全一致**；
    对象导出 XML 的 dictionaryList 与流程 XML 的 BusinessState 用的也是这组值。
    所以它是系统常量，不是按对象算出来的——早先按 `{对象编码}_processState`
    算 md5，每个对象的码都不同，虽然自洽但与全系统不一致，
    列表/看板按码过滤会漏数据。

    这条用例就是防回退：谁把它改回 md5，这里立刻红。
    """
    from pp6kit.model import PROCESS_STATE_CODES, WF_STATE_OPTIONS

    assert PROCESS_STATE_CODES == {
        "待发起": "f9cd5a6d", "进行中": "d432d037",
        "已完成": "78782698", "已失败": "3184e2aa",
    }
    assert list(PROCESS_STATE_CODES) == list(WF_STATE_OPTIONS), "顺序要与状态推导一致"

    # 两条生成路径（对象字典块 / 流程 BusinessState）必须给出同一组码
    res, proj = wf_build(sheets([START, APPROVE, PASS]))
    wf_file = next(d for d in res.deliverables if d.module == "workflow")
    xml = wf_file.path.read_text(encoding="utf-8")
    used = set(re.findall(r"<BusinessState>([0-9a-f]{8})</BusinessState>", xml))
    assert used and used <= set(PROCESS_STATE_CODES.values()), used


@case("WF28", "workflow", "AI 给的流程 JSON 缺开始/结束/判断出边时自动补齐", "P0",
      ["没有开始 → 补一个", "没有通过 → 补一个", "判断只有 1 条出边 → 补兜底",
       "重名节点自动改名", "悬空流转丢弃"])
def _():
    """AI 输出不可信，但也不能因为漏一个节点就整条作废。

    这条用例把「模型常见的四种残缺」一次性喂进去，看归一化能不能修成
    能过 reader/validate 的形状——修不回来就会在用户界面变成
    「流程定义有错误，已中止」。
    """
    from pp6kit.ai_wf import _normalize

    raw = {"workflows": [{
        "name": "采购审批", "boCode": "purchase",
        "nodes": [
            {"name": "部门经理", "kind": "审批", "handlerNames": ["部门经理"]},
            {"name": "部门经理", "kind": "审批"},          # 重名
            {"name": "判断_金额", "kind": "判断"},
            {"name": "总经理", "kind": "审批", "handlerNames": "总经理、财务"},
        ],
        "edges": [
            {"src": "部门经理", "dst": "判断_金额", "name": "同意"},
            {"src": "判断_金额", "dst": "总经理", "order": 1,
             "conditionField": "amount", "conditionOp": "大于等于", "conditionValue": "100000"},
            {"src": "不存在", "dst": "总经理", "name": "悬空"},
        ],
    }]}
    notes: list[str] = []
    wf = _normalize(raw, {"purchase"}, notes)[0]

    names = [n.name for n in wf.nodes]
    assert names[0] == "开始" and names[-1] == "通过", names
    assert len(set(names)) == len(names), names                 # 重名已去
    assert [n.id for n in wf.nodes] == list(range(1, len(names) + 1))
    assert any("重名" in x for x in notes)
    assert any("开始" in x for x in notes)
    assert any("结束" in x for x in notes)

    # 判断节点：原有的 1 条 + 自动补的兜底 = 2 条，且 order 是 1、2
    branch_outs = [e for e in wf.edges if e.src == "判断_金额"]
    assert len(branch_outs) == 2, [(e.src, e.dst) for e in wf.edges]
    assert sorted(e.order for e in branch_outs) == [1, 2]
    assert not any(e.src == "不存在" for e in wf.edges), "悬空流转没丢掉"
    # 字符串形式的处理人也能拆开
    gm = next(n for n in wf.nodes if n.name == "总经理")
    assert gm.handler_names == ["总经理", "财务"], gm.handler_names


@case("WF29", "workflow", "流程写回 Excel 只动三张表且能原样读回", "P0",
      ["改节点后读回一致", "其它 sheet 原样保留", "有/无 # 说明行两种表都能写"])
def _():
    from openpyxl import load_workbook

    from pp6kit import template
    from pp6kit.model import (WF_ONGOING, WF_PASSED, WF_START, WfEdge,
                              WfNode, Workflow)
    from pp6kit.reader import read_workbook

    data = xlsx(sheets([START, APPROVE, PASS]))
    tmp = Path(tempfile.mkdtemp()) / "t.xlsx"
    tmp.write_bytes(data)
    proj, errors, _ = read_workbook(tmp)
    assert not errors, errors

    proj.workflows = [Workflow(name="项目立项", bo_code=BO, nodes=[
        WfNode(wf="项目立项", name="开始", kind=WF_START, id=1),
        WfNode(wf="项目立项", name="部门审批", kind=WF_ONGOING, id=2,
               handler_names=["部门负责人"]),
        WfNode(wf="项目立项", name="财务审批", kind=WF_ONGOING, id=3,
               handler_names=["财务"]),
        WfNode(wf="项目立项", name="通过", kind=WF_PASSED, id=4),
    ], edges=[
        WfEdge(wf="项目立项", src="开始", dst="部门审批", name="提交"),
        WfEdge(wf="项目立项", src="部门审批", dst="财务审批", name="同意"),
        WfEdge(wf="项目立项", src="财务审批", dst="通过", name="同意"),
    ])]

    wb = load_workbook(io.BytesIO(data))
    before = list(wb.sheetnames)
    template.write_workflows(wb, proj)
    # 原有 sheet 一张都不能丢；只有「流程三张表本来就缺」时才允许补建
    assert all(n in wb.sheetnames for n in before), (before, wb.sheetnames)
    assert S.S_WF_EDGE in wb.sheetnames, "缺 11_流程流转 时应该补建出来"

    buf = io.BytesIO()
    wb.save(buf)
    p2 = Path(tempfile.mkdtemp()) / "t2.xlsx"
    p2.write_bytes(buf.getvalue())
    proj2, errors2, _ = read_workbook(p2)
    assert not errors2, errors2
    assert [n.name for n in proj2.workflows[0].nodes] == \
        ["开始", "部门审批", "财务审批", "通过"]
    assert len(proj2.workflows[0].edges) == 3

    # 再写一次（幂等）：行数不该涨，否则会出现「流程名称重复」
    wb2 = load_workbook(io.BytesIO(buf.getvalue()))
    template.write_workflows(wb2, proj2)
    buf2 = io.BytesIO()
    wb2.save(buf2)
    p3 = Path(tempfile.mkdtemp()) / "t3.xlsx"
    p3.write_bytes(buf2.getvalue())
    proj3, errors3, _ = read_workbook(p3)
    assert not errors3, errors3
    assert len(proj3.workflows) == 1 and len(proj3.workflows[0].nodes) == 4


@case("WF30", "workflow", "AI 改流程只替换同名那条，不删用户其它流程", "P0",
      ["同名 → 替换", "改名 → 按对象替换", "没提到的 → 原样保留"])
def _():
    from pp6kit.ai_wf import _merge
    from pp6kit.model import WF_PASSED, WF_START, WfNode, Workflow

    def mk(name, bo, node="审批"):
        return Workflow(name=name, bo_code=bo, nodes=[
            WfNode(wf=name, name="开始", kind=WF_START, id=1),
            WfNode(wf=name, name=node, kind="Ongoing", id=2),
            WfNode(wf=name, name="通过", kind=WF_PASSED, id=3),
        ])

    existing = [mk("项目立项", "project_create"), mk("采购申请", "purchase")]

    # 1) 同名替换
    out, notes = _merge(existing, [mk("项目立项", "project_create", "三级审批")], "")
    assert [w.name for w in out] == ["项目立项", "采购申请"]
    assert out[0].nodes[1].name == "三级审批"
    assert any("更新" in n for n in notes)

    # 2) focus 那条被改名：按对象替换，不新增
    out2, notes2 = _merge(existing, [mk("项目立项审批流", "project_create")], "项目立项")
    assert [w.name for w in out2] == ["项目立项审批流", "采购申请"], [w.name for w in out2]
    assert any("改名" in n for n in notes2)

    # 3) 全新的流程 → 追加，旧的一条都不少
    out3, _ = _merge(existing, [mk("合同审批", "contract")], "")
    assert [w.name for w in out3] == ["项目立项", "采购申请", "合同审批"]
