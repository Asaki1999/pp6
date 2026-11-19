"""「已有包加字段」回归检查：在原 zip 上打补丁，逐项核对保真度。

用法：
    python tools/check_patch.py                       # 跑内置的原生推送包样本
    python tools/check_patch.py 路径/到/导出包.zip      # 追加自己的包

验收口径（2026-11-11 逆向系统推送工作区产物时定下）：
    1. 没被点名的 zip 条目**字节**完全一致，条目顺序 / 时间戳 / 压缩方式不变；
    2. 被改的 bo XML 只允许插入新字段，或把已有字段的 name 定点替换
       （rename 模式：除 name 外零字节变化），不能出现意外删除、
       不能把单行原生 XML 重新格式化；
    3. 补丁后 XML 可解析、字段 code 不重复、新 sn 不撞已有 sn；
    4. 选项字段的字典类型 / 候选值 / attrs 样式一并就位；
    5. comment 仍是 CONFIG_SYNC_NOTE: 格式；
    6. dry-run 报告与真实出包一致（同一套算法，不许漂移）。
"""
from __future__ import annotations

import difflib
import io
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pp6kit import patch  # noqa: E402

_FAILS: list[str] = []
_CHECKS = 0


def ok(cond, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if cond:
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label}")
        _FAILS.append(label)


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def attrs_of(text: str) -> list[dict]:
    root = ET.fromstring(text)
    ent = next(c for c in root if local(c.tag) == "bodefentity")
    return [dict(c.attrib) for c in ent if local(c.tag) == "attribute"]


def diff_tags(old: str, new: str) -> set[str]:
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return {tag for tag, *_ in sm.get_opcodes() if tag != "equal"}


def run(path: Path) -> None:
    print(f"\n=== {path.name} ===")
    raw = path.read_bytes()
    snap = patch.inspect_package(raw)
    ok(bool(snap["objects"]), f"读到 {len(snap['objects'])} 份业务对象 XML")
    if not snap["objects"]:
        return
    target = snap["objects"][0]
    fields = [
        {"code": "chkMemo", "name": "检查备注", "type": "字符串", "length": 200},
        {"code": "chkLevel", "name": "检查分级", "type": "选项", "choices": "高,中,低"},
    ]

    # ---- 1. 整包补丁
    spec = {"targets": [target["file"]], "fields": fields,
            "mode": "skip", "scope": "full"}
    blob, name, report = patch.build_patch(raw, spec)
    print(f"  产物 {name}")
    with zipfile.ZipFile(io.BytesIO(raw)) as a, \
            zipfile.ZipFile(io.BytesIO(blob)) as b:
        ok(a.namelist() == b.namelist(), "zip 条目与顺序不变")
        unchanged = [n for n in a.namelist()
                     if n != target["file"] and a.read(n) == b.read(n)]
        ok(len(unchanged) == len(a.namelist()) - 1, "未点名的条目字节完全一致")
        meta = all(
            (x.date_time, x.compress_type, x.external_attr,
             x.create_system, x.extract_version) ==
            (y.date_time, y.compress_type, y.external_attr,
             y.create_system, y.extract_version)
            for x, y in zip(a.infolist(), b.infolist()))
        ok(meta, "ZipInfo 元数据（时间/压缩/权限/版本）沿用原包")
        # flag_bits 里的 data descriptor 位由写入方式决定，不比较；
        # 但非 ASCII 文件名必须保住 UTF-8 标志，否则解压出乱码名
        utf8 = all(
            all(ord(c) < 128 for c in x.filename) or bool(y.flag_bits & 0x800)
            for x, y in zip(a.infolist(), b.infolist()))
        ok(utf8, "非 ASCII 文件名的 UTF-8 标志保留")
        ok(b.testzip() is None, "新包可完整解压（CRC 校验）")
        ok(b.comment.startswith(b"CONFIG_SYNC_NOTE:"), "comment 为 CONFIG_SYNC_NOTE 格式")

        old = a.read(target["file"]).decode("utf-8")
        new = b.read(target["file"]).decode("utf-8")
        tags = diff_tags(old, new)
        ok(tags <= {"insert"}, f"XML 差异只有插入（实际 {sorted(tags)}）")
        before, after = attrs_of(old), attrs_of(new)
        ok(len(after) == len(before) + 2, f"字段 {len(before)} → {len(after)}")
        codes = [x.get("code", "") for x in after]
        ok(len(codes) == len(set(codes)), "字段编码无重复")
        old_sn = {int(x["sn"]) for x in before if x.get("sn", "").isdigit()}
        new_sn = [int(x["sn"]) for x in after if x.get("sn", "").isdigit()]
        added_sn = [s for s in new_sn if s not in old_sn]
        ok(len(added_sn) == 2 and len(set(new_sn) - old_sn) == 2,
           f"新增 sn 不撞已有 sn（新增 {added_sn}）")
        got = {x["code"]: x for x in after}
        ok(got["chkMemo"]["fieldName"] == "chk_memo_" and
           got["chkMemo"]["dataTypeNew"] == "varchar" and
           got["chkMemo"]["sn"] == str(max(new_sn) - 1), "文本字段属性与生成器口径一致")
        ok('"isDefault":false' in got["chkMemo"].get("options", "").replace("&quot;", '"'),
           "新字段带 isDefault 开关")
        ok(got["chkLevel"]["dataTypeNew"] == "dic" and
           got["chkLevel"]["objectType"] == f"{target['code']}_chkLevel",
           "选项字段的 objectType 指向自建字典")

        root = ET.fromstring(new)
        dtypes = [d.attrib for c in root if local(c.tag) == "dictionaryTypeList"
                  for d in c if local(d.tag) == "dictionaryType"]
        ditems = [d for c in root if local(c.tag) == "dictionaryList"
                  for d in c if local(d.tag) == "dictionary"]
        key = f"{target['code']}_chkLevel"
        hit_type = [d for d in dtypes if d.get("typeKey") == key]
        hit_items = [d for d in ditems if d.get("typeKey") == key]
        ok(len(hit_type) == 1 and hit_type[0].get("typeScope") == "BO_ATTR",
           "字典类型 typeScope=BO_ATTR（自定义）")
        ok(len(hit_items) == 3 and
           [d.get("name") for d in hit_items] == ["高", "中", "低"], "三个候选值按顺序写入")
        ok(all(any(local(x.tag) == "attrs" and x.text and "backgroundColor" in x.text
                   for x in d) for d in hit_items),
           "每个选项都带 attrs 展示样式")

    ok(report["keptEntries"] == snap["entryCount"] and report["droppedEntries"] == 0,
       "full 模式保留全部条目")
    dry = patch.dry_run(raw, spec)
    ok(dry["changed"] == report["changed"] and dry["report"].splitlines()[3:] ==
       report["report"].splitlines()[3:], "dry-run 与真实出包报告一致")

    # ---- 2. 最小增量包
    small, _, rep2 = patch.build_patch(raw, {**spec, "scope": "bo-only"})
    with zipfile.ZipFile(io.BytesIO(small)) as z:
        names = z.namelist()
        ok(all(n.startswith("bo/") and n.endswith(".xml") for n in names),
           "bo-only 只保留 bo/*.xml")
        ok(rep2["droppedEntries"] == snap["entryCount"] - len(names),
           "被丢弃条目数与报告一致")

    # ---- 3. 重名处理
    # 挑一个普通字段做重名测试，别挑到 id / version 这类系统内置字段
    existing_code = next(
        (f["code"] for f in target["fields"]
         if f["code"] not in patch._RESERVED_CODES and f["code"]), "")
    dup = [{"code": existing_code, "name": "同名字段", "type": "varchar"}] \
        if existing_code else []
    if dup:
        # 一个同名字段 + 一个新字段：同名被跳过，新字段照常进包
        mixed = dup + [{"code": "chkNew", "name": "检查新字段", "type": "varchar"}]
        _, _, rep3 = patch.build_patch(raw, {**spec, "fields": mixed})
        detail = rep3["changed"][target["file"]]
        ok(bool(detail["skipped"]) and any(a["code"] == "chkNew" for a in detail["added"]),
           "skip 模式：同名已有字段被跳过，新字段照常写入")
        # 只有同名字段 + skip → 产物和原包一模一样，必须报错而不是给一份假包
        try:
            patch.build_patch(raw, {**spec, "fields": dup})
            ok(False, "纯同名 + skip 应当报错")
        except ValueError as exc:
            ok("没有产生任何改动" in str(exc),
               "纯同名 + skip 明确报「没有产生任何改动」")
        replaced, _, rep4 = patch.build_patch(
            raw, {**spec, "fields": [{**dup[0], "name": "改名后的字段", "type": "clob"}],
                  "mode": "rename"})
        with zipfile.ZipFile(io.BytesIO(raw)) as a, \
                zipfile.ZipFile(io.BytesIO(replaced)) as b:
            old = a.read(target["file"]).decode("utf-8")
            new = b.read(target["file"]).decode("utf-8")
            by_code = {x["code"]: x for x in attrs_of(new)}
            old_row = {x["code"]: x for x in attrs_of(old)}[dup[0]["code"]]
            ok(by_code[dup[0]["code"]]["name"] == "改名后的字段",
               "rename 模式：已有字段名称生效")
            ok(by_code[dup[0]["code"]]["dataTypeNew"] == old_row.get("dataTypeNew"),
               "rename 模式：类型等定义改动被忽略")
            ok(by_code[dup[0]["code"]]["fieldName"] == old_row.get("fieldName"),
               "rename 不改物理列名 fieldName")
            ok(len(attrs_of(new)) == len(attrs_of(old)), "rename 不会多出/少掉字段")
        ok(any(a.get("rename") for a in rep4["changed"][target["file"]]["replaced"]),
           "报告里记了改名")
        try:
            patch.build_patch(raw, {**spec, "mode": "rename",
                                    "deleteFields": [existing_code]})
            ok(False, "删除已有字段应当报错")
        except ValueError as exc:
            ok("删除" in str(exc), "删除已有字段被拒绝")

    # ---- 4. 多副本
    dup_files = [o["file"] for o in snap["objects"] if o["code"] == target["code"]]
    if len(dup_files) > 1:
        _, _, rep5 = patch.build_patch(raw, {**spec, "allCopies": True})
        ok(len(rep5["changedFiles"]) == len(dup_files),
           f"allCopies 覆盖全部 {len(dup_files)} 份副本")

    # ---- 5. 配套 Excel
    tpl = patch.fields_template_bytes()
    back = patch.fields_from_excel(tpl)
    ok(bool(back["fields"]) and back["fields"][0]["code"] == "riskLevel",
       "字段表模板 → 回读往返正常")
    xls, xname = patch.patched_workbook(raw, {**spec, "syncForm": True})
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(xls))
    rows = [[c.value for c in row] for row in wb["2_字段"].iter_rows(min_row=2)]
    ok(any(r[2] == "chkMemo" for r in rows), "规格表 2_字段 里带上新字段")
    ok(xname.endswith(".xlsx"), f"规格表文件名 {xname}")


def main(argv: list[str]) -> int:
    samples = [Path(x) for x in argv[1:]]
    if not samples:
        samples = sorted((ROOT / "PP6" / "原生推送包").glob("*.zip"))
    if not samples:
        print("没有找到样本 zip，请传入路径")
        return 2
    for p in samples:
        try:
            run(p)
        except Exception as exc:
            print(f"  ✗ {p.name} 异常：{exc}")
            _FAILS.append(f"{p.name}: {exc}")
    print(f"\n{_CHECKS - len(_FAILS)}/{_CHECKS} 项通过")
    if _FAILS:
        print("失败项：")
        for f in _FAILS:
            print("  -", f)
        return 1
    print("PASS：增量补丁保真度全部达标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
