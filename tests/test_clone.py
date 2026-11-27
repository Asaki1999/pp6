"""克隆现有业务对象（v7，2026-11-27）。

守三条：
  1. **字段定义来自 zip 本身**，不是大模型转写的——长度/候选值/级联/子表列
     必须逐键等于源对象，任何一项靠自然语言描述都会丢；
  2. 新编码必须与原对象不同（相同就是拿克隆覆盖线上对象）；
  3. 撞库（code_taken）在克隆链路上不可绕过。
"""
from __future__ import annotations

from pathlib import Path

from pp6kit import webui
from tests._harness import case, skip

ROOT = Path(__file__).resolve().parents[1]
SRC_ZIP = ROOT / "PP6" / "业务对象" / "项目.zip"


def _zip_bytes() -> bytes:
    if not SRC_ZIP.is_file():
        skip("没有 PP6/业务对象/项目.zip：克隆用例跳过")
    return SRC_ZIP.read_bytes()


def _src_fields() -> list[dict]:
    """从 zip 里按另一个入口解析一遍字段，作为「基准」逐键对比。"""
    from pp6kit import projlink

    snap = projlink.snapshot_from_zip(_zip_bytes(), "package_upload",
                                      projlink.TargetObject(code="project"))
    return [{"code": f.code, "name": f.name, "type": f.type_new,
             "fieldName": f.field_name, "objectType": f.object_type,
             "length": f.length} for f in snap.fields]


@case("CLONE01", "clone", "克隆预检：zip → 对象清单（名称/字段数/流程标记/建议编码）", "P0",
      ["多对象包全列；单对象包给建议名称与编码",
       "流程对象带 flow=True（前端显示「流程」标签）",
       "没有 bo/*.xml 的 zip 报明确错误，不返回空清单"])
def inspect_zip_objects():
    out = webui.clone_inspect(_zip_bytes())
    assert out["ok"] is True
    objs = out["objects"]
    assert objs, out
    top = objs[0]
    assert top["code"] == "project", top
    assert top["name"] == "项目", top
    assert top["fields"] > 0, top
    assert top["suggestedName"] and top["suggestedCode"], top
    assert top["suggestedCode"] != top["code"], "建议编码不能和源对象相同"
    assert isinstance(top["flow"], bool), top

    # 不是对象包：明确报错（前端把它显示在弹窗里，不能静默给空列表）
    try:
        webui.clone_inspect(b"not a zip")
        raise AssertionError("坏 zip 没报错")
    except ValueError as exc:
        assert "zip" in str(exc).lower() or "业务对象" in str(exc), exc


@case("CLONE02", "clone", "字段逐键复制：长度/类型/物理列/关联对象与源对象完全一致", "P0",
      ["新对象的字段数 = zip 里该对象的字段数",
       "code / name / type / fieldName / objectType / length 逐键相等",
       "名称与编码是用户填的，cloneFrom 指回原对象"])
def clone_fields_copied_verbatim():
    src = _src_fields()
    out = webui.clone_apply(_zip_bytes(), "project", "项目变更二期", "projchange2",
                            with_form=False, with_list=False)
    bo = out["bo"]
    assert bo["name"] == "项目变更二期", bo["name"]
    assert bo["code"] == "projchange2", bo["code"]
    assert bo["cloneFrom"] == {"name": "项目", "code": "project"}, bo.get("cloneFrom")

    got = [{"code": f["code"], "name": f["name"], "type": f["type"],
            "fieldName": f["fieldName"], "objectType": f.get("objectType", ""),
            "length": f.get("length")} for f in bo["fields"]]
    assert len(got) == len(src), (len(got), len(src))
    for a, b in zip(got, src):
        if a["type"] == "dic" and b["type"] == "dic":
            # 字典 key 是 <对象编码>_<字段编码>：新对象编码变了，key 必须重建，
            # 否则指向原对象的私有候选值（导入后是空下拉）。这里断言「换成了新前缀」。
            assert a["objectType"].startswith("projchange2"), a
            a = dict(a, objectType=b["objectType"])
        assert a == b, f"字段被改动了：{a} != {b}"

    # 返回结构与 /api/ai/bo 同构（前端只认一套）
    for key in ("ok", "bo", "form", "list", "notices", "steps", "types",
                "objects", "objectSource", "existObject", "relationships", "model"):
        assert key in out, f"缺 {key}（与 /api/ai/bo 不同构）"
    assert out["model"] == "clone", out["model"]


@case("CLONE03", "clone", "克隆红线：同编码覆盖 / 非法编码 / 撞库 一律拦下", "P0",
      ["新编码 == 源编码 → 报错（相同编码导入会覆盖原对象）",
       "编码格式不合法 → 报错",
       "code_taken 命中 → 报错（服务端最终校验，前端绕过也拦得住）"])
def clone_rejects_dangerous_codes():
    cases = [
        (("project", "project"), "不能和原对象一样"),
        (("project", "Bad Code"), "编码不合法"),
        (("project", "1abc"), "编码不合法"),
    ]
    for (src, code), want in cases:
        try:
            webui.clone_apply(_zip_bytes(), src, "测试对象", code,
                              with_form=False, with_list=False)
            raise AssertionError(f"{code} 没被拦下")
        except ValueError as exc:
            assert want in str(exc), (code, str(exc))

    # 撞库：让 objects.code_taken 命中一条，克隆必须失败
    from pp6kit import objects

    real = objects.code_taken
    objects.code_taken = lambda code: {"code": code, "name": "已存在的对象", "id": "1"}
    try:
        try:
            webui.clone_apply(_zip_bytes(), "project", "测试对象", "projchange9",
                              with_form=False, with_list=False)
            raise AssertionError("撞库没被拦下")
        except ValueError as exc:
            assert "占用" in str(exc) or "已经" in str(exc), exc
    finally:
        objects.code_taken = real


@case("CLONE04", "clone", "流程对象：流程冗余字段照带，并提示流程图不克隆", "P1",
      ["源对象 flow_driven=True 时新对象也标流程",
       "notices 里说明「流程图不克隆，去新建流程配置重画」"])
def clone_flow_object_keeps_fields():
    real_flow = webui._clone_src_flow
    webui._clone_src_flow = lambda payload, code: True
    try:
        out = webui.clone_apply(_zip_bytes(), "project", "流程对象副本", "projflowcopy",
                                with_form=False, with_list=False)
    finally:
        webui._clone_src_flow = real_flow
    notes = " ".join(out["notices"])
    assert "流程冗余字段已带上" in notes, notes
    assert "流程图不克隆" in notes, notes
    assert out["bo"]["fields"], out["bo"]


@case("CLONE05", "clone", "对象包没有 dictionaryTypeList 也能解析（多对象整体导出）", "P0",
      ["inspect_path 对缺失的 dictionaryTypeList 不能 TypeError",
       "「多个对象整体导出.zip」三个对象全列出来",
       "以前整包被记成「解析失败」，界面说「没有业务对象」"])
def parse_zip_without_dictionary_list():
    from pp6kit import webui as w

    multi = ROOT / "PP6" / "业务对象" / "多个对象整体导出.zip"
    if not multi.is_file():
        skip("没有 多个对象整体导出.zip：跳过")
    out = w.clone_inspect(multi.read_bytes())
    codes = [o["code"] for o in out["objects"]]
    assert len(codes) >= 3, codes
    assert all(o["fields"] > 0 for o in out["objects"]), out["objects"]
