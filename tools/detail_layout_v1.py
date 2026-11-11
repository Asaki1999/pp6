"""项目详情表单 → 布局 V1.0 改造。

把「一张 card + 一个 46 格表格布局」的大平铺，重组为
「N 张 card 分组 + 每组一个表格布局V1.0（grid_layout）」，
并按 crsanjiu.pp_project 的真实列类型修正控件类型（clob → textarea）。

用法：
    python3 tools/detail_layout_v1.py <源 zip> <输出 zip>
"""
from __future__ import annotations

import json
import random
import string
import sys
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------- 分组方案
# 每项：(分区名, [ [ (span, 控件name), ... ],  ... ])   span 12=半宽 24=整行
GROUPS: list[tuple[str, list[list[tuple[int, str]]]]] = [
    ("基础信息", [
        [(12, "name"), (12, "code")],
        [(12, "stateId"), (12, "gradeId")],
        [(12, "createMode"), (12, "projectCreateTime")],
        [(12, "managerIds"), (12, "orgId")],
    ]),
    ("分类与归属", [
        [(12, "classificationId"), (12, "productType")],
        [(12, "productClassification"), (12, "strategyCategory")],
        [(12, "projectCategoryCode"), (12, "innovationPipeline")],
        [(12, "projectSource"), (12, "projectFundingSource")],
    ]),
    ("研发属性", [
        [(12, "researchDevelopmentContent"), (12, "technologyPlatform")],
        [(12, "dosageForm"), (12, "developmentMode")],
        [(12, "specification"), (12, "specification2_")],
        [(12, "specification3_"), (12, "isProcessTechnologyProject")],
        [(24, "indication")],
        [(24, "targetMechanism")],
    ]),
    ("组织与承接", [
        [(12, "projectDemandDept"), (12, "projectResearchDept")],
        [(12, "mahHolder"), (12, "productionSiteUnit")],
        [(12, "marketingReceivingDept"), (12, "pmo")],
    ]),
    ("预算与执行", [
        [(12, "totalBudget"), (12, "researchDevelopmentCycle")],
        [(12, "technologyTransferFee"), (12, "outsourceResearchFee")],
        [(12, "alreadyPaidOut"), (12, "excutionRate")],
    ]),
    ("目标与进展", [
        [(24, "projectOverallGoal")],
        [(24, "annualTarget")],
        [(12, "approvalDate"), (12, "actuallyApprovalDate")],
    ]),
    ("变更与风险", [
        [(12, "changeNum"), (12, "changeEarlyWarning")],
        [(24, "projectRiskWarning")],
        [(24, "projectOverview")],
    ]),
]

# crsanjiu.pp_project 里是 longtext、但表单用了单行 text 的字段 → 改 textarea
TEXTAREA = {
    "indication", "specification", "specification2_", "specification3_",
    "targetMechanism", "projectOverallGoal", "annualTarget",
}
# 详情页里的必填
REQUIRED = {"name"}
# 标签过长会在 labelWidth 下被截断，这里收短（括号里的口径改用单位后缀表达）
LABEL = {
    "orgId": "项目主责单位",
    "technologyTransferFee": "技术转让费（含税）",
    "outsourceResearchFee": "委托研究费",
}
# 金额类统一带「万元」后缀，省去在标签里写单位
UNITS = {
    "totalBudget": "万元",
    "technologyTransferFee": "万元",
    "outsourceResearchFee": "万元",
    "alreadyPaidOut": "万元",
}
LABEL_WIDTH = 118

_EPOCH = 1288834974657  # 雪花算法纪元
_seq = 0


def new_big_id() -> str:
    """19 位雪花 id，和系统里既有控件 id 同一形态。"""
    global _seq
    _seq += 1
    return str(((int(time.time() * 1000) - _EPOCH) << 22) | (_seq & 0x3FFFFF))


def short_id(taken: set[str]) -> str:
    """系统里行/单元格/卡片名用的 7 位短 id（小写十六进制）。"""
    alphabet = string.hexdigits.lower()[:16]
    while True:
        s = "".join(random.choice(alphabet) for _ in range(7))
        if s not in taken:
            taken.add(s)
            return s


def main(src: str, dst: str) -> None:
    with zipfile.ZipFile(src) as z:
        name = next(n for n in z.namelist() if n.endswith(".xml"))
        raw = z.read(name)

    root = ET.fromstring(raw)
    fds = root.find("formDefs")
    form_def = fds.find("formDef")
    form_id = form_def.get("id")

    # 标签列 90px 装不下「是否工艺技术类项目」这类 8 字以上标签，会截断
    ext_attr = json.loads(form_def.get("extendAttr") or "{}")
    if int(ext_attr.get("labelWidth") or 0) < LABEL_WIDTH:
        ext_attr["labelWidth"] = LABEL_WIDTH
        form_def.set("extendAttr", json.dumps(ext_attr, ensure_ascii=False, separators=(",", ":")))

    fields = {f.get("name"): f for f in fds.findall("formField")}
    hidden = [fields.pop(n) for n in ("id", "ppParentId") if n in fields]
    old_card = next(f for f in fields.values() if f.get("fieldType") == "card")
    old_grid = next(f for f in fields.values() if f.get("fieldType") == "grid_layout")
    field_els = {k: v for k, v in fields.items() if v.get("fieldType") not in ("card", "grid_layout")}

    # rows 里的完整控件 JSON 才是权威定义
    grid_opts = json.loads(old_grid.get("fieldOptions"))
    row_json: dict[str, dict] = {}
    for row in grid_opts.get("rows", []):
        for cell in row.get("columns", []):
            for fj in cell.get("fields", []):
                row_json[fj.get("name")] = fj

    missing = [n for grp in GROUPS for row in grp[1] for _, n in row if n not in field_els]
    if missing:
        raise SystemExit(f"分组里出现表单中不存在的控件：{missing}")
    used = {n for grp in GROUPS for row in grp[1] for _, n in row}
    extra = sorted(set(field_els) - used)
    if extra:
        raise SystemExit(f"这些控件没被分到任何组（会丢字段）：{extra}")

    taken = {f.get("id") for f in fds.findall("formField")}
    for row in grid_opts.get("rows", []):
        taken.add(row.get("id"))
        for cell in row.get("columns", []):
            taken.add(cell.get("id"))

    card_tpl = json.loads(old_card.get("fieldOptions"))
    toolbar = card_tpl.get("extra")

    # 重新解析一份，攒住 formBo / formRights（重建 formField 时不能丢）
    root2 = ET.fromstring(raw)
    fds2 = root2.find("formDefs")
    fds_bo = fds2.find("formBo")
    fds_rights = fds2.findall("formRights")

    # ---- 清空原有 formField，按新结构重建
    for el in list(fds):
        if el.tag in ("formField", "formBo", "formRights"):
            fds.remove(el)

    for h in hidden:
        fds.append(h)

    for sn, (title, rows) in enumerate(GROUPS, start=1):
        # 第一组沿用原来的 card / grid id，尽量少改动既有引用
        card_id = old_card.get("id") if sn == 1 else new_big_id()
        grid_id = old_grid.get("id") if sn == 1 else new_big_id()
        card_name = old_card.get("name") if sn == 1 else "card_" + short_id(taken)
        grid_name = old_grid.get("name") if sn == 1 else "gridLayout_" + short_id(taken)

        card = ET.SubElement(fds, "formField")
        card_opts = json.loads(json.dumps(card_tpl))
        card_opts["columns"] = [{"name": None, "label": None, "fields": [grid_id], "span": ""}]
        if sn == 1:
            card_opts["extra"] = toolbar
        else:
            card_opts.pop("extra", None)
        card.attrib.update({
            "id": card_id, "formId": form_id,
            "name": card_name, "label": title, "fieldType": "card",
            "fieldOptions": json.dumps(card_opts, ensure_ascii=False, separators=(",", ":")),
            "sn": str(sn + 2),
        })

        grid = ET.SubElement(fds, "formField")
        flat_cols, out_rows = [], []
        field_sn = 0
        for row in rows:
            row_id = short_id(taken)
            cells = []
            for span, fname in row:
                cell_id = short_id(taken)
                fj = json.loads(json.dumps(row_json[fname]))
                fo = fj.setdefault("field_options", {})
                if fname in TEXTAREA:
                    fj["field_type"] = "textarea"
                    fj["icon"] = "Texts"
                    fo["dbFieldType"] = "bigContent"
                    fo["rows"] = 5
                if fname in REQUIRED:
                    fo["required"] = "true"
                if fname in UNITS:
                    fo["units"] = UNITS[fname]
                if fname in LABEL:
                    fj["label"] = LABEL[fname]
                fj["parentId"] = cell_id
                cells.append({"id": cell_id, "span": span, "colSpan": 1, "fields": [fj]})
                flat_cols.append({
                    "name": None, "label": "单元格",
                    "fields": [field_els[fname].get("id")], "span": span,
                })
            out_rows.append({"id": row_id, "columns": cells})

        grid_opts_new = {
            "clearable": True, "controls": True, "selection": True,
            "columns": flat_cols, "mobile": True, "autoCollapse": True,
            "rows": out_rows,
        }
        grid.attrib.update({
            "id": grid_id, "formId": form_id, "parentId": card_id,
            "name": grid_name, "label": "表格布局V1.0", "desc": "",
            "fieldType": "grid_layout",
            "fieldOptions": json.dumps(grid_opts_new, ensure_ascii=False, separators=(",", ":")),
            "sn": "1",
        })

        for row in rows:
            for _, fname in row:
                el = field_els[fname]
                field_sn += 1
                el.set("parentId", grid_id)
                el.set("sn", str(field_sn))
                fo = json.loads(el.get("fieldOptions") or "{}")
                if fname in TEXTAREA:
                    el.set("fieldType", "textarea")
                    fo["dbFieldType"] = "bigContent"
                    fo["rows"] = 5
                if fname in REQUIRED:
                    fo["required"] = "true"
                if fname in UNITS:
                    fo["units"] = UNITS[fname]
                if fname in LABEL:
                    el.set("label", LABEL[fname])
                el.set("fieldOptions", json.dumps(fo, ensure_ascii=False, separators=(",", ":")))
                fds.append(el)

    if fds_bo is not None:
        fds.append(fds_bo)
    for fr in fds_rights:
        fds.append(fr)

    ET.indent(root, space="    ")
    # 声明行照抄原包（双引号 + standalone="yes"），别让导入端踩编码判断
    decl = raw.split(b"?>", 1)[0] + b"?>"
    if not decl.startswith(b"<?xml"):
        decl = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    body = decl + b"\n" + ET.tostring(root, encoding="utf-8", xml_declaration=False)

    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, body)
    print(f"✓ {dst}  ({len(body)/1024:.0f} KB, {len(GROUPS)} 个分组)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
