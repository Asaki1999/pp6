"""Agent 模式：先查证据，再落笔写 SQL。

借鉴 DBX 的 Ask / Agent 双模式（docs/ai-assistant.mdx「Ask and Agent」）：

    Ask   —— 只生成，不查库，schema 一次性塞进上下文
    Agent —— 可以先 list_tables / describe_table / sample_values / run_sql，
             拿到证据再写；复杂问题从「一次猜对」变成「查证后回答」

pp6 为什么需要它：schema 预算（7000 字符）必然要裁掉一部分表，模型看不到就只能
猜。Agent 让模型按需取用，和 DBX 的结论一致——「显式提及比让模型在几十个对象里
猜更可靠」。

**协议：不用 function calling。**
pp6 的用户主要跑免费模型（glm-4.7-flash / deepseek-flash），不保证支持 `tools`
参数。两套协议等于把一半用户挡在门外。这里用「每轮只输出一个 JSON 对象」的
文本协议，解析不出来就把整段当 SQL——**任何情况下都不会完全失败**。
"""
from __future__ import annotations

import json
import re

from . import engine, guard, llm, semantic
from .introspect import SchemaPack

MAX_TURNS = 5              # 每轮一次模型调用，5 轮 ≈ 5 次，成本可控
MAX_TOOL_ROWS = 20         # run_sql 回灌给模型的行数上限
MAX_TOOL_CHARS = 2400      # 单个工具结果的字符上限（超了会挤占后面几轮的预算）

TOOL_HELP = """可用工具（每轮只输出一个 JSON 对象，不要输出别的内容）：
{"tool":"list_tables","args":{"keyword":"项目"}}                     按关键词找表，keyword 可留空
{"tool":"describe_table","args":{"table":"pp_project"}}               看某张表的列/外键/枚举/空列
{"tool":"sample_values","args":{"table":"pp_project","column":"state_id_"}}   看某列的真实取值
{"tool":"run_sql","args":{"sql":"SELECT ... LIMIT 5"}}                试跑一条只读查询，验证条件能否命中
证据够了就输出最终答案：{"sql":"SELECT ...","note":"一句话说明用到的表和条件"}
"""

# engine.SYSTEM 末尾是「请只输出 SQL。」，那是 Ask 模式的收尾语，Agent 模式
# 要换成工具协议。截断而不是重写，是为了让 16 条中文语序规则和 schema 约定
# 始终只有一份，不会改了一边忘了另一边。
_RULES = engine.SYSTEM.split("请只输出 SQL")[0].rstrip()

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def run(conn, pack: SchemaPack, question: str, cfg: dict, allowed: set,
        examples: list[dict] | None = None,
        pinned: list[str] | None = None,
        instructions: str = "",
        on_event=None,
        max_turns: int = MAX_TURNS) -> dict:
    """跑一次 Agent 对话，返回结构与 engine.ask 一致。

    on_event 用来给前端推进度（{"stage": "tool", "tool": ..., "args": ...}）。
    流式端点把它转成 SSE 事件，用户就能看到「正在查 pp_project 的列」。
    """
    if not llm.active_channels(cfg):
        raise llm.LlmError(
            "查证模式需要大模型。可在「连接与模型」里配一个免费平台，"
            "或把模式切回「混合」用本地规则。"
        )

    def _emit(ev: dict) -> None:
        if on_event:
            try:
                on_event(ev)
            except Exception:
                pass          # 前端断了不该拖垮查询

    messages = [
        {"role": "system", "content": _system(pack, instructions, pinned, examples)},
        {"role": "user", "content": question},
    ]
    trace: list[dict] = []
    used_label = ""

    for turn in range(max_turns):
        raw, used = llm.chat_any(cfg, messages)
        used_label = used.get("model") or ""
        parsed = _parse(raw)

        # ① 最终答案：模型把 SQL 交出来了
        if parsed and isinstance(parsed.get("sql"), str) and parsed["sql"].strip():
            try:
                return _finish(conn, allowed, parsed["sql"], parsed.get("note"),
                               question, used_label, trace, turn + 1,
                               source="大模型查证后生成")
            except Exception as exc:
                # 执行报错就回灌让它自己改。实测最常见的是函数名拼错
                # （`COALESLLESCE`），以及字段名差一个后缀——这类错误模型
                # 拿到报错信息后基本一次就能改对，直接把跑不通的 SQL 交给
                # 用户是最差的选择。
                _emit({"stage": "retry", "reason": str(exc)[:80]})
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content":
                     f'这条 SQL 执行失败：{exc}\n修正后重新输出 {{"sql":"...","note":"..."}}。'},
                ]
                continue

        # ② 工具调用：执行 → 结果回灌 → 下一轮
        if parsed and isinstance(parsed.get("tool"), str):
            name = parsed["tool"]
            args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
            _emit({"stage": "tool", "tool": name, "args": args})
            result = _run_tool(name, args, pack, conn, allowed)
            trace.append({"tool": name, "args": args, "result": result[:200]})
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "工具结果：\n" + result},
            ]
            continue

        # ③ 没按协议来：模型直接给了一段 SQL。能用就用，不为了协议而失败
        sql = guard.clean(raw)
        if sql:
            try:
                return _finish(conn, allowed, sql, "", question, used_label,
                               trace, turn + 1, source="大模型生成（未走工具协议）")
            except Exception as exc:
                _emit({"stage": "retry", "reason": str(exc)[:80]})
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content":
                     f'这条 SQL 执行失败：{exc}\n修正后输出 {{"sql":"...","note":"..."}}。'},
                ]
                continue

    # 轮次用尽：不再给工具，直接要 SQL
    messages.append({"role": "user", "content":
                     '轮次用尽。现在直接输出 {"sql":"...","note":"..."}，不要再调用工具。'})
    raw, used = llm.chat_any(cfg, messages)
    used_label = used.get("model") or used_label
    parsed = _parse(raw)
    sql = parsed["sql"] if parsed and isinstance(parsed.get("sql"), str) else raw
    note = parsed.get("note", "") if parsed else ""
    return _finish(conn, allowed, sql, note, question, used_label, trace,
                   max_turns + 1, source="大模型生成（轮次用尽）")


# ---------------------------------------------------------------- 内部

def _system(pack: SchemaPack, instructions: str, pinned: list[str] | None,
            examples: list[dict] | None) -> str:
    parts = [
        _RULES, "",
        "=== 查证模式 ===",
        "你还没看到完整表结构。不确定的地方先用工具取证据，再写 SQL。",
        "证据够了就立刻给最终答案，不要为了用工具而用工具。", "",
        TOOL_HELP,
    ]
    if pinned:
        names = "、".join(f"{pack.tables[n].name}（{pack.tables[n].label}）"
                         for n in pinned if n in pack.tables)
        if names:
            parts += ["", f"=== 用户点名要用的表 ===\n{names}\n最终查询必须从这些表出发。"]
    if notes := semantic.prompt_block(pack):
        parts += ["", "=== 业务口径 ===", notes]
    if instructions:
        parts += ["", "=== 全局指令（用户配置，与上面规则冲突时以此为准） ===", instructions]
    if examples:
        parts += ["", "=== 同库已验证的问答（照这个写法） ==="]
        for ex in examples[:4]:
            parts.append(f"问题：{ex['question']}\nSQL：{ex['sql']}")
    return "\n".join(parts)


def _finish(conn, allowed: set, sql: str, note, question: str,
            used_label: str, trace: list[dict], turns: int, source: str) -> dict:
    safe = guard.validate(guard.clean(sql), allowed)
    cols, rows = engine.run_sql(conn, safe)
    return {
        "sql": safe,
        "columns": cols,
        "rows": rows,
        "note": str(note or "")[:160],
        "trace": trace,
        "turns": turns,
        "channel": used_label,
        "source": source + (f"（{used_label}）" if used_label else "")
                  + (f"，查证 {len(trace)} 次" if trace else ""),
        "origin": "agent",
        "origin_label": "Agent 查证",
        "verified": False,
        "model": used_label,
        "question": question,
    }


def _run_tool(name: str, args: dict, pack: SchemaPack, conn, allowed: set) -> str:
    """执行一个工具。任何异常都翻成人话返回给模型，而不是往上抛。

    工具失败是 Agent 的正常输入（表名写错、列名猜错），让模型自己纠正，
    比整个查询崩掉有用得多。
    """
    try:
        if name == "list_tables":
            return _tool_list_tables(pack, str(args.get("keyword") or ""))
        if name == "describe_table":
            return _tool_describe_table(pack, str(args.get("table") or ""))
        if name == "sample_values":
            return _tool_sample_values(pack, str(args.get("table") or ""),
                                       str(args.get("column") or ""))
        if name == "run_sql":
            return _tool_run_sql(conn, allowed, str(args.get("sql") or ""))
        return f"没有名为「{name}」的工具。\n" + TOOL_HELP
    except Exception as exc:
        return f"工具 {name} 执行出错：{exc}"


def _tool_list_tables(pack: SchemaPack, keyword: str) -> str:
    kw = keyword.strip().lower()
    hits = [t for t in pack.tables.values()
            if not kw or kw in f"{t.name} {t.label or ''} {t.obj_code or ''}".lower()]
    if not hits:
        return (f"没有匹配「{keyword}」的表。换一个更短的关键词试"
                f"（比如只写「项目」两个字），或者不传 keyword 看全部。")
    hits.sort(key=lambda t: -t.rows)
    lines = [f"匹配 {len(hits)} 张表（按数据量排序，最多列 15 张）："]
    lines += [f"  {t.name}（{t.label}，{t.rows} 行）" for t in hits[:15]]
    if len(hits) > 15:
        lines.append(f"  …… 还有 {len(hits) - 15} 张，收窄关键词再看")
    return _clip("\n".join(lines))


def _tool_describe_table(pack: SchemaPack, table: str) -> str:
    name = _resolve_table(pack, table)
    if not name:
        return f"没有找到表「{table}」。先用 list_tables 按关键词找一下。"
    # 复用 schema 块的渲染：列/外键/枚举/空列标记的规则只维护一份
    return _clip(engine._schema_block(pack, [name], budget=10 ** 6, keep=[name]))


def _tool_sample_values(pack: SchemaPack, table: str, column: str) -> str:
    name = _resolve_table(pack, table)
    if not name:
        return f"没有找到表「{table}」。先用 list_tables 找一下。"
    t = pack.tables[name]
    col = t.column(column) or next((c for c in t.columns if c.label == column), None)
    if col is None:
        cols = "、".join(f"{c.label}={c.name}" for c in t.columns[:20])
        return f"「{t.label}」没有列「{column}」。可用列：{cols}"

    parts = [f"{t.label}.{col.label}（{col.name}，类型 {col.dtype}）"]
    if col.filled == 0:
        parts.append("⚠ 这一列整列为空，不能用来筛选")
    if col.enums:
        parts.append("枚举：" + "、".join(f"{k}={v}" for k, v in list(col.enums.items())[:12]))
    if col.samples:
        parts.append("样本：" + "、".join(str(s)[:24] for s in col.samples[:12]))
    if col.ref_table:
        vals = pack.ref_values.get(col.ref_table) or []
        parts.append(f"外键 → {col.ref_table}"
                     + (f"，实际取值：{'、'.join(vals[:10])}" if vals else ""))
    if len(parts) == 1:
        parts.append("没有采样到取值，直接按类型比较即可")
    return _clip("\n".join(parts))


def _tool_run_sql(conn, allowed: set, sql: str) -> str:
    sql = sql.strip()
    if not sql:
        return "sql 参数为空。"
    try:
        safe = guard.validate(sql, allowed)
        cols, rows = engine.run_sql(conn, safe, max_rows=MAX_TOOL_ROWS)
    except Exception as exc:
        return f"执行失败：{exc}\n修正后重试，或先用 sample_values 确认取值。"
    if not rows:
        return (f"执行成功但 0 行（列：{'、'.join(cols) or '无'}）。\n"
                f"条件可能太严：可以放宽，或先用 sample_values 看真实取值。")
    head = " | ".join(cols)
    body = "\n".join(" | ".join("" if v is None else str(v)[:28] for v in r) for r in rows)
    return _clip(f"{len(rows)} 行（最多显示 {MAX_TOOL_ROWS}）：\n{head}\n{body}")


def _resolve_table(pack: SchemaPack, raw: str) -> str | None:
    """物理名 / 中文标签 / 对象编码 三选一。"""
    if not raw:
        return None
    if raw in pack.tables:
        return raw
    low = raw.lower()
    for name in pack.tables:
        if name.lower() == low:
            return name
    for name, t in pack.tables.items():
        if (t.obj_code or "").lower() == low:
            return name
    base = raw.replace("表", "").strip()
    exact = [n for n, t in pack.tables.items()
             if (t.label or "").replace("表", "").strip() == base]
    if len(exact) == 1:
        return exact[0]
    return None


def _clip(text: str) -> str:
    if len(text) <= MAX_TOOL_CHARS:
        return text
    return text[:MAX_TOOL_CHARS] + f"\n……（已截断，原长 {len(text)} 字符）"


def _parse(raw: str) -> dict | None:
    """从模型输出里取出一个 JSON 对象，取不到返回 None。

    三层容错：```json 包裹 → 整段是 JSON → 正文里夹了一个最外层 {...}。
    都失败就返回 None，由调用方退回「整段当 SQL」。
    """
    text = (raw or "").strip()
    if not text:
        return None

    m = _JSON_BLOCK.search(text)
    if m:
        text = m.group(1)

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return None
