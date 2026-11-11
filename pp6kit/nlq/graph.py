"""表间关系图：最短 join 路径。

为什么要它：真实问题经常跨两个对象——「每个客户有多少项目」「每个产品的缺陷数」。
这两张表之间没有直接外键，但可以经中间表连起来（客户 → 合同 → 项目）。
硬编码这些路径换客户就废了，所以从元数据的关系列实时算 BFS。

图的边来自 iform_bo_attr 里 object/user/department 类型的字段，
和 local.py 用的是同一份关系数据，不会出现两套说法。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .introspect import SchemaPack


@dataclass(frozen=True)
class Edge:
    src: str      # 有外键的表
    col: str      # 外键列
    dst: str      # 被指向的表
    multi: bool = False   # 多值（逗号分隔）


def build_edges(pack: SchemaPack) -> list[Edge]:
    out: list[Edge] = []
    for src, rels in pack.relations.items():
        table = pack.tables.get(src)
        if table is None:
            continue
        physical = {c.name for c in table.columns}
        for r in rels:
            dst = r["dst"]
            # 元数据里的属性可能映射到了已改名的列，物理列不存在就别拿去 join
            if r["col"] not in physical:
                continue
            if dst in pack.tables and dst != src:
                out.append(Edge(src, r["col"], dst, bool(r.get("multi"))))
    return out


def _alias_for(name: str, used: set[str]) -> str:
    base = "j_" + name.replace("pp_", "").replace("iform_", "")[:18]
    alias, i = base, 2
    while alias in used:
        alias, i = f"{base}{i}", i + 1
    used.add(alias)
    return alias


def shortest_path(pack: SchemaPack, start: str, target: str,
                  max_hops: int = 3) -> list[Edge] | None:
    """从 start 到 target 的最短关系路径（无向遍历，方向在生成 join 时处理）。"""
    if start == target:
        return []
    edges = build_edges(pack)
    adj: dict[str, list[Edge]] = {}
    for e in edges:
        adj.setdefault(e.src, []).append(e)
        adj.setdefault(e.dst, []).append(e)

    q: deque[tuple[str, list[Edge]]] = deque([(start, [])])
    seen = {start}
    while q:
        node, path = q.popleft()
        if len(path) >= max_hops:
            continue
        for e in adj.get(node, []):
            nxt = e.dst if e.src == node else e.src
            if nxt in seen:
                continue
            new_path = path + [e]
            if nxt == target:
                return new_path
            seen.add(nxt)
            q.append((nxt, new_path))
    return None


def join_clauses(pack: SchemaPack, start: str, path: list[Edge],
                 base_alias: str, used_aliases: set[str] | None = None
                 ) -> tuple[list[str], dict[str, str]]:
    """把路径翻译成 LEFT JOIN 列表，返回 (join 子句, {表名: 别名})。"""
    used = used_aliases if used_aliases is not None else {base_alias}
    aliases = {start: base_alias}
    joins: list[str] = []
    node = start
    for e in path:
        nxt = e.dst if e.src == node else e.src
        if nxt in aliases:
            node = nxt
            continue
        alias = _alias_for(nxt, used)
        aliases[nxt] = alias
        if e.src == node:
            # node 持有外键指向 nxt：JOIN nxt ON nxt.id_ = node.col
            cond = (f"FIND_IN_SET(`{alias}`.`id_`, `{aliases[node]}`.`{e.col}`) > 0"
                    if e.multi else f"`{alias}`.`id_` = `{aliases[node]}`.`{e.col}`")
        else:
            # nxt 持有外键指向 node
            cond = (f"FIND_IN_SET(`{aliases[node]}`.`id_`, `{alias}`.`{e.col}`) > 0"
                    if e.multi else f"`{aliases[node]}`.`id_` = `{alias}`.`{e.col}`")
        joins.append(f"LEFT JOIN `{nxt}` `{alias}` ON {cond}")
        node = nxt
    return joins, aliases


def reachable_hubs(pack: SchemaPack, top: int = 10) -> list[tuple[int, str, str]]:
    """连接度最高的表，用于「哪些对象是中心」的判断。"""
    degree: dict[str, int] = {}
    for e in build_edges(pack):
        degree[e.src] = degree.get(e.src, 0) + 1
        degree[e.dst] = degree.get(e.dst, 0) + 1
    ranked = sorted(degree.items(), key=lambda x: -x[1])[:top]
    return [(d, n, pack.tables[n].label) for n, d in ranked]
