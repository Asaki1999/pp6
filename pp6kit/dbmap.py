"""生成《数据库结构图谱》：换客户数据库重跑一次即可。

    bin/pp6 dbmap [--out docs/数据库结构图谱.md]

输出：表分类统计、按业务域分组的表清单、对象关系图（含「实测有数据/整列为空」）、
常用 join 路径。这份文档既给人看，也用来校验语义层是否覆盖到位。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from .nlq import introspect, store

# 业务域判定（按表名前缀，先匹配者优先）
DOMAINS: list[tuple[str, tuple[str, ...]]] = [
    ("项目主数据", ("pp_project", "pp_program", "pp_classification", "pp_portfolio")),
    ("计划与任务", ("pp_task", "pp_project_plan", "pp_plan_", "pp_dev_task", "pp_dev_sprint", "pp_action_item")),
    ("风险问题缺陷", ("pp_risk", "pp_issue", "pp_defect")),
    ("预算与成本", ("pp_project_budget", "pp_budget_", "pp_project_cost", "pp_finance_", "pp_cost")),
    ("文档", ("pp_document", "pp_project_doc", "pp_directory", "pp_project_expect_doc")),
    ("测试与质量", ("pp_test_", "pp_quality")),
    ("产品与需求", ("pp_product", "pp_requirement", "pp_mrd_", "pp_bom")),
    ("团队与角色", ("pp_project_member", "pp_project_role", "pp_program_project")),
    ("工时与进展", ("pp_work", "pp_project_report", "pp_meeting", "pp_todo")),
    ("交付与结项", ("pp_contract", "pp_project_completion", "pp_material")),
]


def domain_of(name: str) -> str:
    for label, prefixes in DOMAINS:
        for p in prefixes:
            if name.startswith(p):
                return label
    return "其它业务表"


def collect(cfg: dict) -> tuple[introspect.SchemaPack, dict, list[tuple]]:
    conn = introspect.connect(cfg["host"], cfg.get("port") or 3306, cfg["user"],
                              cfg.get("password", ""), cfg["database"])
    cur = conn.cursor()
    pack = introspect.build_schema(conn, cfg["database"])

    cur.execute("""SELECT table_name, table_rows FROM information_schema.tables
                   WHERE table_schema=%s""", (cfg["database"],))
    all_tables = {n: int(r or 0) for n, r in cur.fetchall()}

    # 关系 + 实测是否有数据
    cur.execute("""
        SELECT d.code_ AS src_obj, a.name_ AS col_label, ac.field_name_ AS col,
               a.object_type_ AS dst_obj, a.data_type_new_ AS dtype
        FROM iform_bo_def d
        JOIN iform_bo_attr a ON a.def_id_ = d.id_
        JOIN iform_bo_attr_column ac ON ac.def_id_ = d.id_ AND ac.attr_code_ = a.code_
        WHERE a.data_type_new_ IN ('object','user','department') AND ac.field_name_ IS NOT NULL
    """)
    by_src: dict[str, list] = defaultdict(list)
    for src, label, col, dst, dtype in cur.fetchall():
        dst_t = ("iform_party_employee" if dtype == "user" else
                 "iform_party_org" if dtype == "department" else
                 (f"pp_{dst}" if dst else ""))
        if dst_t and dst_t in all_tables:
            by_src[f"pp_{src}"].append((col, label, dst_t, dtype))

    relations: list[tuple] = []
    for t, rels in by_src.items():
        if t not in all_tables:
            continue
        cur.execute(f"SHOW COLUMNS FROM `{t}`")
        have = {r[0] for r in cur.fetchall()}
        cols = [r[0] for r in rels if r[0] in have]
        if not cols:
            continue
        expr = ", ".join(f"SUM(`{x}` IS NOT NULL AND `{x}`<>'')" for x in cols)
        try:
            cur.execute(f"SELECT COUNT(*), {expr} FROM `{t}`")
            row = cur.fetchone()
        except Exception:
            continue
        total = row[0] or 0
        counts = {c: int(n or 0) for c, n in zip(cols, row[1:])}
        for col, label, dst, dtype in rels:
            if col in counts:
                relations.append((t, col, label, dst, counts[col], total))
    cur.close()
    conn.close()
    return pack, all_tables, relations


def render(pack, all_tables: dict, relations: list[tuple], cfg: dict) -> str:
    L: list[str] = []
    L.append("# 数据库结构图谱\n")
    L.append(f"- 库：`{cfg['database']}`　主机：`{cfg['host']}:{cfg.get('port')}`")
    L.append(f"- 表总数：{len(all_tables)}　业务表：{len(pack.tables)}　"
             f"字段：{sum(len(t.columns) for t in pack.tables.values())}　字典：{len(pack.dictionaries)}")
    live = [r for r in relations if r[4] > 0]
    L.append(f"- 对象关系：{len(relations)} 条，其中**实测有数据 {len(live)} 条**、"
             f"整列为空 {len(relations) - len(live)} 条（空列 join 出来全是 NULL，不要用）\n")

    # 分类
    buckets: dict[str, list] = defaultdict(list)
    for n, r in all_tables.items():
        if n.startswith("pp_"):
            k = "pp_* 业务表"
        elif n.startswith("iform_"):
            k = "iform_* 平台元数据"
        elif n.startswith(("workflow", "act_")):
            k = "流程引擎"
        elif n.startswith(("trigger_", "system_", "sys_")):
            k = "自动化 / 系统"
        else:
            k = "其它"
        buckets[k].append((n, r))
    L.append("## 一、表分类\n")
    L.append("| 分类 | 张数 | 约行数 |")
    L.append("|---|---|---|")
    for k, v in sorted(buckets.items(), key=lambda x: -len(x[1])):
        L.append(f"| {k} | {len(v)} | {sum(r for _, r in v)} |")
    L.append("")

    # 业务域
    L.append("## 二、业务表按域分组\n")
    grouped: dict[str, list] = defaultdict(list)
    for name, t in pack.tables.items():
        grouped[domain_of(name)].append(t)
    for dom in [d[0] for d in DOMAINS] + ["其它业务表"]:
        items = sorted(grouped.get(dom, []), key=lambda t: -t.rows)
        if not items:
            continue
        L.append(f"### {dom}（{len(items)} 张）\n")
        L.append("| 表 | 中文名 | 行数 | 字段数 |")
        L.append("|---|---|---|---|")
        for t in items[:20]:
            L.append(f"| `{t.name}` | {t.label} | {t.rows} | {len(t.columns)} |")
        if len(items) > 20:
            L.append(f"| … | 另有 {len(items) - 20} 张 | | |")
        L.append("")

    # 关系
    L.append("## 三、对象关系（实测有数据的部分）\n")
    by_dst: dict[str, list] = defaultdict(list)
    for src, col, label, dst, n, total in live:
        by_dst[dst].append((src, col, label, n, total))
    for dst, items in sorted(by_dst.items(), key=lambda x: -len(x[1]))[:12]:
        dst_label = pack.tables[dst].label if dst in pack.tables else (
            "人员（平台表）" if dst == "iform_party_employee" else
            "组织（平台表）" if dst == "iform_party_org" else dst)
        L.append(f"### 指向 `{dst}`（{dst_label}）— {len(items)} 处\n")
        L.append("| 源表 | 字段 | 含义 | 非空/总行 |")
        L.append("|---|---|---|---|")
        for src, col, label, n, total in sorted(items, key=lambda x: -x[3])[:12]:
            L.append(f"| `{src}` | `{col}` | {label} | {n}/{total} |")
        L.append("")

    L.append("## 四、可直接使用的 join 路径\n")
    L.append("| 从 | 到 | 写法 |")
    L.append("|---|---|---|")
    samples = sorted(live, key=lambda x: -x[4])[:15]
    for src, col, label, dst, n, total in samples:
        L.append(f"| `{src}`.{col}（{label}） | `{dst}` | `LEFT JOIN {dst} r ON r.id_ = {src}.{col}` |")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/数据库结构图谱.md")
    args = ap.parse_args()

    cfg = store.load().get("db", {})
    if not cfg.get("host"):
        raise SystemExit("尚未配置数据库连接，请先在应用里连一次")
    pack, all_tables, relations = collect(cfg)
    text = render(pack, all_tables, relations, cfg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"✓ 已生成 {out}（{len(all_tables)} 表，{len(relations)} 条关系）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
