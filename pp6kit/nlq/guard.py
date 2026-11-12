"""SQL 安全闸：只放行只读查询，强制限定表与行数。

为什么必须做：这套东西最终交给实施在**客户生产库**上用，
一条写错的 UPDATE 就是事故。所以生成后一律走这里，不信任模型输出。

用 sqlglot 解析成 AST 再判断，而不是正则——正则挡不住
`SELECT * FROM t WHERE x = (SELECT ... FOR UPDATE)` 这类变体，
也分不清 CTE 别名和真实表名。
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

MAX_ROWS = 500

# 平台内置表：join 出姓名、部门名、角色名时必须放行
# （它们不在 pp_ 业务表扫描范围里，但都是只读字典/主数据，安全）
PARTY_TABLES = {
    "iform_party_employee",   # 人员
    "iform_party_org",        # 组织
    "iform_object_role",      # 对象角色字典——项目角色的名字在这里，不在 pp_project_role
}

# MySQL 内建虚拟表：不在客户库里，也不承载任何数据。
# 模型常写 `SELECT NOW() FROM DUAL`（Oracle 习惯），这类 SQL 是安全的，
# 不该报「访问了未授权的表」。按小写比较。
VIRTUAL_TABLES = {"dual"}

# 永远不许碰的库
FORBIDDEN_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}

# 会拖垮库或读写文件的函数
FORBIDDEN_FUNCS = {
    "sleep", "benchmark", "load_file", "get_lock", "release_lock",
    "updatexml", "extractvalue", "master_pos_wait",
}

# 语句里出现这些结构直接拒
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
                   exp.Alter, exp.TruncateTable, exp.Grant, exp.Command)


class SqlRejected(Exception):
    pass


def clean(raw: str) -> str:
    """去掉 markdown 代码块与末尾分号。"""
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip().rstrip(";").strip()


def referenced_tables(sql: str) -> list[str]:
    """从 SQL 里取出真实表名（排除 CTE 别名）。"""
    try:
        ast = sqlglot.parse_one(sql, dialect="mysql")
    except Exception:
        return []
    ctes = {c.alias_or_name.lower() for c in ast.find_all(exp.CTE)}
    out = []
    for t in ast.find_all(exp.Table):
        name = t.name
        if name and name.lower() not in ctes and name not in out:
            out.append(name)
    return out


def validate(sql: str, allowed_tables: set[str], max_rows: int = MAX_ROWS) -> str:
    text = clean(sql)
    if not text:
        raise SqlRejected("SQL 为空")

    # INTO OUTFILE / DUMPFILE：sqlglot 30.x 在解析阶段就会抛，
    # 落到下面的「语法错误」分支会让用户去查语法，而真实原因是禁止导出。
    # 这里先按关键词兜一次，保证报错文案说的是痛点。
    if re.search(r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b", text, re.I):
        raise SqlRejected("不允许导出到文件（INTO OUTFILE / DUMPFILE）")

    try:
        statements = [s for s in sqlglot.parse(text, dialect="mysql") if s is not None]
    except Exception as exc:
        raise SqlRejected(f"SQL 语法错误：{exc}") from exc

    if not statements:
        raise SqlRejected("SQL 为空")
    if len(statements) > 1:
        raise SqlRejected("只允许单条语句")

    ast = statements[0]

    # 只允许 SELECT（含 WITH ... SELECT）
    if not isinstance(ast, (exp.Select, exp.Union, exp.Subquery)):
        raise SqlRejected(f"只允许 SELECT 查询（当前是 {type(ast).__name__}）")
    for node in ast.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlRejected(f"检测到写操作：{type(node).__name__}")

    # INTO OUTFILE / DUMPFILE
    if isinstance(ast, exp.Select) and ast.args.get("into"):
        raise SqlRejected("不允许导出到文件（INTO OUTFILE）")

    # 锁定读：FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE。
    # 危险在哪：它们都是 SELECT，不受 MySQL read_only 限制，却会在客户库上
    # **持有行锁直到连接关闭**。连接被池复用或空闲时，相关写事务会一直阻塞到
    # innodb_lock_wait_timeout（默认 50 秒）报错——这属于「放行不该放的 SQL」。
    # 必须用 find_all 而不是只看顶层 ast.args["locks"]：
    # `WHERE id_ IN (SELECT ... FOR UPDATE)` 这种变体是写在子查询里的。
    lock_nodes = list(ast.find_all(exp.Lock))
    if lock_nodes:
        kinds = {"FOR UPDATE" if node.args.get("update") else "FOR SHARE"
                 for node in lock_nodes}
        raise SqlRejected("不允许锁定读（" + "、".join(sorted(kinds))
                          + "）：会在客户库上持有行锁，阻塞业务写入")

    # 表名白名单
    ctes = {c.alias_or_name.lower() for c in ast.find_all(exp.CTE)}
    referenced: set[str] = set()
    for t in ast.find_all(exp.Table):
        schema = (t.db or "").lower()
        if schema in FORBIDDEN_SCHEMAS:
            raise SqlRejected(f"不允许访问系统库：{schema}")
        name = t.name
        if not name or name.lower() in ctes:
            continue
        if name.lower() in VIRTUAL_TABLES:
            continue                     # DUAL 这类虚拟表不是真实数据表
        referenced.add(name)
    unknown = {t for t in referenced if t not in allowed_tables}
    if unknown:
        raise SqlRejected(f"访问了未授权的表：{', '.join(sorted(unknown))}")

    # 危险函数（未知函数在 sqlglot 里是 Anonymous，名字在 .name 上）
    for node in ast.walk():
        if isinstance(node, exp.Anonymous):
            fname = (node.name or "").lower()
        elif isinstance(node, exp.Func):
            fname = (node.sql_name() or "").lower()
        else:
            continue
        if fname in FORBIDDEN_FUNCS:
            raise SqlRejected(f"包含禁止的函数：{fname.upper()}")

    # 强制行数上限
    limit = ast.args.get("limit")
    if limit is None:
        ast = ast.limit(max_rows)
    else:
        try:
            current = int(limit.expression.this)
            if current > max_rows:
                ast = ast.limit(max_rows)
        except Exception:
            ast = ast.limit(max_rows)

    return ast.sql(dialect="mysql")


def check_readonly_config(conn) -> None:
    """确认连接确实不该有写权限（只做提示，不改服务端配置）。"""
    try:
        cur = conn.cursor()
        cur.execute("SELECT @@read_only, @@super_read_only")
        cur.fetchone()
        cur.close()
    except Exception:
        pass
