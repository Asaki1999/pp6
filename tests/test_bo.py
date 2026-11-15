"""业务对象 XML：字段属性、字典、关系、系统字段。

这一层是「导入后能不能建表、字段能不能用」的最终决定者，
所以断言都直接读 zip 里的 XML，不看中间变量。
"""
from __future__ import annotations

import re

from pp6kit import spec as S
from pp6kit import webui
from tests._harness import (all_tags, attrs, attr_of, case, field, pick,
                            sample_zip, texts, unzip, xlsx, xml_ok)


def build(sheets: dict) -> bytes:
    return webui.build_zip(xlsx(sheets))[0]


def bo_xml(rows: list[dict], bo: dict | None = None) -> str:
    obj = bo or {"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}
    return pick(build({
        S.S_BO: [obj],
        S.S_FIELD: rows,
    }), "bo/")


@case("BO01", "bo", "生成的业务对象 XML 结构合法", "P0",
      ["XML 可被解析", "只有一个对象定义"])
def _():
    x = bo_xml([field("contract", "名称", "字符串")])
    xml_ok(x)
    assert x.count("<tns:bodefentity") == 1
    assert "http://www.ankepower.com/iform-bo-def" in x


@case("BO02", "bo", "表名/编码/名称按填写进 XML", "P0",
      ["tblName=pp_contract", "isCreateTable=Y"])
def _():
    x = bo_xml([field("contract", "名称", "字符串")])
    head = attrs(all_tags(x, "bodefentity")[0])
    assert head["tblName"] == "pp_contract"
    assert head["code"] == "contract"
    assert head["name"] == "合同"
    assert head["isCreateTable"] == "Y"
    assert head["pk"] == "id"


@case("BO03", "bo", "业务字段序号从 1 连续递增", "P0",
      ["sn 连续", "系统字段接在后面"])
def _():
    x = bo_xml([field("contract", "甲", "字符串"), field("contract", "乙", "字符串"),
                field("contract", "丙", "字符串")])
    sns = [int(attrs(t)["sn"]) for t in all_tags(x, "attribute")]
    assert sns == list(range(1, len(sns) + 1)), sns


@case("BO04", "bo", "系统字段自动补齐", "P0",
      ["id/tenantId/version/deleted", "缺了导进去没有主键"])
def _():
    x = bo_xml([field("contract", "名称", "字符串")])
    codes = {attrs(t)["code"] for t in all_tags(x, "attribute")}
    for need in ("id", "tenantId", "version", "deleted", "createTime", "updateTime"):
        assert need in codes, f"缺少系统字段 {need}"


@case("BO05", "bo", "字段物理列名以下划线结尾", "P0",
      ["手册要求", "列名错位会导致数据写不进去"])
def _():
    x = bo_xml([field("contract", "合同金额", "数字")])
    col = attr_of(x, "attribute", "heTongJinE", "fieldName")
    assert col.endswith("_"), col
    assert re.match(r"^[a-z][a-z0-9_]*$", col), col


@case("BO06", "bo", "自定义日期字段不被平台隐藏", "P0",
      ["isDefault=false", "format 到分钟",
       "isDefault=true 时整条字段在属性页消失"])
def _():
    x = bo_xml([field("contract", "签订日期", "日期时间（datetime）")])
    frag = all_tags(x, "attribute")
    item = next(t for t in frag if attrs(t)["code"] == "qianDingRiQi")
    a = attrs(item)
    assert a["dataTypeNew"] == "datetime", a
    assert a.get("format") == "yyyy-MM-dd HH:mm", a.get("format")
    assert "&quot;isDefault&quot;:false" in a["options"], a["options"]


@case("BO07", "bo", "日期字段填了默认值才标 isDefault", "P1",
      ["新建记录自动填时间"])
def _():
    x = bo_xml([field("contract", "归档日期", "日期时间（datetime）", 默认值="2026-01-01")])
    item = next(t for t in all_tags(x, "attribute")
                if attrs(t)["code"] == "guiDangRiQi")
    assert "&quot;isDefault&quot;:true" in attrs(item)["options"]


@case("BO08", "bo", "选项字段同时生成字典类型与候选值", "P0",
      ["dictionaryType + dictionaryList", "只有一处会变成空下拉"])
def _():
    x = bo_xml([field("contract", "状态", "选项", 选项值="草稿,生效,作废")])
    type_key = f"contract_zhuangTai"
    assert f'typeKey="{type_key}"' in x, "没有字典类型"
    names = re.findall(r'<tns:dictionary typeKey="%s"[^>]*name="([^"]+)"' % type_key, x)
    assert names == ["草稿", "生效", "作废"], names


@case("BO09", "bo", "选项候选值顺序与 Excel 一致", "P1",
      ["第一项是默认值", "顺序即界面顺序"])
def _():
    x = bo_xml([field("contract", "级别", "选项", 选项值="高,中,低")])
    sns = re.findall(r'<tns:dictionary typeKey="contract_jiBie"[^>]*sn="(\d+)"', x)
    assert sns == ["1", "2", "3"], sns


@case("BO10", "bo", "选项短码在多次出包间保持稳定", "P0",
      ["库里已存的是 key", "每次变会让旧记录对不上"])
def _():
    rows = [field("contract", "状态", "选项", 选项值="草稿,生效")]
    k1 = re.findall(r'key="(\w+)" name="草稿"', bo_xml(rows))
    k2 = re.findall(r'key="(\w+)" name="草稿"', bo_xml(rows))
    assert k1 and k1 == k2, (k1, k2)


@case("BO11", "bo", "关联对象字段带出 objectType", "P0",
      ["空 objectType 导入后是空下拉"])
def _():
    x = build({
        S.S_BO: [{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
                 {"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("project", "名称", "字符串"),
                    field("contract", "关联项目", "对象", **{"关联对象/字典": "project"})],
    })
    text = texts(x)["bo/002-contract.xml"]
    item = next(t for t in all_tags(text, "attribute")
                if attrs(t)["code"] == "guanLianXiangMu")
    a = attrs(item)
    assert a["dataTypeNew"] == "object"
    assert a["objectType"] == "project", a.get("objectType")


@case("BO12", "bo", "识别不出的类型按字符串落库", "P1",
      ["dataTypeNew 不能写中文", "本轮修复：脏类型会让字段改不了"])
def _():
    # 用真正认不出的写法（「日期」从 2026-11-13 起已是受支持类型，见 BO30）
    x = bo_xml([field("contract", "手写类型", "九宫格")])
    item = next(t for t in all_tags(x, "attribute")
                if attrs(t)["code"] == "shouXieLeiXing")
    a = attrs(item)
    assert a["dataTypeNew"] == "varchar", a
    assert a["datatype"] == "varchar", a


@case("BO13", "bo", "字段名里的特殊字符正确转义", "P1",
      ["尖括号/引号/&", "不转义会直接坏 XML"])
def _():
    x = bo_xml([field("contract", "合同<金额>&税率", "字符串")])
    xml_ok(x)
    assert "合同&lt;金额&gt;" in x or "合同<金额>" not in x.split("desc=")[1][:80]


@case("BO14", "bo", "大文本字段不带长度与格式", "P2",
      ["clob 没有 length", "带了会建表失败"])
def _():
    x = bo_xml([field("contract", "备注", "大文本")])
    item = next(t for t in all_tags(x, "attribute")
                if attrs(t)["code"] == "beiZhu")
    a = attrs(item)
    assert a["dataTypeNew"] == "clob"
    assert "length" not in a, a


@case("BO15", "bo", "开启日志的对象生成变更日志模板", "P1",
      ["保存/删除内容带对象名"])
def _():
    x = bo_xml([field("contract", "名称", "字符串")])
    assert "boLogTemplate" in x
    assert 'boCode="contract"' in x
    assert "创建了合同" in x


@case("BO16", "bo", "关闭日志的对象不生成日志模板", "P2",
      ["多一个模板会在系统里多一条菜单"])
def _():
    x = bo_xml([field("contract", "名称", "字符串")],
               {"对象名称": "合同", "对象编码": "contract",
                "物理表名": "pp_contract", "记录变更日志": "否"})
    assert "<tns:boLogTemplate " not in x


@case("BO17", "bo", "流程驱动对象带 4 个流程冗余字段", "P1",
      ["流程节点/处理人/定义/实例", "流程流转时反写"])
def _():
    x = build({
        S.S_BO: [{"对象名称": "项目立项", "对象编码": "project_create",
                  "物理表名": "pp_project_create", "流程驱动": "是"}],
        S.S_FIELD: [field("project_create", "名称", "字符串")],
    })
    text = texts(x)["bo/001-project_create.xml"]
    codes = {attrs(t)["code"] for t in all_tags(text, "attribute")}
    for need in ("processNode", "processHandler", "processDefId", "processInstanceId"):
        assert need in codes, need


@case("BO18", "bo", "父子关系写进 objRelationship", "P1",
      ["relField 用物理列名", "relType=PARENT"])
def _():
    x = build({
        S.S_BO: [{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
                 {"对象名称": "项目立项", "对象编码": "project_create",
                  "物理表名": "pp_project_create", "父对象编码": "project"}],
        S.S_FIELD: [field("project", "名称", "字符串"),
                    field("project_create", "名称", "字符串"),
                    field("project_create", "关联项目", "对象", **{"关联对象/字典": "project"})],
    })
    text = texts(x)["bo/002-project_create.xml"]
    rel = all_tags(text, "objRelationship")
    assert rel, "没有生成关系"
    a = attrs(rel[0])
    assert a["srcObjCode"] == "project_create"
    assert a["dstObjCode"] == "project"
    assert a["relType"] == "PARENT"
    assert a["relField"].endswith("_"), a["relField"]


@case("BO19", "bo", "同样输入两次出包只差对象 id", "P1",
      ["除 id 外必须逐字节一致", "隐式随机会让出包不可复现"])
def _():
    rows = [field("contract", "名称", "字符串"),
            field("contract", "金额", "数字", 精度=2)]
    a = bo_xml(rows)
    b = bo_xml(rows)
    assert a != b, "两次 id 完全一样，说明 id 不是新生成的（导入会冲突）"
    strip = lambda s: re.sub(r'bodefentity id="\d+"', 'bodefentity id="X"', s)
    assert strip(a) == strip(b)


@case("BO20", "bo", "关联字段留空时自动指向父对象", "P2",
      ["不用手填 projectId", "按父对象编码推断"])
def _():
    x = build({
        S.S_BO: [{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
                 {"对象名称": "项目立项", "对象编码": "project_create",
                  "物理表名": "pp_project_create", "父对象编码": "project"}],
        S.S_FIELD: [field("project", "名称", "字符串"),
                    field("project_create", "名称", "字符串")],
    })
    text = texts(x)["bo/002-project_create.xml"]
    rel = all_tags(text, "objRelationship")
    assert rel, "父子关系没生成"
    assert attrs(rel[0])["relField"].endswith("_")


@case("BO21", "bo", "多对象出包时每个对象各自独立成文件", "P0",
      ["bo/001-*.xml", "一个文件一个对象"])
def _():
    x = build({
        S.S_BO: [{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
                 {"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("project", "名称", "字符串"),
                    field("contract", "名称", "字符串")],
    })
    names = [n for n in unzip(x) if n.startswith("bo/")]
    assert len(names) == 2, names
    for n in names:
        xml_ok(unzip(x)[n].decode("utf-8"))


@case("BO22", "bo", "级联字段的 options 键与系统导出包完全一致", "P0",
      ["精简形态 5 键，键序 isMultiple/optionSource/cascade*/isDefault",
       "带配套空键的走完整 10 键形态，键序对齐导出包",
       "无级联的 dic 字段仍是 3 键，不许多写"])
def _():
    import json as _json

    def opts_of(xml: str, code: str) -> dict:
        m = re.search(r'<tns:attribute code="%s"[^>]*>' % code, xml)
        raw = re.search(r'options="([^"]*)"', m.group(0)).group(1)
        return _json.loads(raw.replace("&quot;", '"'))

    rows = [
        field("contract", "品种类别", "选项", 字段编码="productCategory",
              选项值="中药,化药"),
        field("contract", "品种分类", "选项", 字段编码="productClass",
              选项值="OTC,RX", 级联父字段="productCategory"),
        field("contract", "战略类别", "选项", 字段编码="strategyCategory",
              选项值="新品,其他", 级联父字段="productCategory"),
    ]
    xml = bo_xml(rows, {"对象名称": "合同", "对象编码": "contract",
                        "物理表名": "pp_contract"})

    plain = opts_of(xml, "productCategory")
    assert list(plain) == ["isMultiple", "optionSource", "isDefault"], list(plain)
    assert "cascadeEnabled" not in plain, "根节点不该带级联开关"

    child = opts_of(xml, "strategyCategory")
    assert list(child) == ["isMultiple", "optionSource", "cascadeEnabled",
                           "cascadeParentCode", "isDefault"], list(child)
    assert child["cascadeEnabled"] is True
    assert child["cascadeParentCode"] == "productCategory"

    # 显式给了配套键 → 完整 10 键，且键序照导出包
    full_rows = [rows[0], dict(rows[1], 扩展参数='{"uniqueCheck":false}'), rows[2]]
    full = opts_of(bo_xml(full_rows), "productClass")
    assert list(full) == ["isMultiple", "isDefault", "uniqueCheck", "optionSource",
                          "columns", "identities", "format", "formula",
                          "cascadeEnabled", "cascadeParentCode"], list(full)
    assert full["cascadeEnabled"] is True
    assert full["cascadeParentCode"] == "productCategory"


@case("BO23", "bo", "级联值写在 selectOptions[].dependValues，不是 attrs", "P0",
      ["选项值支持「父：子1、子2」分组写法",
       "selectOptions[].dependValues = 父选项字典短码",
       "selectOptions[].value 与 dictionaryList 的 key 一致",
       "普通 dic 字段不写 selectOptions"])
def _():
    import json as _json

    rows = [
        field("contract", "品种类别", "选项", 字段编码="varietyCategory",
              选项值="中药,化药"),
        field("contract", "品种分类", "选项", 字段编码="varietyType",
              选项值="中药：OTC、RX\n化药：转增加",
              级联父字段="varietyCategory"),
    ]
    xml = bo_xml(rows, {"对象名称": "合同", "对象编码": "contract",
                        "物理表名": "pp_contract"})

    def opts(code):
        m = re.search(r'<tns:attribute code="%s"[^>]*>' % code, xml)
        raw = re.search(r'options="([^"]*)"', m.group(0)).group(1)
        return _json.loads(raw.replace("&quot;", '"'))

    # 普通字段不带 selectOptions
    assert "selectOptions" not in opts("varietyCategory"), "无分组不该写 selectOptions"

    sel = {o["label"]: o for o in opts("varietyType")["selectOptions"]}
    assert set(sel) == {"OTC", "RX", "转增加"}, set(sel)
    # OTC/RX 属于中药（同组），转增加属于化药
    assert sel["OTC"]["dependValues"] == sel["RX"]["dependValues"], "同组父值应相同"
    assert sel["转增加"]["dependValues"] != sel["OTC"]["dependValues"]

    # 父选项字典的 key，必须正好等于子项的 dependValues
    parent_codes = {}
    for m in re.finditer(r'<tns:dictionary typeKey="contract_varietyCategory"[^>]*>',
                         xml):
        parent_codes[re.search(r'name="([^"]*)"', m.group(0)).group(1)] = \
            re.search(r'key="([^"]*)"', m.group(0)).group(1)
    assert parent_codes.get("中药") in sel["OTC"]["dependValues"], parent_codes
    assert parent_codes.get("化药") in sel["转增加"]["dependValues"], parent_codes

    # selectOptions 的 value 与 dictionaryList 的 key 必须一致，否则下拉显示空白
    child_codes = {}
    for m in re.finditer(r'<tns:dictionary typeKey="contract_varietyType"[^>]*>', xml):
        child_codes[re.search(r'name="([^"]*)"', m.group(0)).group(1)] = \
            re.search(r'key="([^"]*)"', m.group(0)).group(1)
    for label, o in sel.items():
        assert o["value"] == child_codes[label], (label, o["value"], child_codes)

    # 级联值不能落在 attrs 里（系统不读那里）
    assert '"dependValues"' not in xml.split("selectOptions")[0], "不该写进 attrs"


@case("BO24", "bo", "AI 显式指定的对象编码不会被名字覆盖", "P0",
      ["微调时 prefer_code=True 照用显式编码",
       "拼音长串仍按名字推导",
       "首次生成（prefer_code=False）保持名字优先"])
def _():
    from pp6kit import ai_bo

    def code_of(name, code, prefer):
        bo, _ = ai_bo.build_bo({"name": name, "code": code, "fields": []},
                               prefer_code=prefer)
        return bo.code

    # 微调：用户让 AI 把编码改成别的，必须照用（否则「说改好了其实没改」）
    assert code_of("项目立项信息", "project_apply_new", True) == "project_apply_new"
    assert code_of("合同", "contract_v2", True) == "contract_v2"
    # 驼峰 → 下划线
    assert code_of("合同", "ContractV2", True) == "contract_v2"
    # 没给编码时仍按名字推导
    assert code_of("项目立项信息", "", True) == "project_create"
    # 拼音长串（AI 把整段标题当名字的产物）不采信，退回名字推导
    assert code_of("项目立项信息表单", "liuxianglingshixinxibiaodan", True) == "project_create"
    # 首次生成保持「名字优先」，防止整段标题生成拼音编码
    assert code_of("项目立项信息", "whatever_code", False) == "project_create"


@case("BO25", "bo", "微调改编码时列表/表单 key 不被改写", "P1",
      ["列表 key 沿用传入值（原地更新，不新建一套）",
       "表单 key 同理"])
def _():
    import json as _json
    from pp6kit import ai_refine

    fake = {"bo": {"code": "project_apply_new", "name": "项目立项信息",
                   "fields": [{"code": "name", "name": "项目名称",
                               "type": "varchar", "values": []}]},
            "form": {"key": "create_project_create", "name": "新建项目立项信息",
                     "fields": [{"name": "name", "label": "项目名称", "type": "text"}]},
            "list": {"key": "project_create_list", "name": "项目立项信息列表",
                     "columns": [{"code": "name", "label": "项目名称"}], "views": ["grid"]},
            "changes": []}
    old = ai_refine.llm.chat_any
    ai_refine.llm.chat_any = lambda *a, **k: (_json.dumps(fake, ensure_ascii=False),
                                              {"model": "fake"})
    try:
        payload = {
            "instruction": "把对象编码改成 project_apply_new",
            "bo": {"code": "project_create", "name": "项目立项信息",
                   "fields": [{"code": "name", "name": "项目名称",
                               "type": "varchar", "choices": []}]},
            "form": {"key": "create_project_create", "name": "新建项目立项信息",
                     "fields": [{"name": "name", "label": "项目名称", "type": "text"}]},
            "list": {"key": "project_create_list", "name": "项目立项信息列表",
                     "columns": [{"code": "name", "label": "项目名称"}], "views": ["grid"]},
        }
        res = ai_refine.refine(payload, {})
    finally:
        ai_refine.llm.chat_any = old

    assert res["bo"]["code"] == "project_apply_new", res["bo"]["code"]
    assert res["list"]["key"] == "project_create_list", res["list"]["key"]
    assert res["form"]["key"] == "create_project_create", res["form"]["key"]
    # 编码变更必须出现在 changes 里，用户才知道真的改了
    assert any("project_apply_new" in c for c in res["changes"]), res["changes"]


@case("BO26", "bo", "AI 路径的分组候选值解析成 choice_groups", "P0",
      ["values 里「父：子1、子2」→ choice_groups + 扁平 choices",
       "出包后 selectOptions[].dependValues 与父字典 key 对上",
       "非分组的 values 不受影响"])
def _():
    import json as _json
    from pp6kit import ai_bo, gen_bo as gb

    spec = {"name": "合同", "fields": [
        {"code": "varietyCategory", "name": "品种类别", "type": "dic",
         "values": ["中药", "化药", "大健康"]},
        {"code": "varietyType", "name": "品种分类", "type": "dic",
         "cascadeParent": "varietyCategory",
         "values": ["中药：OTC、RX、双跨、其他", "化药：OTC、RX、双跨、其他",
                    "大健康：大健康"]},
        {"code": "status", "name": "状态", "type": "dic",
         "values": ["草稿", "进行中", "已完成"]},
    ]}
    bo, _ = ai_bo.build_bo(spec)
    vt = bo.field_by_code("varietyType")
    assert vt.choice_groups == {"中药": ["OTC", "RX", "双跨", "其他"],
                                "化药": ["OTC", "RX", "双跨", "其他"],
                                "大健康": ["大健康"]}, vt.choice_groups
    # 扁平 choices 含父值与子值（与 reader 路径一致）
    assert "中药" in vt.choices and "OTC" in vt.choices

    # 非分组字段不受影响
    st = bo.field_by_code("status")
    assert st.choice_groups == {}
    assert st.choices == ["草稿", "进行中", "已完成"]

    xml = gb.render(bo)
    m = re.search(r'<tns:attribute code="varietyType"[^>]*>', xml)
    opts = _json.loads(re.search(r'options="([^"]*)"', m.group(0))
                       .group(1).replace("&quot;", '"'))
    sel = {o["label"]: o for o in opts["selectOptions"]}
    parent_codes = {}
    for x in re.finditer(r'<tns:dictionary typeKey="contract_varietyCategory"[^>]*>', xml):
        parent_codes[re.search(r'name="([^"]*)"', x.group(0)).group(1)] = \
            re.search(r'key="([^"]*)"', x.group(0)).group(1)
    assert parent_codes["中药"] in sel["OTC"]["dependValues"]
    assert parent_codes["化药"] in sel["OTC"]["dependValues"]
    assert sel["大健康"]["dependValues"] == [parent_codes["大健康"]]
    # 非分组字段不写 selectOptions
    ms = re.search(r'<tns:attribute code="status"[^>]*>', xml)
    assert "selectOptions" not in ms.group(0)


@case("BO27", "bo", "改对象编码后，表单里陈旧的字典 key 出包时被纠正", "P0",
      ["dic/object 控件的取值来源永远按当前对象重算",
       "不沿用预览带来的旧编码时代的字典 key"])
def _():
    from pp6kit import webui as W
    payload = {
        "bo": {"code": "project_apply_2026", "name": "项目立项信息", "fields": [
            {"code": "applyUnit", "name": "立项申请单位", "type": "dic",
             "choices": ["研发中心", "事业部"]},
            {"code": "innovationPipeline", "name": "创新管线", "type": "dic",
             "choices": ["创新药", "改良药"]},
        ]},
        "form": {"key": "create_project_initiation", "name": "新建项目立项信息",
                 "fields": [
            {"name": "applyUnit", "label": "立项申请单位", "type": "select",
             "valueSource": "project_initiation_applyUnit"},
            {"name": "innovationPipeline", "label": "创新管线", "type": "select",
             "valueSource": "project_initiation_innovationPipeline"},
        ]},
        "list": {"key": "project_apply_2026_list", "name": "列表",
                 "columns": [{"code": "applyUnit", "label": "立项申请单位"}],
                 "views": ["grid"]},
    }
    xlsx_body, _ = W.ai_apply_bo(payload, None)
    zip_body, _ = W.build_zip(xlsx_body, [], {})
    z = _z if (_z := None) else None
    import zipfile as _zf, io as _io
    z = _zf.ZipFile(_io.BytesIO(zip_body))
    form = z.read([n for n in z.namelist() if n.startswith("form/")][0]).decode()
    assert "project_initiation_" not in form, "旧编码时代的字典 key 没清掉"
    assert "project_apply_2026_innovationPipeline" in form.replace("&quot;", '"')


@case("BO28", "bo", "AI 路径的裸 objectType 不再污染取值来源", "P0",
      ["build_bo 给 dic 的 objectType 是完整字典 key（对象编码_字段编码）",
       "预览带裸字段编码的 objectType/valueSource 出包时全部纠正",
       "表单 value_source 与 BO objectType 一致且无裸编码"])
def _():
    from pp6kit import ai_bo, webui as W

    # build_bo：AI 没给 objectType 时必须落完整 key
    bo, _ = ai_bo.build_bo({"name": "合同", "fields": [
        {"code": "source", "name": "来源", "type": "dic", "values": ["a", "b"]}]})
    assert bo.field_by_code("source").object_type == "contract_source", \
        bo.field_by_code("source").object_type

    # 预览 payload 带裸编码（旧版 AI 产物）→ 出包时纠正
    payload = {
        "bo": {"code": "project_apply", "name": "项目立项信息", "fields": [
            {"code": "projectSource", "name": "项目来源", "type": "dic",
             "objectType": "projectSource", "choices": ["外部引进", "自主立项"]},
        ]},
        "form": {"key": "create_x", "name": "新建", "fields": [
            {"name": "projectSource", "label": "项目来源", "type": "select",
             "valueSource": "projectSource"}]},
        "list": {"key": "project_apply_list", "name": "列表",
                 "columns": [{"code": "projectSource", "label": "项目来源"}],
                 "views": ["grid"]},
    }
    xlsx_body, _ = W.ai_apply_bo(payload, None)
    zip_body, _ = W.build_zip(xlsx_body, [], {})
    import zipfile as _zf, io as _io
    z = _zf.ZipFile(_io.BytesIO(zip_body))
    form = z.read([n for n in z.namelist() if n.startswith("form/")][0]) \
            .decode().replace("&quot;", '"')
    srcs = set(re.findall(r'"value_source":"([^"]+)"', form))
    assert srcs == {"project_apply_projectSource"}, srcs


@case("BO29", "bo", "dic 字段没有候选值也照样声明字典类型", "P0",
      ["空候选值的 dic 字段产出 dictionaryType（空字典）",
       "表单引用它的字典 key 不再被判「对象里不存在」",
       "导出不因模型漏给候选值而中止"])
def _():
    from pp6kit import webui as W
    payload = {
        "bo": {"code": "project_apply", "name": "项目立项信息", "fields": [
            {"code": "applyUnit", "name": "立项申请单位", "type": "dic",
             "choices": ["研发中心", "事业部"]},
            {"code": "techPlatform", "name": "技术平台", "type": "dic",
             "choices": []},                     # 模型漏给候选值
        ]},
        "form": {"key": "create_project_create", "name": "新建", "fields": [
            {"name": "applyUnit", "label": "立项申请单位", "type": "select",
             "valueSource": "project_apply_applyUnit"},
            {"name": "techPlatform", "label": "技术平台", "type": "select",
             "valueSource": "project_apply_techPlatform"},
        ]},
        "list": {"key": "project_apply_list", "name": "列表",
                 "columns": [{"code": "applyUnit", "label": "立项申请单位"}],
                 "views": ["grid"]},
    }
    xlsx_body, _ = W.ai_apply_bo(payload, None)
    zip_body, _ = W.build_zip(xlsx_body, [], {})
    import zipfile as _zf, io as _io
    z = _zf.ZipFile(_io.BytesIO(zip_body))
    bo = z.read([n for n in z.namelist() if n.startswith("bo/")][0]).decode()
    assert 'typeKey="project_apply_techPlatform"' in bo, "空候选值字段的字典没声明"


@case("BO30", "bo", "日期/文件/附件 的类型与长度按真实库生成", "P0",
      ["日期→datatype=date", "附件→varchar(200) 而非 clob", "文件→varchar"])
def _():
    """2026-11-13 在 8112 真实库（powerproject6_release）核对出来的事实：

        date        → SQL 列类型 date（pp_qjlc.jie_shu_）
        attachment  → varchar(200)（pp_project_initiation 的 4 个附件列）
        file        → varchar(64~640)（pp_tr1 的评审材料 / 会议纪要）

    改之前 `attachment` 是按「附件 JSON 存在大文本」**推断**成 clob 的
    （assets/fieldtypes.json 里 verified=false），与线上不一致；
    `date` 和 `file` 干脆没有这一档。
    """
    from pp6kit import fieldtypes

    assert fieldtypes.resolve("日期") == "date"
    assert fieldtypes.resolve("文件") == "file"
    assert fieldtypes.resolve("附件列表") == "attachment"

    types = fieldtypes.all_types()
    assert types["date"]["datatype"] == "date"
    assert types["attachment"]["datatype"] == "varchar", "附件不是 clob"
    assert types["attachment"]["length"] == 200
    assert types["file"]["datatype"] == "varchar"

    xml = bo_xml([field("contract", "立项日期", "日期"),
                  field("contract", "评审材料", "文件"),
                  field("contract", "附件", "附件列表")])
    assert 'dataTypeNew="date"' in xml and 'datatype="date"' in xml
    assert 'dataTypeNew="attachment"' in xml and 'datatype="varchar"' in xml
    assert 'dataTypeNew="file"' in xml
    assert 'dataTypeNew="attachment" datatype="clob"' not in xml


@case("BO31", "bo", "流程对象的选择模式是 workflow，普通对象是 normal", "P0",
      ['流程驱动=是 → mode="workflow"', '普通对象 → mode="normal"',
       "与线上原生包 project_create 逐字一致"])
def _():
    """线上「业务对象 → 选择模式」在 XML 里就是 bodefentity/@mode。

    实证（2026-11-14 解包复核）：
        PP6/业务对象/项目流程.zip   → project_create  mode="workflow"
        PP6/业务对象/项目立项业务对象.zip → 同上
        PP6/业务对象/项目.zip / 需求.zip / 选项.zip → mode="normal"

    生成器以前把这个属性写死成 normal，流程对象导进去就成了普通对象：
    流程 XML 挂不上对象，对象上也没有流程状态/审批入口。
    """
    wf = bo_xml([field("contract", "名称", "字符串")],
                {"对象名称": "合同", "对象编码": "contract",
                 "物理表名": "pp_contract", "流程驱动": "是"})
    assert attrs(all_tags(wf, "bodefentity")[0])["mode"] == "workflow"

    plain = bo_xml([field("contract", "名称", "字符串")])
    assert attrs(all_tags(plain, "bodefentity")[0])["mode"] == "normal"


@case("BO32", "bo", "反解原生包时流程对象保留流程模式", "P0",
      ["mode=workflow → flow_driven=True", "普通对象仍为 False",
       "否则反解→重出包会把流程对象降级"])
def _():
    import pathlib
    import tempfile

    from pp6kit.inspect import inspect_path
    from tests._harness import native_zip

    def flow_of(name: str) -> bool:
        p = pathlib.Path(tempfile.mkdtemp()) / "pkg.zip"
        p.write_bytes(native_zip(name))
        proj, _ = inspect_path(p)
        assert len(proj.bos) == 1
        return proj.bos[0].flow_driven

    assert flow_of("项目流程.zip") is True
    assert flow_of("项目.zip") is False


@case("BO33", "bo", "全局字典（项目对齐/反解带入）写成 GLOBAL + 固定候选码，不合成 optionSource",
      "P0", ["typeScope=GLOBAL", "不出现 对象_字段 新 key", "候选 key 原样照抄"])
def _():
    import json

    from pp6kit.model import Bo, BoField
    from pp6kit.gen_bo import render
    from tests._harness import attrs, all_tags

    items = [
        {"key": "MonthlyReportModules", "name": "月报", "sn": 146},
        {"key": "QuarterlyReportModules", "name": "季报", "sn": 178},
    ]
    bo = Bo(code="contract", name="合同", tbl_name="pp_contract")
    bo.fields.append(BoField(
        bo_code="contract", code="huiBaoLeiXing", name="汇报类型", type_new="dic",
        length=32, sn=1,
        # 线上真实形态：options 只有这两个键，没有 optionSource
        options={"isMultiple": False, "isDefault": False},
        dict_ref="report_type", dict_name="项目汇报类型", dict_stru="1",
        dict_items=items,
    ))
    bo.fields.append(BoField(
        bo_code="contract", code="second", name="第二个汇报", type_new="dic",
        length=32, sn=2, options={"isMultiple": False, "isDefault": False},
        dict_ref="report_type", dict_name="项目汇报类型", dict_stru="1",
        dict_items=items,
    ))
    text = render(bo, "bo-id")
    assert 'typeScope="GLOBAL"' in text
    assert 'typeKey="report_type"' in text
    assert 'struType="1"' in text
    # 同一个 typeKey 只声明一次字典类型、每个候选值只出现一次
    assert text.count('typeKey="report_type"') == 1 + 2, text.count('typeKey="report_type"')
    assert text.count('key="MonthlyReportModules"') == 1
    assert "contract_huiBaoLeiXing" not in text
    row = attrs(next(t for t in all_tags(text, "attribute")
                     if attrs(t).get("code") == "huiBaoLeiXing"))
    assert row["objectType"] == "report_type"
    opts = json.loads(row["options"].replace("&quot;", '"'))
    assert "optionSource" not in opts, opts
    assert opts == {"isMultiple": False, "isDefault": False}, opts


@case("BO34", "bo", "AI 只生成用户明确提到的字段，不自动补常见字段", "P0",
      ["只保留 6 个", "9 个额外字段被丢弃", "提示里说明"])
def _():
    # 用户实测：描述只提了 6 个字段，AI 回了 15 个（合同类型/承办部门/关联项目/
    # 生效日期/到期日期/合同状态/收付款状态/备注/合同附件全是自己加的）。
    # 两道闸：提示词不许凑 8-16 个；build_bo 再按描述做一次确定性过滤。
    from pp6kit import ai_bo
    desc = "做一个合同台账，记录合同名称、合同编号、合同金额、签订日期、对方单位、经办人。"
    names = ["合同编号", "合同名称", "合同类型", "合同金额", "签订日期", "对方单位",
             "经办人", "承办部门", "关联项目", "生效日期", "到期日期", "合同状态",
             "收付款状态", "备注", "合同附件"]
    bo, notices = ai_bo.build_bo(
        {"name": "合同台账", "code": "contract_ledger",
         "fields": [{"name": n, "type": "varchar"} for n in names]},
        description=desc)
    got = [f.name for f in bo.fields]
    assert got == ["合同编号", "合同名称", "合同金额", "签订日期", "对方单位", "经办人"], got
    for extra in ["合同类型", "承办部门", "关联项目", "生效日期", "到期日期",
                  "合同状态", "收付款状态", "备注", "合同附件"]:
        assert extra not in got, f"{extra} 是 AI 自己加的，应该被过滤掉"
    assert any("只保留明确提到的字段" in n and "9 个" in n for n in notices), notices
    assert "8-16" not in ai_bo.SYSTEM, "提示词还在让 AI 凑 8-16 个字段"
    assert "只生成用户描述里明确提到的业务字段" in ai_bo.SYSTEM, "提示词没有严格要求只生成提到的字段"


@case("BO35", "bo", "描述只说「预算」时，不许扩写成预算名称/预算金额", "P0",
      ["只保留预算", "预算名称/预算金额被丢弃"])
def _():
    # 真实 AI 实测：一句「做一个预算表，记录预算。」被拆成「预算名称/预算金额」，
    # 用户要的是「预算」本身。提示词加「用原词，不扩写」；这里再锁一道过滤。
    from pp6kit import ai_bo
    bo, notices = ai_bo.build_bo(
        {"name": "预算", "code": "budget",
         "fields": [{"name": "预算", "type": "number"},
                    {"name": "预算名称", "type": "varchar"},
                    {"name": "预算金额", "type": "number"}]},
        description="做一个预算表，记录预算。")
    assert [f.name for f in bo.fields] == ["预算"], [f.name for f in bo.fields]
    assert any("2 个" in n for n in notices), notices
    assert "不要生成「预算名称/预算金额」" in ai_bo.SYSTEM
