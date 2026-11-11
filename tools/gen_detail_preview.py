"""按改造后的表单包生成静态预览页（结构、标签、宽度全部来自 XML，避免图文不一致）。"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# 示意数据（字段名 -> 显示值）；长文本用整行展示
SAMPLE = {
    "name": "三九胃泰颗粒二次开发",
    "code": "20261100001",
    "stateId": "执行中",
    "gradeId": "A 类",
    "createMode": "整体立项",
    "projectCreateTime": "2026-03-12",
    "managerIds": "周玉方",
    "orgId": "华润三九医药股份有限公司",
    "classificationId": "中药创新药",
    "productType": "中药",
    "productClassification": "OTC",
    "strategyCategory": "中药-新品管线",
    "projectCategoryCode": "中药-1.1 类",
    "innovationPipeline": "消化代谢",
    "projectSource": "外部引进",
    "projectFundingSource": "华润三九医药股份有限公司",
    "researchDevelopmentContent": "制剂",
    "technologyPlatform": "创新中药",
    "dosageForm": "颗粒剂",
    "developmentMode": "自主研发",
    "specification": "每袋装 10g",
    "specification2_": "每袋装 5g（无蔗糖）",
    "specification3_": "—",
    "isProcessTechnologyProject": "是",
    "indication": "慢性胃炎、胃脘胀痛；症见胃脘隐痛、食少纳呆。",
    "targetMechanism": "作用于胃黏膜修复通路，抑制炎症因子释放。",
    "projectDemandDept": "研发中心",
    "projectResearchDept": "研发中心",
    "mahHolder": "华润三九医药股份有限公司",
    "productionSiteUnit": "华润三九医药股份有限公司",
    "marketingReceivingDept": "处方药事业部",
    "pmo": "练慧君",
    "totalBudget": "1,280.00 万元",
    "researchDevelopmentCycle": "36 个月",
    "technologyTransferFee": "320.00 万元",
    "outsourceResearchFee": "145.00 万元",
    "alreadyPaidOut": "412.60 万元",
    "excutionRate": "32%",
    "projectOverallGoal": "完成三九胃泰颗粒的工艺优化与质量标准提升，取得补充申请批件。",
    "annualTarget": "2026 年：完成中试放大与稳定性考察方案设计。",
    "approvalDate": "2026-09-30",
    "actuallyApprovalDate": "2026-09-26",
    "changeNum": "3",
    "changeEarlyWarning": "已变更 3 次",
    "projectRiskWarning": "关键原料供应存在单一来源风险",
    "projectOverview": "<p>本项目为既有品种的二次开发，聚焦工艺与质量标准提升，覆盖处方、工艺、质量、注册四条主线。</p>",
}

WIDE = {"editor", "textarea"}


def main(zip_path: str, out: str) -> None:
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.endswith(".xml"))
        root = ET.fromstring(z.read(name))
    fds = root.find("formDefs")
    els = {f.get("id"): f for f in fds.findall("formField")}

    cards = [f for f in fds.findall("formField") if f.get("fieldType") == "card"]
    sections = []
    for card in cards:
        grid = next(f for f in fds.findall("formField") if f.get("parentId") == card.get("id"))
        opts = json.loads(grid.get("fieldOptions"))
        rows = []
        for row in opts["rows"]:
            cells = []
            for cell in row["columns"]:
                fj = cell["fields"][0]
                nm = fj["name"]
                val = SAMPLE.get(nm, "")
                if fj.get("field_type") == "editor":
                    val = ""
                cells.append({"span": cell["span"], "label": fj["label"], "value": val,
                              "wide": cell["span"] >= 24,
                              "rich": fj.get("field_type") == "editor",
                              "area": fj.get("field_type") == "textarea"})
            rows.append(cells)
        sections.append({"title": card.get("label"), "rows": rows,
                         "count": sum(len(r) for r in rows)})

    total = sum(s["count"] for s in sections)
    body = []
    for i, s in enumerate(sections, start=1):
        rows_html = []
        for row in s["rows"]:
            cells = []
            for c in row:
                cls = "fld wide" if c["wide"] else "fld"
                if c.get("area"):
                    cls += " area"
                if c["rich"]:
                    v = SAMPLE.get("name") and "<p>本项目为既有品种的二次开发，聚焦工艺与质量标准提升，覆盖处方、工艺、质量、注册四条主线。</p>"
                    cells.append(f'<div class="{cls}"><label>{c["label"]}</label>'
                                 f'<div class="rich">{v}</div></div>')
                else:
                    cells.append(f'<div class="{cls}"><label>{c["label"]}</label>'
                                 f'<div class="val">{c["value"] or "—"}</div></div>')
            rows_html.append('<div class="row">' + "".join(cells) + "</div>")
        body.append(f'''
    <section class="card" id="s{i}">
      <div class="card-hd"><span class="idx">{i}</span><h2>{s["title"]}</h2>
        <span class="cnt">{s["count"]} 项</span></div>
      <div class="card-bd">{"".join(rows_html)}</div>
    </section>''')

    nav = "".join(
        f'<a href="#s{i}">{i}. {s["title"]}</a>' for i, s in enumerate(sections, start=1)
    )

    html = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>项目基本信息 · 布局 V1.0 预览</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f2f4f8;color:#22314f;
  font-size:13px;line-height:1.6;padding:24px}}
.wrap{{max-width:1080px;margin:0 auto}}
.page{{background:#fff;border-radius:10px;box-shadow:0 2px 14px rgba(20,40,80,.08);overflow:hidden}}
.ph{{padding:18px 24px;border-bottom:1px solid #eef1f6;display:flex;align-items:center;gap:14px;
  background:linear-gradient(180deg,#fbfcfe,#f6f8fc)}}
.ph h1{{font-size:19px;font-weight:600;letter-spacing:.2px}}
.tag{{font-size:12px;padding:1px 9px;border-radius:10px;background:#eaf4ff;color:#1a73e8;border:1px solid #d3e6ff}}
.code{{font-family:ui-monospace,Menlo,monospace;color:#7b879b;font-size:12px}}
.acts{{margin-left:auto;display:flex;gap:8px}}
.btn{{font-size:12px;padding:5px 12px;border-radius:5px;border:1px solid #dfe4ec;color:#425067;background:#fff}}
.btn.danger{{color:#e4534f;border-color:#f3d3d2}}
.meta{{padding:10px 24px;background:#fafbfd;border-bottom:1px solid #eef1f6;font-size:12px;color:#7b879b;
  display:flex;gap:18px;flex-wrap:wrap}}
.meta b{{color:#22314f;font-weight:600}}
.nav{{padding:10px 24px;border-bottom:1px solid #eef1f6;display:flex;gap:8px;flex-wrap:wrap}}
.nav a{{font-size:12px;color:#425067;text-decoration:none;padding:3px 10px;border-radius:20px;background:#f1f4f9}}
.nav a:hover{{background:#e6edf8;color:#1a73e8}}
.body{{padding:18px 24px 26px;display:flex;flex-direction:column;gap:14px}}
.card{{border:1px solid #e8ecf3;border-radius:8px;overflow:hidden;background:#fff}}
.card-hd{{display:flex;align-items:center;gap:9px;padding:11px 16px;background:#fafbfd;border-bottom:1px solid #eef1f6}}
.idx{{width:18px;height:18px;border-radius:4px;background:#1a73e8;color:#fff;font-size:11px;
  display:grid;place-items:center;font-weight:600}}
.card-hd h2{{font-size:14px;font-weight:600}}
.cnt{{margin-left:auto;font-size:11px;color:#98a2b3}}
.card-bd{{padding:6px 16px 14px}}
.row{{display:flex;gap:0;border-bottom:1px dashed #f0f2f7}}
.row:last-child{{border-bottom:0}}
.fld{{flex:1;min-width:0;display:flex;align-items:flex-start;padding:9px 0;gap:10px}}
.fld.wide{{flex:0 0 100%}}
.fld label{{flex:0 0 122px;text-align:right;color:#7b879b;font-size:12.5px;padding-top:1px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.val{{flex:1;min-width:0;color:#22314f;word-break:break-word}}
.rich{{flex:1;min-width:0;border:1px solid #eef1f6;border-radius:5px;padding:8px 10px;background:#fcfdff;
  color:#3c4a63;min-height:64px}}
.fld.area .val{{border:1px solid #eef1f6;border-radius:5px;padding:5px 9px;background:#fcfdff;min-height:32px}}
.rich p{{margin:0}}
.foot{{padding:14px 24px 22px;color:#98a2b3;font-size:11.5px;text-align:center}}
@media(max-width:720px){{.row{{flex-direction:column}}.fld{{flex-direction:column;gap:2px}}
 .fld label{{text-align:left;flex:none}}}}
</style></head><body><div class="wrap"><div class="page">
  <div class="ph">
    <h1>三九胃泰颗粒二次开发</h1>
    <span class="tag">执行中</span>
    <span class="code">20261100001</span>
    <div class="acts"><button class="btn">编辑</button><button class="btn danger">删除项目</button></div>
  </div>
  <div class="meta">
    <span>表单：<b>项目基本信息</b>（project_summary）</span>
    <span>布局：<b>表格布局 V1.0</b> × {len(sections)}</span>
    <span>控件：<b>{total}</b></span>
    <span>数据源：<b>crsanjiu · pp_project</b></span>
  </div>
  <div class="nav">{nav}</div>
  <div class="body">{"".join(body)}
  </div>
  <div class="foot">预览用示意数据，字段标签与栅格宽度取自改造后的表单包 form/001-project_summary.xml</div>
</div></div></body></html>'''

    Path(out).write_text(html, encoding="utf-8")
    print(f"✓ {out}  ({len(html)/1024:.0f} KB, {len(sections)} 分区 {total} 控件)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
