"""关联对象 → 值来源 / 联动 / 过滤（对齐线上导出包 config-sync-20261119184322）。

这一组用例钉的是「一个 object 字段到了包里长什么样」：

    新建表单   linkdata + 值来源 + link_config/link_linkage/link_condition
    详情表单   selectObject（原本字段类型，不带值来源）
    值来源     type=valueSource、unique=id_、resultColumns 必须含主键、
               过滤条件的 rule_id 与表单 link_condition 逐字一致

形状全部来自 docs/参考-关联对象值来源-20261119.md。改这里之前先看那份文档：
少写一个键的表现不是报错，而是导入后「下拉能点、但筛选不生效 / 联动不带值」，
线上排查成本极高。
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import zipfile

from pp6kit import projlink
from pp6kit import spec as S
from pp6kit import webui
from tests._harness import case, field, pick, skip, texts, xlsx


def build(sheets: dict) -> bytes:
    return webui.build_zip(xlsx(sheets))[0]


def _form_xml(data: bytes, key: str) -> str:
    """按 key 取表单 XML（pick 只给第一份，一套包里有三张表单）。"""
    for name, text in texts(data).items():
        if name.startswith("form/") and f'key="{key}"' in text:
            return text
    raise AssertionError(f"包里没有 key={key} 的表单：{sorted(texts(data))}")


def _ds_xml(data: bytes, key: str) -> str:
    for name, text in texts(data).items():
        if name.startswith("data_template/") and f'key="{key}"' in text:
            return text
    raise AssertionError(f"包里没有 key={key} 的数据模板：{sorted(texts(data))}")


def _field_options(xml: str, name: str) -> dict:
    for frag in re.findall(r"<formField\b[^>]*>", xml):
        if f'name="{name}"' not in frag:
            continue
        m = re.search(r'fieldOptions="([^"]*)"', frag)
        assert m, f"控件 {name} 没有 fieldOptions"
        return json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))
    raise AssertionError(f"表单里没有控件 {name}")


def _json_attr(xml: str, attr: str):
    m = re.search(attr + r'="([^"]*)"', xml)
    if not m:
        return None
    return json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))


def link_sample(result_fields: str = "", filter_field: str = "",
                linkage: str = "") -> bytes:
    """一份「关联项目 + 项目类型 + 专业」的最小包。

    目标对象 project 放在本包里（离线也能拿到完整字段，不依赖库/快照），
    这样断言的是「生成器怎么把字段变成控件」，与数据源可用性无关。

    ⚠ `4_表单控件` 里**必须把要联动的控件写出来**：``links._linkage`` 会把
    联动映射收敛到「本表单真实存在的控件」上（没有对应控件的列只回显，
    field 为空串）。表单里只放一个关联控件时，专业那列的联动理应被抹掉
    ——那是 LNK26 专门验的边界，别在这里复现。
    """
    ds = {"数据集key": "project_value", "数据集名称": "项目值来源",
          "主表名": "project", "类型": "valueSource"}
    if result_fields:
        ds["返回字段"] = result_fields
    if filter_field:
        ds["过滤字段"] = filter_field
    ff = {"表单key": "create_demo_link", "字段编码": "suoShuXiangMu",
          "显示标签": "所属项目", "控件类型": "linkdata"}
    if linkage:
        ff["联动字段"] = linkage
    # 表单上确实存在的两个业务控件（专业 / 项目类型）：联动与过滤都指向它们
    others = [
        {"表单key": "create_demo_link", "字段编码": "zhuanYe",
         "显示标签": "专业", "控件类型": "text"},
        {"表单key": "create_demo_link", "字段编码": "xiangMuLeiXing",
         "显示标签": "项目类型", "控件类型": "select"},
    ]
    return build({
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
            field("classification", "名称", "字符串"),
            field("demo_link", "所属项目", "对象", **{"关联对象/字典": "project"}),
            field("demo_link", "项目类型", "对象", **{"关联对象/字典": "classification"}),
            field("demo_link", "专业", "字符串"),
        ],
        S.S_FORM: [
            {"表单key": "create_demo_link", "表单名称": "新建关联项目测试",
             "对象编码": "demo_link", "表单类型": "标准全套", "布局": "一行两列"},
        ],
        S.S_FORMFIELD: [ff] + others,
        S.S_DATASET: [ds],
    })


@case("LNK01", "links", "新建表单的关联对象出成 linkdata，值来源挂在 linkdata 键上",
      "P0", ["样本 config-sync-20261119184322", "linkdata"])
def lnk01():
    data = link_sample()
    xml = _form_xml(data, "create_demo_link")
    opts = _field_options(xml, "suoShuXiangMu")
    assert opts.get("linkdata") == "project_value", opts
    # 值来源不是对象编码，也不是数据集名——写错等于空下拉
    assert opts.get("linkdata") != "project", opts


@case("LNK02", "links", "新建表单 linkdata 带齐 link_config / 联动 / 过滤 / 关系键",
      "P0", ["样本逐键"])
def lnk02():
    xml = _form_xml(link_sample(), "create_demo_link")
    opts = _field_options(xml, "suoShuXiangMu")
    cfg = opts["link_config"]
    assert cfg["id"] == "id_" and cfg["text"] == "name_", cfg
    assert cfg["structure"] == "list", cfg
    assert opts["init_linkage"] is True
    assert opts["relation"] == "one2many"
    assert opts["is_fk"] is False and opts["is_pk"] is False
    assert opts["multiple"] == "N"
    assert opts["notInputCondition"] == "conditionIsNone"
    assert opts["dbFieldType"] == "mediumContent"
    assert opts["placeholder"] == "请选择", opts.get("placeholder")
    assert isinstance(opts.get("link_linkage"), list)
    assert isinstance(opts.get("link_condition"), list)


@case("LNK03", "links", "详情表单的关联对象用原本字段类型 selectObject（不带值来源）",
      "P0", ["详情样本 project_summary"])
def lnk03():
    xml = _form_xml(link_sample(), "detail_demo_link")
    opts = _field_options(xml, "suoShuXiangMu")
    assert opts.get("boCode") == "project", opts
    assert opts.get("store") == "id", opts
    assert "value_source" not in opts and "datasource" not in opts, opts
    assert opts.get("linkdata") is None, opts


@case("LNK04", "links", "值来源 unique/uniqueField 默认主键，resultColumns 必含主键",
      "P0", ["用户硬要求：主键必返回"])
def lnk04():
    xml = _ds_xml(link_sample(result_fields="name"), "project_value")
    assert 'type="valueSource"' in xml, xml[:400]
    assert 'unique="id_"' in xml and 'uniqueField="id_"' in xml, xml[:600]
    cols = _json_attr(xml, "resultColumns")
    assert cols, xml[:600]
    names = [c["name"] for c in cols]
    assert "id_" in names, names
    # 名称列也强制带：控件上显示的就是它
    assert "name_" in names, names


@case("LNK05", "links", "过滤条件：rule_id 在值来源与表单两侧一致，列用目标物理列名",
      "P0", ["rule_id 分叉 = 筛选静默失效"])
def lnk05():
    data = link_sample(result_fields="id,name,zhuanYe",
                       filter_field="xiangMuLeiXing")
    ds = _ds_xml(data, "project_value")
    conds = _json_attr(ds, "filterConditions")
    assert conds, "值来源没有 filterConditions"
    rule = conds[0]["filter"]["rules"][0]
    assert rule["field"] == "xiang_mu_lei_xing_", rule
    assert rule["source"] == "dynamic" and rule["field_source"] == "formField", rule
    assert rule["value"] == "text", rule

    form = _form_xml(data, "create_demo_link")
    opts = _field_options(form, "suoShuXiangMu")
    cond = opts["link_condition"][0]
    assert cond["rule_id"] == rule["rule_id"], (cond, rule)
    assert cond["fieldName"] == f'{rule["rule_id"]}_1', cond
    # value 绑的是**本表单控件编码**，不是字段编码以外的别的东西
    assert cond["value"] == "xiangMuLeiXing", cond


@case("LNK06", "links", "联动数据：返回列自动带到本对象同名字段（专业 → zhuanYe）",
      "P0", ["样本 zhuan_ye_ → zhuanYe"])
def lnk06():
    data = link_sample(result_fields="id,name,createTime,zhuanYe")
    form = _form_xml(data, "create_demo_link")
    opts = _field_options(form, "suoShuXiangMu")
    by_name = {it["name"]: it["field"] for it in opts["link_linkage"]}
    assert by_name.get("zhuan_ye_") == "zhuanYe", by_name
    # 系统列只回显，不许映射到 id/name 这种隐藏控件
    assert by_name.get("id_") == "", by_name
    assert by_name.get("name_") == "", by_name


@case("LNK07", "links", "Excel 显式「联动字段」可覆盖自动映射，空值=只回显",
      "P1", ["6_数据集 / 4_表单控件"])
def lnk07():
    data = link_sample(result_fields="id,name,zhuanYe",
                       linkage="zhuan_ye_=xiangMuLeiXing,name_=")
    opts = _field_options(_form_xml(data, "create_demo_link"), "suoShuXiangMu")
    by_name = {it["name"]: it["field"] for it in opts["link_linkage"]}
    assert by_name.get("zhuan_ye_") == "xiangMuLeiXing", by_name
    assert by_name.get("name_") == "", by_name


@case("LNK08", "links", "值来源与表单用的是同一份 link_plan（rule_id 稳定、可重复出包）",
      "P1", ["同一份 Excel 两次出包"])
def lnk08():
    def rules(data: bytes):
        ds = _json_attr(_ds_xml(data, "project_value"), "filterConditions")
        form = _field_options(_form_xml(data, "create_demo_link"), "suoShuXiangMu")
        return ds[0]["filter"]["rules"][0]["rule_id"], form["link_condition"][0]["rule_id"]

    a = rules(link_sample(filter_field="xiangMuLeiXing"))
    b = rules(link_sample(filter_field="xiangMuLeiXing"))
    assert a[0] == a[1], a          # 同一次出包内两侧一致
    assert a == b, (a, b)           # 反复出包不变（不能是雪花 id）


@case("LNK09", "links", "关联对象建了值来源，同一对象的下拉共用一份（不重复建）",
      "P1", ["blueprint._ensure_value_sources"])
def lnk09():
    data = link_sample()
    keys = [name for name in texts(data) if name.startswith("data_template/")]
    # 一份 project_value + 系统已有的 classification 引用不建新条目
    body = "".join(texts(data)[n] for n in keys)
    assert body.count('key="project_value"') >= 1
    assert 'key="project_value_2"' not in body, keys


# ---------------------------------------------------------------- LNK10–LNK13
# 「关联其他业务对象」（用户在 app 上把关联对象选成合同 hetong 之类）：
# 目标既不在本包里、离线时也没有字段快照。这一组钉的是「目标查不到明细时，
# 包仍然要自洽」——
#   · 新建表单的 linkdata 必须指向**包内**一份 type=valueSource 的值来源，
#     不能指向系统清单里的同名 key（清单只登记了 key，看不出类型，可能只是
#     该对象的列表模板，指过去导入后就是一个打不开的下拉）；
#   · 详情表单仍是原本字段类型 selectObject，不带 value_source；
#   · 关联项目这条老路径同理（新建 linkdata、详情 selectObject）。


def other_link_sheets(with_type_field: bool = False) -> dict:
    """「付款申请」要不要带一个「项目类型」关联字段（用来考值来源的过滤条件）。"""
    fields = [field("fuKuan", "申请单号", "字符串"),
              field("fuKuan", "金额", "数字")]
    if with_type_field:
        fields.append(field("fuKuan", "项目类型", "对象",
                            **{"关联对象/字典": "classification"}))
    return {
        S.S_BO: [{"对象名称": "付款申请", "对象编码": "fuKuan",
                  "物理表名": "pp_fu_kuan", "对象类型": "object",
                  "流程驱动": "否", "关联项目": "是"}],
        S.S_FIELD: fields,
        S.S_FORM: [{"表单key": "create_fuKuan", "表单名称": "新建付款申请",
                    "对象编码": "fuKuan", "表单类型": "标准全套",
                    "布局": "一行两列"}],
    }


def _build_with_target(sheets: dict, target) -> bytes:
    """按指定链接目标出包（直接设 ``project.link_target``）。

    为什么不走 webui.build_zip 的 X-Project-Link 头：那头会把「合同」按系统
    对象清单解析成 contract（清单里有中文名→编码的对应），考不到「目标既不在
    本包、也不在清单」这一档。这里直接把目标对象塞进 Project，和 GUI 里
    「关联其他业务对象」真正走的也是同一条 ``project.link_target``。
    """
    import tempfile
    from pathlib import Path

    from pp6kit import builder, webui
    from pp6kit.reader import read_workbook

    with tempfile.TemporaryDirectory() as td:
        xl = Path(td) / "in.xlsx"
        xl.write_bytes(xlsx(sheets))
        proj, errors, _stats = read_workbook(xl)
        assert not errors, errors
        proj.link_target = target
        res = builder.build(proj, webui._snippets(), Path(td) / "out")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for a in res.artifacts:
            if a.xml is not None:
                z.writestr(a.entry, a.xml)
    return buf.getvalue()


def _hetong_target():
    return projlink.target_for("hetong", "合同", "pp_hetong")


@case("LNK10", "links", "目标对象不在本包/清单时（离线兜底）新建表单仍是 linkdata，值来源随包走",
      "P0", ["关联其他业务对象", "offline_target", "包内必须有 type=valueSource"])
def lnk10():
    data = _build_with_target(other_link_sheets(), _hetong_target())
    opts = _field_options(_form_xml(data, "create_fuKuan"), "hetongId")
    assert opts.get("linkdata") == "contract_value", opts
    assert (opts.get("link_config") or {}).get("id") == "id_", opts.get("link_config")
    assert isinstance(opts.get("link_linkage"), list) and opts["link_linkage"], opts

    # 值来源必须在**包里**：目标可能是系统清单里没有的对象（hetong），
    # 指向一个包外 key 等于导入后下拉是空的
    ds = _ds_xml(data, "contract_value")
    assert 'type="valueSource"' in ds, ds[:400]
    assert 'unique="id_"' in ds and 'uniqueField="id_"' in ds, ds[:600]
    cols = [c["name"] for c in (_json_attr(ds, "resultColumns") or [])]
    assert "id_" in cols, cols          # 用户硬要求：返回结果必含主键


@case("LNK11", "links", "同场景详情表单是 selectObject（boCode 正确、不带值来源）",
      "P0", ["详情样本 project_summary", "详情不该挂值来源"])
def lnk11():
    data = _build_with_target(other_link_sheets(), _hetong_target())
    opts = _field_options(_form_xml(data, "detail_fuKuan"), "hetongId")
    assert opts.get("boCode") == "hetong", opts
    assert opts.get("store") == "id", opts
    assert "value_source" not in opts and "datasource" not in opts, opts
    assert opts.get("linkdata") is None, opts


@case("LNK12", "links", "关联项目老路径：新建表单是 linkdata、详情表单是 selectObject",
      "P0", ["target=project", "两种表单两种控件"])
def lnk12():
    data = _build_with_target(other_link_sheets(), projlink.project_target())
    opts = _field_options(_form_xml(data, "create_fuKuan"), "projectId")
    key = str(opts.get("linkdata") or "")
    # 值来源必须是**本包自带**的 dataTemplate（<obj>_value 形状），不能是系统
    # 清单里同名的 dataSet key（project）——那是数据集，不是值来源
    assert key.startswith("project_value"), opts
    _ds_xml(data, key)                       # 包里必须有同名 data_template
    assert (opts.get("link_config") or {}).get("structure") == "list", opts

    det = _field_options(_form_xml(data, "detail_fuKuan"), "projectId")
    assert det.get("boCode") == "project", det
    assert det.get("store") == "id", det
    assert "value_source" not in det and det.get("linkdata") is None, det


@case("LNK13", "links", "关联其他业务对象 + 显式过滤字段：rule_id 两侧一致、过滤列用目标物理列名",
      "P1", ["auto_filter", "link_condition 非空", "显式配置优先"])
def lnk13():
    """目标对象有字段快照（用户传过它的 zip）时，过滤条件要按真实列名出。

    hetong 的「项目类型」列在目标里叫 lei_xing_，本对象同名关联字段是
    xiangMuLeiXing —— 两侧用同一个 rule_id 才会真正生效。

    ⚠ 2026-11-20 口径变更：链接字段（XX选择）的值来源**默认不过滤**（用户在 app
    面板里勾了才加），所以这里必须显式写一行 6_数据集「过滤字段」。
    以前这条用例靠 auto_filter 自动推导，那套推导现在是普通关联字段的默认行为，
    链接字段上已经关掉（见 LNK20–LNK24）。
    """
    from pp6kit import links as _links

    ftarget = _links.LinkTarget(
        code="hetong", label="合同", table="pp_hetong", source="snapshot",
        fields=[
            _links.LinkField(code="id", label="主键", column="id_", is_system=True),
            _links.LinkField(code="name", label="合同名称", column="name_",
                             is_system=True),
            _links.LinkField(code="leiXing", label="项目类型", column="lei_xing_",
                             type_new="object", object_type="classification"),
        ])
    old = _links._target_from_snapshot
    _links._target_from_snapshot = lambda code: ftarget if code == "hetong" else old(code)
    try:
        sheets = other_link_sheets(with_type_field=True)
        sheets[S.S_DATASET] = [{
            "数据集key": "contract_value", "数据集名称": "合同值来源",
            "主表名": "hetong", "类型": "valueSource",
            "过滤字段": "xiangMuLeiXing"}]
        data = _build_with_target(sheets, _hetong_target())
    finally:
        _links._target_from_snapshot = old

    ds = _ds_xml(data, "contract_value")
    conds = _json_attr(ds, "filterConditions")
    assert conds, ds[:600]
    rule = conds[0]["filter"]["rules"][0]
    assert rule["field"] == "lei_xing_", rule

    opts = _field_options(_form_xml(data, "create_fuKuan"), "hetongId")
    assert opts.get("linkdata") == "contract_value", opts
    cond = (opts.get("link_condition") or [None])[0]
    assert cond, opts
    assert cond["rule_id"] == rule["rule_id"], (cond, rule)
    assert cond["value"] == "xiangMuLeiXing", cond
    # 返回列也跟着快照走（主键 + 名称必在），不是只有离线那两列
    cols = [c["name"] for c in (_json_attr(ds, "resultColumns") or [])]
    assert "id_" in cols and "name_" in cols, cols


@case("LNK14", "links", "目标对象有字段快照时，同名返回列自动联动到本表单控件",
      "P1", ["links.with_linked_columns", "样本 zhuan_ye_ → zhuanYe"])
def lnk14():
    """``link_linkage`` 只能引用值来源**返回**的列。

    用户没在 6_数据集 里勾返回列时，返回列只有主键 + 名称，于是「合同金额
    ``he_tong_jin_e_`` → 本表单 ``heTongJinE``」这类自动联动永远配不出来。
    所以生成时按「目标列名 == 本对象某字段的物理列名」把该列补进返回列
    （表单侧 link_linkage 与值来源侧 resultColumns 用同一份，缺一边就是
    「选了值带不出来」）。
    """
    from pp6kit import links as _links

    sheets = other_link_sheets()
    sheets[S.S_FIELD] += [field("fuKuan", "合同金额", "数字"),
                          field("fuKuan", "专业", "字符串", 长度=200)]
    ftarget = _links.LinkTarget(
        code="hetong", label="合同", table="pp_hetong", source="snapshot",
        fields=[
            _links.LinkField(code="id", label="主键", column="id_"),
            _links.LinkField(code="name", label="合同名称", column="name_"),
            _links.LinkField(code="heTongJinE", label="合同金额", column="he_tong_jin_e_",
                             type_new="number", datatype="decimal"),
            _links.LinkField(code="zhuanYe", label="专业", column="zhuan_ye_"),
        ])
    # 目标库元数据优先于快照（三级来源的第 2 级）：这里打掉它，模拟「没连库、
    # 只有用户上传的那份快照」，否则断言会跟着本机库里的合同字段跑。
    old_snap, old_sys = _links._target_from_snapshot, _links._target_from_system
    _links._target_from_snapshot = lambda code: ftarget if code == "hetong" else old_snap(code)
    _links._target_from_system = lambda code: None
    try:
        data = _build_with_target(sheets, _hetong_target())
    finally:
        _links._target_from_snapshot, _links._target_from_system = old_snap, old_sys

    opts = _field_options(_form_xml(data, "create_fuKuan"), "hetongId")
    by_name = {it["name"]: it["field"] for it in opts["link_linkage"]}
    assert by_name.get("he_tong_jin_e_") == "heTongJinE", by_name
    assert by_name.get("zhuan_ye_") == "zhuanYe", by_name
    assert by_name.get("id_") == "" and by_name.get("name_") == "", by_name

    # 值来源必须真的返回这两列，否则联动指向一条取不到的列
    cols = [c["name"] for c in (_json_attr(_ds_xml(data, "contract_value"),
                                          "resultColumns") or [])]
    assert "he_tong_jin_e_" in cols and "zhuan_ye_" in cols, cols
    assert "id_" in cols, cols


@case("LNK15", "links", "过滤字段编码被对齐改名后过滤条件仍然生效（不静默丢条件）",
      "P0", ["对齐改名 xiangMuLeiXing → classificationId", "links.filter_of rebound"])
def lnk15():
    """``6_数据集`` 里写的「过滤字段」是**出包当时**的字段编码。

    关联项目的字段对齐会把本对象的「项目类型」改成目标对象的字段编码
    （xiangMuLeiXing → classificationId）。按编码判等就永远不等 → 整条过滤条件
    被丢掉，用户拿到一份「不过滤」的值来源（下拉全量，界面看着都对，而且只留
    一条 notice）。这里钉住：编码失效时按同名推导补回，两侧 rule_id 仍一致。
    """
    sheets = other_link_sheets(with_type_field=True)
    sheets[S.S_DATASET] = [{
        "数据集key": "project_value", "数据集名称": "值来源_过滤项目类型",
        "主表名": "project", "类型": "valueSource",
        "返回字段": "id,name,zhuanYe", "过滤字段": "xiangMuLeiXing"}]

    # 目标项目对象的字段池（对齐改名 + 值来源列都从这里来）。写成快照而不是靠
    # 本机数据库：rename 的判定必须可重复，否则换一台机器结论就变了。
    snap = projlink.Snapshot(
        source="target_db", generated_at="2026-11-19T09:00:00+08:00",
        project={"code": "project", "name": "项目", "tbl": "pp_project"},
        fields=[
            projlink.ProjectField(
                code="name", name="项目名称", type_new="varchar", datatype="varchar",
                field_name="name_", length=200, is_null=True, is_index=False, sn=1,
                options={}),
            projlink.ProjectField(
                code="classificationId", name="项目类型", type_new="object",
                datatype="varchar", field_name="classification_id_", length=64,
                is_null=True, is_index=False, sn=12, options={},
                object_type="classification"),
            projlink.ProjectField(
                code="zhuanYe", name="专业", type_new="varchar", datatype="varchar",
                field_name="zhuan_ye_", length=200, is_null=True, is_index=False, sn=13,
                options={}),
        ],
        system_codes=set(projlink.SYSTEM_CODES), warnings=[], declared_total=3)

    old = projlink.load_snapshot
    projlink.load_snapshot = lambda *a, **k: snap
    try:
        data = build({**sheets})        # 走 webui.build_zip：改名在 apply 里
    finally:
        projlink.load_snapshot = old

    # ① 本对象的「项目类型」确实被改名了（否则这条用例考不到边界）
    bo_xml = "".join(t for n, t in texts(data).items() if n.startswith("bo/"))
    assert 'code="classificationId"' in bo_xml, bo_xml[:600]
    assert 'code="xiangMuLeiXing"' not in bo_xml

    # ② 表单侧 link_condition 有 1 条，绑的是改名**后**的控件编码
    opts = _field_options(_form_xml(data, "create_fuKuan"), "projectId")
    conds = opts.get("link_condition") or []
    assert len(conds) == 1, opts
    assert conds[0]["value"] == "classificationId", conds
    assert conds[0]["fieldName"] == f'{conds[0]["rule_id"]}_1', conds

    # ③ 值来源侧 filterConditions 用目标物理列名，rule_id 与表单一致
    rules = _json_attr(_ds_xml(data, "project_value"), "filterConditions")
    assert rules, "值来源没有 filterConditions（过滤条件被静默丢掉了）"
    rule = rules[0]["filter"]["rules"][0]
    assert rule["field"] == "classification_id_", rule
    assert rule["rule_id"] == conds[0]["rule_id"], (rule, conds[0])

    # ④ 自动纠正必须写进 notice：用户对着 Excel 里那个旧编码也能看懂发生了什么
    note = zipfile.ZipFile(io.BytesIO(data)).comment.decode("utf-8", "replace")
    assert "xiangMuLeiXing 已不存在（可能被对齐改名），已按「项目类型」绑定" in note, note[-800:]


@case("LNK16", "links", "值来源面板真渲染：主键置灰必选、勾列才有联动下拉、取消勾选联动同步清掉",
      "P1", ["node 探针真跑 renderAiVsPop", "需求 4/5 的界面侧"])
def lnk16():
    """面板是纯前端字符串渲染，grep 只能证明「字符串在文件里」。

    这里用 node + 最小 DOM stub 把 ``pp6kit/static/app.js`` 放进 vm 真跑一遍
    ``renderAiVsPop()``，钉住四件用户直接感知的事：
      · 主键那一行的勾选框是 ``disabled + checked``（面板据此置灰必选）；
      · **默认态（用户没动过面板）把所有业务字段显示成勾上**——与后端
        「返回全部业务字段 + 主键、不加过滤」的口径一致，否则用户以为只返回
        两列、改一个勾就真的只剩两列（预览≠产物）；
      · 只有勾了返回列，才出现「联动」下拉，且能选到本表单的对应控件；
      · 取消勾选某列后，``linkage`` 里对该列的映射必须同步删掉——否则导入后
        联动指向一条值来源不返回的列，表现为「选了值带不出来」。
    没装 node 就 skip（不装依赖、不联网）。
    """
    import json as _json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        skip("没装 node：值来源面板探针跳过（LNK01–LNK15 的产物断言仍然生效）")
    app = Path(__file__).resolve().parents[1] / "pp6kit" / "static" / "app.js"
    probe = Path(__file__).resolve().parent / "fe_ai_vs_probe.js"
    assert probe.is_file(), f"缺少前端探针 {probe}"
    proc = subprocess.run([node, str(probe), str(app)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"node 探针失败：\n{proc.stderr[-1200:]}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert lines, "node 探针没有输出"
    out = _json.loads(lines[-1])

    assert out["D4_pk_disabled"] is True, out
    assert out["D4_pk_checked"] is True, out
    assert out["D4_pk_locked"] is True, out
    assert out["D4_no_link_select_before"] is True, out
    assert out["D4_link_select_after"] is True, out
    assert out["D4_link_can_pick_zhuanYe"] is True, out
    assert out["D4_filter_has_xiangMuLeiXing"] is True, out
    assert out["D4_has_change_handler"] is True, out
    assert out["D4_linkage_after_uncheck"] == "[]", out
    assert _json.loads(out["D4_resultFields_after_uncheck"]) == ["id", "name"], out
    assert _json.loads(out["D4_resultFields_after_check"]) == ["id", "name", "zhuanYe"], out

    # ⑦ 过滤规则（2026-11-20 起面板是「规则行列表」，不再有过滤字段单选下拉）：
    # 选值来源=固定值 → 选目标列 → 选运算符 → 填值，四步都要落到规则对象上，
    # 后端 generation 读的就是这一份。老字段 filterField 不再被面板改写。
    rule = _json.loads(out["D4_rule"])
    assert rule["field"] == "zhuan_ye_", rule
    assert rule["source"] == "fixed" and rule["fieldSource"] == "formField", rule
    assert rule["operator"] == "not_equal", rule
    assert rule["value"] == "Y", rule
    assert rule["control"] == "", rule
    assert _json.loads(out["D4_filterField"]) == "", out
    # 删掉唯一一条 = 明确「不过滤」（filter_rules=[]），不能退回同名推导
    assert _json.loads(out["D4_rules_after_delete"]) == [], out
    assert out["D4_logic_locked"] == "AND", out

    # 默认态（用户没动过面板）必须与后端口径一致：**全部业务字段都显示勾上**。
    # 只勾主键+名称的话，用户会以为「只返回 2 列」，随手改一个勾就真的只剩
    # 他勾的那几列（后端按显式配置生成）——预览≠产物，最难解释的那种问题。
    assert out["D4d_default_pk_checked"] is True, out
    assert out["D4d_default_name_checked"] is True, out
    assert out["D4d_default_biz_checked"] is True, out
    assert out["D4d_default_note"] is True, out            # 面板要写明「默认全部业务字段」
    assert out["D4d_default_summary"] == "值来源（默认：全部字段）", out
    assert _json.loads(out["D4d_default_resultFields"]) == [], out
    # 系统列：渲染出来（用户看得见它不返回），但不勾、也不进「默认全选」种子
    assert out["D4d_system_rendered"] is True, out
    assert out["D4d_system_unchecked"] is True, out
    assert out["D4d_process_unchecked"] is True, out
    # 第一次取消一列前要先「铺」默认全选，否则 resultFields 会只剩他点的那些
    assert _json.loads(out["D4d_seed_then_uncheck"]) == ["id", "name"], out
    assert out["D4d_custom_after_click"] is True, out

    # 加一条过滤规则也是动过面板：同样要先铺默认全选（系统列不进种子）。
    # 不铺的话 custom=true + resultFields=[] → 面板显示「一列没勾」，而产物
    # 仍是全部业务字段（预览≠产物）；用户接着勾一列，产物就只剩那一列。
    assert out["D4f_rule_added"] is True, out
    assert _json.loads(out["D4f_filter_resultFields"]) == ["id", "name", "zhuanYe"], out
    assert out["D4f_filter_summary"] == "返回 3 列 · 过滤 1 条", out
    assert out["D4f_filter_custom"] is True, out
    assert _json.loads(out["D4f_filter_filterField"]) == "", out
    # 值来源=表单控件时，值下拉选的「本表单字段」写成规则的 control（后端据此
    # 写 link_condition.value）；目标列可以留空，由后端按显示名推导。
    got_rule = _json.loads(out["D4f_filter_rule"])
    assert got_rule["source"] == "dynamic" and got_rule["fieldSource"] == "formField", got_rule
    assert got_rule["control"] == "xiangMuLeiXing", got_rule


# ---------------------------------------------------------------- LNK17–LNK19
# 编辑态里 linkdata / selectObject 的**分界是「有没有值来源」**，不是「是不是
# 编辑态」。样本 form/002-glxmd.xml：suoShuXiangMu=linkdata（配了值来源+联动+过滤）、
# xiangMuLeiXing=selectObject（普通关联字段，没有随包值来源）。
# 没有值来源还写 linkdata 的话，linkdata 指向的是一个包里根本不存在的 key
# （系统清单里那个同名 key 只是 dataSet 的 key），下拉点开是空的。


def _only_system_dataset_key(code: str):
    """把系统清单压成「只有一个 dataSet key」并返回还原函数。

    为什么要压：`_ensure_value_sources` 对「目标对象在系统清单里、且清单里有
    同名 key」的字段会走复用（不建随包值来源）——这正是「项目类型」落到
    selectObject 的场景。不压的话，结论会跟着本机 `~/.pp6kit/templates.json`
    跑（有就复用、没有就新建一份随包值来源 → 又变回 linkdata），用例就不可重复。
    """
    from pp6kit import objects as _objects

    old = _objects.extra

    def fake(force: bool = False) -> dict:
        data = dict(old(force))
        data["datasets"] = [code]
        return data

    _objects.extra = fake
    return lambda: setattr(_objects, "extra", old)


@case("LNK17", "links", "编辑态按「有没有值来源」分流：关联XX=linkdata，普通关联字段=selectObject",
      "P0", ["样本 suoShuXiangMu=linkdata / xiangMuLeiXing=selectObject"])
def lnk17():
    restore = _only_system_dataset_key("classification")
    try:
        data = build(other_link_sheets(with_type_field=True))
    finally:
        restore()

    create = _form_xml(data, "create_fuKuan")
    # ① 「所属项目」建了随包值来源 → linkdata
    proj = _field_options(create, "projectId")
    assert str(proj.get("linkdata") or "").startswith("project_value"), proj
    _ds_xml(data, proj["linkdata"])

    # ② 「项目类型」只有系统 dataSet 的 key（包里没有同名 dataTemplate）→
    #    出成系统原本的对象选择器，而不是指向空 key 的 linkdata
    cls = _field_options(create, "classificationId")
    assert cls.get("boCode") == "classification", cls
    assert cls.get("store") == "id", cls
    assert cls.get("linkdata") is None, cls
    assert "value_source" not in cls and "datasource" not in cls, cls

    # ③ 详情/条件专用两张都是 selectObject
    for key in ("detail_fuKuan", "fetch_basic_fuKuan_information"):
        det = _field_options(_form_xml(data, key), "classificationId")
        assert det.get("boCode") == "classification" and det.get("store") == "id", det
        assert det.get("linkdata") is None, det

    # ④ 降级要说明原因：用户填了值来源就能拿回 linkdata。
    # 提示里的表名 2026-11-19 起改成「6.1_值来源 或 6_数据集」：值来源单独成表了，
    # 老工作簿写在 6_数据集 里的行仍然认，所以两个都提。
    note = zipfile.ZipFile(io.BytesIO(data)).comment.decode("utf-8", "replace")
    assert "控件「项目类型」改用对象选择器（该字段没有值来源；" \
           "需要联动/过滤请在 6.1_值来源 或 6_数据集 里建值来源）" in note, note[-800:]


@case("LNK18", "links", "值来源的表名只有一个真相：dataSet.from 与首条 dataTemplateField 都是物理表",
      "P0", ["样本 <dataSet key=project from=pp_project>", "FROM 编码 = FROM 不存在的表"])
def lnk18():
    data = build(other_link_sheets())
    xml = _ds_xml(data, "project_value")
    frag = re.search(r"<dataSet\b[^>]*>", xml)
    assert frag, xml[:600]
    tag = frag.group(0)
    assert 'from="pp_project"' in tag, tag
    # 写对象编码的话，值来源取数时 FROM 的是一张不存在的表
    assert 'from="project"' not in tag, tag
    # 反解来的包可能已经带 pp_ 前缀：不要给人家再套一层
    assert 'from="pp_pp_project"' not in tag, tag

    # 首条 dataTemplateField = 表名行，必须与 dataSet.from 同一个值；
    # label 是**目标对象显示名**（样本「项目」），不是值来源名
    #「项目值来源」——写成对象编码 + 值来源名的话，同一份 XML 里 dataSet 说
    # pp_project、表名行说 project，设计器按表名对数时对不上。
    first = re.search(r"<dataTemplateField\b[^>]*>", xml).group(0)
    assert 'name="pp_project"' in first, first
    assert 'objName="pp_project"' in first, first
    assert 'label="项目"' in first, first
    assert 'name="project"' not in first.replace('name="pp_project"', ""), first


@case("LNK19", "links", "显式配了 6_数据集值来源的关联字段保持 linkdata，不被降级成对象选择器",
      "P0", ["6_数据集 = 用户明确要的值来源", "别把配好的降级"])
def lnk19():
    sheets = other_link_sheets(with_type_field=True)
    sheets[S.S_DATASET] = [{
        "数据集key": "classification_value", "数据集名称": "项目类型值来源",
        "主表名": "classification", "类型": "valueSource", "返回字段": "id,name"}]
    data = build(sheets)

    opts = _field_options(_form_xml(data, "create_fuKuan"), "classificationId")
    assert opts.get("linkdata") == "classification_value", opts
    assert (opts.get("link_config") or {}).get("id") == "id_", opts
    # 这份值来源必须随包走（否则 linkdata 又是一个包外 key）
    ds = _ds_xml(data, "classification_value")
    assert 'from="pp_classification"' in re.search(r"<dataSet\b[^>]*>", ds).group(0)
    # 降级提示不该出现在这条路径上
    note = zipfile.ZipFile(io.BytesIO(data)).comment.decode("utf-8", "replace")
    assert "控件「项目类型」改用对象选择器" not in note, note[-400:]


# ---------------------------------------------------------------- LNK20–LNK24
# 2026-11-20 用户口径（「关联项目 / 关联其他业务对象」整套）：
#
#   ① 强制在对象字段里加一个「XX 选择」控件（项目 → 项目选择，其它对象 →
#      合同选择/客户选择），而且它要在**预览字段列表与三张表单里置顶**；
#   ② 值来源「返回结果」= **全部业务字段 + 主键**（排掉 createBy/createTime/
#      tenantId/deleted/version/process* 这类系统字段），**默认不加任何过滤**——
#      用户在 app 面板里自己选才加；
#   ③ 联动仍然按「返回列物理列名 == 本表单字段物理列名」一一对应，
#      因为现在返回了全部业务字段，能对上的都会自动带上。
#
# 这三条的产物形状前面几组（LNK01–LNK19）已经钉住了，这一组钉的是**新口径**：
# 命名/置顶/全字段/无过滤/显式配置优先/详情仍是对象选择器。


@contextlib.contextmanager
def _home_in(tmp):
    """把 ~ 指到临时目录：上传的目标快照不许读写真实用户目录。

    不隔离的话，本机跑过一次「上传合同 zip」就会在 ~/.pp6kit 里留下快照，
    下次跑用例的目标字段池来源变了，结论跟着变。
    """
    import os

    old = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp)
    projlink.clear_cache()
    try:
        yield tmp
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old
        projlink.clear_cache()


def accept_sheets() -> dict:
    """验收样本：本包自带目标对象 project（字段可预期）。

    为什么把目标对象放进包里、而不是靠 assets 快照：「返回全部业务字段」
    要能**逐列**断言，快照版本一变断言就飘。真实业务里目标对象来自用户导入
    的 zip / 已连库的对象，本包内对象正是那条链路的第一档来源
    （links.LinkPlan.target 的三级降级）。
    """
    return {
        S.S_BO: [
            {"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
            {"对象名称": "项目类型", "对象编码": "classification",
             "物理表名": "pp_classification"},
            {"对象名称": "采购申请", "对象编码": "caiGou", "物理表名": "pp_cai_gou",
             "关联项目": "是"},
        ],
        S.S_FIELD: [
            field("project", "项目名称", "字符串", 长度=200),
            field("project", "项目编号", "字符串", 长度=64),
            field("project", "专业", "字符串", 长度=200),
            field("project", "合同金额", "数字"),
            field("project", "项目类型", "对象",
                  **{"关联对象/字典": "classification"}),
            field("classification", "名称", "字符串"),
            field("caiGou", "申请单号", "字符串", 长度=64),
            field("caiGou", "专业", "字符串", 长度=200),
            field("caiGou", "项目类型", "对象",
                  **{"关联对象/字典": "classification"}),
        ],
        S.S_FORM: [{"表单key": "create_caiGou", "表单名称": "新建采购申请",
                    "对象编码": "caiGou", "表单类型": "标准全套",
                    "布局": "一行两列"}],
    }


def pl_header(objects: dict, target: dict | None = None,
              snapshot: str = "") -> str:
    """造一份 X-Project-Link 决策头（与前端 projLinkHeader() 同形状）。

    target / snapshot 只有「关联其它业务对象」才用：target 选对象，
    snapshot 指向上传包解析出来的字段池（没连库那条路）。
    """
    payload: dict = {"version": 1, "objects": objects}
    if target:
        payload["target"] = target
    if snapshot:
        payload["snapshot"] = snapshot
    return json.dumps(payload, ensure_ascii=False)


# 布局壳子（分区 / 栅格）在 XML 里也是 formField，但它们不是数据控件
# ——「第一个控件是不是 XX选择」比的是**数据控件**的顺序。
_LAYOUT_TYPES = {"fieldset", "grid_layout", "gridLayout", "tabs", "tab", "card"}


def _form_fields(xml: str) -> list[tuple[str, str, str]]:
    """表单里 (name, label, fieldType) 的顺序清单（按 XML 出现顺序，去布局壳）。"""
    out = []
    for frag in re.findall(r"<formField\b[^>]*>", xml):
        a = dict(re.findall(r'([\w:.-]+)="([^"]*)"', frag))
        if a.get("fieldType") in _LAYOUT_TYPES:
            continue
        out.append((a.get("name", ""), a.get("label", ""), a.get("fieldType", "")))
    return out


def _attr_rows(xml: str) -> list[dict]:
    """bo XML 里的 <attribute> 行（命名空间前缀 tns: 要兼容）。"""
    return [dict(re.findall(r'([\w:.-]+)="([^"]*)"', frag))
            for frag in re.findall(r"<[\w:]*attribute\b[^>]*>", xml)]


def _bo_xml(body: bytes, code: str) -> str:
    """取某个对象的 bo XML（包里可能同时有目标对象与业务对象）。"""
    for name, text in texts(body).items():
        if name.startswith("bo/") and f'code="{code}"' in text:
            return text
    raise AssertionError(f"包里没有 code={code} 的对象：{sorted(texts(body))}")


def _bo_card(res: dict, code: str) -> dict:
    return next(o for o in res["objects"] if o["code"] == code)


def _cai_gou_data() -> tuple[dict, str]:
    sheets = accept_sheets()
    return sheets, pl_header({"caiGou": {"linked": True}})


@case("LNK20", "links", "「项目选择」置顶：预览字段第一、新建表单第一个控件、详情也是第一个",
      "P0", ["用户口径 2026-11-20", "预览 fields[0]", "三张表单都排第一",
             "存量包 sn 不变"])
def lnk20():
    """命名与位置。

    · 显示名 = 「项目选择」（项目）/「{对象名}选择」（其它对象）；
    · 预览字段列表第一位就是它（`webui._field_payload` 按 is_link 前置排序）；
    · 新建表单里它是第一个控件且是 linkdata，详情表单里也是第一个但为
      selectObject（详情态用系统原本的字段类型，不带值来源）；
    · **sn 仍是最大值**——置顶只改表单控件顺序与预览顺序，物理列顺序不动
      （存量包铁律：改 sn/列名等于要求线上 ALTER）。
    """
    sheets, header = _cai_gou_data()
    res = webui.parse_excel(xlsx(sheets), header)
    card = _bo_card(res, "caiGou")
    assert card["fields"][0]["code"] == "projectId", card["fields"][:3]
    assert card["fields"][0]["name"] == "项目选择", card["fields"][0]
    assert card["fields"][0]["fieldName"] == "project_id_", card["fields"][0]

    body = webui.build_zip(xlsx(sheets), projlink_header=header)[0]
    # ① 新建表单：第一个控件就是它，且是关联数据（挂值来源 + 联动 + 过滤）
    create = _form_fields(_form_xml(body, "create_caiGou"))
    assert create[0][0] == "projectId", create[:3]
    assert create[0][1] == "项目选择", create[0]
    assert create[0][2] == "linkdata", create[0]

    # ② 详情表单：同样第一个，但控件是系统原本的对象选择器
    detail = _form_fields(_form_xml(body, "detail_caiGou"))
    assert detail[0][0] == "projectId", detail[:3]
    assert detail[0][2] == "selectObject", detail[0]

    # ③ 条件专用表单也排第一
    cond = _form_fields(_form_xml(body, "fetch_basic_caiGou_information"))
    assert cond[0][0] == "projectId", cond[:3]

    # ④ sn 仍是该对象里最大的那个（没有为了置顶动过物理列顺序）
    sns = {a["code"]: int(a["sn"]) for a in _attr_rows(_bo_xml(body, "caiGou"))
           if a.get("sn", "").isdigit()}
    assert sns["projectId"] == max(sns.values()), sns


@case("LNK21", "links", "值来源 = 全部业务字段 + 主键、默认不过滤；联动按物理列名一一对应",
      "P0", ["用户口径：返回全部业务字段", "filterConditions 不出现",
             "link_condition 为空", "zhuan_ye_ → zhuanYe"])
def lnk21():
    """返回列与过滤口径。

    用户口径：值来源的「返回结果」= 全部业务字段 + 主键，**不加任何过滤条件**
    （他在 app 面板里选了才加）。带全字段是为了让联动能一一带过来——
    值来源不返回的列，联动是配不出来的。
    """
    sheets, header = _cai_gou_data()
    body = webui.build_zip(xlsx(sheets), projlink_header=header)[0]
    ds = _ds_xml(body, "project_value")

    cols = [c["name"] for c in (_json_attr(ds, "resultColumns") or [])]
    # 主键第一位（值来源没有主键取不到值，用户硬要求）
    assert cols[0] == "id_", cols
    assert "name_" in cols, cols
    # 目标项目的**全部业务字段**一个不少
    for col in ("xiang_mu_bian_hao_", "zhuan_ye_", "he_tong_jin_e_",
                "xiang_mu_lei_xing_"):
        assert col in cols, (col, cols)
    # 系统列不许进返回列（createBy/createTime/tenantId/process* 这一批）
    assert "tenant_id_" not in cols and "create_time_" not in cols, cols
    assert not any(c.startswith("process") for c in cols), cols

    # 不加过滤：默认口径是「用户在面板里选才加」
    assert _json_attr(ds, "filterConditions") is None, ds[:600]

    # 表单侧同样没有过滤绑定（有 filterConditions 没 link_condition 或反过来
    # 都是错的：设计器里会显示一条永远选不出来的条件）
    opts = _field_options(_form_xml(body, "create_caiGou"), "projectId")
    assert opts.get("link_condition") == [], opts.get("link_condition")
    assert opts.get("linkdata") == "project_value", opts

    # 联动：返回列物理列名 → 本表单同物理列名的控件，一一对应
    by_name = {it["name"]: it["field"] for it in opts["link_linkage"]}
    assert by_name.get("zhuan_ye_") == zhuanYe_code(body), by_name
    # 主键/名称只回显，不许塞进隐藏控件
    assert by_name.get("id_") == "" and by_name.get("name_") == "", by_name
    # 返回列有几条，联动清单就要有几条（两边一一对应，缺一条就是配错）
    assert len(opts["link_linkage"]) == len(cols), (len(opts["link_linkage"]), cols)


def zhuanYe_code(body: bytes) -> str:
    """本对象「专业」字段的编码（对齐改名后可能不是 zhuanYe，别写死）。"""
    for row in _attr_rows(_bo_xml(body, "caiGou")):
        if row.get("fieldName") == "zhuan_ye_":
            return row.get("code", "")
    raise AssertionError("本对象里没有物理列 zhuan_ye_ 的字段")


@case("LNK22", "links", "关联其他业务对象：字段名「合同选择」、置顶、值来源=合同全部业务字段且不过滤",
      "P0", ["X-Project-Link target=合同", "上传合同 zip 当字段池",
             "boCode=contract", "resultColumns 全字段"])
def lnk22():
    """「关联其他业务对象」走同一条口径，只换目标与文案。

    目标对象的字段池来自用户上传的合同 zip（离线没连库时的真实路径），
    值来源的 boCode 必须是 contract、返回列是合同的全部业务字段、不带过滤。
    """
    import tempfile
    from pathlib import Path

    pkg = build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract",
                  "物理表名": "pp_contract"}],
        S.S_FIELD: [
            field("contract", "合同名称", "字符串", 长度=200),
            field("contract", "合同编号", "字符串", 长度=64),
            field("contract", "合同金额", "数字"),
            field("contract", "专业", "字符串", 长度=200),
        ],
    })
    sheets = accept_sheets()
    sheets[S.S_BO] = [b for b in sheets[S.S_BO] if b["对象编码"] != "project"]
    sheets[S.S_FIELD] = [f for f in sheets[S.S_FIELD]
                         if f["对象编码"] != "project"]

    with tempfile.TemporaryDirectory() as td:
        with _home_in(Path(td)):
            tgt = projlink.target_for("contract", "合同", "pp_contract")
            token = projlink.save_snapshot_upload(pkg, tgt)["token"]
            header = pl_header({"caiGou": {"linked": True}},
                               target={"name": "合同", "code": "contract"},
                               snapshot=token)
            res = webui.parse_excel(xlsx(sheets), header)
            body = webui.build_zip(xlsx(sheets), projlink_header=header)[0]

    card = _bo_card(res, "caiGou")
    assert card["fields"][0]["code"] == "contractId", card["fields"][:3]
    assert card["fields"][0]["name"] == "合同选择", card["fields"][0]

    create = _form_fields(_form_xml(body, "create_caiGou"))
    assert (create[0][0], create[0][1], create[0][2]) == (
        "contractId", "合同选择", "linkdata"), create[:3]

    key = _field_options(_form_xml(body, "create_caiGou"), "contractId")["linkdata"]
    ds = _ds_xml(body, key)
    assert 'boCode="contract"' in ds, ds[:600]
    cols = [c["name"] for c in (_json_attr(ds, "resultColumns") or [])]
    assert cols[0] == "id_", cols
    for col in ("he_tong_bian_hao_", "he_tong_jin_e_", "zhuan_ye_"):
        assert col in cols, (col, cols)
    assert _json_attr(ds, "filterConditions") is None, ds[:600]
    # 详情表单仍是对象选择器（不带值来源）
    det = _field_options(_form_xml(body, "detail_caiGou"), "contractId")
    assert det.get("boCode") == "contract" and det.get("store") == "id", det
    assert det.get("linkdata") is None, det


@case("LNK23", "links", "显式 6_数据集 配置优先：过滤条件真的出现、返回列按用户勾的、rule_id 两侧一致",
      "P0", ["用户显式配置不被默认值覆盖", "rule_id 一致",
             "resultColumns = 用户勾的 + 主键/名称"])
def lnk23():
    """用户在 6_数据集 / app 面板上配过的，永远优先于「全字段 + 不过滤」的默认口径。

    这里两个都显式写：过滤字段 + 返回字段。断言的是：
      · filterConditions 真的出现（不是被 auto_filter=False 吞掉）；
      · 值来源与表单两侧 rule_id 逐字一致；
      · 返回列回到「主键 + 名称 + 用户勾的那几列」（不再补全字段）。
    """
    sheets, header = _cai_gou_data()
    sheets[S.S_DATASET] = [{
        "数据集key": "project_value", "数据集名称": "值来源_过滤项目类型",
        "主表名": "project", "类型": "valueSource",
        "返回字段": "id,name,zhuanYe", "过滤字段": "xiangMuLeiXing"}]
    body = webui.build_zip(xlsx(sheets), projlink_header=header)[0]

    ds = _ds_xml(body, "project_value")
    conds = _json_attr(ds, "filterConditions")
    assert conds, "用户显式配了过滤字段，值来源却没有 filterConditions"
    rule = conds[0]["filter"]["rules"][0]
    # 目标对象（本包里的 project）「项目类型」那列的物理列名
    assert rule["field"] == "xiang_mu_lei_xing_", rule

    cols = [c["name"] for c in (_json_attr(ds, "resultColumns") or [])]
    assert cols == ["id_", "name_", "zhuan_ye_"], cols
    # 系统列 / 没勾的列都不许偷偷加回来
    assert "he_tong_jin_e_" not in cols, cols

    opts = _field_options(_form_xml(body, "create_caiGou"), "projectId")
    cond = (opts.get("link_condition") or [None])[0]
    assert cond, opts
    assert cond["rule_id"] == rule["rule_id"], (cond, rule)
    assert cond["fieldName"] == f'{rule["rule_id"]}_1', cond
    # 联动只留用户勾的那几列
    assert [it["name"] for it in opts["link_linkage"]] == cols, opts["link_linkage"]


@case("LNK24", "links", "复用用户已有的关联字段：保留原名、但仍置顶且按新口径返回值来源",
      "P1", ["plan_link_field reuse 分支", "不改用户的字段名",
             "is_link 由 builder 补上"])
def lnk24():
    """Excel 里已经有指向项目的 object 字段时，**不许改名**（用户已经按自己的
    语义在用），但它仍然是本包的「XX选择」字段：置顶 + 全字段返回 + 不过滤。

    这一条防的是「新口径只在自动补的字段上生效」——复用路径与新增路径是两段
    代码，只改一段的话，老对象升级后会突然丢掉置顶与全字段返回。
    """
    sheets = accept_sheets()
    sheets[S.S_FIELD].append(
        field("caiGou", "我的项目", "对象", **{"字段编码": "woDeXiangMu",
                                          "关联对象/字典": "project"}))
    _, header = _cai_gou_data()
    body = webui.build_zip(xlsx(sheets), projlink_header=header)[0]

    # ① 没有另补 projectId，用的就是用户那个字段（名字也不改）
    bo_text = _bo_xml(body, "caiGou")
    assert 'code="projectId"' not in bo_text, bo_text[:600]
    row = next(a for a in _attr_rows(bo_text) if a.get("code") == "woDeXiangMu")
    assert row["name"] == "我的项目", row

    # ② 仍然置顶 + linkdata
    create = _form_fields(_form_xml(body, "create_caiGou"))
    assert create[0][0] == "woDeXiangMu", create[:3]
    assert create[0][2] == "linkdata", create[0]

    # ③ 值来源按新口径：全业务字段、不过滤
    opts = _field_options(_form_xml(body, "create_caiGou"), "woDeXiangMu")
    ds = _ds_xml(body, opts["linkdata"])
    cols = [c["name"] for c in (_json_attr(ds, "resultColumns") or [])]
    assert "zhuan_ye_" in cols and "he_tong_jin_e_" in cols, cols
    assert _json_attr(ds, "filterConditions") is None, ds[:600]


@case("LNK25", "links", "确认版 Excel 往返后口径不变：仍置顶、返回列不掺系统列、仍不过滤",
      "P1", ["预览=产物", "is_system 不落 Excel", "往返幂等"])
def lnk25():
    """出包 → 确认版 Excel → 再出包，产物必须与第一次一致。

    为什么这条值得单列：``BoField.is_system`` 只活在模型里（2_字段 没有这一列），
    往返一次就全丢了。只认这个标记的代码，会在第二遍把 create_time_/
    process_node_ 这些列当成「业务字段」——返回列多出一串审计列、联动还会把
    流程字段映射到表单上，而界面上看起来只是「多了几列」。
    """
    import io as _io
    import zipfile as _zip

    from pathlib import Path
    import tempfile

    from pp6kit import builder, template
    from pp6kit.reader import read_workbook

    sheets, header = _cai_gou_data()

    def pack(proj, out: Path) -> bytes:
        res = builder.build(proj, webui._snippets(), out)
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w") as z:
            for a in res.artifacts:
                if a.xml is not None:
                    z.writestr(a.entry, a.xml)
        return buf.getvalue()

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        first = td / "in.xlsx"
        first.write_bytes(xlsx(sheets))
        proj, errors, _ = read_workbook(first)
        assert not errors, errors
        proj.link_target = projlink.project_target()
        projlink.apply(proj, projlink.parse_decisions(header, proj))
        body1 = pack(proj, td / "o1")

        confirm = td / "confirm.xlsx"
        template.build_workbook(proj).save(confirm)
        proj2, errors2, _ = read_workbook(confirm)
        assert not errors2, errors2
        body2 = pack(proj2, td / "o2")

    def snapshot(body: bytes) -> tuple:
        key = _field_options(_form_xml(body, "create_caiGou"), "projectId")["linkdata"]
        ds = _ds_xml(body, key)
        return (key,
                [c["name"] for c in (_json_attr(ds, "resultColumns") or [])],
                bool(_json_attr(ds, "filterConditions")),
                _form_fields(_form_xml(body, "create_caiGou"))[0])

    one, two = snapshot(body1), snapshot(body2)
    assert one == two, (one, two)
    cols = one[1]
    assert cols[0] == "id_" and "zhuan_ye_" in cols, cols
    # 往返后系统列 / 流程冗余列一个都不许漏进来
    assert not any(c.startswith("process") or c in (
        "create_time_", "update_time_", "tenant_id_", "ip_", "deleted_",
        "version_") for c in cols), cols


@case("LNK26", "links", "表单里没有对应控件时联动只回显：link_linkage 不指向不存在的控件",
      "P0", ["4_表单控件 只写部分字段", "AI 表单漏画字段",
             "links._linkage 的 controls 守卫要真的接上"])
def lnk26():
    """``link_linkage.field`` 只能是**本表单真实存在**的控件编码。

    为什么这条值得单独钉住：``links._linkage`` 里那道
    「本表单没有这个控件 → 只回显（field 留空）」的守卫，只有当调用方把
    ``form`` 传进 ``link.for_field`` 时才生效。以前 ``gen_form`` 调的是
    ``link.for_field(bo, field)``（form 缺省 None）→ 守卫整条失效，产物里就出现
    「联动指向本表单不存在的控件」：导入后选了值带不出来，界面上还看不出哪里错。

    两种真实形态都覆盖：
      · 用户手写 Excel 的 `4_表单控件` 只列了部分字段（很常见）；
      · AI 生成对象时漏画了某个字段的控件。
    """
    # ① 手写 Excel：4_表单控件 只写了「申请单号」
    body = webui.build_zip(xlsx({
        S.S_BO: [
            {"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
            {"对象名称": "采购申请", "对象编码": "caigou", "物理表名": "pp_caigou",
             "关联项目": "是"},
        ],
        S.S_FIELD: [
            field("project", "项目名称", "字符串", 长度=200),
            field("project", "专业", "字符串", 长度=200),
            field("caigou", "申请单号", "字符串"),
            field("caigou", "专业", "字符串"),
        ],
        S.S_FORM: [{"表单key": "create_caigou", "表单名称": "新建采购申请",
                    "对象编码": "caigou", "表单类型": "新建表单",
                    "布局": "一行两列"}],
        S.S_FORMFIELD: [{"表单key": "create_caigou", "字段编码": "shenQingDanHao",
                         "显示标签": "申请单号", "控件类型": "text"}],
    }), projlink_header=pl_header({"caigou": {"linked": True}}))[0]

    opts = _field_options(_form_xml(body, "create_caigou"), "projectId")
    by_name = {it["name"]: it["field"] for it in opts["link_linkage"]}
    # 专业那列照样**返回**（值来源是全字段），但本表单没有 zhuanYe 控件 → 只回显
    assert "zhuan_ye_" in by_name, by_name
    assert by_name["zhuan_ye_"] == "", by_name
    assert not [k for k, v in by_name.items() if v], by_name

    # ② AI 生成：表单漏画了「专业」
    excel, _ = webui.ai_apply_bo({
        "bo": {"code": "qaai", "name": "AI联动", "fields": [
            {"code": "suoShuXiangMu", "name": "所属项目", "type": "object",
             "objectType": "project"},
            {"code": "zhuanYe", "name": "专业", "type": "varchar"},
            {"code": "beiZhu", "name": "备注", "type": "varchar"},
        ]},
        "form": {"key": "create_qaai", "name": "新建", "layout": 2, "fields": [
            {"name": "suoShuXiangMu", "label": "所属项目", "type": "linkdata"},
            {"name": "beiZhu", "label": "备注", "type": "text"},
        ]},
    })
    ai_body, _ = webui.build_zip(excel)
    ai_opts = _field_options(_form_xml(ai_body, "create_qaai"), "suoShuXiangMu")
    bad = [it for it in ai_opts["link_linkage"] if it["field"]]
    assert bad == [], bad            # 本表单没有这些控件，一个都不许带值

    # ③ 反向确认：控件写全了就该带上（守卫只裁「不存在的」，别一刀切）
    full = webui.build_zip(xlsx(accept_sheets()),
                           projlink_header=pl_header({"caiGou": {"linked": True}}))[0]
    full_opts = _field_options(_form_xml(full, "create_caiGou"), "projectId")
    assert {it["name"]: it["field"] for it in full_opts["link_linkage"]}.get(
        "zhuan_ye_") == zhuanYe_code(full)
