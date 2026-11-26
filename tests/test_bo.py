"""业务对象 XML：字段属性、字典、关系、系统字段。

这一层是「导入后能不能建表、字段能不能用」的最终决定者，
所以断言都直接读 zip 里的 XML，不看中间变量。
"""
from __future__ import annotations

import contextlib
import re

from pp6kit import spec as S
from pp6kit import webui
from tests._harness import (all_tags, attrs, attr_of, case, field, pick,
                            project_of, sample_zip, texts, unzip, xlsx, xml_ok)
from tests._projfields import project_fields


def build(sheets: dict) -> bytes:
    return webui.build_zip(xlsx(sheets))[0]


@contextlib.contextmanager
def _target_db(infos: dict):
    """模拟「已连目标库 + 查到了这些字段」。

    只 mock db_object_infos 不够：builder 还看 _db_ready()，本机有 ~/.pp6kit 配置时
    为真、CI 上为假，锁类型那一步会被整段跳过（BO41/BO43 只在 CI 红）。
    """
    from pp6kit import objects

    old_infos, old_ready = objects.db_object_infos, objects._db_ready
    objects.db_object_infos = lambda codes: infos
    objects._db_ready = lambda: {"database": "unit-test"}
    try:
        yield
    finally:
        objects.db_object_infos, objects._db_ready = old_infos, old_ready


@contextlib.contextmanager
def _system_datasets(keys: list[str]):
    """压住系统清单里的 dataSet key，避免结论跟着本机 ~/.pp6kit/templates.json 跑。

    值来源是复用系统那份、还是新建随包一份，直接决定控件是 selectObject 还是
    linkdata（见 tests/test_links.py LNK17 的长注释）。BO48 要的是前者只留
    classification，这样项目类型降级、所属项目建随包值来源。
    """
    from pp6kit import objects

    old = objects.extra

    def fake(force: bool = False) -> dict:
        data = dict(old(force))
        data["datasets"] = list(keys)
        return data

    objects.extra = fake
    try:
        yield
    finally:
        objects.extra = old


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
    # 扁平 choices **只含子值**：父值属于父字段，混进来会让子下拉
    # 多出「中药」「化药」（2026-11-17 尹姣定表实测的问题）
    assert vt.choices == ["OTC", "RX", "双跨", "其他", "大健康"], vt.choices

    # 非分组字段不受影响
    # 合同状态按项目管理标准编码为 contractStatus
    st = bo.field_by_code("contractStatus")
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
    ms = re.search(r'<tns:attribute code="contractStatus"[^>]*>', xml)
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
      ["日期→datatype=date", "附件 BO 类型→file（表单控件仍是 attachment）", "文件→varchar"])
def _():
    """2026-11-13 在 8112 真实库（powerproject6_release）核对出来的事实：

        date        → SQL 列类型 date（pp_qjlc.jie_shu_）
        attachment  → BO dataTypeNew=file、varchar(1000)（iform_bo_attr 实测 6 个 file 字段）
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
    assert types["attachment"]["length"] == 1000
    assert types["file"]["datatype"] == "varchar"

    xml = bo_xml([field("contract", "立项日期", "日期"),
                  field("contract", "评审材料", "文件"),
                  field("contract", "附件", "附件列表")])
    assert 'dataTypeNew="date"' in xml and 'datatype="date"' in xml
    # 附件在 BO 侧必须写 file；attachment 是表单控件类型，不是 BO 类型
    assert 'dataTypeNew="file"' in xml and 'datatype="varchar"' in xml
    assert 'dataTypeNew="attachment"' not in xml
    assert 'dataTypeNew="file" datatype="clob"' not in xml


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
    # 名称保持用户原词；编码按 PM 标准（不改变字段名）
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


@case("BO36", "bo", "AI 预览里自选的显示列原样落进 Excel 与出包列表", "P0",
      ["预览页「列表视图」可勾选要展示的字段",
       "导出 Excel 的 5_列表「显示字段」只写勾中的，不再全量",
       "出包 data_template 的 displayColumns 与勾选一致"])
def _():
    import io as _io
    import json as _json
    import zipfile as _zf

    import openpyxl

    from pp6kit import webui as W

    payload = {
        "bo": {"code": "purchase", "name": "采购申请", "fields": [
            {"code": "name", "name": "名称", "type": "varchar", "length": 100},
            {"code": "classificationId", "name": "项目类型", "type": "object",
             "objectType": "project"},
            {"code": "amount", "name": "金额", "type": "number"},
            {"code": "state", "name": "状态", "type": "dic", "choices": ["待审批", "已通过"]},
        ]},
        "form": None,
        # 用户在预览里只勾了这两列
        "list": {"key": "purchase_list", "name": "采购申请列表", "boCode": "purchase",
                 "views": ["grid"],
                 "columns": [{"code": "name", "label": "名称"},
                             {"code": "classificationId", "label": "项目类型"}],
                 "sort": "", "titleField": "name", "boardGroup": "",
                 "detailField": "name", "gantt": None},
    }
    xlsx_body, _ = W.ai_apply_bo(payload, None)
    wb = openpyxl.load_workbook(_io.BytesIO(xlsx_body), data_only=True)
    ws = wb["5_列表"]
    header = [c.value for c in ws[1]]
    col = header.index("显示字段") + 1
    got = ws.cell(row=3, column=col).value
    assert got == "name,classificationId", got
    # 级联详情必须写回 Excel：只留在内存里，用户「下载 → 拖回出包」就丢了，
    # 表现是「列表上选了级联显示，导入后点不动」（2026-11-20 反馈）
    dcol = header.index("级联详情字段") + 1
    assert ws.cell(row=3, column=dcol).value == "name", \
        ws.cell(row=3, column=dcol).value

    zip_body, _ = W.build_zip(xlsx_body, [], {})
    z = _zf.ZipFile(_io.BytesIO(zip_body))
    tpl = next(n for n in z.namelist() if n.startswith("data_template/"))
    import xml.etree.ElementTree as ET
    node = next(ET.fromstring(z.read(tpl)).iter("dataTemplateTpl"))
    cols_json = _json.loads(node.get("displayColumns") or "[]")
    cols = [c["name"] for c in cols_json]
    assert cols == ["name_", "classification_id_"], cols
    clickable = [c for c in cols_json if (c.get("cascade_configuration") or {}).get("formKey")]
    assert clickable, "级联详情没生成 cascade_configuration：点列打不开详情表单"
    assert clickable[0]["cascade_configuration"]["type"] == "form"
    assert clickable[0]["enabledCascade"] is True and clickable[0]["triggerEvent"] == "font"


@case("BO37", "bo", "两种模式的内置字段口径：普通 16 个，流程再加 7 个", "P0",
      ["普通：业务字段 + 名称 + id/tenantId/ip/createBy/createTime/updateBy/"
       "updateTime/deleted/version/baselineVersion + 流程四件套(300/1000/36/36) + isDraft",
       "普通对象也要流程四件套——系统新建对象照样落这 4 列（只读查库 "
       "labor_budget_rate / test_case_base_category），缺了「默认字段」就是 12 个",
       "流程：普通 12 个 + processDefId/processInstanceId/processNode/"
       "processHandler/processName/processCode/processState",
       "系统字段的 options.isDefault 一律 true（原生包 16 个默认字段全是 true）",
       "规格对齐线上 pp_project_create：deleted=20、handler=1000、name=255、"
       "node=dic(1000)、code=autoNumber(WF-)"])
def _():
    import html as _html
    import json as _json

    SYS12 = ["name", "id", "tenantId", "ip", "createBy", "createTime",
             "updateBy", "updateTime", "deleted", "version",
             "baselineVersion", "isDraft"]
    # 普通对象的流程四件套：varchar，不是 dic/autoNumber
    PLAIN_PROC = ["processNode", "processHandler", "processDefId",
                  "processInstanceId"]
    PLAIN16 = SYS12[:-1] + PLAIN_PROC + ["isDraft"]
    FLOW6 = ["processDefId", "processInstanceId", "processNode",
             "processHandler", "processName", "processCode"]

    def built(flow: str):
        x = pick(build({
            S.S_BO: [{"对象名称": "测试对象", "对象编码": "test_obj",
                      "物理表名": "pp_test_obj", "流程驱动": flow}],
            S.S_FIELD: [field("test_obj", "标题", "字符串")],
        }), "bo/")
        items = [attrs(t) for t in all_tags(x, "attribute")]
        return x, [a["code"] for a in items], {a["code"]: a for a in items}

    x, codes, by = built("否")
    assert codes == ["biaoTi"] + PLAIN16, codes
    assert len(SYS12) + len(PLAIN_PROC) == 16, "系统默认字段是 16 个"
    assert by["deleted"]["length"] == "20", by["deleted"]
    assert by["isDraft"]["length"] == "1"
    assert by["id"]["isindex"] == "Y" and by["tenantId"]["isindex"] == "Y"
    # 四件套规格照系统新建对象：300 / 1000 / 36 / 36，全是 varchar
    assert (by["processNode"]["length"], by["processNode"]["dataTypeNew"]) == ("300", "varchar")
    assert (by["processHandler"]["length"], by["processHandler"]["dataTypeNew"]) == ("1000", "varchar")
    assert (by["processDefId"]["length"], by["processDefId"]["dataTypeNew"]) == ("36", "varchar")
    assert (by["processInstanceId"]["length"], by["processInstanceId"]["dataTypeNew"]) == ("36", "varchar")
    # 默认字段标记：16 个系统字段全是 isDefault=true（原来只有 datetime 两个）
    for code in PLAIN16:
        o = _json.loads(_html.unescape(by[code]["options"]))
        assert o.get("isDefault") is True, f"{code} 缺默认字段标记：{o}"
    # 业务字段不能沾上这个标记（否则会在对象属性页里被当成系统自动填充字段）
    assert _json.loads(_html.unescape(by["biaoTi"]["options"])).get("isDefault") is not True

    x, codes, by = built("是")
    assert codes == ["biaoTi"] + FLOW6 + ["processState"] + SYS12, codes
    assert by["deleted"]["length"] == "20"
    assert by["processDefId"]["length"] == "200"
    assert by["processInstanceId"]["length"] == "200"
    assert by["processHandler"]["length"] == "1000", "处理人线上是 1000，不是 200"
    assert by["processName"]["length"] == "255"
    assert by["processNode"]["dataTypeNew"] == "dic"
    assert by["processNode"]["length"] == "1000"
    node_opts = _json.loads(_html.unescape(by["processNode"]["options"]))
    assert node_opts.get("isMultiple") is True
    assert by["processCode"]["dataTypeNew"] == "autoNumber"
    ident = _json.loads(_html.unescape(by["processCode"]["options"]))["identities"][0]
    assert ident["regulation"] == "WF-{yyyy}{MM}{dd}{NO}"
    assert ident["noLength"] == 3 and ident["duplicateType"] == "global"
    # processNode 的字典项 = 默认流程的三个节点
    assert "test_obj_processNode" in x
    for node in ("开始", "审核", "通过"):
        assert node in x


@case("BO38", "bo", "老包/反解 Excel 里的系统字段规格会被拉回统一口径", "P0",
      ["存量包里 deleted 常是 1、processHandler 是 200、processNode 是 varchar",
       "重建时只改类型/长度/索引，不动 code/fieldName/sn",
       "缺的 processName/processCode/processState 照补"])
def _():
    rows = [
        {"对象编码": "test_obj", "序号": 1, "字段编码": "biaoTi",
         "字段名称": "标题", "数据类型": "字符串", "长度": 200},
        {"对象编码": "test_obj", "序号": 2, "字段编码": "deleted",
         "字段名称": "数据删除状态", "数据类型": "字符串", "长度": 1, "建索引": "是"},
        {"对象编码": "test_obj", "序号": 3, "字段编码": "processNode",
         "字段名称": "流程节点", "数据类型": "字符串", "长度": 1000},
        {"对象编码": "test_obj", "序号": 4, "字段编码": "processHandler",
         "字段名称": "流程处理人", "数据类型": "字符串", "长度": 200},
        {"对象编码": "test_obj", "序号": 5, "字段编码": "processState",
         "字段名称": "流程状态", "数据类型": "字符串", "长度": 64},
    ]
    x = pick(build({
        S.S_BO: [{"对象名称": "测试对象", "对象编码": "test_obj",
                  "物理表名": "pp_test_obj", "流程驱动": "是"}],
        S.S_FIELD: rows,
    }), "bo/")
    by = {attrs(t)["code"]: attrs(t) for t in all_tags(x, "attribute")}
    assert by["deleted"]["length"] == "20", by["deleted"]
    assert by["processHandler"]["length"] == "1000", by["processHandler"]
    assert by["processNode"]["dataTypeNew"] == "dic", by["processNode"]
    assert by["processState"]["dataTypeNew"] == "dic", by["processState"]
    assert "processName" in by and "processCode" in by
    assert by["biaoTi"]["length"] == "200", "业务字段不许被改"


@case("BO39", "bo", "级联候选值：父值不能混进子字段（读表 + AI 两条路）", "P0",
      ["读表：『中药：OTC、RX』只把子项写进子字段字典",
       "AI 拍平（子字段里同时有中药/化药/OTC）也能按父字段去污",
       "AI 漏写 cascadeParent 时，按分组父值自动认父字段",
       "真实样本：尹姣定立项表的分组能被确定性抽出来"])
def _():
    import html as _html
    import json as _json
    from pathlib import Path

    from pp6kit import ai_bo, ai_sheet

    # ① 读表：级联列按「父：子」分组，父值不进子字段
    x = pick(build({
        S.S_BO: [{"对象名称": "测试对象", "对象编码": "test_obj",
                  "物理表名": "pp_test_obj"}],
        S.S_FIELD: [
            {"对象编码": "test_obj", "序号": 1, "字段编码": "pinZhongLeiBie",
             "字段名称": "品种类别", "数据类型": "选项", "选项值": "中药、化药、大健康"},
            {"对象编码": "test_obj", "序号": 2, "字段编码": "pinZhongFenLei",
             "字段名称": "品种分类", "数据类型": "选项",
             "选项值": "中药：OTC、RX、双跨、其他\n化化：OTC、RX\n大健康：大健康",
             "级联父字段": "pinZhongLeiBie"},
        ],
    }), "bo/")
    child = attrs(next(t for t in all_tags(x, "attribute")
                       if attrs(t)["code"] == "pinZhongFenLei"))
    opts = _json.loads(_html.unescape(child["options"]))
    labels = [o["label"] for o in opts.get("selectOptions") or []]
    assert labels == ["OTC", "RX", "双跨", "其他", "大健康"], labels
    assert "中药" not in labels and "化化" not in labels

    # ② AI 拍平 + 显式父字段：父值从子字段候选值里去掉
    bo, notes = ai_bo.build_bo({"name": "立项", "fields": [
        {"code": "productCategory", "name": "品种类别", "type": "dic",
         "values": ["中药", "化药", "大健康"]},
        {"code": "productClass", "name": "品种分类", "type": "dic",
         "cascadeParent": "productCategory",
         "values": ["中药", "化药", "OTC", "RX", "双跨", "其他"]},
    ]})
    child = bo.field_by_code("productClass")
    assert child.choices == ["OTC", "RX", "双跨", "其他"], child.choices
    assert any("混进了父字段" in n for n in notes), notes

    # ③ AI 只给分组、没写 cascadeParent：按父值集合自动认父
    bo, notes = ai_bo.build_bo({"name": "立项", "fields": [
        {"code": "productCategory", "name": "品种类别", "type": "dic",
         "values": ["中药", "化药", "大健康"]},
        {"code": "productClass", "name": "品种分类", "type": "dic",
         "values": ["中药：OTC、RX、双跨、其他", "化药：OTC、RX", "大健康：大健康"]},
    ]})
    child = bo.field_by_code("productClass")
    assert child.cascade_parent == "productCategory", child.cascade_parent
    assert child.choices == ["OTC", "RX", "双跨", "其他", "大健康"], child.choices
    assert child.choice_groups["中药"] == ["OTC", "RX", "双跨", "其他"]

    # ④ 真实样本：说明里的「随…联动」和「父：子」分组要能抽出来
    root = Path(__file__).resolve().parents[1]
    from tests._harness import skip

    real = root / "PP6" / "项目基本信息表单-尹姣定.xlsx"
    if not real.is_file():
        skip("缺少原生样本 PP6/项目基本信息表单-尹姣定.xlsx（①②③ 已跑完，只跳过真实样本段）")
    summary = ai_sheet.summarize(real.read_bytes())
    fields = {f["label"]: f for f in summary["sheets"][0]["fields"]}
    pc = fields["品种分类"]
    assert pc["parentLabel"] == "品种类别", pc.get("parentLabel")
    assert pc["groups"]["中药"] == ["OTC", "RX", "双跨", "其他"], pc["groups"]
    assert "N/A" in pc["groups"], "N/A 被斜杠拆开了"
    assert fields["项目注册类别"]["parentLabel"] == "战略类别"

    # ⑤ 概要抽出的分组真的能盖到「模型拍平」的 spec 上
    flat = {"fields": [
        {"code": "productCategory", "name": "品种类别", "type": "dic",
         "values": ["中药", "化药", "大健康"]},
        {"code": "productClass", "name": "品种分类", "type": "dic",
         "values": ["中药", "化药", "大健康", "OTC", "RX", "双跨", "其他"]},
    ]}
    notes: list[str] = []
    ai_sheet._apply_cascade_hints(flat, summary, notes)
    assert flat["fields"][1]["cascadeParent"] == "productCategory"
    bo, _ = ai_bo.build_bo(flat)
    assert bo.field_by_code("productClass").choices == ["OTC", "RX", "双跨", "其他", "大健康"]

    # ⑥ 「L1：创新药」是带代码前缀的单个取值，不按父子分组拆开
    flat_vals, _g, _t = ai_bo._clean_values(
        ["L1：创新药、改良型新药项目", "L2：仿制药、进口注册药品"], cascade=True)
    assert flat_vals == ["L1：创新药、改良型新药项目", "L2：仿制药、进口注册药品"], flat_vals


@case("BO40", "bo", "文档/PDF 里的「当 A 为 X 时 B 为 Y」能抽成级联", "P0",
      ["不限 Excel：Word/PDF 抽出的文字也走同一套",
       "父值 X 不能出现在 B 的候选值里",
       "句子里的字段名可能带「」/引号，要剥掉"])
def _():
    from pp6kit import ai_bo

    text = ("当「品种类别」为中药时，品种分类的下拉值为 OTC、RX、双跨、其他；"
            "当 a 下拉候选值为 xxx 时，b 字段的下拉值为 yyy、zzz。")
    hints = ai_bo.parse_conditional_cascades(text)
    assert [(h["parent"], h["parentValue"], h["child"]) for h in hints] == [
        ("品种类别", "中药", "品种分类"), ("a", "xxx", "b")], hints

    spec = {"fields": [
        {"code": "pinZhongLeiBie", "name": "品种类别", "type": "dic",
         "values": ["中药", "化药"]},
        {"code": "pinZhongFenLei", "name": "品种分类", "type": "dic",
         "values": ["中药", "化药", "OTC", "RX"]},
    ]}
    ai_bo._apply_text_cascades(spec, hints[:1])
    assert spec["fields"][1]["cascadeParent"] == "pinZhongLeiBie"
    bo, _ = ai_bo.build_bo(spec)
    assert bo.field_by_code("pinZhongFenLei").choices == ["OTC", "RX", "双跨", "其他"]


@case("BO41", "bo", "更新导入：目标已有字段的类型/长度被锁回去", "P0",
      ["平台不支持 ALTER COLUMN，一个字段类型不同整包失败",
       "目标库已有 clob，本包写 varchar → 改回 clob",
       "没标已有对象ID时，GUI 出包会自动认成更新（adopt）"])
def _():
    from pp6kit import builder, objects

    proj, _, _ = project_of(xlsx({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract",
                  "物理表名": "pp_contract", "已有对象ID": "644"}],
        S.S_FIELD: [{"对象编码": "contract", "序号": 1, "字段编码": "note",
                     "字段名称": "说明", "数据类型": "字符串", "长度": 200}],
    }))
    bo = proj.bos[0]
    with _target_db({"contract": {"id": "644", "fields": {"note": {"type_new": "clob", "length": None}}}}):
        notes = builder.align_existing_object_fields(proj)
    assert bo.field_by_code("note").type_new == "clob"
    assert any("不支持改类型" in n for n in notes), notes

    # adopt：没标 id，但目标同编码对象存在 → 自动按更新处理并锁类型
    proj2, _, _ = project_of(xlsx({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract",
                  "物理表名": "pp_contract"}],
        S.S_FIELD: [{"对象编码": "contract", "序号": 1, "字段编码": "note",
                     "字段名称": "说明", "数据类型": "字符串", "长度": 200}],
    }))
    bo2 = proj2.bos[0]
    with _target_db({"contract": {"id": "777", "fields": {"note": {"type_new": "clob", "length": None}}}}):
        builder.align_existing_object_fields(proj2, adopt=True)
    assert bo2.exist_id == "777"
    assert bo2.field_by_code("note").type_new == "clob"


@case("BO42", "bo", "目标表单没有主历史版本 → 换新 key 并改引用", "P0",
      ["8112 的 project_create_details 是遗留表单，没有主历史版本",
       "平台无法更新它，只能新建；不改库的前提下换 key",
       "对象 options.formKey / 控件 form_key 一起改"])
def _():
    from pp6kit import builder, objects

    proj, _, _ = project_of(xlsx({
        S.S_BO: [{"对象名称": "项目立项", "对象编码": "project_create",
                  "物理表名": "pp_project_create"}],
        S.S_FIELD: [field("project_create", "摘要", "字符串")],
        S.S_FORM: [{"表单key": "project_create_details", "表单名称": "立项详情",
                    "对象编码": "project_create", "表单类型": "详情表单"}],
        S.S_FORMFIELD: [{"表单key": "project_create_details", "字段编码": "zhaiYao",
                         "显示标签": "摘要", "控件类型": "text"}],
    }))
    bo = proj.bos[0]
    bo.options["formKey"] = "project_create_details"
    form = proj.forms[0]
    orig = objects.form_main_versions
    objects.form_main_versions = lambda keys: {
        k: (0 if k == "project_create_details" else -1) for k in keys}
    try:
        notes = builder.avoid_unupdatable_forms(proj)
    finally:
        objects.form_main_versions = orig
    assert form.key == "project_create_details_v2", form.key
    assert form.fields[0].form_key == "project_create_details_v2"
    assert bo.options["formKey"] == "project_create_details_v2"
    assert any("没有主历史版本" in n for n in notes), notes


@case("BO43", "bo", "更新导入：表格字段不被库里的 clob 锁成「大文本」", "P0",
      ["table 与 clob 同属 clob 物理族，互换不 ALTER，没必要锁",
       "2026-11-17 用户反馈：需求里是表格，导入后变成大文本",
       "库里上次已导成 clob 时，重出的包仍要保留 dataTypeNew=table"])
def _():
    from pp6kit import builder, objects

    proj, _, _ = project_of(xlsx({
        S.S_BO: [{"对象名称": "项目立项", "对象编码": "project_create",
                  "物理表名": "pp_project_create", "已有对象ID": "644"}],
        S.S_FIELD: [{"对象编码": "project_create", "序号": 1, "字段编码": "planExcel",
                     "字段名称": "里程碑计划表", "数据类型": "表格（table）"}],
        S.S_TABLE_COL: [{"对象编码": "project_create", "字段编码": "planExcel",
                         "序号": 1, "列名": "阶段", "列类型": "字符串（varchar）"}],
    }))
    bo = proj.bos[0]
    # 库里那条字段是上次误导成的大文本（clob）
    with _target_db({"project_create": {"id": "644",
                                       "fields": {"planExcel": {"type_new": "clob", "length": None}}}}):
        notes = builder.align_existing_object_fields(proj)

    f = bo.field_by_code("planExcel")
    assert f.type_new == "table", f"表格被库里的 clob 锁回去了：{f.type_new}"
    r = f.resolved()
    assert r["dataTypeNew"] == "table" and r["datatype"] == "clob" and r["type"] == "", r
    assert any("按表格出包" in n for n in notes), notes

    # 反过来：跨族（varchar → clob）仍然必须锁，否则整包被系统拒绝
    proj2, _, _ = project_of(xlsx({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract",
                  "物理表名": "pp_contract", "已有对象ID": "645"}],
        S.S_FIELD: [{"对象编码": "contract", "序号": 1, "字段编码": "note",
                     "字段名称": "说明", "数据类型": "字符串"}],
    }))
    with _target_db({"contract": {"id": "645", "fields": {"note": {"type_new": "clob", "length": None}}}}):
        builder.align_existing_object_fields(proj2)
    assert proj2.bos[0].field_by_code("note").type_new == "clob"


@case("BO44", "bo", "级联值写到 dictionary.dependValues（平台修复后的导入格式）", "P0",
      ["参考 config-sync-20261117204647.zip：dependValues 在字典项上，不在 selectOptions",
       "父值 key 必须与父字段字典项 key 完全一致（否则导入后级联是空的）",
       "根字段/无父项写 []，三层链逐级对上"])
def _():
    import html as _html
    import json as _json

    x = pick(build({
        S.S_BO: [{"对象名称": "测试级联", "对象编码": "csjl",
                  "物理表名": "pp_csjl"}],
        S.S_FIELD: [
            {"对象编码": "csjl", "序号": 1, "字段编码": "xuanXiangA",
             "字段名称": "选项A", "数据类型": "选项", "选项值": "A、B、C"},
            {"对象编码": "csjl", "序号": 2, "字段编码": "xuanXiangB",
             "字段名称": "选项B", "数据类型": "选项",
             "选项值": "A：1\nB：2\nC：2", "级联父字段": "xuanXiangA"},
            {"对象编码": "csjl", "序号": 3, "字段编码": "xuanXiangC",
             "字段名称": "选项C", "数据类型": "选项",
             "选项值": "1：J\n2：Q", "级联父字段": "xuanXiangB"},
        ],
    }), "bo/")

    items = {}
    for d in all_tags(x, "dictionary"):
        a = attrs(d)
        items.setdefault(a["typeKey"], {})[a["name"]] = {
            "key": a["key"],
            "deps": _json.loads(_html.unescape(a.get("dependValues") or "[]")),
        }
    A = items["csjl_xuanXiangA"]
    B = items["csjl_xuanXiangB"]
    C = items["csjl_xuanXiangC"]
    # 「A、B、C」必须拆成 3 个选项（顿号分隔）
    assert set(A) == {"A", "B", "C"}, A
    assert B["1"]["deps"] == [A["A"]["key"]], (B["1"], A)
    assert B["2"]["deps"] == [A["B"]["key"], A["C"]["key"]], B["2"]
    assert C["J"]["deps"] == [B["1"]["key"]], (C["J"], B)
    assert C["Q"]["deps"] == [B["2"]["key"]], (C["Q"], B)
    # 根字段也带 dependValues=[]（与系统导出格式一致）
    assert all(v["deps"] == [] for v in A.values())
    # 属性侧仍需声明父子关系，平台靠它决定控件是否级联
    b = attrs(next(t for t in all_tags(x, "attribute")
                   if attrs(t)["code"] == "xuanXiangB"))
    opts = _json.loads(_html.unescape(b["options"]))
    assert opts["cascadeEnabled"] is True
    assert opts["cascadeParentCode"] == "xuanXiangA"


@case("BO45", "bo", "AI 预览的级联分组经 Excel 往返后仍写进 dependValues", "P0",
      ["AI → ai_apply_bo → 2_字段「选项值」必须写成「父：子1、子2」",
       "出包 dictionary.dependValues 指向父字段真实 key",
       "分组父值不存在时不写悬空依赖码"])
def _():
    import io as _io
    import json as _json
    import zipfile as _zip

    payload = {"bo": {"code": "casc_ai", "name": "级联AI", "fields": [
        {"name": "品种类别", "code": "productCategory", "type": "dic",
         "choices": ["中药", "化药", "大健康"]},
        {"name": "品种分类", "code": "productClass", "type": "dic",
         "cascadeParent": "productCategory",
         "choices": ["OTC", "RX", "大健康"],
         "choiceGroups": {"中药": ["OTC", "RX"], "化药": ["OTC", "RX"],
                          "大健康": ["大健康"]}},
    ]}}
    xlsx_bytes, _ = webui.ai_apply_bo(payload)
    body, _ = webui.build_zip(xlsx_bytes)
    z = _zip.ZipFile(_io.BytesIO(body))
    bo = next(n for n in z.namelist() if n.startswith("bo/"))
    root = __import__("xml.etree.ElementTree", fromlist=["ET"]).fromstring(z.read(bo).decode())

    parent = {d.get("name"): d.get("key") for d in root.iter()
              if d.tag.endswith("dictionary")
              and d.get("typeKey") == "casc_ai_productCategory"}
    kids = {d.get("name"): d for d in root.iter()
            if d.tag.endswith("dictionary")
            and d.get("typeKey") == "casc_ai_productClass"}
    assert parent, "父字段字典缺失"
    assert set(kids) == {"OTC", "RX", "大健康"}, kids
    assert _json.loads(kids["大健康"].get("dependValues")) == [parent["大健康"]]
    assert set(_json.loads(kids["OTC"].get("dependValues"))) == {
        parent["中药"], parent["化药"]}

    # Excel 的选项值列要保留分组写法，否则下一次读表又变平
    import openpyxl
    wb = openpyxl.load_workbook(_io.BytesIO(xlsx_bytes))
    texts = {}
    for row in wb["2_字段"].iter_rows(min_row=2, values_only=True):
        if row[2]:
            texts[row[2]] = str(row[7] or "")
    assert "中药：OTC、RX" in texts["productClass"], texts["productClass"]


def _vs_payload(value_sources=None) -> dict:
    """值来源用例的固定夹具：本对象有「项目类型」→ 能推导出 filtered 那份。"""
    payload = {
        "bo": {"code": "purchase", "name": "采购申请", "fields": [
            {"code": "name", "name": "名称", "type": "varchar", "length": 100},
            {"code": "classificationId", "name": "项目类型", "type": "object",
             "objectType": "classification"},
            {"code": "projectId", "name": "所属项目", "type": "object",
             "objectType": "project"},
        ]},
        "form": {"key": "create_purchase", "name": "新建采购申请", "fields": [
            {"name": "name", "label": "名称", "type": "text"},
            {"name": "classificationId", "label": "项目类型", "type": "linkdata"},
            {"name": "projectId", "label": "所属项目", "type": "linkdata"},
        ]},
        "list": None,
    }
    if value_sources is not None:
        payload["valueSources"] = value_sources
    return payload


@case("BO46", "bo", "值来源清单：自动锁死、过滤那份可关；关掉后字段降级对象选择器", "P0",
      ["/api/ai/values 的 key 与出包一致（同一套 ensure）",
       "自动/全量 toggleable=False，过滤那份 toggleable=True",
       "关闭后不出 data_template XML，控件 fieldType 变 selectObject",
       "经 Excel 往返（build_zip 直接读这份 Excel）关闭状态仍生效"])
def _():
    with project_fields():
        import io as _io
        import zipfile as _zf

        from pp6kit import webui as W

        got = W.ai_value_sources(_vs_payload())
        items = got["valueSources"]
        filtered = [it for it in items if it["origin"] == "filtered"]
        assert filtered, [(it["key"], it["origin"]) for it in items]
        target = filtered[0]
        assert target["toggleable"] is True and target["enabled"] is True
        auto = [it for it in items if it["origin"] == "auto"]
        assert auto and not any(it["toggleable"] for it in auto), \
            [(it["key"], it["origin"], it["toggleable"]) for it in items]

        body, _ = W.ai_apply_bo(_vs_payload([{"key": target["key"], "enabled": False}]))
        zb, _ = W.build_zip(body, [], {})
        z = _zf.ZipFile(_io.BytesIO(zb))
        names = z.namelist()
        assert not any(n.endswith(f"{target['key']}.xml") for n in names), names
        form_xml = z.read(next(n for n in names if n.startswith("form/"))).decode()
        seg = form_xml.split('name="projectId"', 1)[1][:240]
        assert 'fieldType="selectObject"' in seg, seg


@case("BO47", "bo", "值来源只有三类用途：自定义新增已移除，传进来也不出包", "P1",
      ["清单里的 origin 只在 auto / link / filtered 之中",
       "带 custom 标记的项被忽略（包里找不到那个 key）",
       "/api/ai/values 不再返回 targets（新增浮层的产物）"])
def _():
    import io as _io
    import zipfile as _zf

    from pp6kit import webui as W

    got = W.ai_value_sources(_vs_payload())
    origins = {it["origin"] for it in got["valueSources"]}
    assert origins <= {"auto", "link", "filtered"}, origins
    assert "targets" not in got, got.keys()

    body, _ = W.ai_apply_bo(_vs_payload([{
        "custom": True, "name": "有效项目值来源", "target": "project",
        "key": "active_project_value",
        "filterRules": [{"field": "name_", "operator": "not_equal",
                         "source": "fixed", "value": "X"}],
    }]))
    zb, _ = W.build_zip(body, [], {})
    z = _zf.ZipFile(_io.BytesIO(zb))
    assert not [n for n in z.namelist() if "active_project_value" in n], z.namelist()


def _list_cols(value_sources=None) -> tuple[list[dict], str]:
    """列表列的固定夹具：出包后把 data_template 的 displayColumns 解出来。"""
    import io as _io
    import json as _json
    import re as _re
    import zipfile as _zf

    from pp6kit import webui as W

    payload = {
        "bo": {"code": "purchase", "name": "采购申请", "fields": [
            {"code": "name", "name": "名称", "type": "varchar", "length": 100},
            {"code": "classificationId", "name": "项目类型", "type": "object",
             "objectType": "classification"},
            {"code": "projectId", "name": "所属项目", "type": "object",
             "objectType": "project"},
            {"code": "managerId", "name": "责任人", "type": "user"},
            {"code": "category", "name": "类别", "type": "dic"},
            {"code": "amount", "name": "金额", "type": "number"},
        ]},
        "form": {"key": "create_purchase", "name": "新建采购申请", "fields": [
            {"name": "name", "label": "名称", "type": "text"},
            {"name": "classificationId", "label": "项目类型", "type": "linkdata",
             "valueSource": "classification_value"},
            {"name": "projectId", "label": "所属项目", "type": "linkdata"},
            {"name": "managerId", "label": "责任人", "type": "user"},
            {"name": "category", "label": "类别", "type": "select"},
            {"name": "amount", "label": "金额", "type": "number"},
        ]},
        "list": {"key": "purchase_list", "name": "采购申请列表",
                 "columns": ["name", "classificationId", "projectId",
                             "managerId", "category", "amount"]},
    }
    if value_sources is not None:
        payload["valueSources"] = value_sources
    body, _ = W.ai_apply_bo(payload)
    zb, _ = W.build_zip(body, [], {})
    z = _zf.ZipFile(_io.BytesIO(zb))
    xml = z.read(next(n for n in z.namelist()
                      if n.startswith("data_template/"))).decode()
    raw = _re.search(r'displayColumns="([^"]*)"', xml).group(1)
    return _json.loads(raw.replace("&quot;", '"')), xml


@case("BO48", "bo", "列表下拉列：控件形态跟表单一致，并带纯英文的显示字段名", "P0",
      ["关联列 field_type 与表单控件一致（linkdata / selectObject）",
       "linkdata 列带 linkdata key + link_config",
       "下拉列的 displayName 有值、与物理列名不同、纯 ASCII",
       "模板 attrs.display_field 开启（接口合并）",
       "字典列带 dictionary key，数字列不带 displayName"])
def _():
    with _system_datasets(["classification"]):
        cols, xml = _list_cols()
    by = {c["name"]: c for c in cols}

    cls = by["classification_id_"]
    assert cls["field_type"] == "selectObject", cls["field_type"]
    assert cls["field_options"]["boCode"] == "classification"
    proj = by["project_id_"]
    assert proj["field_type"] == "linkdata", proj["field_type"]
    assert proj["field_options"]["linkdata"] == "project_value"
    assert proj["field_options"]["link_config"]["text"] == "name_"
    assert proj["field_options"]["link_config"]["id"] == "id_"
    assert by["manager_id_"]["field_type"] == "dropSelector"
    assert by["manager_id_"]["field_options"]["selector_type"] == "user"
    assert by["category_"]["field_type"] == "dictionary"
    assert by["category_"]["field_options"]["dictionary"]
    assert by["amount_"]["field_type"] == "number"

    for name in ("classification_id_", "project_id_", "manager_id_"):
        dn = by[name].get("displayName")
        assert dn, f"{name} 缺显示字段名"
        assert dn != name, f"{name} 的显示字段名不能等于原字段名"
        assert dn.isascii() and dn.replace("_", "").isalnum(), f"{name} 显示字段名不是纯英文：{dn}"
        assert not any("\u4e00" <= ch <= "\u9fff" for ch in dn), dn
    assert by["amount_"].get("displayName") is None, "数字列不该有显示字段名"

    # 「跟字段控件一致」开关（显示列上的 same）：下拉类列必须打开，
    # 文本/数字列保持关闭。依据原生导出包 006-xmhbfxxb.xml 的「创建人」列
    # （{"same":"Y","field_type":"dropSelector"}）；2026-11-20 用户反馈
    # 列表上的下拉列这个开关没打开，点开取不到名称。
    for name in ("classification_id_", "project_id_", "manager_id_", "category_"):
        assert by[name].get("same") == "Y", f"{name} 没打开「跟字段控件一致」：{by[name].get('same')}"
    assert by["amount_"].get("same") == "N", by["amount_"].get("same")

    import json as _json
    tpl_attrs = _json.loads(
        __import__("re").search(r'dataTemplateTpl[^>]*attrs="([^"]*)"', xml)
        .group(1).replace("&quot;", '"'))
    assert tpl_attrs.get("display_field") is True, "接口合并没开，displayName 不会生效"


@case("BO49", "bo", "值来源被关掉后，列表列不能指向空 key（退回原生对象下拉）", "P0",
      ["关闭 project_value", "列表那列从 linkdata 变成 selectObject",
       "field_options 里不再有 linkdata key"])
def _():
    with project_fields():
        cols, _ = _list_cols([{"key": "project_value", "enabled": False}])
        by = {c["name"]: c for c in cols}
        proj = by["project_id_"]
        assert proj["field_type"] == "selectObject", proj["field_type"]
        assert "linkdata" not in proj["field_options"], proj["field_options"]
        assert proj["field_options"]["boCode"] == "project"
        assert proj.get("displayName") == "project_name", proj.get("displayName")


@case("BO50", "bo", "详情表单的关联对象一律 selectObject，不再挂着值来源（AI 路径）", "P0",
      ["「项目类型」与「项目选择」在详情页都是 selectObject",
       "fieldOptions 里没有 value_source / datasource",
       "boCode 仍写着目标对象编码"])
def _():
    # 2026-11-20 用户反馈：生成的详情页里「项目类型」成了下拉框、还挂着值来源，
    # 与线上详情表单（关联对象 = selectObject，只带 boCode + store=id）不一致。
    # 根因在字段对齐链路（projlink._rewrite_refs）不传 form.kind，见 PL29。
    payload = {
        "bo": {"code": "purchase_apply", "name": "采购申请", "fields": [
            {"code": "projectName", "name": "项目名称", "type": "varchar"},
            {"code": "code", "name": "项目编号", "type": "varchar"},
            {"code": "classificationId", "name": "项目类型", "type": "object",
             "objectType": "classification"},
            {"code": "purchaseOrder", "name": "采购订单", "type": "varchar"},
            {"code": "projectId", "name": "项目选择", "type": "object",
             "objectType": "project", "isLink": True},
        ]},
        "form": {"key": "create_purchase_apply", "name": "新建采购申请",
                 "fields": [
            {"name": "projectId", "label": "项目选择", "type": "selectObject"},
            {"name": "projectName", "label": "项目名称", "type": "text"},
            {"name": "code", "label": "项目编号", "type": "text"},
            {"name": "classificationId", "label": "项目类型", "type": "selectObject"},
            {"name": "purchaseOrder", "label": "采购订单", "type": "text"},
        ]},
        "list": {"key": "purchase_apply_list", "name": "采购申请列表", "views": ["grid"],
                 "columns": ["projectId", "projectName", "code", "classificationId"]},
    }
    xlsx_body, _ = webui.ai_apply_bo(payload)
    zip_body, _ = webui.build_zip(xlsx_body, [])

    import io as _io
    import zipfile as _zf
    z = _zf.ZipFile(_io.BytesIO(zip_body))
    hits = [n for n in z.namelist() if n.startswith("form/") and "detail" in n]
    assert hits, z.namelist()
    detail = z.read(hits[0]).decode().replace("&quot;", '"')

    for code, target in (("classificationId", "classification"), ("projectId", "project")):
        m = re.search(r'name="%s"[^>]*fieldType="selectObject"' % code, detail)
        assert m, f"详情表单的 {code} 不是 selectObject：{detail[:400]}"
        assert f'"boCode":"{target}"' in detail, f"{code} 丢了 boCode={target}"

    # 详情页是查看态：不许带值来源（带了就是一个能点开的下拉）
    assert '"value_source"' not in detail, "详情表单还挂着值来源"
    assert '"datasource":"valuesource"' not in detail, "详情表单还写着 datasource=valuesource"
    # 网格里的单元格也要是同一个口径（formField 与 grid cell 两处都写）
    assert '"field_type":"select"' not in detail, "详情表单里仍有普通 select 控件"


@case("BO51", "bo", "级联字典字段的列表列出成 select，并打开「跟字段控件一致」", "P0",
      ["控件是级联形态（select + 数据字典）时，列表列也出 select",
       "列 field_options 与表单控件同口径：datasource=dictionary + cascadeParentCode",
       "same=\"Y\"：类型不一致时设计器会把这个开关自动关掉",
       "级联链的父级列同样出 select，但不带 cascadeParentCode",
       "非级联的 dic 字段仍是 dictionary 列，same 也是 Y"])
def _():
    import json as _json

    data = build({
        S.S_BO: [{"对象名称": "测试级联", "对象编码": "csjl", "物理表名": "pp_csjl"}],
        S.S_FIELD: [
            {"对象编码": "csjl", "序号": 1, "字段编码": "mingCheng",
             "字段名称": "名称", "数据类型": "字符串"},
            {"对象编码": "csjl", "序号": 2, "字段编码": "pinZhongLeiBie",
             "字段名称": "品种类别", "数据类型": "选项", "选项值": "中药,化药"},
            {"对象编码": "csjl", "序号": 3, "字段编码": "pinZhongFenLei",
             "字段名称": "品种分类", "数据类型": "选项",
             "选项值": "中药：OTC、RX\n化药：转增加", "级联父字段": "pinZhongLeiBie"},
            {"对象编码": "csjl", "序号": 4, "字段编码": "zhanLueLeiBie",
             "字段名称": "战略类别", "数据类型": "选项", "选项值": "新品,其他"},
        ],
        S.S_FORM: [{"表单key": "create_csjl", "表单名称": "新建测试级联",
                    "对象编码": "csjl", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [
            {"表单key": "create_csjl", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
            {"表单key": "create_csjl", "字段编码": "pinZhongLeiBie",
             "显示标签": "品种类别", "控件类型": "dictionary"},
            {"表单key": "create_csjl", "字段编码": "pinZhongFenLei",
             "显示标签": "品种分类", "控件类型": "dictionary",
             "级联父字段": "pinZhongLeiBie"},
            {"表单key": "create_csjl", "字段编码": "zhanLueLeiBie",
             "显示标签": "战略类别", "控件类型": "dictionary"},
        ],
        S.S_LIST: [{"列表key": "csjl_list", "列表名称": "测试级联列表",
                    "对象编码": "csjl", "视图类型": "grid",
                    "显示字段": "mingCheng,pinZhongLeiBie,pinZhongFenLei,zhanLueLeiBie"}],
    })
    xml = pick(data, "data_template/")
    raw = re.search(r'displayColumns="([^"]*)"', xml).group(1)
    cols = _json.loads(raw.replace("&quot;", '"'))
    by = {c["label"]: c for c in cols}
    assert set(by) >= {"品种类别", "品种分类", "战略类别"}, sorted(by)

    # ① 级联子级：列类型跟表单控件一致（都是 select），否则 same 打不开
    child = by["品种分类"]
    assert child["field_type"] == "select", child["field_type"]
    assert child["same"] == "Y", f"「跟字段控件一致」没打开：{child.get('same')}"
    o = child["field_options"]
    assert o["datasource"] == "dictionary", o
    assert o["options_source"] == "bo_attr", o
    assert o["dictionary"] == "csjl_pinZhongFenLei", o
    assert o["cascadeEnabled"] is True, o
    assert o["cascadeParentCode"] == "pinZhongLeiBie", o
    assert o["init_linkage"] is True and o["selection"] is True and o["filterable"] is True, o
    assert o["placeholder"] == "请选择", o
    assert o["link_config"] is None and o["options"] == [], o
    # 显示字段名照旧只给关联/人员列，字典列不带（字段选项里已有 dictionary）
    assert child.get("displayName") is None, child.get("displayName")

    # ② 级联链的父级：表单里也被改成 select 了，列必须跟着改，但不带父编码
    parent = by["品种类别"]
    assert parent["field_type"] == "select", parent["field_type"]
    assert parent["same"] == "Y", parent.get("same")
    assert parent["field_options"]["cascadeEnabled"] is False, parent["field_options"]
    assert parent["field_options"]["cascadeParentCode"] == "", parent["field_options"]

    # ③ 非级联的 dic：仍是树形「数据字典」控件，列也还是 dictionary
    plain = by["战略类别"]
    assert plain["field_type"] == "dictionary", plain["field_type"]
    assert plain["same"] == "Y", plain.get("same")
    assert plain["field_options"] == {"dictionary": "csjl_zhanLueLeiBie"}, plain["field_options"]

    # ④ 表单侧确实是 select（两处必须对上，这正是 bug 的根因）
    fx = pick(data, "form/")
    frag = next(t for t in all_tags(fx, "formField")
                if attrs(t).get("name") == "pinZhongFenLei")
    assert attrs(frag).get("fieldType") == "select", attrs(frag).get("fieldType")


@case("BO52", "bo", "连库时新对象编码不许和库里已有对象一样（存量包修订除外）",
      "P0", ["配库实时查 iform_bo_def，命中就拦", "查询成功但库里没有 → 即使本地缓存有同名也放行",
             "连不上降级本地清单并说明来源", "编码归一（PP_Project=project）",
             "AI 生成 / 微调 / 套用导出都拦", "存量包修订不经过这道闸"])
def _():
    """2026-11-26 用户要求：AI 生成的新对象 code 撞库 = 导入后跟线上对象搅在一起，
    必须在生成时报错，不能等出包（bind 只给 ⚠ 提示）。豁免：存量包修订本来
    就在改已有对象（ai_patch 只动字段草稿，不经过这条链路）；Excel 手填要
    保留「显式更新已有对象」的场景，维持 bind() 的提示口径。"""
    import contextlib
    import json as _json

    from pp6kit import ai_refine, objects, webui
    from pp6kit.model import Bo

    @contextlib.contextmanager
    def _db(rows_by_code, connect_fail=False, catalog=None):
        """模拟连库状态：rows_by_code[code] = (id, name)；connect_fail 模拟连不上。"""
        class _Cur:
            def execute(self, sql, args=()):
                # code_taken 是全量拉回来归一比对，fake 就按 (id, code, name) 给
                self._rows = [(v[0], k, v[1]) for k, v in rows_by_code.items()]
            def fetchall(self):
                return self._rows
            def close(self):
                pass
        class _Conn:
            def cursor(self):
                if connect_fail:
                    raise RuntimeError("connection refused")
                return _Cur()
            def close(self):
                pass

        old = (objects._db_ready, objects._connect_ro, objects.catalog,
               objects._cache_source)
        objects._db_ready = lambda: {"database": "unit-test"}
        objects._connect_ro = lambda db, timeout=4: _Conn()
        objects.catalog = lambda force=False: catalog or []
        objects._cache_source = "目标库"
        try:
            yield
        finally:
            (objects._db_ready, objects._connect_ro, objects.catalog,
             objects._cache_source) = old

    # ---- ① 实时查库：命中 / 不命中 / 归一 / 连不上降级
    with _db({"project": ("1001", "项目")}):
        hit = objects.code_taken("project")
        assert hit and hit["source"] == "目标库" and hit["id"] == "1001", hit
        assert "项目" in objects.code_conflict_message(hit)
        # 查询成功且库里没有 → 即使本地清单/快照里有同名也放行（实时库说了算）
        assert objects.code_taken("contract") is None
        with _db({"project": ("1001", "项目")},
                 catalog=[{"code": "contract", "name": "合同"}]):
            pass
        # 归一：AI 写 PP_Project / pp-project 也要拦
        assert objects.code_taken("PP_Project") is not None
        assert objects.code_taken("pp-project") is not None
    # 连不上 → 降级本地清单，且来源标注不是「目标库」
    with _db({}, connect_fail=True,
             catalog=[{"code": "contract", "name": "合同", "id": "7"}]):
        hit = objects.code_taken("contract")
        assert hit and hit["source"] == "本地对象清单" and hit["id"] == "7", hit
        assert "本地对象清单" in objects.code_conflict_message(hit)
        assert objects.code_taken("nothing_here") is None

    # ---- ② 没配库（用户口径：连接了数据库才校验）→ 只看本地清单
    old_ready = objects._db_ready
    objects._db_ready = lambda: None
    try:
        old_cat = objects.catalog
        objects.catalog = lambda force=False: [{"code": "contract", "name": "合同"}]
        try:
            assert objects.code_taken("contract") is not None
            assert objects.code_taken("other") is None
        finally:
            objects.catalog = old_cat
    finally:
        objects._db_ready = old_ready

    # ---- ③ AI 一句话生成：撞了在表单/列表设计之前报错
    orig_generate = webui.ai_bo.generate
    def _fake_generate(desc, cfg, known_objects=None):
        return {"bo": Bo(code="project", name="项目", tbl_name="pp_project"),
                "notices": [], "model": "fake", "seconds": 0}
    webui.ai_bo.generate = _fake_generate
    try:
        with _db({"project": ("1001", "项目")}):
            try:
                webui.ai_generate_bo({"description": "生成一个项目"}, None)
                raise AssertionError("撞库没有拦下生成")
            except ValueError as exc:
                assert "project" in str(exc) and "编码改成" in str(exc), exc
    finally:
        webui.ai_bo.generate = orig_generate

    # ---- ④ 微调：改撞库编码 / 带着撞库编码加字段，都拦；不撞时照常返回
    fake = {"bo": {"code": "project", "name": "项目",
                   "fields": [{"code": "name", "name": "项目名称",
                               "type": "varchar", "values": []}]},
            "form": {"key": "create_project", "name": "新建项目",
                     "fields": [{"name": "name", "label": "项目名称", "type": "text"}]},
            "list": {"key": "project_list", "name": "项目列表",
                     "columns": [{"code": "name", "label": "项目名称"}],
                     "views": ["grid"]},
            "changes": []}
    old_chat = ai_refine.llm.chat_any
    ai_refine.llm.chat_any = lambda *a, **k: (_json.dumps(fake, ensure_ascii=False),
                                              {"model": "fake"})
    try:
        payload = {
            "instruction": "把对象编码改成 project",
            "bo": {"code": "contract", "name": "合同",
                   "fields": [{"code": "name", "name": "合同名称",
                               "type": "varchar", "choices": []}]},
            "form": {"key": "create_contract", "name": "新建合同",
                     "fields": [{"name": "name", "label": "合同名称", "type": "text"}]},
            "list": {"key": "contract_list", "name": "合同列表",
                     "columns": [{"code": "name", "label": "合同名称"}],
                     "views": ["grid"]},
        }
        with _db({"project": ("1001", "项目")}):
            try:
                ai_refine.refine(payload, {})
                raise AssertionError("微调撞库没有拦下")
            except ValueError as exc:
                assert "project" in str(exc) and "编码改成" in str(exc), exc
        # 不撞（实时库没有 contract）→ 正常返回（BO25 的老路径不受影响）
        with _db({"project": ("1001", "项目")}):
            payload2 = dict(payload)
            payload2["instruction"] = "改一下"
            fake2 = dict(fake)
            fake2["bo"] = dict(fake["bo"], code="contract")
            ai_refine.llm.chat_any = lambda *a, **k: (
                _json.dumps(fake2, ensure_ascii=False), {"model": "fake"})
            res = ai_refine.refine(payload2, {})
            assert res["bo"]["code"] == "contract", res["bo"]["code"]
    finally:
        ai_refine.llm.chat_any = old_chat

    # ---- ⑤ 套用导出：最后一道闸，撞了拒绝导出 Excel
    with _db({"project": ("1001", "项目")}):
        try:
            webui.ai_apply_bo({"bo": {"code": "project", "name": "项目",
                                      "fields": []}})
            raise AssertionError("导出撞库没有拦下")
        except ValueError as exc:
            assert "project" in str(exc), exc

    # ---- ⑥ 豁免：存量包修订链路不接这道闸（它本来就在改已有对象）
    import pathlib
    patch_src = (pathlib.Path(objects.__file__).parent / "ai_patch.py").read_text(
        encoding="utf-8")
    assert "code_taken" not in patch_src, \
        "存量包修订不该做新建编码撞库校验（它修订的就是已有对象）"


@case("BO53", "bo", "对话克隆：照上传的对象包快照生成一模一样的新对象",
      "P0", ["描述点名关联目标 + 克隆词才触发（只点名不算）",
             "字段编码/名称/类型/长度/候选值/级联/子表列原样复制",
             "AI 只起新名字，起名失败回退「原名副本」",
             "dic 字段不带旧字典 key（新对象的 key 由编码重新生成）",
             "快照不在时提示先上传对象包，退化成从零设计"])
def _():
    """2026-11-26 用户要求：导入对象 zip 后，AI 对话说「生成与它完全一样的」，
    要能克隆。AI 抄几十个字段必丢细节，实现是程序化复制 + AI 起名。"""
    import json as _json

    from pp6kit import projlink, webui
    from pp6kit.model import BoField

    # ---- ① 意图识别：点名 + 克隆词
    target = projlink.target_for("projchange", "项目变更", "pp_projchange", "", "snapshot")
    assert webui._clone_intent("生成与项目变更完全一样的字段 所有字段都要", target) is True
    assert webui._clone_intent("帮我设计一个项目变更对象", target) is False, \
        "只点名不算克隆"
    assert webui._clone_intent("生成与项目变更完全一样的字段", projlink.target_for(
        "contract", "合同", "pp_contract", "", "snapshot")) is False, \
        "克隆词有了但没点名目标"
    assert webui._clone_intent("生成一个采购申请", target) is False

    # ---- ② 快照字段 → build_bo spec：全要素复制
    snap = projlink.Snapshot(
        source="package_upload", generated_at="2026-11-26T16:00:00+08:00",
        project={"code": "projchange", "name": "项目变更", "tbl": "pp_projchange"},
        fields=[
            projlink.ProjectField(
                code="name", name="变更名称", type_new="varchar", length=200, sn=1),
            projlink.ProjectField(
                code="changeType", name="变更类型", type_new="dic", sn=2,
                choices=["范围变更", "进度变更", "成本变更"]),
            projlink.ProjectField(
                code="status", name="状态", type_new="dic", sn=3,
                dict=projlink.DictMeta(ref="change_status", name="变更状态",
                                       stru_type="1", scope="GLOBAL",
                                       items=(projlink.DictItem(key="open", name="待处理", sn=1),
                                              projlink.DictItem(key="done", name="已完成", sn=2)))),
            projlink.ProjectField(
                code="level", name="影响级别", type_new="dic", sn=4,
                choices=["高", "中", "低"], cascade_parent="changeType",
                choice_groups={"范围变更": ["高", "中"], "进度变更": ["低"]}),
            projlink.ProjectField(
                code="projectId", name="项目选择", type_new="object",
                object_type="project", sn=5),
            projlink.ProjectField(
                code="items", name="变更明细", type_new="table", sn=6,
                table_columns=[{"name": "内容", "type": "varchar", "precision": 0,
                                "widthPercent": 60, "required": True, "values": []}]),
        ])
    spec = webui._clone_fields_spec(snap)
    by_code = {f["code"]: f for f in spec}
    assert by_code["name"]["type"] == "varchar" and by_code["name"]["length"] == 200
    assert by_code["changeType"]["values"] == ["范围变更", "进度变更", "成本变更"]
    # dic 不带 objectType（旧字典 key 对新对象是失效引用，新 key 由编码重新生成）
    assert "objectType" not in by_code["changeType"], by_code["changeType"]
    # 全局字典字段的候选值从 dict.items 取显示名
    assert by_code["status"]["values"] == ["待处理", "已完成"]
    # 级联与分组
    assert by_code["level"]["cascadeParent"] == "changeType"
    assert by_code["level"]["choiceGroups"] == {"范围变更": ["高", "中"], "进度变更": ["低"]}
    # object 保留指向；子表列原样
    assert by_code["projectId"]["objectType"] == "project"
    assert by_code["items"]["columns"][0]["name"] == "内容"

    # ---- ③ 克隆结果：AI 起名 mock 成功路径 / 失败回退
    from pp6kit.nlq import llm as _llm
    old_chat = _llm.chat_any
    _llm.chat_any = lambda *a, **k: (
        _json.dumps({"name": "项目变更二期", "code": "projchange2"},
                    ensure_ascii=False), {"model": "fake"})
    try:
        res = webui._clone_bo_result(snap, target, "生成与项目变更完全一样的字段", {})
        bo = res["bo"]
        assert bo.code == "projchange2" and bo.name == "项目变更二期", res
        # 字段数与快照一致（系统字段除外——那部分 build_bo 自动补）
        codes = [f.code for f in bo.fields]
        for need in ("name", "changeType", "status", "level", "projectId", "items"):
            assert need in codes, codes
        level = bo.field_by_code("level")
        assert level.choices == ["高", "中", "低"], level.choices
        assert level.cascade_parent == "changeType", level.cascade_parent
        assert level.choice_groups == {"范围变更": ["高", "中"], "进度变更": ["低"]}
        assert "已照「项目变更」克隆" in res["notices"][0], res["notices"]
    finally:
        _llm.chat_any = old_chat

    # 起名失败 → 回退「原名副本」
    def _no_key(*a, **k):
        raise RuntimeError("no key")
    _llm.chat_any = _no_key
    try:
        res = webui._clone_bo_result(snap, target, "照项目变更复制一份", {})
        assert res["bo"].code == "projchange_copy", res["bo"].code
        assert res["bo"].name == "项目变更副本", res["bo"].name
        assert any("没起成新名字" in n for n in res["notices"]), res["notices"]
    finally:
        _llm.chat_any = old_chat

    # ---- ④ 旧快照（没存候选值）给出重新上传提示
    old_snap = projlink.Snapshot(
        source="package_upload", project={"code": "projchange", "name": "项目变更"},
        fields=[projlink.ProjectField(code="changeType", name="变更类型",
                                      type_new="dic", sn=1)])
    hint = webui._clone_missing_values_hint(old_snap)
    assert "重新上传" in hint, hint

    # ---- ⑤ 生成链路分流：带头 + 点名 + 克隆词 → 走克隆而不是从零设计
    class _FakeDecisions(dict):
        pass
    decisions = {"version": 1, "objects": {}, "snapshot": "0123456789abcdef0123"}
    old_ready = webui.objects._db_ready
    webui.objects._db_ready = lambda: None
    old_load = projlink.load_snapshot
    old_load_file = projlink.load_snapshot_file
    projlink.load_snapshot = lambda token=None, target=None, **kw: snap
    # resolve_target 也会按 token 读快照文件（_ai_link_target 阶段），一并 mock
    projlink.load_snapshot_file = lambda token=None, force=False, **kw: snap
    from pp6kit.nlq import llm as _llm2
    _llm2.chat_any = lambda *a, **k: (
        _json.dumps({"name": "项目变更二期", "code": "projchange2"},
                    ensure_ascii=False), {"model": "fake"})
    try:
        header = _json.dumps(decisions, ensure_ascii=False)
        out = webui.ai_generate_bo(
            {"description": "生成与项目变更完全一样的字段 所有字段都要"},
            header)
        codes = [f["code"] for f in out["bo"]["fields"]]
        assert "changeType" in codes and "level" in codes, codes
        assert any("已照「项目变更」克隆" in n for n in out["notices"]), out["notices"]
    finally:
        webui.objects._db_ready = old_ready
        projlink.load_snapshot = old_load
        projlink.load_snapshot_file = old_load_file
        _llm2.chat_any = old_chat
