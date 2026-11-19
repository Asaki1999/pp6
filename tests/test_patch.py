"""改包（增量补丁）：在已有 zip 上加字段、给已有字段改名称。

这组用例守的是「你改的东西真的进了新 zip」，以及**改不到的东西真的没被改**：
  1. skip 模式下同名编码被跳过，产物和原包逐字节一致却报告「已加上」；
  2. 已有字段只允许改 name —— 类型/选项/长度/必填一律不生效（2026-11-18 口径）；
  3. 已有字段不能被删除（收到删除意图必须报错，不能静默忽略）。
"""
from __future__ import annotations

import io
import re
import zipfile

from pp6kit import patch
from tests._harness import (all_tags, attrs, case, native_zip, pick, texts,
                            unzip, xml_ok)


def snapshot(name: str = "项目.zip") -> dict:
    return patch.inspect_package(native_zip(name))


def first_object(name: str = "项目.zip") -> dict:
    return snapshot(name)["objects"][0]


def add_field(fields: list[dict], mode: str = "skip", scope: str = "full",
              src: str = "项目.zip"):
    raw = native_zip(src)
    obj = patch.inspect_package(raw)["objects"][0]
    spec = {"targets": [obj["file"]], "scope": scope, "mode": mode, "fields": fields}
    body, pkg_name, report = patch.build_patch(raw, spec)
    return raw, body, pkg_name, report, obj


def row_of(text: str, code: str) -> dict:
    return attrs(next(t for t in all_tags(text, "attribute")
                      if attrs(t).get("code") == code))


@case("PAT01", "patch", "上传原包能列出对象与字段数", "P0",
      ["项目 51 个字段", "表名 pp_project"])
def _():
    snap = snapshot()
    assert snap["ok"]
    obj = snap["objects"][0]
    assert obj["code"] == "project"
    assert obj["tbl"] == "pp_project"
    assert obj["fieldCount"] == 51
    assert obj["fields"][0]["code"] == "name"


@case("PAT02", "patch", "新加字段真的进新包", "P0",
      ["字段数 +1", "XML 里能找到"])
def _():
    _, body, _, report, obj = add_field(
        [{"code": "riskLevel", "name": "风险等级", "type": "varchar", "length": 50}])
    text = texts(body)[obj["file"]]
    assert "riskLevel" in text
    detail = report["changed"][obj["file"]]
    assert detail["fieldCountBefore"] == 51
    assert detail["fieldCountAfter"] == 52


@case("PAT03", "patch", "新字段序号接在已有最大 sn 之后", "P0",
      ["不撞已有序号", "撞了设计器会错位"])
def _():
    _, body, _, _, obj = add_field(
        [{"code": "riskLevel", "name": "风险等级", "type": "varchar"}])
    text = texts(body)[obj["file"]]
    sns = [int(a["sn"]) for a in (attrs(t) for t in all_tags(text, "attribute")) if "sn" in a]
    assert len(sns) == len(set(sns)), "有重复 sn"
    assert row_of(text, "riskLevel")["sn"] == "52"


@case("PAT04", "patch", "新字段物理列名带尾下划线", "P0",
      ["手册要求", "不带会导致建表失败"])
def _():
    _, body, _, _, obj = add_field(
        [{"code": "riskLevel", "name": "风险等级", "type": "varchar"}])
    col = row_of(texts(body)[obj["file"]], "riskLevel")["fieldName"]
    assert col.endswith("_"), col


@case("PAT05", "patch", "默认「跳过」模式下同名编码不生效并报错", "P0",
      ["以前会给你一份和原包一模一样的假包"])
def _():
    raw = native_zip()
    obj = patch.inspect_package(raw)["objects"][0]
    spec = {"targets": [obj["file"]], "scope": "full", "mode": "skip",
            "fields": [{"code": "name", "name": "项目名称", "type": "clob"}]}
    try:
        patch.build_patch(raw, spec)
    except ValueError as exc:
        assert "没有产生任何改动" in str(exc) or "跳过" in str(exc), exc
    else:
        raise AssertionError("同名编码被静默跳过还出了包")


@case("PAT06", "patch", "已有字段只改名称，类型/长度一概不动", "P0",
      ["varchar→clob 的请求被忽略", "报告里逐条说明已忽略"])
def _():
    raw = native_zip()
    obj = patch.inspect_package(raw)["objects"][0]
    before = row_of(texts(raw)[obj["file"]], "name")
    _, body, _, report, obj = add_field(
        [{"code": "name", "name": "项目名称X", "type": "clob", "length": 999}],
        mode="rename")
    after = row_of(texts(body)[obj["file"]], "name")
    assert after["name"] == "项目名称X"
    assert after["dataTypeNew"] == before["dataTypeNew"], (before, after)
    assert after.get("length") == before.get("length"), (before, after)
    changed = report["changed"][obj["file"]]["replaced"][0]
    assert changed["rename"] is True and changed["attrs"] == ["name"], changed
    warns = " ".join(report["changed"][obj["file"]]["warnings"])
    assert "只支持改名称" in warns, warns


@case("PAT07", "patch", "改名只动 name 那一段，其余属性逐字节不变", "P0",
      ["物理列名不动", "没建模的属性不能被重写"])
def _():
    raw = native_zip()
    obj = patch.inspect_package(raw)["objects"][0]
    before = texts(raw)[obj["file"]]
    _, body, _, _, obj = add_field(
        [{"code": "name", "name": "项目名称X"}], mode="rename")
    after = texts(body)[obj["file"]]

    def frag(text: str) -> str:
        return next(t for t in all_tags(text, "attribute")
                    if attrs(t).get("code") == "name")

    old, new = frag(before), frag(after)
    assert new == old.replace('name="项目名称"', 'name="项目名称X"'), \
        f"除 name 外有别的变化：\n旧 {old}\n新 {new}"


@case("PAT08", "patch", "改名保留原 sn，不重排", "P1",
      ["重排会让表单控件对不上字段"])
def _():
    raw = native_zip()
    obj = patch.inspect_package(raw)["objects"][0]
    before = row_of(texts(raw)[obj["file"]], "name")["sn"]
    _, body, _, _, obj = add_field(
        [{"code": "name", "name": "项目名称X"}], mode="rename")
    after = row_of(texts(body)[obj["file"]], "name")["sn"]
    assert before == after, (before, after)


@case("PAT09", "patch", "已有选项字段的候选值不给改，字典一字不动", "P0",
      ["选项值属于「其他信息」", "请求被忽略并提示"])
def _():
    raw = native_zip("选项.zip")
    obj = patch.inspect_package(raw)["objects"][0]
    field = next(f for f in obj["fields"] if f["type"] == "dic")
    spec = {"targets": [obj["file"]], "scope": "full", "mode": "rename",
            "fields": [{"code": field["code"], "name": "改名后的选项字段",
                        "type": "dic", "choices": "甲,乙,丙,新选项"}]}
    body, _, report = patch.build_patch(raw, spec)
    text = texts(body)[obj["file"]]
    assert "新选项" not in text, "候选值被改了"
    assert row_of(text, field["code"])["name"] == "改名后的选项字段"
    warns = " ".join(report["changed"][obj["file"]]["warnings"])
    assert "选项值" in warns, warns


@case("PAT10", "patch", "删除已有字段被明确拒绝", "P0",
      ["不能静默忽略", "用户会以为字段被删了"])
def _():
    raw = native_zip()
    obj = patch.inspect_package(raw)["objects"][0]
    for spec in (
        {"targets": [obj["file"]], "mode": "rename", "fields": [],
         "deleteFields": ["riskLevel"]},
        {"targets": [obj["file"]], "mode": "rename",
         "fields": [{"code": "name", "delete": True}]},
    ):
        try:
            patch.build_patch(raw, spec)
        except ValueError as exc:
            assert "删除" in str(exc), exc
        else:
            raise AssertionError("删除意图被静默忽略")


@case("PAT11", "patch", "新增选项字段同时生成字典类型与候选值", "P0",
      ["只有属性行没有字典=空下拉"])
def _():
    _, body, _, report, obj = add_field(
        [{"code": "riskLevel", "name": "风险等级", "type": "dic",
          "choices": "高,中,低"}])
    text = texts(body)[obj["file"]]
    assert "dictionaryTypeList" in text
    assert re.search(r'typeKey="project_riskLevel"', text), "没有字典类型"
    names = re.findall(r'<tns:dictionary [^>]*name="([^"]+)"', text)
    assert {"高", "中", "低"} <= set(names), names


@case("PAT12", "patch", "预览改动不产生 zip", "P1",
      ["只看报告再决定要不要下"])
def _():
    raw = native_zip()
    obj = patch.inspect_package(raw)["objects"][0]
    spec = {"targets": [obj["file"]], "scope": "full", "mode": "skip",
            "fields": [{"code": "riskLevel", "name": "风险等级", "type": "varchar"}]}
    res = patch.dry_run(raw, spec)
    assert res["ok"]
    assert res["changed"][obj["file"]]["added"][0]["code"] == "riskLevel"


@case("PAT13", "patch", "最小增量包只留 bo 目录", "P1",
      ["导入时不碰表单和列表"])
def _():
    raw = native_zip("多个对象整体导出.zip")
    snap = patch.inspect_package(raw)
    obj = snap["objects"][0]
    spec = {"targets": [obj["file"]], "scope": "bo-only", "mode": "skip",
            "fields": [{"code": "riskLevel", "name": "风险等级", "type": "varchar"}]}
    body, _, report = patch.build_patch(raw, spec)
    names = list(unzip(body))
    assert all(n.startswith("bo/") for n in names), names
    assert report["droppedEntries"] >= 0


@case("PAT14", "patch", "非目标文件按原字节原样复制", "P0",
      ["改一个对象不能碰另一个"])
def _():
    raw = native_zip("多个对象整体导出.zip")
    snap = patch.inspect_package(raw)
    if len(snap["objects"]) < 2:
        from tests._harness import skip
        skip("样本包只有一个对象")
    target = snap["objects"][0]["file"]
    other = snap["objects"][1]["file"]
    spec = {"targets": [target], "scope": "full", "mode": "skip",
            "fields": [{"code": "riskLevel", "name": "风险等级", "type": "varchar"}]}
    body, _, _ = patch.build_patch(raw, spec)
    before = unzip(raw)
    after = unzip(body)
    assert before[other] == after[other], f"{other} 被改动了"


@case("PAT15", "patch", "改完的字段表能回写成 Excel", "P1",
      ["给不熟悉 XML 的人核对"])
def _():
    raw = native_zip()
    obj = patch.inspect_package(raw)["objects"][0]
    spec = {"targets": [obj["file"]], "scope": "full", "mode": "skip",
            "fields": [{"code": "riskLevel", "name": "风险等级", "type": "varchar"}]}
    body, name = patch.patched_workbook(raw, spec)
    assert body[:2] == b"PK", "不是 xlsx"
    assert name.endswith(".xlsx"), name


@case("PAT16", "patch", "新增字段表模板可下载且能被解析", "P1",
      ["下载→填写→导入这条路要通"])
def _():
    from tests._harness import read_xlsx_rows

    tpl = patch.fields_template_bytes()
    assert tpl[:2] == b"PK"
    rows = read_xlsx_rows(tpl, "新增字段")
    assert rows, "模板里没有字段表"
    assert any("字段编码" in str(k) for k in rows[0]), rows[0]


@case("PAT17", "patch", "目标文件不存在时明确报错", "P1",
      ["别静默出个没改的包"])
def _():
    raw = native_zip()
    spec = {"targets": ["bo/999-nope.xml"], "scope": "full", "mode": "skip",
            "fields": [{"code": "x", "name": "X", "type": "varchar"}]}
    try:
        patch.build_patch(raw, spec)
    except Exception as exc:
        assert any(k in str(exc) for k in ("不存在", "没有", "找不到")), exc
    else:
        raise AssertionError("目标不存在还出了包")


@case("PAT18", "patch", "产物包结构合法且可完整解压", "P0",
      ["条目名不重复", "每个 XML 可解析"])
def _():
    _, body, _, _, _ = add_field(
        [{"code": "riskLevel", "name": "风险等级", "type": "varchar"}])
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        assert z.testzip() is None
        names = z.namelist()
    assert len(names) == len(set(names)), names
    for text in texts(body).values():
        xml_ok(text)


@case("PAT19", "patch", "产物名写明做了什么", "P2",
      ["项目-加字段-日期.zip", "一眼看出不是重出的整包"])
def _():
    _, _, name, _, _ = add_field(
        [{"code": "riskLevel", "name": "风险等级", "type": "varchar"}])
    assert "加字段" in name, name


@case("PAT20", "patch", "改已有字段名的产物名写「改名称」", "P2",
      ["与加字段区分"])
def _():
    _, _, name, _, _ = add_field(
        [{"code": "name", "name": "项目名称X"}], mode="rename")
    assert "改名称" in name, name


@case("PAT21", "patch", "对齐行强制覆盖定义类键（isindex N→Y 也要生效，列名不动）",
      "P0", ["align=True", "fieldName/sn 不变"])
def _():
    from pp6kit.model import BoField

    old = {"code": "code", "fieldName": "contract_no_", "name": "项目编号",
           "datatype": "varchar", "dataTypeNew": "varchar", "isnull": "Y",
           "isindex": "N", "sn": "3", "length": "50"}
    field = BoField(bo_code="contract", code="code", name="项目编号",
                    type_new="varchar", length=64, is_index=True, is_null=True)
    line, changed, old_type = patch._merge_attr_line(old, {}, field, "tns:", align=True)
    got = attrs(line)
    assert got["isindex"] == "Y", got          # 原值是 N，对齐要能翻过来
    assert got["length"] == "64", got
    assert got["fieldName"] == "contract_no_", got   # 物理列名铁律
    assert got["sn"] == "3", got                     # 行序号不动
    assert old_type == "varchar"
    assert "isindex" in changed
