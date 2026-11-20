"""@ 显式提及：用户点名的表优先于打分器猜。

借鉴 DBX 的显式表引用设计（docs/ai-assistant.mdx「Schema, Table, and SQL
File Context」）：`@users` 优先给 users 的元数据，比让模型在几十张表里猜
可靠得多，也让 schema 预算可以放心裁剪——用户点了名就不会被截掉。

支持的写法：
    @pp_project          物理表名
    @项目                中文标签（完全相等优先，其次最短包含匹配）
    @PRJ                 对象编码（obj_code，大小写不敏感）

认不出来的 token 原样保留：用户可能只是在写邮箱，不能把 `a@b.com` 吃掉。
"""
from __future__ import annotations

import re

from .introspect import SchemaPack

_ASCII = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CJK = re.compile(r"[\u4e00-\u9fa5]{1,12}")
_GAP = re.compile(r"\s{2,}")


def parse(pack: SchemaPack, question: str) -> tuple[list[str], str]:
    """返回 (点名表, 去掉 @ 之后的问句)。

    同一个 token 只认一张表；表名重复提及只算一次。
    """
    if not question or "@" not in question:
        return [], question or ""

    pinned: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while True:
        at = question.find("@", i)
        if at < 0:
            break
        hit, end = _match_at(pack, question, at)
        if hit:
            if hit not in pinned:
                pinned.append(hit)
            spans.append((at, end))
            i = end
        else:
            i = at + 1

    if not spans:
        return [], question

    out, last = [], 0
    for a, b in spans:
        out.append(question[last:a])
        last = b
    out.append(question[last:])
    clean = _GAP.sub(" ", "".join(out)).strip()
    # 整句都是 @表名 时清理后为空——退回原句，别让 empty question 把请求打死
    return pinned, clean or question


def _match_at(pack: SchemaPack, text: str, at: int) -> tuple[str | None, int]:
    """从 @ 之后开始匹配，返回 (表名, 含 @ 的结束下标)。"""
    rest = text[at + 1:]

    m = _ASCII.match(rest)
    if m:
        hit = _resolve(pack, m.group(0))
        return (hit, at + 1 + len(m.group(0))) if hit else (None, 0)

    # 中文没有分隔符：`@项目里` 必须先贪心取到最长，再逐字回退，
    # 否则「项目」永远匹配不到后面还跟着字的情况
    cjk = _CJK.match(rest)
    if not cjk:
        return None, 0
    run = cjk.group(0)
    for n in range(len(run), 0, -1):
        hit = _resolve(pack, run[:n])
        if hit:
            return hit, at + 1 + n
    return None, 0


def _resolve(pack: SchemaPack, raw: str) -> str | None:
    """把一个 token 解析成物理表名，认不出来返回 None。"""
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

    # 中文标签：完全相等优先，其次取最短的包含匹配
    # （「项目」不能命中「项目阶段」——150 张表里最短的才是用户想说的那张）
    exact = [n for n, t in pack.tables.items() if _base(t.label) == raw]
    if len(exact) == 1:
        return exact[0]
    contains = [(len(_base(t.label)), n) for n, t in pack.tables.items()
                if raw in _base(t.label)]
    if contains:
        contains.sort()
        return contains[0][1]
    return None


def _base(label: str) -> str:
    return (label or "").replace("表", "").strip()
