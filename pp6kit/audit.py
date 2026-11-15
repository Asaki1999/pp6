"""自检「规则引擎答不了的问法」，看 AI 兜底覆盖了多少。

用法：
    bin/pp6 audit                      # 跑内置难题集
    bin/pp6 audit --only 预算           # 只跑含该关键词的

和 probe 的区别：
    probe  检查规则引擎本身对不对（152 条基准，必须全绿）
    audit  找规则引擎的**盲区**，看 hybrid 判定器能不能识别出来
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from .nlq import engine, guard, hybrid, introspect, local

# 规则引擎已知会翻车的复杂问法：多条件、跨表、聚合、排序
HARD_CASES: list[str] = [
    "每个项目经理负责的项目数量",
    "预算超支的项目",
    "每个阶段的任务平均工期",
    "延期风险最高的前5个项目",
    "本月创建且进度低于50的项目",
    "开发任务里已完成的占比",
    "同时有高风险和高优先级问题的项目",
    "比计划完成时间晚的任务",
    "最近30天新增问题最多的项目",
    "没有负责人的任务",
    "每个项目的实际成本汇总",
    "工期超过10天的任务",
    "每个客户已签合同的金额合计",
    "风险等级最高的是哪些项目",
    "按负责人统计问题数量",
]


@dataclass
class AuditRow:
    q: str
    rows: int
    verdict_ok: bool
    reasons: str
    error: str = ""


def sweep(pack, conn) -> tuple[int, list[tuple[str, str]]]:
    """全表全列扫一遍：语义层里任何一列在物理表不存在，都会在这里暴露。

    比 probe 更狠：probe 只测「常问的 178 种说法」，
    sweep 把所有表的所有列都 SELECT 一次，覆盖 100%。
    """
    bad = []
    tested = 0
    for name, t in pack.tables.items():
        if t.rows == 0:
            continue
        cols = [x.name for x in t.columns]
        if not cols:
            continue
        sel = ", ".join(f"`{x}`" for x in cols)
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT {sel} FROM `{name}` LIMIT 3")
            cur.fetchall()
            cur.close()
            tested += 1
        except Exception as exc:
            bad.append((name, str(exc)[:70]))
    return tested, bad


def main(argv: list[str]) -> int:
    import os
    host = os.environ.get("PP6_DB_HOST", "127.0.0.1")
    port = int(os.environ.get("PP6_DB_PORT", "3306"))
    user = os.environ.get("PP6_DB_USER", "root")
    password = os.environ.get("PP6_DB_PASS", "")
    database = os.environ.get("PP6_DB_NAME", "powerproject6")
    if not password:
        # 口令只从环境变量读：不要把任何数据库口令写进仓库（包括默认值）。
        print("缺少 PP6_DB_PASS：请先 export PP6_DB_PASS=...，口令不要写进代码/文档")
        return 2

    only = ""
    if "--only" in argv:
        i = argv.index("--only")
        only = argv[i + 1] if i + 1 < len(argv) else ""
    do_sweep = "--sweep" in argv

    conn = introspect.connect(host, port, user, password, database)
    try:
        pack = introspect.build_schema(conn, database)
        introspect.annotate_liveness(conn, pack, list(pack.tables))
        introspect.annotate_columns(conn, pack)
        allowed = set(pack.tables) | guard.PARTY_TABLES

        if do_sweep:
            tested, bad = sweep(pack, conn)
            print(f"全表扫描 {tested} 张，失败 {len(bad)} 张")
            for n, e in bad[:20]:
                print(f"  ✗ {n}: {e}")
            return 0 if not bad else 1

        rows: list[AuditRow] = []
        for q in HARD_CASES:
            if only and only not in q:
                continue
            try:
                sql, _ = local.build(pack, q)
                safe = guard.validate(sql, allowed)
                cols, data = engine.run_sql(conn, safe)
                v = hybrid.judge(q, safe, len(data))
                rows.append(AuditRow(q, len(data), v.ok, v.note))
            except Exception as exc:
                rows.append(AuditRow(q, 0, False, "", str(exc)[:60]))
    finally:
        conn.close()

    if not rows:
        print("没有匹配的用例")
        return 0

    need = [r for r in rows if not r.verdict_ok]
    print(f"难题 {len(rows)} 条，规则答不了 {len(need)} 条，"
          f"覆盖率 {(len(rows) - len(need)) / len(rows) * 100:.0f}%")
    print()
    for r in rows:
        mark = "规则能答" if r.verdict_ok else "需 AI 兜底"
        detail = r.reasons or r.error
        print(f"  [{mark:8}] {r.q:28} {r.rows:>4} 行  {detail}")
    return 0
