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


@case("BLD06", "builder", "只选表单时不带业务对象", "P0", ["codes=form"])
def _():
    body, _ = build(one_form_package(), codes=["form"])
    names = list(unzip(body))
    assert len(names) == 1 and names[0].startswith("form/"), names


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
