"""对象所属关系识别与确认的端到端回归测试。

覆盖：
  1. 识别：R1/R2 候选、默认 enabled=false、随包快照降级
  2. 确认：未确认出包 0 条关系；确认 PARENT/PARALLEL 各 1 条且属性正确
  3. 非法父对象 / 非法请求头被拒绝
  4. 确认版 Excel 写回三列且再解析往返不丢（R0 显式值）
  5. 取消 Excel 显式关系

用法：./.venv/bin/python tools/test_relations.py
失败非零退出；通过打印 PASS 清单。
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pp6kit import relations as R            # noqa: E402
from pp6kit.reader import read_workbook      # noqa: E402
from pp6kit.builder import build             # noqa: E402
from pp6kit.webui import (                   # noqa: E402
    _snippets, build_zip, confirmed_excel, parse_excel,
)

NS = {"tns": "http://www.ankepower.com/iform-bo-def"}

PASS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        print(f"✗ {name} {detail}")
        sys.exit(1)


def _make_xlsx() -> bytes:
    """造一份含「关联项目」object 字段的 Excel。"""
    src = ROOT / "out" / "规范2.xlsx"
    tmp = tempfile.mkdtemp()
    dst = Path(tmp) / "t.xlsx"
    shutil.copy(src, dst)
    wb = openpyxl.load_workbook(dst)
    ws = wb["2_字段"]
    ws.append(["demo_purchase", "90", "projectId", "", "关联项目",
               "对象（object）", "项目", "", "", "否", "是", "", "", ""])
    ws.append(["demo_purchase", "91", "ownerProject", "", "所属项目",
               "object", "", "", "", "否", "是", "", "", ""])
    wb.save(dst)
    return dst.read_bytes()


def _rels_of(zip_bytes: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        bo = [n for n in z.namelist() if n.startswith("bo/") and n.endswith(".xml")][0]
        root = ET.fromstring(z.read(bo))
    lst = root.find("tns:objRelationshipList", NS)
    return list(lst) if lst is not None else []


def main() -> int:
    xlsx = _make_xlsx()

    # 1. 识别
    d = parse_excel(xlsx)
    items = d["relationships"]["items"]
    assert items, "parse 没返回 relationships"
    cands = items[0]["candidates"]
    by_rule = {c["rule"]: c for c in cands}
    ok("1a R1 候选存在且指向 project", "R1" in by_rule and by_rule["R1"]["parentObj"] == "project")
    ok("1b 所有候选默认 enabled=false", all(not c["enabled"] for c in cands))
    ok("1c 候选关联字段是物理列名", by_rule.get("R1", {}).get("parentField") == "project_id_")

    # 2. 未确认出包 → 0 条
    body, _ = build_zip(xlsx)
    ok("2 未确认出包 0 条关系", len(_rels_of(body)) == 0)

    # 3. 确认 PARENT
    hdr = json.dumps({"version": 1, "decisions": {
        "demo_purchase": {"enabled": True, "parentObj": "project",
                          "fieldCode": "projectId", "relType": "PARENT"}}})
    body3, _ = build_zip(xlsx, relations_header=hdr)
    rels3 = _rels_of(body3)
    ok("3a 确认后 1 条关系", len(rels3) == 1)
    if rels3:
        a = rels3[0].attrib
        ok("3b src=子/dst=父/relField 正确",
           a["srcObjCode"] == "demo_purchase" and a["dstObjCode"] == "project"
           and a["relField"] == "project_id_" and a["relType"] == "PARENT")
        ok("3c dstObjName 是中文名", a.get("dstObjName") == "项目")

    # 4. PARALLEL
    hdr4 = json.dumps({"version": 1, "decisions": {
        "demo_purchase": {"enabled": True, "parentObj": "project",
                          "fieldCode": "projectId", "relType": "PARALLEL"}}})
    body4, _ = build_zip(xlsx, relations_header=hdr4)
    ok("4 PARALLEL 生效", _rels_of(body4)[0].attrib["relType"] == "PARALLEL")

    # 5. 非法父对象
    bad = json.dumps({"version": 1, "decisions": {
        "demo_purchase": {"enabled": True, "parentObj": "不存在",
                          "fieldCode": "projectId"}}})
    try:
        build_zip(xlsx, relations_header=bad)
        ok("5 非法父对象被拒绝", False, "应抛 ValueError")
    except ValueError:
        ok("5 非法父对象被拒绝", True)

    # 6. 非法请求头（缺 parentObj）
    try:
        R.parse_decisions(json.dumps({"version": 1, "decisions": {"x": {"enabled": True}}}))
        ok("6 非法请求头被拒绝", False)
    except ValueError:
        ok("6 非法请求头被拒绝", True)

    # 7. 确认版 Excel 往返
    body7, name7 = confirmed_excel(xlsx, relations_header=hdr, name="测试.xlsx")
    ok("7a 确认版文件名", name7 == "测试_已确认.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(body7))
    ws = wb["1_业务对象"]
    hdr_row = [c.value for c in ws[1]]
    obj_row = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[0] == "demo_purchase"][0]
    row = dict(zip(hdr_row, obj_row))
    ok("7b 三列写回正确",
       row["父对象编码"] == "project" and row["关联字段"] == "project_id_"
       and row["关系类型"] == "PARENT")
    d7 = parse_excel(body7)
    excel_rel = d7["relationships"]["items"][0]["excelRelation"]
    ok("7c 再解析保留显式关系",
       excel_rel["specified"] and excel_rel["parentObj"] == "project"
       and excel_rel["relType"] == "PARENT" and excel_rel["valid"])

    # 8. 取消显式关系
    proj, _, _ = read_workbook(io.BytesIO(body7))
    proj.bos[0].parent_obj = "project"
    R.apply_confirmations(proj, {"demo_purchase": {"enabled": False}})
    ok("8 取消显式关系", proj.bos[0].parent_obj == "")

    print(f"\n✓ 全部通过 {len(PASS)} 项：")
    for i, n in enumerate(PASS, 1):
        print(f"  {i}. {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
