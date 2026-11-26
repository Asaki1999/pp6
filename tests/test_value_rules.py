"""值来源的「值来源」：过滤规则（dynamic / fixed / sqlSnippet / systemVariable）。

用户口径（2026-11-20）：面板与 Excel 不能再只会「按本对象某个字段过滤」。
线上 333 条规则（266 份模板全库只读统计）里 `source` 与 `field_source` 是两个
维度，只有 4 种组合真实存在，本组用例把这 4 种逐条钉住：

    (dynamic, formField)        值来自表单控件  → 表单侧要绑一条 link_condition
    (fixed, formField)          固定字面值      → 只写 filterConditions，表单侧不绑
    (fixed, sqlSnippet)         整段 SQL        → id==field==SQL、operator=""、value=null
    (systemVariable, formField) 系统变量        → value=currentUserId，表单侧不绑

四条硬事实（架构文档 docs/架构-值来源过滤条件-20261120.md §5，全部有线上依据）：

  1. `(source, field_source)` 白名单外的组合**报错**，不降级、不猜；
  2. `field_type` / `field_input` 恒 `string` / `text`（线上 333/333，含数字列），
     `type` / `input` 才随列类型变；
  3. 表单侧 `link_condition` **只为 dynamic 且 control 非空的那条**写一条，
     `fieldName` 恒 `<rule_id>_1`；
  4. `rule_id` 由 `stable_code(ds.key:序号:列)` 派生，两侧读同一个对象，
     反复出包不变——两次出包不同就等于「过滤静默失效」。

兼容红线也在这一组里：`过滤规则` 留空 = 老口径（产物逐字不变），写 `无` =
一条过滤都不要，两者不能混。
"""
from __future__ import annotations

import io
import json
import re
import tempfile
import zipfile
from pathlib import Path

from pp6kit import spec as S
from pp6kit import webui
from pp6kit.reader import read_workbook
from tests._harness import case, field, texts, xlsx
from tests._projfields import project_fields

# ---------------------------------------------------------------- 夹具


def _sheets(cell, extra_fn=None):
    """最小可出包：demo_link.所属项目 → project（项目里有 项目类型/专业/计划状态）。

    `过滤规则` 单元格（cell）就是这一组用例的被测输入：
    留空 / `无` / JSON。
    """
    return {
        S.S_BO: [
            {"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
            {"对象名称": "项目类型", "对象编码": "classification",
             "物理表名": "pp_classification"},
            {"对象名称": "关联项目测试", "对象编码": "demo_link",
             "物理表名": "pp_demo_link"},
        ],
        S.S_FIELD: [
            field("project", "项目名称", "字符串", 长度=200),
            field("project", "项目类型", "对象", **{"关联对象/字典": "classification"}),
            field("project", "专业", "字符串", 长度=200),
            field("project", "计划状态", "数字"),
            field("classification", "名称", "字符串"),
            field("demo_link", "所属项目", "对象", **{"关联对象/字典": "project"}),
            field("demo_link", "项目类型", "对象", **{"关联对象/字典": "classification"}),
            field("demo_link", "专业", "字符串"),
        ],
        S.S_FORM: [
            {"表单key": "create_demo_link", "表单名称": "新建关联项目测试",
             "对象编码": "demo_link", "表单类型": "标准全套", "布局": "一行两列"},
        ],
        S.S_FORMFIELD: [
            {"表单key": "create_demo_link", "字段编码": "suoShuXiangMu",
             "显示标签": "所属项目", "控件类型": "linkdata", "取值来源": "project_value"},
            {"表单key": "create_demo_link", "字段编码": "xiangMuLeiXing",
             "显示标签": "项目类型", "控件类型": "select"},
            {"表单key": "create_demo_link", "字段编码": "zhuanYe",
             "显示标签": "专业", "控件类型": "text"},
        ],
        S.S_VALUE: [
            {"值来源key": "project_value", "名称": "项目值来源", "对象编码": "project",
             "返回字段": "id,name,zhuan_ye_", "过滤规则": cell},
        ],
    }


SL_SQL = "project_id_ = {{params:objectId}}"


def _build(cell):
    """出包 → (值来源 XML, 表单 XML, 包注释)。"""
    body = webui.build_zip(xlsx(_sheets(cell)))[0]
    tpl = form = None
    for name, text in texts(body).items():
        if name.startswith("data_template/") and 'type="valueSource"' in text \
                and 'key="project_value"' in text:
            tpl = text
        if name.startswith("form/") and 'key="create_demo_link"' in text:
            form = text
    assert tpl is not None and form is not None, sorted(texts(body))
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        note = (zf.comment or b"").decode("utf-8", "replace")
    return tpl, form, note


def _json_attr(xml: str, attr: str):
    m = re.search(attr + r'="([^"]*)"', xml)
    if not m:
        return None
    return json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))


def _filters(tpl: str):
    """filterConditions → (组内 condition, [规则...])；没有过滤返回 (None, None)。"""
    got = _json_attr(tpl, "filterConditions")
    if not got:
        return None, None
    assert len(got) == 1, f"本期只生成 1 个条件组：{got}"
    return got[0]["filter"]["condition"], got[0]["filter"]["rules"]


def _control_opts(form: str, name: str):
    """取某个控件的 fieldOptions（表单里有很多 fieldOptions，必须按控件名定位）。"""
    for frag in re.findall(r"<formField\b[^>]*>", form):
        if f'name="{name}"' not in frag:
            continue
        opts = _json_attr(frag, "fieldOptions")
        assert opts is not None, frag
        return opts
    raise AssertionError(f"表单里没有控件 {name}")


def _link_condition(form: str, name: str = "suoShuXiangMu"):
    return _control_opts(form, name)["link_condition"]


def _picker_cols(tpl: str):
    return re.findall(r'<dataTemplateField\b[^>]*\bname="([^"]+)"', tpl)


def _rules_json(rules):
    return json.dumps({"logic": "AND", "rules": rules}, ensure_ascii=False)


# ---------------------------------------------------------------- 四种形态


@case("VR01", "value_rules", "固定值过滤：只写 filterConditions，表单侧不绑 link_condition",
      "P0", ["fixed/formField", "field_type 恒 string", "value 保持字面值"])
def vr01():
    tpl, form, _ = _build(_rules_json([
        {"field": "name_", "operator": "contains", "source": "fixed", "value": "XY"}]))
    cond, rules = _filters(tpl)
    assert cond == "AND" and len(rules) == 1, rules
    rule = rules[0]
    assert rule["source"] == "fixed" and rule["field_source"] == "formField", rule
    assert rule["id"] == "name_" == rule["field"], rule
    assert rule["operator"] == "contains", rule
    assert rule["value"] == "XY", rule                       # 字面值原样，不是 "text"
    # 线上 333/333：field_type / field_input 恒 string / text
    assert rule["field_type"] == "string" and rule["field_input"] == "text", rule
    assert rule["type"] == "string" and rule["input"] == "text", rule
    assert rule["rule_id"] and rule["desc"] == rule["rule_id"], rule
    # 固定值没有控件可绑：表单侧 link_condition 必须为空（线上 356 个控件即如此）
    assert _link_condition(form) == [], _link_condition(form)


@case("VR02", "value_rules", "表单控件 + 固定值两条 AND：表单侧只绑 dynamic 那一条",
      "P0", ["link_condition 长度 = 1", "rule_id 两侧逐字一致", "fieldName 恒 <rule_id>_1"])
def vr02():
    tpl, form, _ = _build(_rules_json([
        {"field": "xiang_mu_lei_xing_", "operator": "equal", "control": "xiangMuLeiXing"},
        {"field": "name_", "operator": "equal", "source": "fixed", "value": "Y"}]))
    cond, rules = _filters(tpl)
    assert cond == "AND" and len(rules) == 2, rules
    dyn = [r for r in rules if r["source"] == "dynamic"]
    assert len(dyn) == 1, rules
    assert dyn[0]["value"] == "text", dyn[0]          # dynamic 的 value 恒 "text"
    assert dyn[0]["field_source"] == "formField", dyn[0]
    lc = _link_condition(form)
    assert len(lc) == 1, lc                           # 只有 dynamic 那条
    got = lc[0]
    assert got["mode"] == "bind", got
    assert got["value"] == "xiangMuLeiXing", got      # 本表单控件编码
    assert got["rule_id"] == dyn[0]["rule_id"], (got, dyn[0])
    # 线上一个控件挂 3 条条件时也是 xxx_1 —— fieldName 不是索引
    assert got["fieldName"] == f"{dyn[0]['rule_id']}_1", got
    assert got["fieldLabel"] == dyn[0]["label"], (got, dyn[0])
    # 11 个键，不新增
    assert set(got) == {"name", "fieldName", "fieldLabel", "fieldDataType", "mode",
                        "value", "type", "controlTypeOptions", "controlType",
                        "rule_id", "desc"}, sorted(got)


@case("VR03", "value_rules", "SQL 片段：id==field==整段 SQL、operator 为空、value=null",
      "P0", ["fixed/sqlSnippet", "线上 58/58 形态", "表单侧不绑"])
def vr03():
    tpl, form, _ = _build(_rules_json([
        {"field": SL_SQL, "operator": "", "source": "fixed",
         "field_source": "sqlSnippet", "value": None}]))
    cond, rules = _filters(tpl)
    assert len(rules) == 1, rules
    rule = rules[0]
    assert rule["id"] == rule["field"] == SL_SQL, rule
    assert rule["operator"] == "", rule
    assert rule["value"] is None, rule
    assert rule["field_source"] == "sqlSnippet" and rule["source"] == "fixed", rule
    assert rule["field_type"] == "string" and rule["field_input"] == "text", rule
    assert _link_condition(form) == [], _link_condition(form)
    # SQL 片段没有「目标列」，不能凭它多出一行 dataTemplateField
    assert SL_SQL not in _picker_cols(tpl), _picker_cols(tpl)


@case("VR04", "value_rules", "系统变量：value 写变量名，表单侧按「待实测」不绑",
      "P1", ["systemVariable/formField", "currentUserId", "link_condition 为空"])
def vr04():
    tpl, form, _ = _build(_rules_json([
        {"field": "name_", "operator": "contains", "source": "systemVariable",
         "value": "currentUserId"}]))
    _, rules = _filters(tpl)
    assert len(rules) == 1, rules
    assert rules[0]["source"] == "systemVariable", rules[0]
    assert rules[0]["field_source"] == "formField", rules[0]
    assert rules[0]["value"] == "currentUserId", rules[0]
    assert _link_condition(form) == [], _link_condition(form)


# ---------------------------------------------------------------- 数字列 / 逻辑


@case("VR05", "value_rules", "数字列：value 保持 JSON number，type/input 随列走但 field_type 仍是 string",
      "P0", ["less_or_equal 的 1 是数字", "field_type 恒 string"])
def vr05():
    tpl, _, _ = _build(_rules_json([
        {"field": "ji_hua_zhuang_tai_", "operator": "less_or_equal", "source": "fixed",
         "value": 1, "type": "number", "input": "number"}]))
    _, rules = _filters(tpl)
    rule = rules[0]
    assert rule["value"] == 1 and not isinstance(rule["value"], str), rule
    assert rule["type"] == "number" and rule["input"] == "number", rule
    assert rule["field_type"] == "string" and rule["field_input"] == "text", rule


@case("VR06", "value_rules", "多条规则用 OR：落到组内 condition，不是一个组合并",
      "P1", ["filter.condition = OR", "只出 1 个条件组"])
def vr06():
    cell = json.dumps({"logic": "OR", "rules": [
        {"field": "name_", "operator": "equal", "source": "fixed", "value": "A"},
        {"field": "zhuan_ye_", "operator": "equal", "source": "fixed", "value": "B"},
    ]}, ensure_ascii=False)
    tpl, _, _ = _build(cell)
    cond, rules = _filters(tpl)
    assert cond == "OR", cond
    assert [r["value"] for r in rules] == ["A", "B"], rules


# ---------------------------------------------------------------- 三档兼容


@case("VR07", "value_rules", "「过滤规则=无」= 一条过滤都不出（与留空的老口径区分开）",
      "P0", ["filter_rules=[]", "不写 filterConditions", "link_condition 为空"])
def vr07():
    tpl, form, _ = _build("无")
    assert _json_attr(tpl, "filterConditions") is None, tpl
    assert _link_condition(form) == [], _link_condition(form)


@case("VR08", "value_rules", "「过滤规则」留空 = 老口径：产物与改动前逐字一致",
      "P0", ["老 Excel / 没配规则", "仍按同名推导", "filterConditions 存在"])
def vr08():
    tpl, form, _ = _build("")
    _, rules = _filters(tpl)
    assert rules and rules[0]["source"] == "dynamic", rules
    assert rules[0]["value"] == "text", rules[0]
    lc = _link_condition(form)
    assert len(lc) == 1, lc
    assert lc[0]["rule_id"] == rules[0]["rule_id"], (lc, rules)


@case("VR09", "value_rules", "白名单外的组合直接报错，不静默降级",
      "P0", ["不支持的组合", "不支持的运算符", "坏 JSON", "logic 非 AND/OR"])
def vr09():
    bad = [
        _rules_json([{"field": "name_", "source": "fixed", "field_source": "sqlSnippetX"}]),
        _rules_json([{"field": "name_", "operator": "greater"}]),
        '{"rules": "not-a-list"}',
        '{"logic": "XOR", "rules": []}',
        "不是 JSON{{{",
        _rules_json([{"field": "", "source": "fixed", "value": "Y"}]),
    ]
    for cell in bad:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "in.xlsx"
            p.write_bytes(xlsx(_sheets(cell)))
            _proj, errors, _stats = read_workbook(p)
        assert any(S.S_VALUE in e and ("过滤规则" in e or "值来源组合" in e
                                      or "运算符" in e) for e in errors), (cell, errors)


# ---------------------------------------------------------------- 稳定性 / 容错


@case("VR10", "value_rules", "同一份 Excel 两次出包：rule_id 与整段 filterConditions 都不变",
      "P0", ["stable_code 派生", "两次出包逐字一致"])
def vr10():
    cell = _rules_json([
        {"field": "xiang_mu_lei_xing_", "operator": "equal", "control": "xiangMuLeiXing"},
        {"field": "name_", "operator": "equal", "source": "fixed", "value": "Y"}])
    tpl1, form1, _ = _build(cell)
    tpl2, form2, _ = _build(cell)
    assert _json_attr(tpl1, "filterConditions") == _json_attr(tpl2, "filterConditions")
    assert _link_condition(form1) == _link_condition(form2)
    # 7 位小写短码（线上 161/333 是 7 位）
    for rule in _filters(tpl1)[1]:
        rid = rule["rule_id"]
        assert len(rid) == 7 and re.fullmatch(r"[a-z0-9]{7}", rid), rid


@case("VR11", "value_rules", "目标列解析不到：丢弃该条 + 出提示，其余规则照常生效",
      "P0", ["不静默出全量", "notice 说清哪一列"])
def vr11():
    tpl, _, note = _build(_rules_json([
        {"field": "no_such_column_", "operator": "equal", "source": "fixed", "value": "1"},
        {"field": "name_", "operator": "equal", "source": "fixed", "value": "Y"}]))
    _, rules = _filters(tpl)
    assert [r["field"] for r in rules] == ["name_"], rules
    assert "no_such_column_" in note, note[-400:]


@case("VR12", "value_rules", "dynamic 规则没写目标列：按本表单字段的显示名推导一次",
      "P1", ["只写 control 也能跑", "推导口径与 auto_filter 一致"])
def vr12():
    tpl, form, _ = _build(_rules_json([
        {"field": "", "operator": "equal", "control": "xiangMuLeiXing"}]))
    _, rules = _filters(tpl)
    assert rules and rules[0]["field"] == "xiang_mu_lei_xing_", rules
    lc = _link_condition(form)
    assert lc and lc[0]["rule_id"] == rules[0]["rule_id"], (lc, rules)


# ---------------------------------------------------------------- 往返 / 契约


@case("VR13", "value_rules", "Excel 往返：「过滤规则」列读→写→读幂等，三档不互相吞",
      "P0", ["留空 / 无 / JSON 三档", "规则不丢", "legacy 两列留空"])
def vr13():
    from pp6kit import template
    from pp6kit.reader import read_workbook as _read

    cases = {
        "": None,
        "无": [],
        _rules_json([{"field": "name_", "operator": "equal", "source": "fixed", "value": "Y"}]):
            "has-rules",
    }
    for cell, kind in cases.items():
        data = _sheets(cell)
        if kind == "has-rules":
            # 同时把 legacy 两列也填上：有规则时回写必须**两列都留空**。
            # 以前这条断言是空的——夹具没填「过滤目标」，"" 天生成立，于是
            # 「只清过滤字段、留着过滤目标」这个真问题一直没人发现（QA 抓到）。
            data[S.S_VALUE][0]["过滤字段"] = "xiangMuLeiXing"
            data[S.S_VALUE][0]["过滤目标"] = "xiang_mu_lei_xing_"
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.xlsx"
            src.write_bytes(xlsx(data))
            proj, errors, _stats = _read(src)
            assert not errors, errors
            ds = next(d for d in proj.datasets if d.key == "project_value")
            if kind is None:
                assert ds.filter_rules is None, ds.filter_rules
            elif kind == []:
                assert ds.filter_rules == [], ds.filter_rules
            else:
                assert ds.filter_rules and ds.filter_rules[0].source == "fixed", ds.filter_rules
                # 读进来的 legacy 两列确实是有值的（否则下面两条断言仍为空断言）
                assert (ds.filter_field, ds.filter_target) == (
                    "xiangMuLeiXing", "xiang_mu_lei_xing_"), (ds.filter_field, ds.filter_target)
            out = Path(td) / "out.xlsx"
            template.build_workbook(proj).save(out)
            proj2, errors2, _ = _read(out)
            assert not errors2, errors2
            ds2 = next(d for d in proj2.datasets if d.key == "project_value")
            first = [r.to_dict() for r in (ds.filter_rules or [])] if ds.filter_rules else ds.filter_rules
            second = [r.to_dict() for r in (ds2.filter_rules or [])] if ds2.filter_rules else ds2.filter_rules
            assert first == second, (cell, first, second)
            # 有规则时 legacy 两列必须**都**留空：Excel 里不能出现半套配置
            # （过滤字段空了、过滤目标还留着 → 用户以为规则没生效）
            if ds2.filter_rules:
                assert ds2.filter_field == "", ds2.filter_field
                assert ds2.filter_target == "", ds2.filter_target


@case("VR14", "value_rules", "面板契约：filterRules 进 DataSet，指纹含规则（不同规则另起一份）",
      "P0", ["_source_configs 兼容 fieldSource/field_source", "两份值来源", "老 payload 不变"])
def vr14():
    rule_a = {"field": "name_", "operator": "equal", "source": "fixed",
              "fieldSource": "formField", "value": "Y"}
    cfgs = webui._source_configs([
        {"code": "suoShuXiangMu", "sourceConfig": {
            "custom": True, "resultFields": ["id"], "linkage": [],
            "filterRules": [rule_a], "filterLogic": "OR"}},
        {"code": "qiTa", "sourceConfig": {"filterRules": [
            {"field": "name_", "operator": "equal", "source": "fixed",
             "field_source": "formField", "value": "N"}]}},
        # 老会话：只带 filterField，没有 filterRules
        {"code": "laoHuiHua", "sourceConfig": {
            "custom": True, "resultFields": ["id"], "filterField": "xiangMuLeiXing"}},
    ])
    got = cfgs["suoShuXiangMu"]
    assert got["filterRules"] and got["filterRules"][0].value == "Y", got
    assert got["filterLogic"] == "OR", got
    # fieldSource（前端）与 field_source（Excel）两种写法都认
    assert cfgs["qiTa"]["filterRules"][0].field_source == "formField", cfgs["qiTa"]
    # 老会话只带 filterField → filterRules 是 None（走 legacy，产物逐字不变）
    assert cfgs["laoHuiHua"]["filterRules"] is None, cfgs["laoHuiHua"]

    # 同一目标对象、返回列相同、规则不同 → 必须另起一份值来源：
    # 共用一份时必有一个控件的过滤条件失效（导入后看不出错，下拉全是量）。
    from pp6kit.model import Bo, BoField, Project

    proj = Project()
    bo = Bo(code="demo_link", name="关联项目测试", tbl_name="pp_demo_link")
    bo.fields = [
        BoField(bo_code="demo_link", code="suoShuXiangMu", name="所属项目", sn=1,
                type_new="object", object_type="project"),
        BoField(bo_code="demo_link", code="beiYong", name="备用项目", sn=2,
                type_new="object", object_type="project"),
    ]
    proj.bos = [bo]
    notices = webui._apply_source_configs(proj, bo, [
        {"code": "suoShuXiangMu", "sourceConfig": {
            "custom": True, "resultFields": ["id"], "linkage": [],
            "filterRules": [rule_a]}},
        {"code": "beiYong", "sourceConfig": {
            "custom": True, "resultFields": ["id"], "linkage": [],
            "filterRules": [{"field": "name_", "operator": "equal", "source": "fixed",
                             "value": "N"}]}},
    ])
    keys = sorted(d.key for d in proj.datasets)
    assert len(keys) == 2, (keys, notices)
    assert all(d.filter_rules for d in proj.datasets), [d.filter_rules for d in proj.datasets]


@case("VR16", "value_rules", "AI 面板配的规则：进 Excel「过滤规则」列 → 出包真过滤 → 控件指向这份值来源",
      "P0", ["预览=产物", "规则不丢", "控件不被另建的干净值来源顶替"])
def vr16():
    with project_fields():
        """app 面板 → /api/ai/bo → Excel → 出包 这一整条链。

        最怕的一种静默失效：blueprint 给「默认那份值来源」另建了一份**干净**的
        （`_local_value_dataset` 不把带规则的当默认），而控件被接到新的那份上——
        用户在面板里配的过滤条件看着在、包里却没生效。这条用例就是钉它。
        """
        payload = {
            "bo": {"code": "hetong_apply", "name": "合同申请", "fields": [
                {"code": "suoShuXiangMu", "name": "所属项目", "type": "object",
                 "objectType": "project",
                 "sourceConfig": {
                     "custom": True, "resultFields": ["id", "name"], "linkage": [],
                     "filterLogic": "AND",
                     "filterRules": [
                         {"field": "name_", "operator": "contains",
                          "source": "fixed", "fieldSource": "formField", "value": "XY"},
                         {"field": "xiang_mu_lei_xing_", "operator": "equal",
                          "source": "dynamic", "fieldSource": "formField",
                          "control": "xiangMuLeiXing"},
                     ]}},
                {"code": "xiangMuLeiXing", "name": "项目类型", "type": "object",
                 "objectType": "classification"},
            ]},
            "form": {"key": "create_hetong_apply", "name": "新建合同申请", "fields": [
                {"name": "suoShuXiangMu", "label": "所属项目", "type": "linkdata"},
                {"name": "xiangMuLeiXing", "label": "项目类型", "type": "linkdata"},
            ]},
            "list": {"key": "hetong_apply_list", "name": "列表",
                     "columns": [{"code": "suoShuXiangMu", "label": "所属项目"}],
                     "views": ["grid"]},
        }
        excel, _name = webui.ai_apply_bo(payload, None)
        # 跳过模板自带的「# 说明」行（表头下一行是列说明，不是数据）
        rows = [r for r in _xlsx_rows(excel, S.S_VALUE)
                if str(r.get("值来源key") or "").strip()
                and not str(r.get("值来源key")).startswith("#")]
        assert rows, rows
        row = rows[0]
        assert row["过滤规则"] and row["过滤规则"] != "无", row
        cell_rules = json.loads(row["过滤规则"])["rules"]
        assert {r["source"] for r in cell_rules} == {"fixed", "dynamic"}, cell_rules

        body, _ = webui.build_zip(excel, [], {})
        tpl = form = None
        for name, text in texts(body).items():
            if name.startswith("data_template/") and 'type="valueSource"' in text:
                tpl = text if tpl is None else tpl
            if name.startswith("form/") and 'key="create_hetong_apply"' in text:
                form = text
        assert tpl is not None and form is not None
        cond, rules = _filters(tpl)
        assert cond == "AND" and len(rules) == 2, rules
        lc = _link_condition(form, "suoShuXiangMu")
        assert len(lc) == 1, lc                       # 只有 dynamic 那条要绑
        dyn = next(r for r in rules if r["source"] == "dynamic")
        assert lc[0]["rule_id"] == dyn["rule_id"], (lc, dyn)
        # 控件必须指向**带规则的那份**值来源（key 与 linkdata 一致），
        # 不能被 blueprint 另建的「干净那份」顶替——那样面板里配的过滤就白配了
        opts = _control_opts(form, "suoShuXiangMu")
        assert opts.get("linkdata") == row["值来源key"], (opts.get("linkdata"), row["值来源key"])


def _xlsx_rows(data: bytes, sheet: str) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True)
    if sheet not in wb.sheetnames:
        return []
    rows = list(wb[sheet].iter_rows(values_only=True))
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    return [{h: r[i] for i, h in enumerate(header) if i < len(r)}
            for r in rows[1:] if any(v not in (None, "") for v in r)]


@case("VR15", "value_rules", "blueprint 不把「带规则的那份」当默认值来源复用",
      "P1", ["_local_value_dataset 认 filter_rules", "避免把过滤份当全量份"])
def vr15():
    from pp6kit import blueprint
    from pp6kit.model import DataSet, FilterRule, Project

    proj = Project()
    filtered = DataSet(key="b_value", name="B", from_table="pp_a",
                       filter_rules=[FilterRule(field="name_", source="fixed", value="Y")])
    # filter_rules=[] 是「明确不过滤」= 全量那份，可以当默认值来源复用
    emptied = DataSet(key="c_value", name="C", from_table="pp_a", filter_rules=[])
    proj.datasets = [filtered, emptied]
    got = blueprint._local_value_dataset(proj, "a", "z_value")
    assert got != "b_value", got                     # 带规则的绝不能被当成「全部数据」
    assert got == "c_value", got
    # 只有带规则的份 → 不复用，让调用方另建一份干净的
    proj.datasets = [filtered]
    assert blueprint._local_value_dataset(proj, "a", "z_value") == ""
    # 不带任何规则的普通份仍然照旧复用（老口径不变）
    plain = DataSet(key="a_value", name="A", from_table="pp_a")
    proj.datasets = [plain, filtered]
    assert blueprint._local_value_dataset(proj, "a", "z_value") == "a_value"


# ---------------------------------------------------------------- 变异测试补的 4 条
# QA 用变异测试发现三处行为**没有用例钉住**（改坏了全绿）：dynamic 的 value、
# 同列多条规则的 rule_id、以及 fieldName 的 _1 后缀。补在这里，以后谁改坏了
# 立刻红。VR20 对应 P2「同时填过滤字段 + 过滤规则时，自动名要跟规则走」。


@case("VR17", "value_rules", "dynamic 规则的 value 恒为 \"text\"，不采信用户填的值",
      "P0", ["传了 value=AAA 也不能漏进产物", "线上 191 条 dynamic 全是 text"])
def vr17():
    tpl, _, _ = _build(_rules_json([
        {"field": "xiang_mu_lei_xing_", "operator": "equal",
         "control": "xiangMuLeiXing", "value": "AAA"}]))
    _, rules = _filters(tpl)
    assert len(rules) == 1, rules
    assert rules[0]["value"] == "text", rules[0]


@case("VR18", "value_rules", "同一目标列挂两条规则：rule_id 必须不同（派生不能少了序号）",
      "P0", ["seed 含规则下标", "否则两条条件会被系统当成同一条"])
def vr18():
    tpl, _, _ = _build(_rules_json([
        {"field": "name_", "operator": "contains", "source": "fixed", "value": "A"},
        {"field": "name_", "operator": "contains", "source": "fixed", "value": "B"}]))
    _, rules = _filters(tpl)
    assert len(rules) == 2, rules
    ids = [r["rule_id"] for r in rules]
    assert len(set(ids)) == 2, ids
    # 两条都保留、值各归各的（不是同一条被写了两遍）
    assert [r["value"] for r in rules] == ["A", "B"], rules


@case("VR19", "value_rules", "dynamic 在第二条时，fieldName 仍是 <rule_id>_1（不是 _2）",
      "P0", ["线上一个控件挂 3 条条件也全是 _1", "fieldName 不是索引",
             "两条都是 dynamic 时也都必须是 _1（序号不能取自「可绑定子集」）"])
def vr19():
    tpl, form, _ = _build(_rules_json([
        {"field": "name_", "operator": "equal", "source": "fixed", "value": "Y"},
        {"field": "xiang_mu_lei_xing_", "operator": "equal", "control": "xiangMuLeiXing"}]))
    _, rules = _filters(tpl)
    dyn = next(r for r in rules if r["source"] == "dynamic")
    lc = _link_condition(form)
    assert len(lc) == 1, lc
    assert lc[0]["rule_id"] == dyn["rule_id"], (lc, dyn)
    assert lc[0]["fieldName"] == f"{dyn['rule_id']}_1", lc[0]
    assert not lc[0]["fieldName"].endswith("_2"), lc[0]

    # 两条**都是** dynamic：这条以前抓不到——上面那条用例里 dynamic 在「可绑定
    # 子集」里恰好是第 0 个，把 fieldName 写成「可绑定规则里的序号」也不会红
    # （QA 的等价变异存活）。两条都必须 `_1`，且 rule_id 必须不同。
    tpl2, form2, _ = _build(_rules_json([
        {"field": "name_", "operator": "equal", "control": "xiangMuLeiXing"},
        {"field": "zhuan_ye_", "operator": "equal", "control": "zhuanYe"}]))
    _, rules2 = _filters(tpl2)
    lc2 = _link_condition(form2)
    assert len(lc2) == 2, lc2
    assert all(c["fieldName"].endswith("_1") for c in lc2), lc2
    assert not any(c["fieldName"].endswith("_2") for c in lc2), lc2
    assert len({c["rule_id"] for c in lc2}) == 2, lc2
    assert {c["rule_id"] for c in lc2} == {r["rule_id"] for r in rules2}, (lc2, rules2)


@case("VR20", "value_rules", "同时填「过滤字段」+「过滤规则」且名称留空：自动名跟规则走",
      "P2", ["有规则时 legacy 两列被忽略", "名字不能再说「过滤项目类型」"])
def vr20():
    data = _sheets(_rules_json([
        {"field": "name_", "operator": "equal", "source": "fixed", "value": "Y"}]))
    row = data[S.S_VALUE][0]
    # key 不以 _value 结尾，名称才会带用途后缀（value_source_name 的口径）
    row["值来源key"] = "proj_vs_a"
    row["过滤字段"] = "xiangMuLeiXing"      # legacy 那一列也在，但应被规则盖过
    row.pop("名称", None)                    # 名称留空 → 走自动命名

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "in.xlsx"
        p.write_bytes(xlsx(data))
        proj, errors, _stats = read_workbook(p)
    assert not errors, errors
    ds = next(d for d in proj.datasets if d.key == "proj_vs_a")
    assert "过滤name_" in ds.name, ds.name          # 取的是规则那一侧
    assert "项目类型" not in ds.name, ds.name        # 不是过滤字段那一侧
