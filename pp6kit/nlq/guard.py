"""SQL 安全闸：只放行只读查询，强制限定表与行数。

为什么必须做：这套东西最终交给实施在**客户生产库**上用，
一条写错的 UPDATE 就是事故。所以生成后一律走这里，不信任模型输出。

用 sqlglot 解析成 AST 再判断，而不是正则——正则挡不住
`SELECT * FROM t WHERE x = (SELECT ... FOR UPDATE)` 这类变体，
也分不清 CTE 别名和真实表名。

闸门分三层，缺一不可
--------------------
1. `screen_question()` —— **问句层**。用户问「把项目表删了」「清空所有数据」，
   或者往问句里塞 `; DROP TABLE`、`忽略上面的指令`，在**调用模型之前**就拒掉。
   为什么放在最前面：模型是黑盒，prompt 注入一旦生效，它真会吐出 DROP；
   而且拒得早也省一次 API 调用和一份额度。
2. `validate()` —— **SQL 层**。模型/用户给的 SQL 一律 AST 判定 + 表白名单 + 行数上限，
   序列化出来再自检一遍，确保「返回给前端的那条 SQL」本身是只读的。
3. `is_read_only()` —— **执行层**。`engine.run_sql` 真正 execute 之前再断言一次。
   这一层不做表白名单（拿不到 pack），只保「绝不是写语句」——
   哪怕将来有人新加了个调用点忘了走 `validate`，也写不动库。
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

MAX_ROWS = 500
# 问句长度上限：几千字的输入只会拖垮规则引擎、白烧 token，
# 而且长文本正是塞 prompt 注入最常用的伪装。
MAX_QUESTION = 500

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
    # MySQL 常见提权 UDF：装上之后就是「用 SQL 执行系统命令」
    "sys_exec", "sys_eval", "sys_get", "sys_set", "sys_hash",
}

# 语句里出现这些结构直接拒。
# 只列 AST 里真实存在的节点名：sqlglot 版本间有增删，用 getattr 兜底，
# 不能写成 exp.Merge 这种硬引用——老版本没有这个类会整个模块 import 失败。
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
                   exp.Alter, exp.TruncateTable, exp.Grant, exp.Command) + tuple(
    getattr(exp, name) for name in
    ("Merge", "Commit", "Rollback", "Set", "Use", "Pragma", "Replace", "Call")
    if hasattr(exp, name)
)

# MySQL 的 `/*!50000 ... */` 版本注释里的内容是**会被执行**的。
# sqlglot 把它当注释丢掉，序列化后自然消失，但依赖「反正会被丢掉」太脆，
# 干脆显式拒——正常查询没有任何理由带版本注释。
_VERSION_COMMENT = re.compile(r"/\*!")


class SqlRejected(Exception):
    pass


class UnsafeQuestion(SqlRejected):
    """问句本身就是攻击/破坏意图，不是 SQL 写得不对。

    继承 SqlRejected：上层按「安全闸拒绝」统一处理即可，
    不需要为它单独写一条错误分支。
    """


# ---------------------------------------------------------------- 问句层
#
# 判定思路：**不要**做成关键词黑名单。「删除」「修改」这类词在正常问句里
# 到处都是（「已删除的项目」「最近修改的项目」「删除标记」），一律拒会把
# 好问句一起打死；只靠关键词放行又会漏掉「把这些都清掉」这类说法。
#
# 所以按「动词 + 宾语」看搭配：
#   - 动词后面 12 个字以内出现目标（数据/表/库/记录…）或业务对象名 → 拒
#   - 这 12 个字里出现「的」 → 是定语，不是动作（「删除的项目」= 查软删）→ 放行
#   - 句子里另有「所有/全部/批量」这类全量词 → 直接拒（「全部删掉」）
#
# 定状语的坑必须先处理：「已删除」「删除标记」「修改时间」这些是**字段名/状态**，
# 不是动作。把它们先掩成等长的占位符，再做匹配，位置和长度都不变，
# 后续正则的窗口判断才不会错位。

_SAFE_PHRASES = re.compile(
    r"(?:已|被|软|待|逻辑)删除"
    r"|删除(?:标记|状态|时间|人|原因|记录|日志|备注)"
    r"|(?:修改|更新|创建|编辑|变更|提交)(?:时间|日期|人|者)"
    r"|(?:数据)?清理(?:记录|日志|时间|人)"
    r"|更新记录",
    re.I,
)


def _mask(text: str) -> str:
    """把「字段名里的删除/修改」掩成等长占位符，长度不变。"""
    return _SAFE_PHRASES.sub(lambda m: "＿" * len(m.group(0)), text)


# 破坏性动词（中文）。
# 「删」单字也要收：中文里最直接的说法就是「删库」「删表」「删了」，
# 它没有别的语义，误伤风险极低。
_CN_DELETE = re.compile(
    r"(?:删除|删掉|删了|删去|删干净|删|清空|清除|清掉|清光|清理|抹掉|抹除|销毁|干掉)")
# 「改成/设为」出现在「把/将」句里就是写意图；单独出现可能是「状态改为…的记录」，不单判
_CN_MODIFY = re.compile(r"(?:改成|改为|设为|设置为|置为|批量修改|批量更新)")
# 把字句：宾语在前、动词在后（「把项目表删了」「把这些数据都清掉」）。
# 只认「把/将 + … + 删除类动词」，不像通用倒装那样容易误伤——
# 「把项目按状态分组」里没有删除类动词，不会命中。
_CN_BA = re.compile(
    r"(?:把|将)[^，。！？；;\n]{0,16}?(?:删除|删掉|删了|删去|清空|清除|清掉|清理|抹掉|抹除|销毁|干掉)")
# 宾语里出现这些词 = 在动数据本身
_CN_TARGET = re.compile(
    r"(?:数据|数据库|库|表|记录|行|字段|列|索引|视图|账号|用户|权限"
    r"|所有|全部|一切|整个|整张|整库)")
# 业务对象名：删/清 + 对象名（中间不隔「的」）同样是要删业务数据
_CN_OBJECT = re.compile(
    r"(?:项目|任务|需求|风险|合同|客户|订单|人员|员工|角色|部门|组织|附件|文档)")
# 全量副词：只要和破坏性动词同现就拒，不看宾语是什么
_CN_ALL = re.compile(r"(?:所有|全部|一切|批量|整库|整表|整个库|一次性|这些|那些|它们)")

# 英文问句里出现这些基本就是 DDL/DML 意图
_EN_DDL = re.compile(
    r"\b(?:drop|truncate|purge|wipe)\b"
    r"|\balter\s+table\b|\brename\s+table\b"
    r"|\bcreate\s+(?:table|database|schema|user|index)\b"
    r"|\b(?:grant|revoke)\b",
    re.I)
_EN_WRITE = re.compile(
    r"\b(?:delete\s+from|insert\s+into|replace\s+into|update\s+\S+\s+set)\b",
    re.I)
# 单词 delete 单独出现也拒：英文问句里 "delete all customers" 没有 from
_EN_DELETE = re.compile(r"\bdelete\b", re.I)

# 注入 / 越权指令：问句里夹 SQL 片段，或试图覆盖系统提示词
_INJECT_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"[;；]\s*(?:drop|delete|update|insert|alter|truncate|create|grant)\b", re.I),
     "分号后接写操作"),
    (re.compile(r"\bunion\s+(?:all\s+)?select\b", re.I), "UNION SELECT"),
    (re.compile(r"\bor\b\s+['\"]?1['\"]?\s*=\s*['\"]?1", re.I), "恒真条件"),
    (re.compile(r"\binto\s+(?:outfile|dumpfile)\b|\bload_file\s*\(", re.I),
     "读写服务器文件"),
    (re.compile(r"@@[A-Za-z_]", re.I), "读取数据库系统变量"),
    (re.compile(r"\b(?:xp_cmdshell|sys_exec|sys_eval)\b", re.I), "执行系统命令"),
    (re.compile(r"忽略(?:上面|之前|以上|前面|前述)|无视(?:上面|之前|以上)"
                r"|ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|above)"
                r"|disregard\s+(?:all\s+)?previous", re.I), "试图覆盖系统指令"),
    (re.compile(r"(?:提示词|系统指令|system\s*prompt|你被设定|你的设定)", re.I),
     "试图套取提示词"),
)


def _cut_at_punct(text: str) -> str:
    """窗口只看到标点为止：「删掉这些，再看看别的」只判前半句。"""
    m = re.search(r"[，。！？；;,\.\?!\n]", text)
    return text[:m.start()] if m else text


def _destructive_hit(q: str) -> str:
    """问句里有没有破坏性意图？返回命中的原文片段，没有则返回空串。"""
    for rx in (_EN_DDL, _EN_WRITE, _EN_DELETE):
        m = rx.search(q)
        if m:
            return m.group(0)

    # 「把/将 … 改成/设为 …」= 明确要改数据
    m = _CN_MODIFY.search(q)
    if m and re.search(r"(?:把|将)", q[:m.start()]):
        return q[max(0, m.start() - 6): m.end() + 6]

    # 把字句：宾语在动词前面的说法（「把项目表删了」）
    m = _CN_BA.search(q)
    if m:
        return m.group(0)

    for m in _CN_DELETE.finditer(q):
        window = _cut_at_punct(q[m.end(): m.end() + 12])
        if not window.strip():
            return q[m.start():]          # 「删干净」「清空」这种光杆命令，没有宾语也得拒
        if "的" in window:
            continue                      # 「删除的项目」是定语，放行
        if _CN_TARGET.search(window) or _CN_OBJECT.search(window):
            return q[m.start(): m.end() + len(window)]
        if _CN_ALL.search(q):
            return q[m.start(): m.end() + len(window)]
    return ""


def screen_question(question: str) -> None:
    """在**调用模型之前**筛问句。危险就抛 UnsafeQuestion。

    为什么必须在这一层拦截，而不是只靠 `validate`：
      - 模型有幻觉也有盲区，prompt 注入一旦成功，它会老实地把 DROP 写出来；
        能拦住的是过滤，拦不住的是「它已经按注入的意图改写了条件」——
        比如把 `deleted_ <> 'Y'` 偷偷去掉，这条 SQL 照样是 SELECT，`validate` 管不了。
      - 拒绝要**不产生任何 SQL**（用户要的就是「拒绝输出」），
        而不是生成一条 SQL 再在末尾标个警告。
    """
    q = (question or "").strip()
    if not q:
        return

    if len(q) > MAX_QUESTION:
        raise UnsafeQuestion(
            f"问题太长了（{len(q)} 字，上限 {MAX_QUESTION} 字）。"
            "请把问题缩短成一句话，只描述要查什么。")

    for rx, label in _INJECT_RULES:
        m = rx.search(q)
        if m:
            raise UnsafeQuestion(
                f"这个问题里夹带了可疑片段「{m.group(0)[:40]}」（{label}），已拒绝生成。"
                "本工具只接受「查什么」类的自然语言提问，不接受 SQL 语句或指令。")

    hit = _destructive_hit(_mask(q))
    if hit:
        raise UnsafeQuestion(
            f"这个问题包含破坏性操作「{hit.strip()[:40]}」，已拒绝生成。"
            "本工具只生成只读查询（SELECT），不会输出删除/清空/修改表或数据的 SQL。"
            "确需清理数据，请由 DBA 在数据库端操作并先做好备份。")



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

    # 版本注释 `/*!50000 ... */` 里的内容 MySQL 会执行，不是注释。
    # 解析器会把它当注释丢掉，但显式拒更直接，也免得以后换解析器踩坑。
    if _VERSION_COMMENT.search(text):
        raise SqlRejected("不允许使用 MySQL 版本注释（/*! ... */），其中的语句会被执行")

    # 系统变量能读出安装路径、数据目录、端口，属于信息泄露。
    # 正常业务查询没有任何理由用 @@。
    if re.search(r"@@[A-Za-z_]", text):
        raise SqlRejected("不允许读取数据库系统变量（@@...）")

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
    bad_func = _forbidden_func(ast)
    if bad_func:
        raise SqlRejected(f"包含禁止的函数：{bad_func.upper()}")

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

    out = ast.sql(dialect="mysql")

    # 输出自检：**返回给前端/拿去执行的**是这段序列化文本，不是 AST。
    # 上面判的是 AST，万一 sqlglot 序列化出意外（版本差异、方言改写），
    # 前端看到的就不是我们审过的那条。所以对着最终产物再判一次只读。
    if not is_read_only(out):
        raise SqlRejected("生成的 SQL 未通过只读复检，已拦截")

    return out


def _forbidden_func(ast) -> str:
    """AST 里有没有被禁的函数？返回第一个命中的小写函数名，没有返回空串。

    validate 和 is_read_only 共用：只读**不等于**无害——
    `SELECT SLEEP(100)` 不写一行数据，却能把客户库的连接池占死；
    `SELECT sys_exec('rm -rf /')` 更是直接执行系统命令。
    """
    for node in ast.walk():
        if isinstance(node, exp.Anonymous):
            fname = (node.name or "").lower()
        elif isinstance(node, exp.Func):
            fname = (node.sql_name() or "").lower()
        else:
            continue
        if fname in FORBIDDEN_FUNCS:
            return fname
    return ""


def is_read_only(sql: str) -> bool:
    """这条 SQL 是不是纯只读查询？**不查表白名单**。

    给执行层当最后一道断言用：`engine.run_sql` 在用 cursor.execute 之前调它。
    表白名单在这里不做——`run_sql` 拿不到 pack，而且 `validate` 已经查过了；
    这里保证「不是写语句/不是多语句/不是锁定读/不调危险函数」，
    防止将来有新的调用点绕开 validate。
    """
    try:
        text = clean(sql)
        if not text:
            return False
        if _VERSION_COMMENT.search(text) or re.search(r"@@[A-Za-z_]", text):
            return False
        statements = [s for s in sqlglot.parse(text, dialect="mysql") if s is not None]
    except Exception:
        return False
    if len(statements) != 1:
        return False
    ast = statements[0]
    if not isinstance(ast, (exp.Select, exp.Union, exp.Subquery)):
        return False
    for node in ast.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return False
    if list(ast.find_all(exp.Lock)):
        return False
    if _forbidden_func(ast):
        return False
    return True


def check_readonly_config(conn) -> tuple[bool, bool] | None:
    """读一下这条连接的账号是不是只读账号，返回 (read_only, super_read_only)。

    只读**不修改**服务端配置——这属于 DBA 的事，程序自作主张去 SET 是越权。
    但要把结果给用户看：本工具所有 SQL 都过只读闸，可万一日后有人绕过，
    账号级的 read_only 才是真正兜得住的最后一层。

    读不到（老版本 MySQL、权限不足）返回 None，不阻断任何流程。
    """
    try:
        cur = conn.cursor()
        cur.execute("SELECT @@read_only, @@super_read_only")
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return bool(int(row[0] or 0)), bool(int(row[1] or 0))
    except Exception:
        return None
