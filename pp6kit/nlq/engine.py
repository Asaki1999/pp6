"""NL2SQL 引擎：schema linking → 提示词 → 生成 → 校验 → 执行 → 纠错重试。

设计参照 Vanna 的「检索增强 + 执行反馈」闭环，但语义层用的是平台自己的元数据表，
所以不用向量库也能把「项目状态」准确映射到 state_id_ + pp_project_state。
"""
from __future__ import annotations

import datetime
import decimal
import re
import time

from . import graph, guard, llm, semantic
from .introspect import SchemaPack

MAX_TABLES_IN_PROMPT = 6

SYSTEM = """你是 PowerProject 项目管理系统的 MySQL 8 查询专家。
根据用户的中文问题，生成一条可直接执行的 MySQL 查询。

硬性规则：
1. 只输出 SQL 本体，不要解释，不要 markdown 代码块，不要分号结尾。
2. 只能使用给定 schema 中出现的表和列，绝不臆造表名或列名。
3. schema 里给了「中文名 = 物理列」的映射，请直接使用物理列。
4. 标注「值来自 X，实际值：...」的列是外键，按名称过滤时必须用子查询，例如：
   项目状态是执行中 → state_id_ IN (SELECT id_ FROM pp_project_state WHERE name_ = '执行中')
5. 标注「可选值（中文=库中值）」的列，直接用库中值比较，不要用中文标签。
6. 软删除列 deleted_ **不是每张表都有**（225 张 pp_ 表里 58 张没有，例如
   pp_timesheet / pp_project_report / pp_directory / pp_config）。
   只有 schema 里那张表**真的列出了 deleted_** 时才加条件，且必须用
   IFNULL(t.deleted_, 'N') <> 'Y'；写了 deleted_ <> 'Y' 会把整表过滤光。
   表头标了「无 deleted_ 列」的，绝对不要写这一句。
6.1 人员字段（user）姓名在 iform_party_employee.name_，部门字段（department）名称在 iform_party_org.name_，
    多值字段（列名以 _ids_ 结尾，或标注了 multi）用 FIND_IN_SET(e.id_, t.col) 关联，**绝不能写 =**。
6.2 标注「该列整列为空」的外键不要用于关联，否则结果全是 NULL。
7. 时间列是 datetime 类型，用标准比较（create_time_ >= '2026-01-01'）。
8. 默认加 LIMIT 200；用户明确要全部时也不要超过 500。
9. 输出列建议用中文别名（AS `项目名称`），方便阅读。

中文语序规则（非常重要，违反就会答非所问）：
10. 「每个 X 有多少 Y」「每个 X 的 Y 数」——Y 是被查询的主体，X 只是分组维度。
    例：每个项目有多少阶段 → 主表是 pp_project_phase，LEFT JOIN pp_project，GROUP BY 项目名。
11. 「X 都有哪些 Y」「列出 X 的 Y」——Y 才是要展示的对象。
    例：文档都有哪些目录 → 主表是 pp_directory。
12. 「每个 X 挂了多少人 / 有多少成员」——数的是人数，主表用成员/人员关联表。
13. 否定说法必须生成反向条件：
    排除 / 不要 / 除了 / 不含 / 不能包含 / 不是 / 不为
    「任务名称不能包含根任务」→ name_ NOT LIKE '%根任务%'
    「风险等级不是高的」→ rating_ <> 'High'（中文值要先映射成库中值）
14. 疑问词不是筛选值：「责任人是谁」「谁在管」只表示要展示人员列，不要写 name_ = '谁'。
15. 中文省略：用户说「需求」可能指「系统需求」，「成员」指「项目成员」，「预算」通常指预算明细分项，
    以 schema 里的中文列为准做最贴近的匹配。
16. 「收入 / 回款 / 合同额」→ 合同表金额列；「花在哪 / 花销」→ 预算明细分项。

请只输出 SQL。"""


def select_tables(pack: SchemaPack, question: str,
                  limit: int = MAX_TABLES_IN_PROMPT,
                  pinned: list[str] | None = None) -> list[str]:
    """挑这个问句可能用到的表。

    优先走 select.link_tables（综合打分 + 外键目标表 + 多跳中间表），
    它比「标签包含即命中」准得多；失败时退回旧的宽松匹配。

    pinned 是用户用 @ 点名的表：无条件排在最前且不被 limit 截掉。
    打分器再准也是猜，用户点名了就该听用户的。
    """
    try:
        from . import local, select
        picked = select.link_tables(pack, question, local._label_variants, limit=limit)
        if not picked:
            picked = _select_tables_fallback(pack, question, limit)
    except Exception:
        picked = _select_tables_fallback(pack, question, limit)

    if pinned:
        head = [n for n in pinned if n in pack.tables]
        if head:
            return head + [n for n in picked if n not in head]
    return picked


def _select_tables_fallback(pack: SchemaPack, question: str, limit: int) -> list[str]:
    """旧的宽松匹配，只在打分器异常时兜底。"""
    q = question or ""
    scored: list[tuple[int, int, str]] = []
    for name, t in pack.tables.items():
        score = 0
        label = (t.label or "").replace("表", "")
        if label and label in q:
            score += 12
        if t.obj_code and t.obj_code in q.lower():
            score += 10
        for c in t.columns:
            if c.label and len(c.label) >= 2 and c.label in q:
                score += 3
        if score:
            scored.append((score, t.rows, name))
    if not scored:
        scored = [(0, t.rows, n) for n, t in pack.tables.items()]
    scored.sort(key=lambda x: (-x[0], -x[1]))
    picked = [n for _, _, n in scored[:limit]]

    # 外键目标表一起带上，否则模型没法写子查询
    extra = []
    for n in picked:
        for c in pack.tables[n].columns:
            if c.ref_table and c.ref_table not in picked and c.ref_table not in extra:
                extra.append(c.ref_table)
    for n in list(extra)[:6]:
        if n not in picked:
            picked.append(n)

    # 问句里提到多个对象时，把中间表也带上（客户→合同→项目），
    # 否则模型只能猜怎么连
    mentioned = [n for n, t in pack.tables.items()
                 if t.label and len(t.label) >= 2 and t.label in question
                 and n != (picked[0] if picked else None)]
    for other in mentioned[:2]:
        path = graph.shortest_path(pack, picked[0], other, max_hops=3)
        for e in path or []:
            for n in (e.src, e.dst):
                if n not in picked:
                    picked.insert(1, n)     # 中间表放前面，别被截断掉
        if other not in picked:
            picked.append(other)
    return picked[:9]


# 纯系统列：模型永远不会在业务条件里用到，塞进提示词纯属浪费 token
# （create_time_/update_time_/deleted_ 不在此列：SYSTEM 规则 6/7 教了模型
#  「上个月创建的项目」「软删除」怎么写，schema 里却看不到这些列，
#   模型就只能靠检索到的示例碰运气——2026-11-11 实测发现的矛盾）
_SYS_COLS = frozenset({
    "tenant_id_", "ip_", "version_", "baseline_version_", "is_draft_",
    "create_by_", "update_by_", "cut_flow_inst_id_", "cut_flow_bo_id_",
    "order_", "pinyin_",
})


# schema 说明的字符预算（≈3500 token）。DBX 对超大 schema 也是截断处理：
# 「Large schemas are truncated. Explicit mentions are more reliable than
# asking the model to guess among every object」。它们靠 @ 提及兜底，
# 我们两边都有（pinned 表不受预算约束），所以裁剪是安全的。
SCHEMA_BUDGET_CHARS = 7000


def _schema_block(pack: SchemaPack, tables: list[str],
                  budget: int = SCHEMA_BUDGET_CHARS,
                  keep: list[str] | None = None) -> str:
    """生成 schema 说明。

    提示词长度直接决定响应速度（免费模型尤其明显）：
    原来每个外键都列 8 个实际值、每个枚举列列 8 个取值、系统列全带上，
    12 张表就 7700 tokens、生成 15 秒。这里只保留**模型必须知道**的：
      - 外键指向哪张表（不列具体值，模型能自己子查询）
      - 整列为空的列（否则会写出必然 0 行的条件）
      - 枚举值（只列前 4 个，够模型判断格式）
      - 系统列（租户/IP/版本/审计）一律不列

    budget 是字符预算。超预算的表只留表头——整张删掉的话模型会以为这个
    对象不存在，转而臆造表名；留表头它至少知道「有这张表，要看明细请
    @表名」。keep 里的表（用户 @ 点名的）永远完整输出。
    """
    keep_set = set(keep or ())
    lines: list[str] = []
    used = 0
    trimmed: list[str] = []

    for name in tables:
        t = pack.tables.get(name)
        if not t:
            continue
        tag = "" if t.rows > 0 else "（⚠无数据）"
        # 实测 225 张 pp_ 表里有 58 张没有 deleted_ 列（pp_timesheet、pp_project_report、
        # pp_directory…）。不标出来的话模型会照着「所有 pp_ 表都有软删除」的规则写，
        # 直接报 Unknown column 'xxx.deleted_'。
        has_del = any(c.name == "deleted_" for c in t.columns)
        if not has_del:
            tag += "（无 deleted_ 列，禁止加软删除条件）"
        head = f"表 {t.name}（{t.label}，{t.rows} 行）{tag}"

        body: list[str] = []
        for c in t.columns:
            if c.name in _SYS_COLS and not c.ref_table:
                continue
            extra = []
            if c.ref_table:
                rel = next((r for r in pack.relations.get(name, []) if r["col"] == c.name), None)
                dead = rel is not None and rel.get("live") == 0
                ref = f"→{c.ref_table}" + ("(空列勿用)" if dead else "")
                # 被引用表的实际取值（ref_values 在 introspect 里已采样）：
                # 「高级别项目」猜成 name_ LIKE '%高%' 就查不到——库里是
                # 「集团战略级/事业部重点级/部门级」。值给到手模型就不用猜。
                vals = (pack.ref_values.get(c.ref_table) or [])[:6]
                if vals:
                    ref += f"(值:{','.join(vals)})"
                extra.append(ref)
            if c.has_cn_enums:
                pairs = [f"{lab}={code}" for lab, code in list(c.enums.items())[:4]
                         if lab != code]
                if pairs:
                    extra.append("值:" + ",".join(pairs))
            elif c.filled == 0:
                extra.append("⚠空列")
            suffix = f" ←{';'.join(extra)}" if extra else ""
            body.append(f"  {c.label}={c.name}{suffix}")

        cost = len(head) + sum(len(x) + 1 for x in body)
        if used + cost > budget and name not in keep_set and lines:
            note = "  ←列已省略，需要时用 @表名 指定"
            lines.append(head + note)
            used += len(head) + len(note)
            trimmed.append(name)
            continue
        lines.append(head)
        lines.extend(body)
        used += cost

    # 内置人员/组织/字典表不在 pack.tables（避免被当业务表乱查），但模型经常
    # 需要它们（「每个部门有多少人」）。不给出真实列名，模型就臆造 org_ids_。
    lines.append("内置表（不在上面的清单里，需要时可直接查询）：")
    lines.append("  iform_party_employee 员工：name_=姓名，pp_org_id_=所属部门(单值)→iform_party_org.id_")
    lines.append("  iform_party_org 部门：name_=部门名称")
    lines.append("  iform_cat_dic 字典项：key_=字典key，name_=项名，parent_id_=父项")
    if trimmed:
        lines.append(f"（另有 {len(trimmed)} 张表的列因长度预算省略；"
                     f"用户用 @ 点名可强制展开）")
    return "\n".join(lines)


def build_messages(pack: SchemaPack, question: str, examples: list[dict] | None = None,
                   pinned: list[str] | None = None,
                   instructions: str = "") -> list[dict]:
    tables = select_tables(pack, question, pinned=pinned)
    parts = [SYSTEM]
    if instructions:
        # 用户配置的全局指令：放在通用规则之后、schema 之前。
        # 「优先级高于上面的通用规则」必须写明——模型遇到冲突时需要一个
        # 明确的裁决顺序，否则它会随机挑一条遵守（DBX 文档也强调了这点：
        # 「Keep rules concise and non-conflicting」）。
        parts += ["", "=== 全局指令（用户配置，与上面规则冲突时以此为准） ===", instructions]
    parts += ["", "=== 业务口径 ===", semantic.prompt_block(pack)]
    if pinned:
        names = "、".join(f"{pack.tables[n].name}（{pack.tables[n].label}）"
                         for n in pinned if n in pack.tables)
        if names:
            parts += ["", f"=== 用户点名要用的表 ===\n{names}\n查询必须从这些表出发。"]
    parts += ["", "=== 可用 schema ===", _schema_block(pack, tables, keep=pinned)]
    if examples:
        parts += ["", "=== 参考示例（同库已验证的问答，照这个写法） ==="]
        for ex in examples[:6]:
            tag = "【已验证】" if ex.get("verified") or ex.get("source") == "corrected" else ""
            parts.append(f"{tag}问题：{ex['question']}\nSQL：{ex['sql']}")
    parts += ["", "=== 待回答 ===", f"问题：{question}", "SQL："]
    return [
        {"role": "system", "content": "\n".join(parts)},
        {"role": "user", "content": question},
    ]


def run_sql(conn, sql: str, max_rows: int = guard.MAX_ROWS) -> tuple[list[str], list[list]]:
    """执行查询。**执行前再断言一次只读**。

    所有调用点其实都先走 `guard.validate` 了，这行是冗余的——但冗余得值：
    `run_sql` 是公开函数，probe / audit / 未来的新功能都可能直接调它，
    哪一处忘了校验，写语句就落到客户库上了。多花一次 AST 解析（毫秒级），
    换掉「某个调用点漏判＝生产事故」的可能性。
    """
    if not guard.is_read_only(sql):
        raise guard.SqlRejected("拒绝执行：这条 SQL 不是纯只读查询")
    cur = conn.cursor()
    t0 = time.time()
    cur.execute(sql)
    rows = cur.fetchmany(max_rows)
    cols = [d[0] for d in cur.description] if cur.description else []
    cur.close()
    if time.time() - t0 > 25:
        raise RuntimeError("查询耗时过长，已中止")
    return cols, [[_cell(v) for v in r] for r in rows]


def _cell(v):
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "ignore")
    # date 没有 isoformat(sep=...)，datetime 才有——工时表这类纯日期列会踩到
    if isinstance(v, datetime.datetime):
        return v.isoformat(sep=" ")[:19]
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, datetime.timedelta):
        return str(v)
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return int(f) if f == int(f) else f
    if hasattr(v, "isoformat"):
        return str(v.isoformat())[:19]
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def ask(conn, pack: SchemaPack, question: str, cfg: dict,
        examples: list[dict] | None = None,
        pinned: list[str] | None = None,
        instructions: str = "",
        on_delta=None) -> dict:
    """生成 + 执行；失败时把错误回灌给模型重试一次。

    多平台：cfg 里有多个渠道时，按顺序尝试，限流/失败自动切下一家。

    两种重试：
      1. SQL 执行报错 → 把错误信息回灌，让模型改
      2. 执行成功但 0 行 → 让模型自查条件是不是写死了
         （实测「本月创建的项目有哪些」偶尔会被写成年月两次比较之类的歪条件，
          SQL 不报错但结果为空，比报错更隐蔽）
    """
    if not llm.active_channels(cfg):
        raise llm.LlmError("未配置大模型 API Key（可在「连接与模型」里粘贴，免费平台见预设）")

    allowed = set(pack.tables) | guard.PARTY_TABLES | {"iform_cat_dic", "iform_cat_type"}
    messages = build_messages(pack, question, examples, pinned=pinned,
                              instructions=instructions)

    # on_delta 非空 = 调用方要流式：把首字尽早吐出去，别让用户盯着黑屏。
    # 重试路径（下面两处）仍走非流式——第一版 SQL 已经显示在用户眼前了，
    # 第二版再流式会把两段内容拼在一起，比不动更让人困惑。
    if on_delta:
        raw, used = llm.chat_stream_any(cfg, messages, on_delta=on_delta)
    else:
        raw, used = llm.chat_any(cfg, messages)
    sql = guard.clean(raw)
    used_label = f"{used.get('model') or ''}"

    try:
        safe = guard.validate(sql, allowed)
        cols, rows = run_sql(conn, safe)
    except Exception as first_err:
        retry = messages + [
            {"role": "assistant", "content": sql},
            {"role": "user", "content": f"上面的 SQL 执行失败：{first_err}\n请修正后只输出新的 SQL。"},
        ]
        raw2, used = llm.chat_any(cfg, retry)
        sql2 = guard.clean(raw2)
        safe2 = guard.validate(sql2, allowed)
        cols, rows = run_sql(conn, safe2)
        return {"sql": safe2, "columns": cols, "rows": rows, "retried": True,
                "firstError": str(first_err), "channel": used_label}

    if not rows and _looks_wrongly_empty(question):
        # 让模型自查一次；它可能答「就是这样」而给出同样的 SQL，
        # 那就老老实实返回 0 行 + 诊断，别装作查到了
        try:
            retry = messages + [
                {"role": "assistant", "content": sql},
                {"role": "user", "content": (
                    "上面的 SQL 能执行，但结果 0 行。请检查是不是条件写得太严"
                    "（比如把时间写成精确月份、把枚举值写成了库里没有的中文）。"
                    "如果确实应该为空，就原样返回这条 SQL；否则输出修正后的 SQL。只输出 SQL。"
                )},
            ]
            raw2, used2 = llm.chat_any(cfg, retry)
            sql2 = guard.clean(raw2)
            if sql2 and sql2.strip() != sql.strip():
                safe2 = guard.validate(sql2, allowed)
                cols2, rows2 = run_sql(conn, safe2)
                if rows2:
                    return {"sql": safe2, "columns": cols2, "rows": rows2,
                            "retried": True, "firstError": "首次结果为 0 行，已自查修正",
                            "channel": f"{used2.get('model') or ''}"}
        except Exception:
            pass       # 自查失败不影响主流程，下面照常返回 0 行

    return {"sql": safe, "columns": cols, "rows": rows, "retried": False,
            "channel": used_label}


# 「有哪些 / 列出 / 查询…」这类问法，0 行几乎都是条件写歪了。
# 「有多少个」问 0 也是可疑的；但带具体数字条件（如「金额大于 1000 万」）0 行很正常，不重试。
_EMPTY_SUSPECT = ("有哪些", "列出", "查询", "查一下", "找出", "显示", "看看", "所有", "全部")


def _looks_wrongly_empty(question: str) -> bool:
    q = question or ""
    if re.search(r"\d", q):          # 带数字条件的不猜
        return False
    return any(k in q for k in _EMPTY_SUSPECT)


def _fmt(v) -> str:
    try:
        f = float(v)
        return f"{f:,.0f}" if f == int(f) else f"{f:,.2f}"
    except Exception:
        return str(v)


def diagnose(conn, pack: SchemaPack, sql: str) -> str:
    """0 行时给出人话解释：表本身空？筛选值写错？关键词没命中？

    只做能确定结论的诊断，猜不出来就说猜不出来——误导比不解释更糟。
    """
    import sqlglot
    from sqlglot import exp

    try:
        ast = sqlglot.parse_one(sql, dialect="mysql")
    except Exception:
        return ""

    names = [t for t in guard.referenced_tables(sql) if t in pack.tables]
    if not names:
        return ""
    main = names[0]
    table = pack.tables[main]
    if table.rows == 0:
        return f"「{table.label}」在当前库里没有数据（该模块还没启用），换个对象试试。"

    detail = "、".join(f"{n}（{pack.tables[n].label}）{pack.tables[n].rows} 行" for n in names[:3])

    # 0) SQL 用到的列如果整列为空，直接说是数据问题，别让用户以为条件写错了
    empty_cols = []
    for col_node in ast.find_all(exp.Column):
        cname = col_node.name
        for n in names:
            c = pack.tables[n].column(cname)
            if c is not None and c.filled == 0:
                empty_cols.append(f"{pack.tables[n].label}.{c.label}")
                break
    if empty_cols:
        uniq = list(dict.fromkeys(empty_cols))[:3]
        return (f"这些字段在库里整列都是空的：{'、'.join(uniq)}，"
                f"所以查不出数据。换一批有值的字段试试。")

    # 1) 关键词没命中：LIKE '%x%' 的列里没有这个字
    for like in ast.find_all(exp.Like):
        if not isinstance(like.this, exp.Column):
            continue
        colname = like.this.name
        pattern = like.expression
        kw = ""
        if isinstance(pattern, exp.Literal):
            kw = str(pattern.this).strip("%")
        col = table.column(colname)
        if col is None or not kw:
            continue
        sample = "、".join(str(v)[:14] for v in col.samples[:5])
        return (
            f"「{col.label}」里没有包含「{kw}」的记录。"
            + (f"现有取值示例：{sample}" if sample else "")
        )

    # 2) 数值比较没落在数据范围内：把这一列的真实范围报出来
    for cmp_node in list(ast.find_all((exp.GT, exp.GTE, exp.LT, exp.LTE))):
        left, right = cmp_node.this, cmp_node.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Literal):
            continue
        col = table.column(left.name)
        if col is None:
            continue
        try:
            probe = conn.cursor()
            probe.execute(
                f"SELECT COUNT(`{col.name}`), MIN(`{col.name}`), MAX(`{col.name}`) "
                f"FROM `{table.name}`"
            )
            filled, lo, hi = probe.fetchone()
            probe.close()
        except Exception:
            continue
        filled = int(filled or 0)
        if filled == 0:
            return (f"「{col.label}」这一列在 {table.rows} 行里全是空值，没法用它筛选。"
                    f"换个字段试试。")
        if filled < table.rows:
            return (f"「{col.label}」只有 {filled}/{table.rows} 行有值，"
                    f"范围 {_fmt(lo)} ~ {_fmt(hi)}；条件 {_fmt(right.this)} 没落在范围内。")
        return (f"「{col.label}」范围 {_fmt(lo)} ~ {_fmt(hi)}，"
                f"条件 {_fmt(right.this)} 没有命中任何一行。")

    # 3) 条件过滤掉了：去掉 WHERE 再数一次
    stripped = ast.copy()
    for key in ("where", "limit", "order", "having"):
        stripped.set(key, None)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM ({stripped.sql(dialect='mysql')}) AS _probe")
        loose = int(cur.fetchone()[0] or 0)
        cur.close()
    except Exception:
        loose = -1

    if loose > 0:
        msg = f"{detail}，去掉筛选条件能查到 {loose} 行 —— 是筛选值没匹配上。"
        hints = []
        for n in names:
            vals = pack.ref_values.get(n, [])
            if vals:
                hints.append(f"{pack.tables[n].label} 实际取值：{'、'.join(vals[:8])}")
        if hints:
            msg += " " + "；".join(hints[:2])
        return msg

    return f"{detail}，但当前条件组合过滤后为空。"


# ---------------------------------------------------------------- 开箱即用模板

def templates(pack: SchemaPack) -> list[dict]:
    """没有大模型也能用的常用查询；列名按当前库动态取，客户扩展字段也不会失效。"""
    out: list[dict] = []

    def add(title: str, question: str, sql: str):
        if sql:
            out.append({"title": title, "question": question, "sql": sql})

    if "pp_project" in pack.tables:
        t = pack.tables["pp_project"]
        has = {c.name for c in t.columns}
        if {"name_", "state_id_"} <= has:
            add("项目清单", "列出所有项目及其状态、级别、负责人",
                "SELECT p.name_ AS `项目名称`, p.code_ AS `项目编号`,\n"
                "       s.name_ AS `项目状态`, g.name_ AS `项目级别`,\n"
                "       p.create_time_ AS `创建时间`\n"
                "FROM pp_project p\n"
                "LEFT JOIN pp_project_state s ON s.id_ = p.state_id_\n"
                "LEFT JOIN pp_project_grade g ON g.id_ = p.grade_id_\n"
                "WHERE IFNULL(p.deleted_, 'N') <> 'Y'\nORDER BY p.create_time_ DESC\nLIMIT 200")
            add("按状态统计项目数", "每个项目状态各有多少个项目",
                "SELECT s.name_ AS `项目状态`, COUNT(*) AS `项目数`\n"
                "FROM pp_project p\nJOIN pp_project_state s ON s.id_ = p.state_id_\n"
                "WHERE IFNULL(p.deleted_, 'N') <> 'Y'\nGROUP BY s.name_\nORDER BY `项目数` DESC")
            add("按项目类型统计", "每种项目类型下有多少个项目",
                "SELECT c.name_ AS `项目类型`, COUNT(*) AS `项目数`\n"
                "FROM pp_project p\nJOIN pp_classification c ON c.id_ = p.classification_id_\n"
                "WHERE IFNULL(p.deleted_, 'N') <> 'Y'\nGROUP BY c.name_\nORDER BY `项目数` DESC")

    for name, title, question in (
        ("pp_task", "任务清单", "列出最近的任务和负责人"),
        ("pp_issue", "问题清单", "列出所有未关闭的问题"),
        ("pp_risk", "风险清单", "列出所有风险及其等级"),
        ("pp_dev_task", "开发任务", "列出开发任务和执行状态"),
    ):
        t = pack.tables.get(name)
        if not t:
            continue
        cols = {c.name for c in t.columns}
        label = next((c.name for c in t.columns if c.label in ("名称", "标题", "任务名称")), "name_")
        if label in cols:
            add(title, question,
                f"SELECT * FROM `{name}` WHERE IFNULL(deleted_, 'N') <> 'Y' LIMIT 200")
    return out


def explain_sql(sql: str, pack) -> str:
    """把 SQL 翻成一句人话，让用户一眼看出「它到底理解成了什么」。

    只做低风险的语法层摘要，不猜业务语义：
      查询「项目」，按 状态、创建时间 筛选，按 创建时间 倒序，最多 200 行
    """
    import sqlglot
    from sqlglot import exp

    try:
        ast = sqlglot.parse_one(sql or "", dialect="mysql")
    except Exception:
        return ""

    # 表别名 → 物理表名，否则 s.name_ 会被猜成第一个有 name_ 的表（项目名称）
    alias_map: dict[str, str] = {}
    for t in ast.find_all(exp.Table):
        alias_map[t.alias_or_name] = t.name

    def _col_label(col) -> str:
        table = alias_map.get(col.table or "", col.table or "")
        cname = col.name
        t = pack.tables.get(table) if table else None
        if t is not None:
            c = t.column(cname)
            if c is not None:
                # 关联表的 name_ 列在库里就叫「名称」，单独看没意义；
                # 用表名代替（项目状态 / 项目类型 / 隶属部门…）
                if c.label in ("名称", "名字") and cname == "name_":
                    return t.label
                return c.label
        for t2 in pack.tables.values():
            c = t2.column(cname)
            if c is not None:
                return c.label
        return cname

    bits: list[str] = []
    table = ast.find(exp.Table)
    if table is not None:
        t = pack.tables.get(table.name)
        bits.append(f"查询「{t.label}」" if t is not None else f"查询 {table.name}")

    # 聚合
    if any(ast.find(x) is not None for x in (exp.Count, exp.Sum, exp.Avg, exp.Max, exp.Min)):
        bits.append("做统计")
    else:
        n = len(ast.expressions or [])
        if n:
            bits.append(f"取 {n} 列")

    where = ast.find(exp.Where)
    if where is not None:
        labels: list[str] = []
        # 只把「列 = 字面量 / IN / LIKE / BETWEEN」当筛选条件；
        # join 的 a.id_=b.id_ 不是筛选，COALESCE(deleted_,'N') 这种审计条件也略过
        for node in where.find_all(exp.EQ, exp.NEQ, exp.In, exp.Like, exp.Between):
            if node.find(exp.Literal) is None:
                continue
            for col in node.find_all(exp.Column):
                if col.name.lower() in ("deleted_", "tenant_id_", "id_"):
                    continue
                label = _col_label(col)
                if label not in labels:
                    labels.append(label)
        wsql = where.sql().upper()
        if "INTERVAL" in wsql or "DATE_SUB" in wsql or "CURDATE" in wsql \
                or "CURRENT_DATE" in wsql or "DATEDIFF" in wsql:
            bits.append("有时间范围")
        if labels:
            bits.append("按 " + "、".join(labels[:4]) + " 筛选")

    group = ast.find(exp.Group)
    if group is not None:
        labels = []
        for col in group.find_all(exp.Column):
            label = _col_label(col)
            if label not in labels:
                labels.append(label)
        if labels:
            bits.append("按 " + "、".join(labels[:3]) + " 分组")

    order = ast.find(exp.Order)
    if order is not None:
        parts = []
        for ordered in order.expressions:
            label = _col_label(ordered.this) if isinstance(ordered.this, exp.Column) else ""
            direction = "倒序" if (ordered.args.get("desc") is True) else "正序"
            parts.append(f"{label} {direction}".strip())
        if parts:
            bits.append("按 " + "、".join(parts[:2]))

    limit = ast.find(exp.Limit)
    if limit is not None:
        try:
            n = int(limit.expression.name)
            bits.append(f"最多 {n} 行")
        except Exception:
            pass

    return "，".join(bits)
