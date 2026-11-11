"""批量问答自检：把自己的语料库跑一遍，不靠人肉点。

用法：
    bin/pp6 probe                 # 跑内置语料
    bin/pp6 probe --show-sql      # 顺带打印生成的 SQL
    bin/pp6 probe --only 项目      # 只跑问题里含「项目」的

设计目标：**先自己发现 0 行/报错/选错表**，再交给实施用。
判定维度：
    error        生成或执行失败
    empty        0 行（多半是条件写法或表选错）
    wrong_table  SQL 没落到期望的表
    wrong_value  结果里出现了不该出现的值（如"排除X"却查出 X）
    missing_col  结果缺少期望的列
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .nlq import engine, guard, introspect, local, store
from .resources import resource_root

# 和 objects/fieldtypes 一样必须走 resource_root()/assets：写死相对路径的话，
# 换个工作目录跑就找不到文件，冻结后更是必失效。
CORPUS = resource_root() / "assets" / "probe_questions.json"


@dataclass
class Case:
    q: str
    min_rows: int = 1
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    forbid: list[dict] = field(default_factory=list)   # [{"column": "任务名称", "contains": "根任务"}]


@dataclass
class Result:
    case: Case
    status: str
    rows: int = 0
    sql: str = ""
    detail: str = ""


def load_cases(path: Path = CORPUS) -> list[Case]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for item in raw:
        out.append(Case(
            q=item["q"],
            min_rows=item.get("min_rows", 1),
            tables=item.get("tables", []),
            columns=item.get("columns", []),
            forbid=item.get("forbid", []),
        ))
    return out


def run_one(conn, pack, case: Case, allowed: set[str]) -> Result:
    try:
        sql, _note = local.build(pack, case.q)
    except Exception as exc:
        return Result(case, "error", detail=f"生成失败: {exc}")
    try:
        safe = guard.validate(sql, allowed)
    except Exception as exc:
        return Result(case, "error", sql=sql, detail=f"安全闸拦截: {exc}")
    try:
        cols, rows = engine.run_sql(conn, safe)
    except Exception as exc:
        return Result(case, "error", sql=safe, detail=f"执行失败: {str(exc)[:120]}")

    res = Result(case, "pass", rows=len(rows), sql=safe)
    low = safe.lower()
    missing = [t for t in case.tables if t.lower() not in low]
    if missing:
        res.status, res.detail = "wrong_table", f"SQL 未用到 {', '.join(missing)}"
        return res
    if len(rows) < case.min_rows:
        res.status, res.detail = "empty", f"只返回 {len(rows)} 行"
        return res
    for want in case.columns:
        if not any(want in c for c in cols):
            res.status = "missing_col"
            res.detail = f"缺少列 {want}（实际 {cols[:4]}）"
            return res
    for rule in case.forbid:
        idx = next((i for i, c in enumerate(cols) if rule["column"] in c), None)
        if idx is None:
            continue
        hits = [r[idx] for r in rows if r[idx] and rule["contains"] in str(r[idx])]
        if hits:
            res.status = "wrong_value"
            res.detail = f"{rule['column']} 里仍出现 {rule['contains']}（{len(hits)} 行）"
            return res
    return res


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    show_sql = "--show-sql" in argv
    only = ""
    if "--only" in argv:
        only = argv[argv.index("--only") + 1]

    cfg = store.load()["db"]
    if not (cfg.get("host") and cfg.get("database")):
        print("未配置数据库，先执行 bin/pp6 ui 里「连接并读取结构」")
        return 1

    conn = introspect.connect(cfg["host"], int(cfg.get("port") or 3306),
                              cfg.get("user", ""), cfg.get("password", ""), cfg["database"])
    try:
        t0 = time.time()
        pack = introspect.build_schema(conn, cfg["database"])
        introspect.annotate_liveness(conn, pack, list(pack.tables))
        introspect.annotate_columns(conn, pack)
        allowed = set(pack.tables) | guard.PARTY_TABLES | {"iform_cat_dic", "iform_cat_type"}

        cases = [c for c in load_cases() if not only or only in c.q]
        results = [run_one(conn, pack, c, allowed) for c in cases]
    finally:
        conn.close()

    bad = [r for r in results if r.status != "pass"]
    print(f"语料 {len(results)} 条，用时 {time.time()-t0:.1f}s")
    print(f"通过 {len(results)-len(bad)}，失败 {len(bad)}\n")

    if bad:
        by_status: dict[str, list[Result]] = {}
        for r in bad:
            by_status.setdefault(r.status, []).append(r)
        for status, items in by_status.items():
            print(f"[{status}] {len(items)} 条")
            for r in items:
                print(f"  ✗ {r.case.q}")
                print(f"    {r.detail}")
                if show_sql and r.sql:
                    print("    " + r.sql.replace("\n", " ")[:200])
            print()

    if show_sql:
        print("--- 通过的用例 SQL ---")
        for r in results:
            if r.status == "pass":
                print(f"✓ {r.case.q}  ({r.rows} 行)")

    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
