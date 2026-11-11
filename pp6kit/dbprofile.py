"""数据库画像：把「这个库里到底有什么、哪些表真有数据」摸清楚。

产出给两处用：
  1. 人看：docs/数据库画像.md
  2. 引擎用：assets/dbprofile.json（选表加权、空结果自诊断）

用法：
    bin/pp6 profile            # 连接当前配置的库
    bin/pp6 profile --json out.json
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .nlq import introspect


@dataclass
class TableProfile:
    name: str
    rows: int = 0
    has_deleted: bool = False
    deleted_null: int = 0          # deleted_ IS NULL 的行数
    deleted_y: int = 0             # deleted_ = 'Y' 的行数
    deleted_other: int = 0
    tenants: int = 0
    has_create_time: bool = False
    create_min: str = ""
    create_max: str = ""
    label: str = ""                # 中文名（来自 iform_bo_table）
    obj_code: str = ""             # 对应业务对象编码
    relation_cols: int = 0
    error: str = ""

    @property
    def usable(self) -> bool:
        """有数据且不是全被软删除。"""
        return self.rows > 0 and (self.rows - self.deleted_y) > 0


@dataclass
class Profile:
    database: str
    generated_at: str = ""
    tables: dict[str, TableProfile] = field(default_factory=dict)
    objects: list[dict] = field(default_factory=list)   # 业务对象 → 表 → 行数
    empty_objects: list[str] = field(default_factory=list)
    orphan_tables: list[str] = field(default_factory=list)  # 有数据但无对象定义
    deleted_styles: dict[str, str] = field(default_factory=dict)  # NULL / N / Y 三种写法分布
    notes: list[str] = field(default_factory=list)


def _cols(cur, table: str) -> set[str]:
    cur.execute(f"SHOW COLUMNS FROM `{table}`")
    return {r[0] for r in cur.fetchall()}


def profile_table(cur, name: str) -> TableProfile:
    p = TableProfile(name=name)
    try:
        cur.execute(f"SELECT COUNT(*) FROM `{name}`")
        p.rows = int(cur.fetchone()[0] or 0)
        cols = _cols(cur, name)
        if "deleted_" in cols:
            p.has_deleted = True
            cur.execute(
                f"SELECT SUM(`deleted_` IS NULL), SUM(`deleted_` = 'Y'), "
                f"SUM(`deleted_` IS NOT NULL AND `deleted_` <> 'Y') FROM `{name}`"
            )
            row = cur.fetchone()
            p.deleted_null = int(row[0] or 0)
            p.deleted_y = int(row[1] or 0)
            p.deleted_other = int(row[2] or 0)
        if "tenant_id_" in cols and p.rows:
            cur.execute(f"SELECT COUNT(DISTINCT `tenant_id_`) FROM `{name}`")
            p.tenants = int(cur.fetchone()[0] or 0)
        if "create_time_" in cols and p.rows:
            p.has_create_time = True
            cur.execute(f"SELECT MIN(`create_time_`), MAX(`create_time_`) FROM `{name}`")
            lo, hi = cur.fetchone()
            p.create_min = str(lo or "")[:10]
            p.create_max = str(hi or "")[:10]
    except Exception as exc:
        p.error = str(exc)[:120]
    return p


def build(conn, database: str) -> Profile:
    cur = conn.cursor()
    prof = Profile(database=database, generated_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    # ---- 1. 表清单（真实行数）
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
        (database,),
    )
    names = [r[0] for r in cur.fetchall()]
    t0 = time.time()
    for i, n in enumerate(names, 1):
        prof.tables[n] = profile_table(cur, n)
        if i % 100 == 0:
            print(f"  ... 已扫描 {i}/{len(names)} 张表 ({time.time()-t0:.0f}s)", flush=True)

    # ---- 2. 业务对象 → 物理表
    # iform_bo_table 的 def_code_ 有两种写法：不带前缀（dev_sprint → pp_dev_sprint）
    # 和带前缀（pp_equipment，外部表）。name_ 才是真实物理表名，优先用它。
    cur.execute(
        """
        SELECT d.code_, d.name_, t.name_, t.comment_
        FROM iform_bo_def d
        LEFT JOIN iform_bo_table t
               ON t.def_code_ = d.code_ OR t.def_code_ = CONCAT('pp_', d.code_)
        ORDER BY d.code_
        """
    )
    obj_rows = cur.fetchall()
    for code, label, tbl, comment in obj_rows:
        if tbl and tbl not in prof.tables:
            tbl = f"pp_{code}" if f"pp_{code}" in prof.tables else tbl
        tp = prof.tables.get(tbl) if tbl else None
        prof.objects.append({
            "code": code, "name": label, "table": tbl or "",
            "rows": tp.rows if tp else 0,
            "usable": bool(tp and tp.usable),
        })
        if tp:
            tp.label = label or comment or ""
            tp.obj_code = code
        if not tp or tp.rows == 0:
            prof.empty_objects.append(f"{label}（{code}）")

    # ---- 3. 有数据但没有对象定义的表（平台自有表或历史表）
    known = {o["table"] for o in prof.objects if o["table"]}
    for n, tp in prof.tables.items():
        if n.startswith("pp_") and n not in known and tp.rows > 0:
            prof.orphan_tables.append(n)

    # ---- 4. 软删除写法分布（这是最容易埋雷的地方）
    styles = Counter()
    for tp in prof.tables.values():
        if not tp.has_deleted or tp.rows == 0:
            continue
        if tp.deleted_null == tp.rows:
            styles["全 NULL（必须用 IFNULL）"] += 1
        elif tp.deleted_y > 0 and tp.deleted_other > 0:
            styles["Y/N 混用"] += 1
        elif tp.deleted_y == 0 and tp.deleted_other == tp.rows:
            styles["全 N（可用 <> 'Y'）"] += 1
        else:
            styles["其它"] += 1
    prof.deleted_styles = dict(styles)

    cur.close()
    return prof


def summarize(prof: Profile) -> dict:
    total_rows = sum(t.rows for t in prof.tables.values())
    pp = [t for n, t in prof.tables.items() if n.startswith("pp_")]
    pp_live = [t for t in pp if t.usable]
    return {
        "总表数": len(prof.tables),
        "总行数": total_rows,
        "业务表": len(pp),
        "业务表有数据": len(pp_live),
        "业务表空表": len(pp) - len(pp_live),
        "业务对象": len(prof.objects),
        "对象有数据": sum(1 for o in prof.objects if o["usable"]),
        "有表无对象": len(prof.orphan_tables),
    }


def to_markdown(prof: Profile) -> str:
    s = summarize(prof)
    lines = [
        f"# 数据库画像：{prof.database}",
        "",
        f"生成时间：{prof.generated_at}",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
    ]
    for k, v in s.items():
        lines.append(f"| {k} | {v} |")

    lines += ["", "## 软删除写法分布", "",
              "> `deleted_` 全为 NULL 的表，用 `deleted_ <> 'Y'` 会把整表数据过滤光。", "",
              "| 写法 | 表数 |", "|---|---|"]
    for k, v in sorted(prof.deleted_styles.items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v} |")

    lines += ["", "## 有数据的业务表（按行数）", "",
              "| 表 | 中文名 | 对象 | 行数 | 时间范围 |", "|---|---|---|---|---|"]
    live = sorted([t for n, t in prof.tables.items() if n.startswith("pp_") and t.usable],
                  key=lambda t: -t.rows)
    for t in live[:60]:
        rng = f"{t.create_min} ~ {t.create_max}" if t.create_min else ""
        lines.append(f"| {t.name} | {t.label} | {t.obj_code} | {t.rows} | {rng} |")

    lines += ["", "## 空表（有对象定义但无数据）", ""]
    for name in prof.empty_objects[:60]:
        lines.append(f"- {name}")

    if prof.orphan_tables:
        lines += ["", "## 有数据但无对象定义的表", ""]
        for n in sorted(prof.orphan_tables, key=lambda x: -prof.tables[x].rows)[:40]:
            lines.append(f"- {n}（{prof.tables[n].rows} 行）")

    return "\n".join(lines) + "\n"


def dump(prof: Profile, path: Path) -> None:
    payload = {
        "database": prof.database,
        "generated_at": prof.generated_at,
        "summary": summarize(prof),
        "deleted_styles": prof.deleted_styles,
        "tables": {n: asdict(t) for n, t in prof.tables.items()},
        "objects": prof.objects,
        "empty_objects": prof.empty_objects,
        "orphan_tables": prof.orphan_tables,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    from .nlq import store

    cfg = store.load()["db"]
    if not (cfg.get("host") and cfg.get("database")):
        print("未配置数据库连接，先执行 bin/pp6 ui 里的「连接并读取结构」")
        return 1
    conn = introspect.connect(cfg["host"], int(cfg.get("port") or 3306),
                              cfg.get("user", ""), cfg.get("password", ""), cfg["database"])
    try:
        print(f"开始画像 {cfg['database']} ...")
        prof = build(conn, cfg["database"])
    finally:
        conn.close()

    out_md = Path("docs/数据库画像.md")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(to_markdown(prof), encoding="utf-8")
    dump(prof, Path("assets/dbprofile.json"))

    for k, v in summarize(prof).items():
        print(f"  {k}: {v}")
    print(f"\n已生成 {out_md} 与 assets/dbprofile.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
