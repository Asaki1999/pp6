"""表单 XML：布局结构、控件属性、隐藏规则、脚本。

表单这层最容易「导入成功但打开是空白」：id 引用错一处，设计器里就什么都没有。
所以结构类断言直接复用 tools/check_form_layout.py 的契约检查。
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from pp6kit import gen_form
from pp6kit import spec as S
from pp6kit import webui
from tests._harness import (all_tags, attrs, case, field, pick, project_of,
                            texts, unzip, xlsx, xml_ok)
from tools.check_form_layout import check as check_layout


def build(sheets: dict) -> bytes:
    return webui.build_zip(xlsx(sheets))[0]


def form_only(rows: list[dict], fields: list[dict] | None = None,
              form: dict | None = None) -> str:
    """出一份只含表单的包，取第一份表单 XML。"""
    data = {
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: fields or [
            field("contract", "名称", "字符串"),
            field("contract", "金额", "数字", 精度=2),
            field("contract", "状态", "选项", 选项值="草稿,生效"),
        ],
        S.S_FORM: [form or {"表单key": "create_contract", "表单名称": "新建合同",
                            "对象编码": "contract", "表单类型": "新建表单"}],
        S.S_FORMFIELD: rows,
    }
    return pick(build(data), "form/")


def layout_bad(data: bytes) -> list[str]:
    """把包写到临时文件，跑一遍布局契约检查。"""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as fh:
        fh.write(data)
        path = Path(fh.name)
    try:
        bad, _, _ = check_layout(path)
    finally:
        path.unlink(missing_ok=True)
    return bad


def ctrl(name: str) -> dict:
    return {"表单key": "create_contract", "字段编码": name,
            "显示标签": name, "控件类型": "text"}


@case("FRM01", "form", "表单 XML 结构合法且带 formDefs", "P0",
      ["XML 可解析", "formDef 一个"])
def _():
    x = form_only([ctrl("mingCheng")])
    xml_ok(x)
    assert "<formDefs>" in x
    assert len(all_tags(x, "formDef")) == 1


@case("FRM02", "form", "表单 key/名称/状态按填写进 XML", "P0",
      ["key=create_contract", "status=deploy"])
def _():
    x = form_only([ctrl("mingCheng")])
    a = attrs(all_tags(x, "formDef")[0])
    assert a["key"] == "create_contract"
    assert a["name"] == "新建合同"
    assert a["status"] == "deploy"


@case("FRM03", "form", "布局结构符合 V1.0 契约（id 引用全对得上）", "P0",
      ["parentId 存在", "columns↔rows 一致", "两份 field_options 一致",
       "sn 连续"])
def _():
    data = build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串"),
                    field("contract", "金额", "数字", 精度=2),
                    field("contract", "状态", "选项", 选项值="草稿,生效"),
                    field("contract", "备注", "大文本")],
        S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                    "对象编码": "contract", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [ctrl("mingCheng"), ctrl("jinE"),
                        {"表单key": "create_contract", "字段编码": "zhuangTai",
                         "显示标签": "状态", "控件类型": "dictionary",
                         "取值来源": "contract_zhuangTai"},
                        {"表单key": "create_contract", "字段编码": "beiZhu",
                         "显示标签": "备注", "控件类型": "editor", "占满整行": "是"}],
    })
    # 只出表单模块：check_form_layout 取包里第一个 xml，
    # 混合包会先读到 bo/*.xml 而报「不是表单包」
    only_form = webui.build_zip(xlsx({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串"),
                    field("contract", "金额", "数字", 精度=2),
                    field("contract", "状态", "选项", 选项值="草稿,生效"),
                    field("contract", "备注", "大文本")],
        S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                    "对象编码": "contract", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [ctrl("mingCheng"), ctrl("jinE"),
                        {"表单key": "create_contract", "字段编码": "zhuangTai",
                         "显示标签": "状态", "控件类型": "dictionary",
                         "取值来源": "contract_zhuangTai"},
                        {"表单key": "create_contract", "字段编码": "beiZhu",
                         "显示标签": "备注", "控件类型": "editor", "占满整行": "是"}],
    }), codes=["form"])[0]
    bad = layout_bad(only_form)
    assert not bad, "布局契约不通过：\n" + "\n".join(bad[:6])


@case("FRM04", "form", "每个字段都生成对应控件", "P0",
      ["控件数 = 字段数 + 补齐的隐藏控件"])
def _():
    x = form_only([ctrl("mingCheng"), ctrl("jinE"),
                   {"表单key": "create_contract", "字段编码": "zhuangTai",
                    "显示标签": "状态", "控件类型": "dictionary",
                    "取值来源": "contract_zhuangTai"}])
    names = {attrs(t)["name"] for t in all_tags(x, "formField")}
    assert {"mingCheng", "jinE", "zhuangTai"} <= names, names


@case("FRM05", "form", "必填勾选写进控件配置", "P0",
      ["required=true", "不写等于没配"])
def _():
    x = form_only([{"表单key": "create_contract", "字段编码": "mingCheng",
                    "显示标签": "名称", "控件类型": "text", "必填": "是"}])
    item = next(t for t in all_tags(x, "formField") if attrs(t).get("name") == "mingCheng")
    opts = json.loads(attrs(item)["fieldOptions"].replace("&quot;", '"'))
    assert str(opts.get("required")).lower() == "true", opts


@case("FRM06", "form", "只读勾选写进控件配置", "P1", ["readonly=true"])
def _():
    x = form_only([{"表单key": "create_contract", "字段编码": "mingCheng",
                    "显示标签": "名称", "控件类型": "text", "只读": "是"}])
    item = next(t for t in all_tags(x, "formField") if attrs(t).get("name") == "mingCheng")
    opts = json.loads(attrs(item)["fieldOptions"].replace("&quot;", '"'))
    # 只读在控件配置里是 disabled（原生表单同样用这个键）
    assert opts.get("disabled") is True, opts


@case("FRM07", "form", "数字字段生成数字控件而不是文本框", "P1",
      ["fieldType=number", "否则导入后填不了小数"])
def _():
    x = form_only([{"表单key": "create_contract", "字段编码": "jinE",
                    "显示标签": "金额", "控件类型": "number"}])
    item = next(t for t in all_tags(x, "formField") if attrs(t).get("name") == "jinE")
    assert attrs(item)["fieldType"] == "number"


@case("FRM08", "form", "字典控件的取值来源与该字段字典一致", "P0",
      ["取错了选项会重复显示两遍"])
def _():
    x = form_only([{"表单key": "create_contract", "字段编码": "zhuangTai",
                    "显示标签": "状态", "控件类型": "dictionary",
                    "取值来源": "contract_zhuangTai"},
                   ctrl("mingCheng")])
    item = next(t for t in all_tags(x, "formField") if attrs(t).get("name") == "zhuangTai")
    opts = json.loads(attrs(item)["fieldOptions"].replace("&quot;", '"'))
    raw = json.dumps(opts, ensure_ascii=False)
    assert "contract_zhuangTai" in raw, raw[:300]


@case("FRM09", "form", "隐藏规则写进表单的 formRules", "P0",
      ["条件隐藏", "写在 formDef.extendAttr 里而不是控件上",
       "不写等于白配"])
def _():
    data = {
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "状态", "选项", 选项值="草稿,生效"),
                    field("contract", "原因", "字符串")],
        S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                    "对象编码": "contract", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [
            {"表单key": "create_contract", "字段编码": "zhuangTai",
             "显示标签": "状态", "控件类型": "dictionary",
             "取值来源": "contract_zhuangTai"},
            ctrl("yuanYin")],
        S.S_HIDE: [{"表单key": "create_contract", "隐藏控件": "yuanYin",
                    "依赖控件": "zhuangTai", "判断条件": "等于", "条件值": "生效"}],
    }
    x = pick(build(data), "form/")
    head = attrs(all_tags(x, "formDef")[0])
    ext = json.loads(head["extendAttr"].replace("&quot;", '"'))
    rules = ext.get("formRules") or []
    assert rules, "没写 formRules，隐藏规则丢了"
    rule = rules[0]
    assert rule.get("controlName") == "yuanYin", rule
    assert rule.get("action") == "hidden", rule
    cond = json.dumps(rule.get("condition") or {}, ensure_ascii=False)
    assert "zhuangTai" in cond, cond


@case("FRM10", "form", "默认不补隐藏字段；填「补隐藏字段=是」才补 id/version", "P1",
      ["2026-11-16 用户要求：生成器默认不要往表单上塞隐藏字段",
       "普通对象默认一个 hidden 控件都没有",
       "需要时（保存并继续/编辑）在 3_表单 填「是」，补 id 与 version"])
def _():
    # 默认：一个 hidden 都不该有
    x = form_only([ctrl("mingCheng")])
    hidden = {attrs(t)["name"] for t in all_tags(x, "formField")
              if attrs(t).get("fieldType") == "hidden"}
    assert not hidden, f"默认表单不该有隐藏字段：{hidden}"

    # 显式开启：id / version 回来
    x = form_only([ctrl("mingCheng")],
                  form={"表单key": "create_contract", "表单名称": "新建合同",
                        "对象编码": "contract", "表单类型": "新建表单",
                        "补隐藏字段": "是"})
    names = {attrs(t)["name"] for t in all_tags(x, "formField")}
    assert {"id", "version"} <= names, names


@case("FRM11", "form", "标签宽度默认 106（新建表单）", "P2",
      ["与原生表单一致"])
def _():
    x = form_only([ctrl("mingCheng")])
    head = attrs(all_tags(x, "formDef")[0])
    ext = json.loads(head["extendAttr"].replace("&quot;", '"'))
    assert ext.get("labelWidth") == 106, ext.get("labelWidth")


@case("FRM12", "form", "面板宽度统一 90%", "P2", ["手册规定"])
def _():
    x = form_only([ctrl("mingCheng")])
    head = attrs(all_tags(x, "formDef")[0])
    ext = json.loads(head["extendAttr"].replace("&quot;", '"'))
    assert str(ext.get("width")) in ("90", "90%"), ext.get("width")


@case("FRM13", "form", "「标准全套」一次出三张表单", "P0",
      ["新建/详情/条件专用", "key 按规范"])
def _():
    data = build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
        S.S_FORM: [{"表单key": "bundle_contract", "表单名称": "合同表单",
                    "对象编码": "contract", "表单类型": "标准全套"}],
        S.S_FORMFIELD: [{"表单key": "bundle_contract", "字段编码": "mingCheng",
                         "显示标签": "名称", "控件类型": "text"}],
    })
    forms = [n for n in unzip(data) if n.startswith("form/")]
    assert len(forms) == 3, forms
    names = " ".join(forms)
    assert "create_contract" in names and "detail_contract" in names


@case("FRM14", "form", "表单脚本写进 extendAttr.script", "P1",
      ["整段 JS 原样保留"])
def _():
    data = {
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串")],
        S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                    "对象编码": "contract", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [ctrl("mingCheng")],
        S.S_SCRIPT: [{"表单key": "create_contract", "脚本说明": "加载时打印",
                      "JForm 脚本": 'Object.assign(JForm, { onLoad(form){} });'}],
    }
    x = pick(build(data), "form/")
    assert "onLoad" in x, "脚本没写进去"
    assert "JForm" in x


@case("FRM15", "form", "分区字段生成卡片容器", "P1",
      ["同名的归到一个卡片", "详情页分组"])
def _():
    x = form_only([
        {"表单key": "create_contract", "字段编码": "mingCheng",
         "显示标签": "名称", "控件类型": "text", "分区": "基本信息"},
        {"表单key": "create_contract", "字段编码": "jinE",
         "显示标签": "金额", "控件类型": "number", "分区": "金额信息"},
    ])
    labels = [attrs(t).get("label") for t in all_tags(x, "formField")
              if attrs(t).get("fieldType") == "fieldset"]
    assert "基本信息" in labels, labels
    assert "金额信息" in labels, labels


@case("FRM16", "form", "一行两列时布局列数为 2", "P1",
      ["columns 两个", "单行最多 2 个控件"])
def _():
    x = form_only([ctrl("mingCheng"), ctrl("jinE")])
    grid = next(t for t in all_tags(x, "formField")
                if attrs(t).get("fieldType") == "grid_layout")
    opts = json.loads(attrs(grid)["fieldOptions"].replace("&quot;", '"'))
    rows = opts.get("rows") or []
    assert rows, "rows 为空"
    assert len(rows[0].get("columns") or []) == 2, rows[0]


@case("FRM17", "form", "控件序号在同一布局内从 1 连续", "P1",
      ["sn 断了设计器会错位"])
def _():
    x = form_only([ctrl("mingCheng"), ctrl("jinE"),
                   {"表单key": "create_contract", "字段编码": "zhuangTai",
                    "显示标签": "状态", "控件类型": "dictionary",
                    "取值来源": "contract_zhuangTai"}])
    grid = next(t for t in all_tags(x, "formField")
                if attrs(t).get("fieldType") == "grid_layout")
    gid = attrs(grid)["id"]
    kids = [t for t in all_tags(x, "formField") if attrs(t).get("parentId") == gid]
    sns = sorted(int(attrs(k).get("sn") or 0) for k in kids)
    assert sns == list(range(1, len(kids) + 1)), sns


@case("FRM18", "form", "详情表单覆盖全部字段", "P1",
      ["少一个字段详情页就看不到"])
def _():
    data = build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串"),
                    field("contract", "金额", "数字", 精度=2)],
        S.S_FORM: [{"表单key": "detail_contract", "表单名称": "合同详情",
                    "对象编码": "contract", "表单类型": "详情表单"}],
        S.S_FORMFIELD: [
            {"表单key": "detail_contract", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
            {"表单key": "detail_contract", "字段编码": "jinE",
             "显示标签": "金额", "控件类型": "number"},
        ],
    })
    x = pick(data, "form/")
    names = {attrs(t)["name"] for t in all_tags(x, "formField")}
    assert {"mingCheng", "jinE"} <= names, names


@case("FRM19", "form", "拖拽布局 V1.0：整张表单一个无标题容器 + 一个 grid，不按分区拆", "P0",
      ["布局样式=拖拽布局V1.0 时只有一个无标题 fieldset/card（平台根级必须有容器）",
       "所有普通控件挂在同一个 grid_layout 下，parentId 指向它",
       "字段顺序保持（跨分区也不丢）",
       "布局契约检查通过"])
def _():
    from pp6kit.reader import read_workbook
    import io as _io

    data = {
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串"),
                    field("contract", "金额", "数字", 精度=2),
                    field("contract", "状态", "选项", 选项值="草稿,生效")],
        S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                    "对象编码": "contract", "表单类型": "新建表单",
                    "布局样式": "拖拽布局V1.0"}],
        S.S_FORMFIELD: [
            {"表单key": "create_contract", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text", "分区": "基础信息"},
            {"表单key": "create_contract", "字段编码": "jinE",
             "显示标签": "金额", "控件类型": "number", "分区": "基础信息"},
            {"表单key": "create_contract", "字段编码": "zhuangTai",
             "显示标签": "状态", "控件类型": "dictionary",
             "取值来源": "contract_zhuangTai", "分区": "扩展信息"},
        ],
    }
    body = build(data)
    x = pick(body, "form/")
    # 平台根级必须有容器：只允许一个**无标题** fieldset/card，不按分区拆
    boxes = [t for t in all_tags(x, "formField")
             if attrs(t).get("fieldType") in ("fieldset", "card")]
    assert len(boxes) == 1, len(boxes)
    assert not attrs(boxes[0]).get("label"), "flat 容器不该带分区标题"
    grids = [t for t in all_tags(x, "formField")
             if attrs(t).get("fieldType") == "grid_layout"]
    assert len(grids) == 1, len(grids)
    gid = attrs(grids[0])["id"]
    assert attrs(grids[0]).get("parentId") == attrs(boxes[0]).get("id")
    kids = [t for t in all_tags(x, "formField") if attrs(t).get("parentId") == gid]
    assert len(kids) == 3, len(kids)
    assert [attrs(k)["name"] for k in kids] == ["mingCheng", "jinE", "zhuangTai"]
    # 契约检查读的是单个表单 XML（整包里第一份 XML 是 bo，会误判）
    fx = Path(tempfile.mkdtemp()) / "form.xml"
    fx.write_text(x, encoding="utf-8")
    try:
        bad, _, _ = check_layout(fx)
    finally:
        fx.unlink(missing_ok=True)
    assert bad == [], bad

    # 读回来仍是 flat
    proj, _, _ = read_workbook(_io.BytesIO(xlsx(data)))
    assert proj.forms[0].layout_style == "flat"

    # 回写 Excel 再读：布局样式列不能丢
    from pp6kit import template
    wb = template.build_workbook(proj)
    buf = _io.BytesIO()
    wb.save(buf)
    proj2, _, _ = read_workbook(_io.BytesIO(buf.getvalue()))
    assert proj2.forms[0].layout_style == "flat"


# ---------------------------------------------------------------- 详情表单：只读 + 无底部按钮
#
# 2026-11-25 用户反馈：导入后的详情页「所有字段都能编辑」「底部多出取消/保存并继续/保存」。
# 原生口径（PP6/表单/项目详情表单 1.zip → form/001-project_summary.xml、
# 项目流程详情表单.zip → form/001-total_project_create_details.xml）：
#   · 业务控件（text/select/selectObject/dropSelector/datePicker/number/editor/
#     tableField）的 fieldOptions 一律 "read_rights": "true"，少数故意可编辑的是 "false"；
#   · label / card / grid_layout / hidden 不写 read_rights；
#   · **没有任何一个控件写 disabled**；
#   · extendAttr.buttons 是空数组 []（详情页靠右上角关闭 + 遮罩关闭）。

# 详情表单里算「业务控件」的类型：它们的 fieldOptions 必须带 read_rights
_DETAIL_BIZ = {"text", "textarea", "editor", "number", "datePicker", "select",
               "selectObject", "dropSelector", "dropSelectOrg", "dictionary",
               "tableField", "attachment", "autoNumber"}


def _opts(frag: str) -> dict:
    return json.loads((attrs(frag).get("fieldOptions") or "{}").replace("&quot;", '"'))


def _detail_package() -> str:
    """含 文本/数字/字典/人员/表格 的详情表单包（取详情表单 XML）。"""
    return pick(build({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [
            field("contract", "名称", "字符串"),
            field("contract", "金额", "数字", 精度=2),
            field("contract", "状态", "选项", 选项值="草稿,生效"),
            field("contract", "负责人", "用户"),
            field("contract", "备注", "字符串"),
            field("contract", "明细", "表格（table）"),
        ],
        S.S_TABLE_COL: [
            {"对象编码": "contract", "字段编码": "mingXi", "序号": 1,
             "列名": "摘要", "列类型": "字符串（varchar）"},
        ],
        S.S_FORM: [{"表单key": "detail_contract", "表单名称": "合同详情",
                    "对象编码": "contract", "表单类型": "详情表单"}],
        S.S_FORMFIELD: [
            {"表单key": "detail_contract", "字段编码": "mingCheng", "显示标签": "名称",
             "控件类型": "text", "只读": "是"},
            {"表单key": "detail_contract", "字段编码": "jinE", "显示标签": "金额",
             "控件类型": "number"},
            {"表单key": "detail_contract", "字段编码": "zhuangTai", "显示标签": "状态",
             "控件类型": "dictionary"},
            {"表单key": "detail_contract", "字段编码": "fuZeRen", "显示标签": "负责人",
             "控件类型": "dropSelector"},
            # 显式覆盖优先级最高：原生详情表单里也有少数故意可编辑的字段（false）
            {"表单key": "detail_contract", "字段编码": "beiZhu", "显示标签": "备注",
             "控件类型": "text", "扩展参数": '{"read_rights":"false"}'},
            {"表单key": "detail_contract", "字段编码": "mingXi", "显示标签": "明细",
             "控件类型": "tableField"},
        ],
    }), "form/")


@case("FRM20", "form", "详情表单的业务控件一律 read_rights=true，且从不写 disabled", "P0",
      ["业务控件（含表格控件）fieldOptions 都有 read_rights=\"true\"",
       "详情表单不写 disabled（那是「当前不可交互」，不是读权限）",
       "容器 card / grid_layout 与 hidden 不写 read_rights",
       "「只读=是」的字段在详情表单里也是 read_rights，不是 disabled",
       "扩展参数显式写 read_rights=false 的字段仍可编辑",
       "表格控件只读时 columns[].editable 全 False、operation 三个开关全 False"])
def _():
    x = _detail_package()
    got = {}
    for frag in all_tags(x, "formField"):
        a = attrs(frag)
        ft = a.get("fieldType") or ""
        if ft not in _DETAIL_BIZ:
            continue
        got[a["name"]] = (ft, _opts(frag))

    assert {"mingCheng", "jinE", "zhuangTai", "fuZeRen", "beiZhu", "mingXi"} <= set(got), sorted(got)
    for name, (ft, o) in got.items():
        assert "disabled" not in o, \
            f"{name}（{ft}）写了 disabled：详情表单只用 read_rights 表达只读"
        if name == "beiZhu":
            assert o.get("read_rights") == "false", o
        else:
            assert o.get("read_rights") == "true", f"{name}（{ft}）缺 read_rights：{o}"

    # 容器与隐藏控件：原生一律不写 read_rights
    for frag in all_tags(x, "formField"):
        ft = attrs(frag).get("fieldType") or ""
        if ft in ("card", "fieldset", "grid_layout", "hidden"):
            assert "read_rights" not in _opts(frag), f"{ft} 容器写了 read_rights"

    # 表格控件：控件级只读拦不住子表，单元格与增删行要一起关（见 FRM25）
    assert got["mingXi"][0] == "tableField", got["mingXi"][0]
    cells = got["mingXi"][1].get("columns") or []
    assert cells, "表格控件没有列定义"
    assert all(c["editable"] is False for c in cells), cells

    # 布局内嵌的那份控件 JSON 也要一致（formField 与 grid cell 两处都写，缺一处不生效）
    grid = next(t for t in all_tags(x, "formField")
                if attrs(t).get("fieldType") == "grid_layout")
    g = json.loads(attrs(grid)["fieldOptions"].replace("&quot;", '"'))
    embedded = [w for row in g.get("rows") or []
                for cell in row.get("columns") or []
                for w in cell.get("fields") or []]
    assert embedded, "grid_layout 里没有内嵌控件"
    for w in embedded:
        if w.get("field_type") in _DETAIL_BIZ:
            assert (w.get("field_options") or {}).get("read_rights") in ("true", "false"), w


@case("FRM21", "form", "详情表单不带底部按钮（extendAttr.buttons 显式空数组）", "P0",
      ["buttons == []（原生两份详情表单都是空数组）",
       "不能只 pop：pop 掉会残留骨架里的三个按钮",
       "悬浮 moreBtn 也不出现，按钮位置在右上角"])
def _():
    x = _detail_package()
    head = attrs(all_tags(x, "formDef")[0])
    ext = json.loads(head["extendAttr"].replace("&quot;", '"'))
    assert ext.get("buttons") == [], f"详情表单还有按钮：{ext.get('buttons')}"
    # 必须是显式空数组，键要在（pop 掉会残留骨架里的三个按钮）
    assert '"buttons":[]' in head["extendAttr"].replace("&quot;", '"'), \
        "extendAttr 里没有显式的 \"buttons\":[]"
    assert "moreBtn" not in ext, "详情表单不该有悬浮更多按钮"
    assert ext.get("buttonPosition") == "topRight"
    assert ext.get("customClass") == "pp-details-sideslip"


@case("FRM22", "form", "新建表单不受详情只读化影响：不写 read_rights，仍保留三个标准按钮", "P0",
      ["新建表单不写 read_rights=true",
       "「只读=是」在新建表单里仍用 disabled",
       "底部按钮仍是 取消 / 保存并继续 / 保存，保存为蓝色主按钮"])
def _():
    x = form_only([
        ctrl("mingCheng"),
        {"表单key": "create_contract", "字段编码": "jinE", "显示标签": "金额",
         "控件类型": "number", "只读": "是"},
        {"表单key": "create_contract", "字段编码": "zhuangTai", "显示标签": "状态",
         "控件类型": "dictionary", "取值来源": "contract_zhuangTai"},
    ])
    ext = json.loads(attrs(all_tags(x, "formDef")[0])["extendAttr"].replace("&quot;", '"'))
    assert [b.get("alias") for b in ext.get("buttons") or []] == \
        ["close", "saveAndContinue", "save"], ext.get("buttons")
    save = next(b for b in ext["buttons"] if b.get("alias") == "save")
    assert save.get("style") == "primary"

    for frag in all_tags(x, "formField"):
        ft = attrs(frag).get("fieldType") or ""
        if ft not in _DETAIL_BIZ:
            continue
        o = _opts(frag)
        assert o.get("read_rights") != "true", f"{ft} 控件被写成了只读：{o}"
    hot = next(frag for frag in all_tags(x, "formField")
               if attrs(frag).get("name") == "jinE")
    assert _opts(hot).get("disabled") is True, "新建表单的「只读=是」丢了 disabled"


@case("FRM23", "form", "detail_expand（展开详情/流程详情）与详情表单同口径：只读 + 无按钮", "P0",
      ["detail_expand 的业务控件同样 read_rights=true、不写 disabled",
       "detail_expand 的 extendAttr.buttons 也是显式空数组",
       "detail_expand 与详情表单只有样式套件不同（class/标签宽度两套）",
       "form_item_eidt_mode 仍为 true（单行编辑模式按手册要开）"])
def _():
    """detail_expand 只能由反解产生（Excel 的「表单类型」没有这一项），
    所以这里直接驱动 gen_form.render —— 走的是出包时同一条生成路径。

    原生依据：PP6/表单/项目流程详情表单.zip →
    form/001-total_project_create_details.xml，extendAttr.buttons 是 []。
    """
    proj, errors, _ = project_of(xlsx({
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("contract", "名称", "字符串"),
                    field("contract", "金额", "数字", 精度=2),
                    field("contract", "状态", "选项", 选项值="草稿,生效")],
        S.S_FORM: [{"表单key": "expand_contract", "表单名称": "合同展开详情",
                    "对象编码": "contract", "表单类型": "详情表单"}],
        S.S_FORMFIELD: [
            {"表单key": "expand_contract", "字段编码": "mingCheng", "显示标签": "名称",
             "控件类型": "text"},
            {"表单key": "expand_contract", "字段编码": "jinE", "显示标签": "金额",
             "控件类型": "number"},
            {"表单key": "expand_contract", "字段编码": "zhuangTai", "显示标签": "状态",
             "控件类型": "dictionary", "取值来源": "contract_zhuangTai"},
        ],
    }))
    assert not errors, errors
    form, bo = proj.forms[0], proj.bos[0]
    snip = gen_form.Snippets(webui._snippets())

    got = {}
    for kind in ("detail", "detail_expand"):
        form.kind = kind
        xml, _notices = gen_form.render(form, bo, "1", snip)
        got[kind] = xml

    for kind, xml in got.items():
        for frag in all_tags(xml, "formField"):
            a = attrs(frag)
            if (a.get("fieldType") or "") not in _DETAIL_BIZ:
                continue
            o = _opts(frag)
            assert "disabled" not in o, f"{kind}/{a.get('name')} 写了 disabled：{o}"
            assert o.get("read_rights") == "true", \
                f"{kind}/{a.get('name')} 缺 read_rights：{o}"

        ext = json.loads(attrs(all_tags(xml, "formDef")[0])["extendAttr"]
                         .replace("&quot;", '"'))
        assert ext.get("buttons") == [], f"{kind} 还有按钮：{ext.get('buttons')}"
        assert ext.get("form_item_eidt_mode") is True, \
            f"{kind} 的单行编辑模式被关掉了：{ext.get('form_item_eidt_mode')}"

    # 两套样式不能混：展开详情是 pp-drawer-details + 120px
    styles = {kind: json.loads(attrs(all_tags(xml, "formDef")[0])["extendAttr"]
                               .replace("&quot;", '"'))
              for kind, xml in got.items()}
    assert styles["detail"]["customClass"] == "pp-details-sideslip"
    assert styles["detail_expand"]["customClass"] == "pp-drawer-details"
    assert styles["detail_expand"]["labelWidth"] == 120
    assert styles["detail_expand"]["compact"] is False, "展开详情的布局应是 expand"


@case("FRM24", "form", "反解按 extendAttr 认详情表单，重出后仍无按钮、全字段只读", "P0",
      ["原生「项目基本信息」名字里没有「详情」，也要反解成 detail",
       "判定按 customClass 前缀 / 单行编辑模式，表单名降为兜底",
       "展开详情（pp-drawer-details）按详情处理并给出提示",
       "反解→重出的表单 buttons == []、业务控件 read_rights=true"])
def _():
    import io as _io
    import tempfile
    import zipfile as _zf
    from pathlib import Path as _Path

    from pp6kit import template
    from pp6kit.inspect import inspect_path
    from tests._harness import ROOT, skip

    form_zip = ROOT / "PP6" / "表单" / "项目详情表单 1.zip"
    bo_zip = ROOT / "PP6" / "业务对象" / "项目.zip"
    if not (form_zip.is_file() and bo_zip.is_file()):
        skip("缺少原生样本（PP6/表单/项目详情表单 1.zip 或 PP6/业务对象/项目.zip）")

    # ① 只喂表单包：kind 必须是 detail（这份表单名是「项目基本信息」，
    #    customClass 是原生项目专有的 pp-projectOverview，靠 form_item_eidt_mode 认）
    tmp = _Path(tempfile.mkdtemp()) / "pkg.zip"
    tmp.write_bytes(form_zip.read_bytes())
    proj, notes = inspect_path(tmp)
    assert len(proj.forms) == 1, [f.key for f in proj.forms]
    form = proj.forms[0]
    assert form.key == "project_summary", form.key
    assert form.name == "项目基本信息", form.name
    assert form.kind == "detail", f"反解成了 {form.kind}（name={form.name}）"

    # ② 连对象包一起反解→重出：详情口径要活着（否则用户「反解→改→重出」全白改）
    both = _Path(tempfile.mkdtemp())
    (both / "bo.zip").write_bytes(bo_zip.read_bytes())
    (both / "form.zip").write_bytes(form_zip.read_bytes())
    proj2, _ = inspect_path(both)
    got = next(f for f in proj2.forms if f.key == "project_summary")
    assert got.kind == "detail", got.kind
    # 原生这份表单的隐藏规则引用了包里已不存在的控件，出包校验会拦；
    # 本用例只验「详情类型 → 按钮/只读」，先把隐藏规则摘掉。
    got.hide_rules = []
    body = _io.BytesIO()
    template.build_workbook(proj2).save(body)
    from pp6kit import webui

    zipped, _notices = webui.build_zip(body.getvalue())
    z = _zf.ZipFile(_io.BytesIO(zipped))
    names = [n for n in z.namelist() if n.startswith("form/")]
    assert len(names) == 1, names
    x = z.read(names[0]).decode()
    head = attrs(all_tags(x, "formDef")[0])
    ext = json.loads(head["extendAttr"].replace("&quot;", '"'))
    assert ext.get("buttons") == [], f"重出的详情表单还有按钮：{ext.get('buttons')}"
    assert ext.get("customClass") == "pp-details-sideslip", ext.get("customClass")
    biz = {attrs(frag).get("name"): _opts(frag) for frag in all_tags(x, "formField")
           if attrs(frag).get("fieldType") in _DETAIL_BIZ}
    assert biz, "重出的表单里没有业务控件"
    for name, o in biz.items():
        assert o.get("read_rights") == "true", f"{name} 没写 read_rights：{o}"
        assert "disabled" not in o, f"{name} 写了 disabled"

    # ③ 展开详情（pp-drawer-details）：spec 没有这一档，按详情处理并提示一次
    attr = json.dumps({"customClass": "pp-drawer-details",
                       "form_item_eidt_mode": True},
                      ensure_ascii=False).replace('"', "&quot;")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<form>\n  <formDefs>\n'
           f'    <formDef id="1" name="采购申请详情" key="detail_purchase" mode="bo"'
           f' status="deploy" typeId="1" formType="form" shared="Y" isMain="Y"'
           f' version="1" extendAttr="{attr}">\n'
           '      <formBo id="2" formId="1" boId="3" boCode="purchase"/>\n'
           '    </formDef>\n  </formDefs>\n</form>\n')
    tmp2 = _Path(tempfile.mkdtemp()) / "pkg.zip"
    with _zf.ZipFile(tmp2, "w") as zz:
        zz.writestr("form/001-detail_purchase.xml", xml)
    proj3, notes3 = inspect_path(tmp2)
    assert proj3.forms[0].kind == "detail", proj3.forms[0].kind
    assert any("展开详情" in n for n in notes3), notes3


@case("FRM25", "form", "详情表单子表只读：列不可编辑、不能增删行；新建表单逐字不变", "P0",
      ["详情表单 tableField：columns[].editable 全 False",
       "详情表单 tableField：operation 三个开关全 False",
       "configurableColumns 的 forbidConfig* 不动（那是设计器配置，不是运行时编辑）",
       "同一份列定义出新建表单时 editable/operation 全 True"])
def _():
    sheets = {
        S.S_BO: [{"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [
            field("contract", "名称", "字符串"),
            field("contract", "明细", "表格（table）"),
        ],
        S.S_TABLE_COL: [
            {"对象编码": "contract", "字段编码": "mingXi", "序号": 1,
             "列名": "摘要", "列类型": "字符串（varchar）"},
            {"对象编码": "contract", "字段编码": "mingXi", "序号": 2,
             "列名": "金额", "列类型": "数字（number）", "精度": 2},
        ],
        S.S_FORMFIELD: [
            {"表单key": "__FORM__", "字段编码": "mingXi", "显示标签": "明细",
             "控件类型": "tableField"},
        ],
    }

    def cell_opts(kind: str) -> dict:
        data = dict(sheets)
        data[S.S_FORM] = [{"表单key": "f1", "表单名称": "合同表单",
                           "对象编码": "contract", "表单类型": kind}]
        data[S.S_FORMFIELD] = [dict(r, 表单key="f1") for r in sheets[S.S_FORMFIELD]]
        x = pick(build(data), "form/")
        frag = next(t for t in all_tags(x, "formField")
                    if attrs(t).get("fieldType") == "tableField")
        return _opts(frag)

    for kind, editable, flags in (("详情表单", False, False), ("新建表单", True, True)):
        o = cell_opts(kind)
        cells = o.get("columns") or []
        assert cells, f"{kind}的表格控件没有列定义"
        assert all(c["editable"] is editable for c in cells), (kind, cells)
        op = o.get("operation") or {}
        assert op == {"addEnabled": flags, "deleteEnabled": flags, "dragEnabled": flags}, \
            (kind, op)
        # 设计器配置开关不跟着动
        cfg = o.get("configurableColumns") or []
        assert cfg and all(c["forbidConfigEditable"] is False for c in cfg), (kind, cfg)


@case("FRM26", "form", "反解判 kind 的顺序：条件专用_/导入专用_ 名字前缀优先于单行编辑模式", "P0",
      ["条件专用_XXX 且 form_item_eidt_mode=true → condition（不是 detail）",
       "导入专用_XXX 且 form_item_eidt_mode=true → import（不是 detail）",
       "没有名字前缀时，form_item_eidt_mode=true 仍判 detail（别把兜底一起改坏）"])
def _():
    """钉住判据**顺序**，这是 L1 修复里唯一会互相抢判的地方。

    `form_item_eidt_mode` 是为了兜住客户自定义类名的详情表单（原生
    「项目基本信息」customClass=pp-projectOverview）才引进来的通用开关；
    而「条件专用_」「导入专用_」这两个名字前缀是我们自己写死的强信号
    （gen_form 出包时就是这么命名的），不该被一个通用开关抢判。
    """
    import tempfile
    import zipfile as _zf
    from pathlib import Path as _Path

    from pp6kit.inspect import inspect_path

    def kind_of(name: str, ext: dict) -> str:
        attr = json.dumps(ext, ensure_ascii=False).replace('"', "&quot;")
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<form>\n  <formDefs>\n'
            f'    <formDef id="1" name="{name}" key="f1" mode="bo" status="deploy"'
            f' typeId="1" formType="form" shared="Y" isMain="Y" version="1"'
            f' extendAttr="{attr}">\n'
            '      <formBo id="2" formId="1" boId="3" boCode="purchase"/>\n'
            '    </formDef>\n'
            '    <formField id="11" formId="1" name="mingCheng" label="名称"'
            ' fieldType="text" fieldOptions="{&quot;required&quot;:&quot;false&quot;}"'
            ' sn="1"/>\n'
            '  </formDefs>\n</form>\n')
        tmp = _Path(tempfile.mkdtemp()) / "pkg.zip"
        with _zf.ZipFile(tmp, "w") as z:
            z.writestr("form/001-x.xml", xml)
        proj, _notes = inspect_path(tmp)
        assert len(proj.forms) == 1, [f.key for f in proj.forms]
        return proj.forms[0].kind

    # ① 名字前缀优先：自己写死的强信号不能被通用开关抢判
    assert kind_of("条件专用_采购申请", {"form_item_eidt_mode": True}) == "condition", \
        "「条件专用_」前缀被 form_item_eidt_mode 抢判成 detail 了"
    assert kind_of("导入专用_采购申请", {"form_item_eidt_mode": True}) == "import", \
        "「导入专用_」前缀被 form_item_eidt_mode 抢判成 detail 了"
    # 带到样式类也不该改变结论（生成侧条件/导入专用没有 class，这里是防将来加样式）
    assert kind_of("条件专用_采购申请",
                   {"form_item_eidt_mode": True, "customClass": "pp-details-sideslip"}) \
        == "condition"

    # ② 兜底不能被改坏：没有前缀时，单行编辑模式仍认详情
    assert kind_of("采购申请", {"form_item_eidt_mode": True}) == "detail"
    assert kind_of("项目基本信息", {"form_item_eidt_mode": True}) == "detail"

    # ③ 两者都没有 → 新建
    assert kind_of("采购申请", {}) == "create"
