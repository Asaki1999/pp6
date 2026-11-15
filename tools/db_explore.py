#!/usr/bin/env python3
"""对着真实库做**只读**探索：只发 SELECT / SHOW / DESC / EXPLAIN，每条都打出来。

为什么单独写一个脚本，而不是随手连上去敲 SQL：
  1. 只读是**构造上**保证的——本文件里没有任何 execute 写语句的地方，
     连接还用 introspect.connect(readonly=True) 设了会话只读；
  2. 每条语句都打到 stdout，事后能一条条核对「到底对库做了什么」；
  3. 不读命令行参数里的密码（用环境变量），免得落进 shell 历史。

用法（密码只走环境变量）：

    PP6_DB_HOST=pp6-test.example.com PP6_DB_PORT=3306 \\
    PP6_DB_NAME=powerproject6 PP6_DB_USER=xxx PP6_DB_PASS=xxx \\
    .venv/bin/python tools/db_explore.py

    # 只看某几张表
    ... tools/db_explore.py pp_project pp_task

输出：表清单 / 行数 / 列定义 / 索引，够画结构图谱用。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pp6kit.nlq import guard  # noqa: E402
from pp6kit.nlq.introspect import connect  # noqa: E402

LOG: list[str] = []


def q(conn, sql: str, args=None):
    """执行一条**只读**语句。任何写语句在发出去之前就被这里拦住。"""
    if not guard.is_read_only(sql):
        raise SystemExit(f"拒绝执行（不是只读语句）：{sql}")
    LOG.append(sql)
    cur = conn.cursor()
    try:
        cur.execute(sql, args or ())
        return cur.fetchall()
    finally:
        cur.close()


def main(argv: list[str]) -> int:
    conf = {k: os.environ.get(f"PP6_DB_{k.upper()}", "") for k in ("host", "port", "name", "user", "pass")}
    if not (conf["host"] and conf["name"] and conf["user"]):
        print(__doc__)
        return 2

    conn = connect(conf["host"], int(conf["port"] or 3306), conf["user"],
                   conf["pass"], conf["name"], readonly=True)
    state = guard.check_readonly_config(conn)
    print(f"连接 {conf['user']}@{conf['host']}:{conf['port']}/{conf['name']}")
    print(f"只读状态：session={state.get('session')} server={state.get('server')} super={state.get('super')}")
    if state.get("session") is not True:
        print("⚠️ 会话只读没设上（老版本或权限不足）——本次仅靠脚本自身的只读断言，注意核对日志")
    print()

    tables = [r[0] for r in q(conn, "SHOW TABLES")]
    want = [t for t in argv if t] or tables
    print(f"共 {len(tables)} 张表" + (f"，本次只看 {len(want)} 张" if argv else "") + "\n")

    rows_summary = []
    for name in want:
        if name not in tables:
            print(f"  跳过 {name}（不存在）")
            continue
        total = q(conn, f"SELECT COUNT(*) FROM `{name}`")[0][0]
        cols = q(conn, f"SHOW COLUMNS FROM `{name}`")
        rows_summary.append((name, total, len(cols)))

    print(f"{'表名':<42}{'行数':>10}{'列数':>7}")
    for name, total, ncols in sorted(rows_summary, key=lambda x: -x[1]):
        print(f"{name:<42}{total:>10}{ncols:>7}")

    print("\n--- 实际执行的语句（共 %d 条，全部只读）---" % len(LOG))
    for sql in dict.fromkeys(LOG):
        print("  " + sql)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
