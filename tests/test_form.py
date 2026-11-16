"""表单 XML：布局结构、控件属性、隐藏规则、脚本。

表单这层最容易「导入成功但打开是空白」：id 引用错一处，设计器里就什么都没有。
所以结构类断言直接复用 tools/check_form_layout.py 的契约检查。
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from pp6kit import spec as S
from pp6kit import webui
from tests._harness import (all_tags, attrs, case, field, pick, texts,
                            unzip, xlsx, xml_ok)
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
