"""SQL 助手动作：把 DBX 的 Ask / Agent 全套能力接到 pp6。

DBX 的两级结构原样照搬：

    Ask   —— 只看表结构，不执行任何语句
             自动 / 通用问答 / 生成 SQL / 解释 SQL / 优化 SQL / 修复错误 /
             转换方言 / 生成样例
    Agent —— 可以先查表结构、看取值、试跑查询，证据够了再落笔
             自动 / 通用问答 / 查询数据 / 查看表结构 / 执行并解释 / 生成但不执行

与 DBX 的一处刻意差异：DBX 的 Ask 里「生成 SQL」只生成、不执行；pp6 的「自动」
沿用老用户的习惯——没有 SQL 就生成并执行，已有 SQL 就执行并解释。想只生成，
显式选「生成但不执行」。

安全边界（本项目唯一红线）：
  - Ask 的每个动作都**不执行** SQL；「生成样例」产出的 INSERT 只展示。
  - Agent 的执行仍走 `guard.validate` + `engine.run_sql`（后者内部二次断言只读）。
  - 所有产物都过 `guard.is_read_only` 打 `executable` 标记，不是只读的前端
    禁用「重新执行」，也永远进不了示例库。
"""
from __future__ import annotations

import re

from . import agent, engine, guard, llm, semantic
from .introspect import SchemaPack

# ---------------------------------------------------------------- 动作清单
# 前端菜单、后端校验、提示词选择共用这一份。
#   needs_sql —— 没有 SQL 时点它属于误操作，直接提示，别让模型瞎猜
#   executes  —— 会不会真的在库上跑（决定要不要追问时间范围、要不要给结果表）
ACTIONS: dict[str, list[dict]] = {
    "ask": [
        # 自动 = 直接交给大模型；模型不可用（没配 Key / 全渠道失败）才回退本地规则。
        # 2026-11-27 用户否掉了「规则优先」：规则引擎覆盖有限，让它先跑等于让一个
        # 猜得不准的东西决定答案。规则现在的定位是离线兜底与 AI 答案的缓存。
        {"id": "auto", "label": "自动", "icon": "sparkle",
         "hint": "大模型直接生成，不可用时回退本地", "needs_sql": False,
         "executes": True, "needs_llm": False},
        {"id": "chat", "label": "通用问答", "icon": "chat",
         "hint": "问业务概念与数据口径，不生成 SQL", "needs_sql": False, "executes": False},
        {"id": "generate", "label": "生成 SQL", "icon": "wand",
         "hint": "只生成不执行，可以先改再跑", "needs_sql": False, "executes": False},
        {"id": "explain", "label": "解释 SQL", "icon": "help",
         "hint": "逐段讲清这条 SQL 在查什么", "needs_sql": True, "executes": False},
        {"id": "optimize", "label": "优化 SQL", "icon": "bolt",
         "hint": "改写为更快更稳的写法", "needs_sql": True, "executes": False},
        {"id": "fix", "label": "修复错误", "icon": "wrench",
         "hint": "按报错信息修正 SQL", "needs_sql": True, "executes": False},
        {"id": "dialect", "label": "转换方言", "icon": "swap",
         "hint": "MySQL 转成其它数据库方言", "needs_sql": True, "executes": False},
        {"id": "sample", "label": "生成样例", "icon": "rows",
         "hint": "生成测试数据语句，绝不执行", "needs_sql": False, "executes": False},
    ],
    "agent": [
        {"id": "auto", "label": "自动", "icon": "sparkle",
         "hint": "有 SQL 就执行并解释，没有就查数据", "needs_sql": False, "executes": True},
        {"id": "chat", "label": "通用问答", "icon": "chat",
         "hint": "先查证再回答，不生成 SQL", "needs_sql": False, "executes": False},
        {"id": "query", "label": "查询数据", "icon": "play",
         "hint": "生成 SQL 并执行", "needs_sql": False, "executes": True},
        {"id": "schema", "label": "查看表结构", "icon": "table",
         "hint": "字段含义、类型、外键与空列", "needs_sql": False, "executes": False},
        {"id": "run_explain", "label": "执行并解释", "icon": "play-help",
         "hint": "跑完再用人话讲结果", "needs_sql": False, "executes": True},
        {"id": "dryrun", "label": "生成但不执行", "icon": "pause",
         "hint": "只给 SQL，不碰库", "needs_sql": False, "executes": False},
    ],
}

MODE_LABELS = {"ask": "Ask", "agent": "Agent"}

# 转换方言的目标。达梦/金仓是国内项目常迁的目标，一并列上。
DIALECT_TARGETS = ["PostgreSQL", "SQL Server", "Oracle", "SQLite", "达梦 DM", "人大金仓"]

_TARGET_ALIASES = {
    "postgres": "PostgreSQL", "postgresql": "PostgreSQL", "pg": "PostgreSQL",
    "sqlserver": "SQL Server", "mssql": "SQL Server", "sql server": "SQL Server",
    "oracle": "Oracle", "ora": "Oracle",
    "sqlite": "SQLite",
    "dm": "达梦 DM", "达梦": "达梦 DM",
    "kingbase": "人大金仓", "金仓": "人大金仓", "人大金仓": "人大金仓",
}

# 无大模型时仍可用的动作（本地语法层能给出有用结果）
_LOCAL_FALLBACK = {"explain"}

# 不连库也能用的动作：解释/改写/方言/通用问答只跟 SQL 文本和模型打交道。
# 其余动作（生成、样例、查结构、查数据）离开 schema 只能靠猜，必须先连库。
DB_OPTIONAL = {"chat", "explain", "optimize", "fix", "dialect"}


def needs_db(action: str) -> bool:
    return action not in DB_OPTIONAL


def catalog() -> dict:
    """给前端的菜单配置。纯数据，不含任何库信息。

    needs_llm 让前端能提前标出「这个动作要配 Key」——用户点完才发现要配 Key
    是最容易让人放弃的一种交互。
    """
    actions = {}
    for m, acts in ACTIONS.items():
        actions[m] = [
            dict(a, needs_llm=a.get("needs_llm",
                                    not (m == "ask" and a["id"] == "explain")))
            for a in acts
        ]
    return {
        "modes": [{"id": "ask", "label": MODE_LABELS["ask"]},
                  {"id": "agent", "label": MODE_LABELS["agent"]}],
        "actions": actions,
        "targets": list(DIALECT_TARGETS),
    }


def actions_for(mode: str) -> list[dict]:
    return ACTIONS.get((mode or "").lower(), [])


def find_action(mode: str, action: str) -> dict:
    """取动作定义；不存在就报错（拼错的 action 不该被静默当成默认）。"""
    m = (mode or "ask").lower()
    if m not in ACTIONS:
        raise ValueError(f"未知模式：{mode}")
    a = (action or "auto").lower()
    for spec in ACTIONS[m]:
        if spec["id"] == a:
            return spec
    names = "、".join(x["id"] for x in ACTIONS[m])
    raise ValueError(f"{MODE_LABELS[m]} 模式下没有「{action}」这个动作，可选：{names}")


def resolve(mode: str, action: str, has_sql: bool) -> str:
    """把「自动」翻译成具体动作。

    Ask：有 SQL 就解释，没有就生成（用户点了自动，说明他想省心，不想先选动作）
    Agent：有 SQL 就执行并解释，没有就查数据
    """
    a = find_action(mode, action)["id"]
    if a != "auto":
        return a
    if has_sql:
        return "explain" if (mode or "").lower() == "ask" else "run_explain"
    return "generate" if (mode or "").lower() == "ask" else "query"


def wants_clarify(mode: str, action: str, has_sql: bool) -> bool:
    """只有「要执行的查询」才追问时间范围。

    解释/优化/方言/样例都不查库，追问时间范围只会打断用户。
    """
    return find_action(mode, action)["executes"] or \
        resolve(mode, action, has_sql) in ("generate", "query")


# ---------------------------------------------------------------- 只读标注

def _judge_output(sql_text: str, allowed: set) -> dict:
    """给产物打「能不能执行」的标记，**不改写内容**、也不执行。

    Ask 模式产出的 SQL 没经过任何闸门就直接展示给用户了，用户很可能复制到
    生产库去跑——所以这里至少要标清楚两件事：它是不是只读，能不能过闸。
    """
    text = (sql_text or "").strip()
    if not text:
        return {"executable": False, "warning": ""}
    if not guard.is_read_only(text):
        return {"executable": False,
                "warning": "这条语句会改动数据，工具不会执行它，请只在测试库使用"}
    try:
        safe = guard.validate(text, allowed)
    except guard.SqlRejected as exc:
        return {"executable": False, "warning": f"未通过只读校验：{exc}"}
    return {"executable": True, "sql": safe}


# ---------------------------------------------------------------- 提示词

_COMMON = """你是 PowerProject 项目管理系统的 MySQL 数据助手。
系统里的业务表以 pp_ 开头，字典/人员表以 iform_ 开头。"""

_RULES = engine.SYSTEM.split("请只输出 SQL")[0].rstrip()

_ASK_PROMPTS = {
    "chat": """回答用户关于业务概念、字段含义、数据口径、系统用法的问题。
要求：
1. 先给结论，再给依据，3-6 句；条目多时用短列表。
2. 只依据下面给出的表结构与业务口径作答；不确定就直说不确定，绝不编造表名或列名。
3. 不写 SQL。用户要 SQL 时，提醒他切到「生成 SQL」。
4. 涉及具体数字时说明依据（哪张表、什么口径），没有查过就不要编数字。""",

    "generate": "",     # 直接用 engine.SYSTEM

    "explain": """把用户给的 SQL 逐段讲清楚，让不写 SQL 的人也能看懂。
按四段输出，用中文小标题：
【一句话】这条查询在找什么。
【数据来源】用到哪些表，各是什么角色（主表 / 关联 / 字典）。
【筛选与分组】每个 WHERE、GROUP BY、HAVING 的业务含义。
【注意点】可能的坑：漏了软删除过滤、笛卡尔积、NULL 比较、全表扫描、隐式类型转换。
只讲这条 SQL 里真实存在的东西，不要复述通用知识，不要重写 SQL。""",

    "optimize": """用户会给你一条 MySQL 查询，请给出优化后的版本。
输出顺序：
1. 一个 ```sql 代码块，里面是改写后的完整 SQL（可直接替换原文）。
2. 「改了什么」：逐条列改动与理由，最多 6 条。
优化手段按收益排序：命中索引（别把列包进函数）、去掉 SELECT *、减少子查询改 JOIN、
去掉重复 DISTINCT、分页用游标或覆盖索引、避免 OR 导致索引失效。
硬性：改写后必须仍是**只读查询**，不得改变业务语义；没有把握的优化不要做。""",

    "fix": """用户会给一条 SQL，可能附带报错信息。请输出修好的 SQL。
输出顺序：
1. 一个 ```sql 代码块，里面是修正后的完整 SQL。
2. 「问题在哪」：一条一句，说清原因和改法。
排查优先级：列名/表名不存在 → deleted_ 用在没有该列的表上 → GROUP BY 与
ONLY_FULL_GROUP_BY → 聚合函数误用 → 保留字未转义 → 字符集/排序规则。
若信息不足，先说明还缺什么（报错原文 / 表结构），不要瞎改。""",

    "dialect": """把用户给的 MySQL SQL 转换成 {target} 方言。
输出顺序：
1. 一个 ```sql 代码块，里面是目标方言的完整 SQL。
2. 「转换点」：函数、日期处理、字符串拼接、分页、自增与序列、标识符引用、布尔与 NULL。
不改变业务语义；目标方言不支持的写法要明确说不可转换或给出替代方案。""",

    "sample": """根据用户指定的表，生成可直接执行的测试数据语句。
硬性要求：
1. 只输出 ```sql 代码块，里面是 INSERT 语句，每张表最多 5 行。
2. 主键 id_ 用可读的测试值（如 'test_001'），不要用 UUID。
3. 外键列必须填**下面给出的真实取值**（值来自 introspect 采样），绝不编造，
   否则插进去就是悬空外键。
4. 标注「整列为空」的列可以给值，但要在注释里写明这是测试数据。
5. 语句开头加一行 `-- 仅用于测试库，请勿在生产库执行`。
这些语句**不会被本工具执行**，用户会自己复制出去跑。""",
}

_AGENT_PROMPTS = {
    "chat": """用工具核实后再回答用户的问题。
规则：
1. 能查证就先查证（表结构、真实取值、试跑一条小查询），再下结论。
2. 引用具体数字必须说明依据：哪张表、什么条件、多少行。
3. 最终答案用纯文本，不要输出 SQL 代码块，除非用户明确要 SQL。
4. 查不到就说查不到，不要用常识补；不确定的地方标出来。""",

    "schema": """用户想知道某张表或某个业务对象有哪些字段、字段什么意思、能不能为空、外键指向哪。
先用 list_tables 找到表，再用 describe_table 看列，必要时 sample_values 看真实取值。
最终答案用 Markdown 表格：| 字段 | 含义 | 类型 | 说明 |
表格后补两行：这张表多少行；哪些列整列为空 / 是外键。""",
}


def _schema_catalog(pack: SchemaPack, limit: int = 40) -> str:
    """表目录：只有名字、中文名、行数。给「通用问答」用，不占预算。"""
    rows = sorted(pack.tables.values(), key=lambda t: -t.rows)[:limit]
    lines = [f"共 {len(pack.tables)} 张业务表，按数据量列前 {len(rows)} 张："]
    lines += [f"  {t.name}（{t.label or '无中文名'}，{t.rows} 行）" for t in rows]
    lines.append("内置表：iform_party_employee 员工、iform_party_org 部门、iform_cat_dic 字典项")
    return "\n".join(lines)


def _schema_for(action: str, pack: SchemaPack, question: str, sql: str,
                pinned: list[str] | None) -> str:
    """按动作决定给多少 schema——schema 是提示词里最贵的一段。"""
    if action == "chat":
        return _schema_catalog(pack)
    if action == "dialect":
        return ""            # 纯语法转换，看表结构没用
    if action == "sample":
        tables = engine.select_tables(pack, question, pinned=pinned)
        return engine._schema_block(pack, tables, keep=pinned)
    if action in ("explain", "optimize", "fix"):
        # 解释/优化/修复：只给 SQL 里真的引用了的表，避免塞 6 张无关表
        names = [n for n in guard.referenced_tables(sql) if n in pack.tables]
        if not names:
            names = engine.select_tables(pack, question or sql, pinned=pinned)
        else:
            names = names[:8]
        return engine._schema_block(pack, names, keep=names)
    tables = engine.select_tables(pack, question, pinned=pinned)
    return engine._schema_block(pack, tables, keep=pinned)


def build_messages(action: str, pack: SchemaPack, question: str, sql: str = "",
                   sql_error: str = "", target: str = "PostgreSQL",
                   instructions: str = "", pinned: list[str] | None = None,
                   examples: list[dict] | None = None,
                   context: dict | None = None) -> list[dict]:
    """按动作拼提示词。纯函数，不读环境、不联网，便于单测。"""
    if action == "generate":
        # 生成 SQL 沿用 engine 那份 16 条中文语序规则，避免两处规则漂移
        return engine.build_messages(pack, question, examples, pinned=pinned,
                                     instructions=instructions, context=context)

    body = _ASK_PROMPTS.get(action, "")
    if not body:
        raise ValueError(f"没有「{action}」的提示词")
    if "{target}" in body:
        body = body.replace("{target}", target)

    parts = [_COMMON, "", body]
    if instructions:
        parts += ["", "=== 全局指令（用户配置，与上面规则冲突时以此为准） ===", instructions]
    if notes := semantic.prompt_block(pack):
        parts += ["", "=== 业务口径 ===", notes]
    schema = _schema_for(action, pack, question, sql, pinned)
    if schema:
        parts += ["", "=== 可用表结构 ===", schema]
    if examples and action in ("generate", "sample"):
        parts += ["", "=== 同库已验证的问答（照这个写法） ==="]
        for ex in examples[:4]:
            parts.append(f"问题：{ex['question']}\nSQL：{ex['sql']}")

    if sql:
        parts += ["", "=== 待处理的 SQL ===", sql]
    if sql_error:
        parts += ["", "=== 报错信息 ===", sql_error]
    parts += engine.context_block(context)
    parts += ["", "=== 待回答 ===", f"问题：{question}" if question else "问题：（未填写）"]
    return [
        {"role": "system", "content": "\n".join(parts)},
        {"role": "user", "content": question or "请按上面的要求处理这条 SQL。"},
    ]


def agent_messages(objective: str, pack: SchemaPack, question: str,
                   instructions: str = "", pinned: list[str] | None = None,
                   examples: list[dict] | None = None,
                   sql: str = "",
                   context: dict | None = None) -> list[dict]:
    """Agent 的问答类提示词：复用 agent.TOOL_HELP，但不要「只输出 SQL」的收尾。"""
    parts = [
        _RULES, "",
        "=== 查证模式 ===",
        "你还没看到完整表结构。不确定的地方先用工具取证据。",
        "证据够了就立刻给最终答案，不要为了用工具而用工具。", "",
        agent.TOOL_HELP,
        "",
        "=== 本次任务 ===",
        objective,
    ]
    if pinned:
        names = "、".join(f"{pack.tables[n].name}（{pack.tables[n].label}）"
                         for n in pinned if n in pack.tables)
        if names:
            parts += ["", f"=== 用户点名要用的表 ===\n{names}"]
    if notes := semantic.prompt_block(pack):
        parts += ["", "=== 业务口径 ===", notes]
    if instructions:
        parts += ["", "=== 全局指令（用户配置） ===", instructions]
    if examples:
        parts += ["", "=== 同库已验证的问答（参考） ==="]
        for ex in examples[:3]:
            parts.append(f"问题：{ex['question']}\nSQL：{ex['sql']}")
    if sql:
        parts += ["", "=== 当前 SQL ===", sql]
    parts += engine.context_block(context)
    parts += ["", "=== 用户的问题 ===", question or "（未填写）"]
    return [{"role": "system", "content": "\n".join(parts)},
            {"role": "user", "content": question or "请按上面的任务作答。"}]


# ---------------------------------------------------------------- 结果提取

# 代码块围栏后面那一行可能是语言标记，也可能直接是第一行 SQL。
# 只认白名单，别用「纯字母」之类的宽判据——SQL 关键字 INSERT/SELECT 也是纯字母，
# 早先按词表匹配语言标记，「生成样例」返回 ```INSERT 时整段 SQL 被当成标记丢掉。
_LANG = re.compile(
    r"^(?:sql|mysql|mariadb|postgres(?:ql)?|pgsql|plsql|tsql|sqlite|oracle|"
    r"dm|kingbase|text|plain|plaintext|json|yaml|bash|sh|console)$", re.I)


def split_code(raw: str) -> tuple[str, str]:
    """把「代码块 + 说明」拆开。模型几乎总是代码块在前、解释在后。

    按 ``` 切分：奇数段是代码块，偶数段是说明文字。比正则抠更稳，
    多块、单行、语言标记缺失都不会丢内容。
    """
    text = (raw or "").strip()
    if "```" not in text:
        return "", text

    codes: list[str] = []
    prose: list[str] = []
    for i, chunk in enumerate(text.split("```")):
        if i % 2 == 0:
            prose.append(chunk)
            continue
        lines = chunk.split("\n")
        if lines and _LANG.match(lines[0].strip()):
            lines = lines[1:]
        body = "\n".join(lines).strip()
        if body:
            codes.append(body)

    if codes:
        return "\n\n".join(codes), "\n".join(prose).strip()
    return "", text


def _looks_like_sql(text: str) -> bool:
    head = guard.clean(text or "")[:1200].upper()
    return head.startswith(("SELECT", "WITH", "SHOW", "DESC", "DESCRIBE", "EXPLAIN"))


def _looks_like_write(text: str) -> bool:
    head = guard.clean(text or "")[:1200].upper()
    return head.startswith(("INSERT", "REPLACE", "UPDATE", "DELETE"))


# ---------------------------------------------------------------- 本地降级

def local_explain(sql: str, pack: SchemaPack) -> str:
    """没有大模型时的「解释 SQL」：只讲语法层能证实的东西。

    比模型版浅，但每句话都能从 SQL 本身验证——这恰好是模型最容易编造的地方。
    """
    if not sql.strip():
        return ""
    bits: list[str] = []
    summary = ""
    try:
        summary = engine.explain_sql(sql, pack)
    except Exception:
        summary = ""
    if summary:
        bits.append(f"【一句话】{summary}。")
    try:
        tables = guard.referenced_tables(sql)
    except Exception:
        tables = []
    if tables:
        named = [f"{n}（{pack.tables[n].label}）" if n in pack.tables else n for n in tables]
        bits.append("【数据来源】" + "、".join(named[:8]))
    from .explain import describe
    info = describe(sql)
    if info.get("filters"):
        bits.append(f"【筛选】{len(info['filters'])} 个 WHERE 段")
    if info.get("groups"):
        bits.append(f"【分组】{'；'.join(info['groups'][:2])}")
    if info.get("aggregates"):
        bits.append(f"【聚合】{'、'.join(info['aggregates'][:6])}")
    if info.get("limits"):
        bits.append(f"【行数】{'；'.join(info['limits'][:2])}")
    bits.append("（未配置大模型，这里是语法层摘要；配置后可以给出业务层面的解释）")
    return "\n".join(bits)


# ---------------------------------------------------------------- Agent 循环

_ANS_KEYS = ("answer", "text", "reply")


def _agent_loop(conn, pack: SchemaPack, cfg: dict, allowed: set,
                messages: list[dict], want: str, on_event=None,
                max_turns: int = agent.MAX_TURNS) -> tuple[dict, str, list[dict]]:
    """want="sql" 时拿到 SQL 就返回（不执行）；want="answer" 时拿到文本就返回。

    协议与 agent.run 完全一致（每轮一个 JSON 对象），工具实现也共用一份。
    """
    trace: list[dict] = []
    used_label = ""
    last_raw = ""

    for turn in range(max_turns):
        raw, used = llm.chat_any(cfg, messages)
        used_label = used.get("model") or ""
        last_raw = raw
        parsed = agent._parse(raw)

        if parsed and isinstance(parsed.get("tool"), str):
            name = parsed["tool"]
            args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
            if on_event:
                try:
                    on_event({"stage": "tool", "tool": name, "args": args})
                except Exception:
                    pass
            result = agent._run_tool(name, args, pack, conn, allowed)
            trace.append({"tool": name, "args": args, "result": result[:200]})
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "工具结果：\n" + result},
            ]
            continue

        if parsed:
            if want == "sql":
                sql = parsed.get("sql")
                if isinstance(sql, str) and sql.strip():
                    return ({"sql": sql.strip(), "note": str(parsed.get("note") or "")},
                            used_label, trace)
            else:
                for key in _ANS_KEYS:
                    val = parsed.get(key)
                    if isinstance(val, str) and val.strip():
                        return ({"answer": val.strip()}, used_label, trace)

        # 没按协议来：SQL 就整段当 SQL，问答就整段当回答
        if want == "sql" and _looks_like_sql(raw):
            return ({"sql": guard.clean(raw), "note": ""}, used_label, trace)
        if want == "answer" and (raw or "").strip():
            return ({"answer": raw.strip()}, used_label, trace)

        messages += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": "请继续：可以调用工具核实，或直接给出最终答案。"},
        ]

    # 轮次用尽：给一次不带工具的机会
    messages.append({"role": "user", "content":
                     "轮次用尽。直接给出最终答案，不要再调用工具。"
                     if want == "answer" else
                     '轮次用尽。直接输出 {"sql":"...","note":"..."}。'})
    raw, used = llm.chat_any(cfg, messages)
    used_label = used.get("model") or used_label
    parsed = agent._parse(raw)
    if want == "sql":
        sql = (parsed or {}).get("sql") if parsed else None
        if not (isinstance(sql, str) and sql.strip()):
            sql = guard.clean(raw)
        return ({"sql": sql or "", "note": str((parsed or {}).get("note") or "")},
                used_label, trace)
    answer = ""
    if parsed:
        for key in _ANS_KEYS:
            if isinstance(parsed.get(key), str) and parsed[key].strip():
                answer = parsed[key].strip()
                break
    return ({"answer": answer or (last_raw or "").strip()}, used_label, trace)


def _rows_brief(columns: list[str], rows: list[list], limit: int = 20) -> str:
    if not columns:
        return "（没有列）"
    head = " | ".join(columns)
    body = "\n".join(" | ".join("" if v is None else str(v)[:30] for v in r)
                     for r in rows[:limit])
    more = f"\n…… 共 {len(rows)} 行（只回灌前 {limit} 行）" if len(rows) > limit else ""
    return f"{len(rows)} 行：\n{head}\n{body}{more}"


# ---------------------------------------------------------------- 主入口

def run(conn, pack: SchemaPack, payload: dict, cfg: dict, allowed: set,
        on_delta=None, on_event=None) -> dict:
    """执行一次助手动作。返回结构与 engine.ask / agent.run 对齐，前端一套渲染。"""
    mode = (payload.get("mode") or "ask").lower()
    action_raw = (payload.get("action") or "auto").lower()
    question = (payload.get("question") or "").strip()
    sql = _pick_sql(payload.get("sql"), question)
    sql_error = (payload.get("error") or "").strip()
    target = _target(payload.get("target"))
    pinned = payload.get("pinned") or []
    instructions = payload.get("instructions") or ""
    examples = payload.get("examples") or []
    context = payload.get("context") if isinstance(payload.get("context"), dict) else None

    spec = find_action(mode, action_raw)
    if spec["needs_sql"] and not sql:
        raise ValueError("这个动作需要一条 SQL：请先让工具生成，或把 SQL 贴进 SQL 框")
    if not question and not sql:
        raise ValueError("请先输入问题或 SQL")

    action = resolve(mode, action_raw, bool(sql))

    if not llm.active_channels(cfg) and action not in _LOCAL_FALLBACK:
        raise llm.LlmError(
            "这个动作需要大模型。请在「系统设置」里填一个 API Key；"
            "没配模型时仍可用「自动」走本地规则查询。")
    if action in ("query", "run_explain"):
        return _run_agent_query(conn, pack, payload, cfg, allowed, action,
                                question, sql, pinned, instructions, examples,
                                on_event, context)
    if mode == "agent":
        return _run_agent_text(conn, pack, cfg, allowed, action, question, sql,
                               pinned, instructions, examples, on_event, context)
    return _run_ask(pack, action, question, sql, sql_error, target,
                    cfg, allowed, pinned, instructions, examples, on_delta,
                    context)


# ---------------------------------------------------------------- Ask 分支

def _run_ask(pack: SchemaPack, action: str, question: str, sql: str,
             sql_error: str, target: str, cfg: dict, allowed: set,
             pinned: list[str], instructions: str, examples: list[dict],
             on_delta=None, context: dict | None = None) -> dict:
    # 解释 SQL 是唯一能在没配模型时给出真实结果的 Ask 动作：
    # 语法层摘要虽浅，但每句都能从 SQL 本身验证，比一句「请先配 Key」有用。
    if not llm.active_channels(cfg) and action == "explain":
        return _text_result(action, "ask", local_explain(sql, pack), "", question,
                            extra={"source": "本地语法解析（未配置大模型）"})

    messages = build_messages(action, pack, question, sql=sql,
                              sql_error=sql_error, target=target,
                              instructions=instructions, pinned=pinned,
                              examples=examples if action == "generate" else None,
                              context=context)

    if on_delta:
        raw, used = llm.chat_stream_any(cfg, messages, on_delta=on_delta,
                                        max_tokens=1600)
    else:
        raw, used = llm.chat_any(cfg, messages, max_tokens=1600)
    model = used.get("model") or ""

    code, prose = split_code(raw)

    if action == "chat":
        answer = (raw or "").strip()
        return _text_result(action, "ask", answer, model, question)

    if action == "explain":
        # 解释类答案保留原文：模型偶尔会顺手贴一段改写版 SQL，一并展示更完整
        return _text_result(action, "ask", (raw or "").strip(), model, question)

    if action == "dialect":
        # 目标方言不是 MySQL：既不能拿 MySQL 的闸去改写它，也绝不能真去执行。
        # 展示的必须是模型原样输出，否则用户复制出去会拿到一条「被 MySQL 加过
        # LIMIT、还混着 MySQL 函数」的四不像。
        return {
            "kind": "sql",
            "sql": code or guard.clean(raw),
            "answer": prose,
            "columns": [], "rows": [],
            "executed": False,
            "executable": False,
            "warning": f"已转换为 {target}，不能在当前 MySQL 连接上执行",
            "action": action, "mode": "ask",
            "origin": "assist", "origin_label": _label(action),
            "source": f"{_label(action)} → {target}（{model}）" if model
                      else f"{_label(action)} → {target}",
            "model": model, "question": question,
        }

    if action in ("optimize", "fix"):
        if not code and _looks_like_sql(raw):
            code, prose = guard.clean(raw), ""
        if not code:
            raise ValueError("模型没有给出 SQL，请把问题说得更具体或稍后重试")
        verdict = _judge_output(code, allowed)
        out = {
            "kind": "sql",
            "sql": verdict.get("sql") or code,
            "answer": prose,
            "columns": [], "rows": [],
            "executed": False,
            "executable": bool(verdict.get("executable")),
            "warning": verdict.get("warning") or "已生成，尚未执行；确认后再点「重新执行」",
            "action": action, "mode": "ask",
            "origin": "assist", "origin_label": _label(action),
            "source": f"{_label(action)}（{model}）" if model else _label(action),
            "model": model, "question": question,
        }
        return out

    if action == "sample":
        body = code or (guard.clean(raw) if _looks_like_write(raw) else "")
        if not body:
            raise ValueError("模型没有给出可用的语句；说清目标表试试，例如「给项目表生成两条测试数据」")
        return {
            "kind": "sql",
            "sql": body,
            "answer": prose,
            "columns": [], "rows": [],
            "executed": False,
            "executable": False,
            "warning": "样例语句只展示、不执行；请复制到测试库运行",
            "action": action, "mode": "ask",
            "origin": "assist", "origin_label": "生成样例",
            "source": f"生成样例（{model}）" if model else "生成样例",
            "model": model, "question": question,
        }

    # generate
    if not code and _looks_like_sql(raw):
        code = guard.clean(raw)
    if not code:
        raise ValueError("模型没有给出 SQL，请把问题说得更具体或稍后重试")
    verdict = _judge_output(code, allowed)
    return {
        "kind": "sql",
        "sql": verdict.get("sql") or code,
        "answer": prose,
        "columns": [], "rows": [],
        "executed": False,
        "executable": bool(verdict.get("executable")),
        "warning": verdict.get("warning") or "已生成，尚未执行；确认后再点「重新执行」",
        "action": action, "mode": "ask",
        "origin": "assist", "origin_label": "生成 SQL",
        "source": f"生成 SQL（{model}）" if model else "生成 SQL",
        "model": model, "question": question,
    }


def _text_result(action: str, mode: str, answer: str, model: str,
                 question: str, extra: dict | None = None) -> dict:
    out = {
        "kind": "text",
        "sql": "", "columns": [], "rows": [],
        "answer": answer,
        "executed": False,
        "executable": False,
        "action": action, "mode": mode,
        "origin": "assist", "origin_label": _label(action),
        "source": f"{_label(action)}（{model}）" if model else _label(action),
        "model": model, "question": question,
    }
    if extra:
        out.update(extra)
    return out


# ---------------------------------------------------------------- Agent 分支

def _run_agent_query(conn, pack: SchemaPack, payload: dict, cfg: dict, allowed: set,
                     action: str, question: str, sql: str, pinned: list[str],
                     instructions: str, examples: list[dict], on_event=None,
                     context: dict | None = None) -> dict:
    """query / run_explain：两个都要真的执行。

    query        —— 从问题生成再执行（走现成的查证 agent）
    run_explain  —— 有 SQL 就执行现有的，没有就先生成；执行完再要一段人话解释
    """
    res = None
    if action == "run_explain" and sql:
        # 用户已经有一条 SQL：直接校验 + 执行，不再让模型重写一遍
        safe = guard.validate(guard.clean(sql), allowed)
        cols, rows = engine.run_sql(conn, safe)
        res = {"sql": safe, "columns": cols, "rows": rows, "note": "",
               "trace": [], "channel": ""}
    else:
        res = agent.run(conn, pack, question, cfg, allowed,
                        examples=examples, pinned=pinned,
                        instructions=instructions, on_event=on_event,
                        context=context)

    if action == "run_explain":
        res["answer"] = _explain_rows(pack, question, res, cfg)
        res["kind"] = "result"
    else:
        res["kind"] = "result"
    res["action"] = action
    res["mode"] = "agent"
    res["executed"] = True
    res["executable"] = True
    res["origin"] = "assist"
    res["origin_label"] = _label(action)
    res.setdefault("source", _label(action))
    res["question"] = question
    return res


def _explain_rows(pack: SchemaPack, question: str, res: dict, cfg: dict) -> str:
    """执行完再解释：把真实结果回灌，让模型说人话。

    失败不影响主流程——查询结果本身是有效的，解释只是锦上添花。
    """
    cols = res.get("columns") or []
    rows = res.get("rows") or []
    if not cols:
        return ""
    brief = _rows_brief(cols, rows)
    sql = res.get("sql") or ""
    prompt = f"""把下面这次查询的结果讲清楚，让业务同事直接看懂。
问题：{question or '（未填写）'}
SQL：{sql}
结果：
{brief}

要求：
1. 先用一句话回答用户的问题（有数字就把关键数字说出来）。
2. 再说明这个数字是怎么来的（哪张表、什么条件、统计口径）。
3. 结果为空就直说没查到，并给出最可能的两个原因，不要编数据。
4. 不超过 6 句，不要重复贴 SQL。"""
    try:
        text, _ = llm.chat_any(cfg, [{"role": "system", "content": _COMMON},
                                     {"role": "user", "content": prompt}],
                               max_tokens=800)
        return (text or "").strip()
    except Exception:
        return ""


def _run_agent_text(conn, pack: SchemaPack, cfg: dict, allowed: set, action: str,
                    question: str, sql: str, pinned: list[str],
                    instructions: str, examples: list[dict], on_event=None,
                    context: dict | None = None) -> dict:
    """chat / schema / dryrun：Agent 模式里不查数据的三个动作。

    dryrun 例外——它要「查证后生成」，但**不执行**，所以走 SQL 目标。
    """
    if action == "dryrun":
        messages = agent_messages(
            """先用工具核实表结构和真实取值，再给出一条只读查询。
不要执行它。最终输出 {"sql":"...","note":"一句话说明"}。""",
            pack, question, instructions, pinned, examples, sql=sql,
            context=context)
        got, model, trace = _agent_loop(conn, pack, cfg, allowed, messages, "sql",
                                        on_event)
        code = got.get("sql") or ""
        verdict = _judge_output(code, allowed)
        return {
            "kind": "sql",
            "sql": verdict.get("sql") or code,
            "answer": got.get("note") or "",
            "columns": [], "rows": [],
            "executed": False,
            "executable": bool(verdict.get("executable")),
            "warning": verdict.get("warning") or "已查证后生成，尚未执行",
            "trace": trace, "action": action, "mode": "agent",
            "origin": "assist", "origin_label": _label(action),
            "source": f"生成但不执行（{model}）" if model else "生成但不执行",
            "model": model, "question": question,
        }

    objective = _AGENT_PROMPTS.get(action) or _AGENT_PROMPTS["chat"]
    messages = agent_messages(objective, pack, question, instructions, pinned,
                              examples, sql=sql, context=context)
    got, model, trace = _agent_loop(conn, pack, cfg, allowed, messages, "answer",
                                    on_event)
    return _text_result(action, "agent", got.get("answer") or "", model, question,
                        extra={"trace": trace,
                               "source": f"{_label(action)}（{model}）" if model else _label(action)})


# ---------------------------------------------------------------- 小工具

def _label(action: str) -> str:
    for acts in ACTIONS.values():
        for spec in acts:
            if spec["id"] == action:
                return spec["label"]
    return action


def _pick_sql(field_value, question: str) -> str:
    """SQL 来源：显式传入的 SQL 框 > 问题里直接贴的 SQL。"""
    sql = (field_value or "").strip()
    if sql:
        return sql
    q = (question or "").strip()
    if not q:
        return ""
    # 问题里夹着一段 SQL：从第一个 SELECT/WITH 起截到结尾（去掉中文说明）
    m = re.search(r"(?is)\b(select|with)\b", q)
    if m and m.start() > 0:
        return q[m.start():].strip()
    if _looks_like_sql(q):
        return q
    return ""


def _target(raw) -> str:
    key = (raw or "").strip().lower()
    if not key:
        return DIALECT_TARGETS[0]
    return _TARGET_ALIASES.get(key, raw.strip())
