"""配置与示例库的本地持久化。

存到用户目录而不是程序目录：桌面包解压后目录是只读的，
而且实施换机器时这个文件可以跟着走。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# 项目团队类问题曾经按「项目 → 成员」直连生成，漏掉了角色这一层，
# 而且错误答案被存成了 few-shot 示例，会持续污染「用 AI 重答」。
# 这类过时示例在加载时剔除，交给 local 的语义模板。
_RE_TEAM_Q = re.compile(r"团队|成员|角色")


def _drop_stale_examples(data: dict) -> None:
    keep = []
    for e in data.get("examples") or []:
        q = str(e.get("question") or "")
        sql = str(e.get("sql") or "")
        # 团队问题 + 碰了成员表 + 完全没碰角色表 → 就是那条错答案
        if _RE_TEAM_Q.search(q) and "pp_project_member" in sql and "pp_project_role" not in sql:
            continue
        keep.append(e)
    data["examples"] = keep


def _dir() -> Path:
    base = Path.home() / ".pp6kit"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _file() -> Path:
    return _dir() / "nlq.json"


def load() -> dict:
    path = _file()
    if not path.exists():
        return {"db": {}, "llm": {}, "examples": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"db": {}, "llm": {}, "examples": []}
    data.setdefault("db", {})
    data.setdefault("llm", {})
    data.setdefault("examples", [])
    _drop_stale_examples(data)
    return data


def save(data: dict) -> None:
    """写配置，并保留一份上一版备份。

    踩过的坑：调试时用假 Key 调用测试接口，直接把用户真 Key 覆盖了，
    又没备份，只能让用户重新去申请。现在每次写入前先留 .bak。
    """
    path = _file()
    bak = path.with_suffix(".json.bak")
    try:
        if path.exists() and path.stat().st_size > 0:
            bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass          # 备份失败不能影响正常保存
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def restore_backup() -> bool:
    """从 .bak 恢复上一次的配置，成功返回 True。"""
    path = _file()
    bak = path.with_suffix(".json.bak")
    if not bak.exists():
        return False
    try:
        path.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
        return True
    except Exception:
        return False


def save_db(cfg: dict) -> None:
    data = load()
    data["db"] = {k: cfg.get(k, "") for k in ("host", "port", "user", "password", "database")}
    save(data)


def save_llm(cfg: dict) -> None:
    """保存大模型配置，支持多渠道。

    cfg 可含：
        channels: [{"key","base_url","api_key","model","enabled"}, ...]
        base_url / api_key / model   ← 旧格式，自动归一成单渠道
    """
    data = load()
    cur = data.get("llm") or {}

    channels = cfg.get("channels")
    if channels is not None:
        # 空 Key 的渠道：如果之前存过同 key 的 Key，就沿用（用户只改了模型名的情况）
        # 但带了 clear_key=True 时是真的要删掉，不能沿用
        old = {c.get("key"): c.get("api_key", "") for c in (cur.get("channels") or [])}
        cleaned = []
        for ch in channels:
            item = {
                "key": ch.get("key", ""),
                "base_url": (ch.get("base_url") or "").strip(),
                "api_key": (ch.get("api_key") or "").strip(),
                "model": (ch.get("model") or "").strip(),
                "enabled": bool(ch.get("enabled", True)),
            }
            if ch.get("clear_key"):
                item["api_key"] = ""
            elif not item["api_key"] and item["key"] in old:
                item["api_key"] = old[item["key"]]
            # 同平台备用模型：主模型限流时自动降级（免费档 429 太常见）
            fb = [m for m in (ch.get("fallbacks") or []) if m and m != item["model"]]
            item["fallbacks"] = fb[:4]
            cleaned.append(item)
        data["llm"] = {"channels": cleaned}
    else:
        # 旧格式兼容
        data["llm"] = {
            "base_url": (cfg.get("base_url") or "").strip(),
            "api_key": (cfg.get("api_key") or cur.get("api_key") or "").strip(),
            "model": (cfg.get("model") or "").strip(),
        }
        if not data["llm"]["api_key"] and cfg.get("api_key") is None:
            data["llm"]["api_key"] = cur.get("api_key", "")
    save(data)


def load_llm() -> dict:
    """读出大模型配置，并把旧格式归一成 channels，调用方只需关心一种结构。"""
    llm = load().get("llm") or {}
    if llm.get("channels"):
        return llm
    if llm.get("base_url") or llm.get("api_key"):
        return {"channels": [{
            "key": "",
            "base_url": llm.get("base_url", ""),
            "api_key": llm.get("api_key", ""),
            "model": llm.get("model", ""),
            "enabled": True,
        }]}
    return {"channels": []}


def add_example(question: str, sql: str, verified: bool = False,
                source: str = "ai") -> None:
    data = load()
    old = next((e for e in data["examples"] if e.get("question") == question), {})
    # 人工修正过的问法，AI 再学一次也不能覆盖（人比模型权威）
    if old.get("source") == "corrected":
        return
    examples = [e for e in data["examples"] if e.get("question") != question]
    examples.insert(0, {
        "question": question,
        "sql": sql,
        # 之前人工验证过的，不能被后来一次 AI 学习降级
        "verified": bool(verified or old.get("verified")),
        "source": source,
    })
    data["examples"] = examples[:200]
    save(data)


def verify_example(question: str, sql: str = "") -> dict:
    """把一条问答标成「已验证」——之后检索优先用它，提示词里也会标注。

    只信人点过的那一下；AI 自己答对不算验证（它也会错）。
    """
    q = (question or "").strip()
    if not q:
        raise ValueError("问题不能为空")
    data = load()
    hit = next((e for e in data["examples"] if (e.get("question") or "").strip() == q), None)
    if hit is None:
        if not sql.strip():
            raise ValueError("示例库里没有这个问题，需要同时提供 SQL")
        hit = {"question": q, "sql": sql.strip()}
        data["examples"].insert(0, hit)
    hit["verified"] = True
    hit["verified_at"] = datetime.now().isoformat(timespec="seconds")
    if sql.strip():
        hit["sql"] = sql.strip()
    save(data)
    return {"saved": True, **library_stats(data)}


def library_stats(data: dict | None = None) -> dict:
    ex = (data or load()).get("examples") or []
    return {"total": len(ex), "verified": sum(1 for e in ex if e.get("verified"))}


def save_correction(question: str, sql: str, original_sql: str = "", note: str = "") -> dict:
    """记录「用户手工改过的 SQL」。

    这是最有价值的训练数据：用户把机器生成的 SQL 改对了，
    说明原来那条在他这个业务语境下是错的。存下来之后
    ① 同样的问题再来，直接用他改的这条；
    ② 相似的问题也能从这条借写法。

    note 是他写的一句话原因（如「我们只看未关闭的」）——
    半年后回看「这条规则为什么长这样」，全靠这句话。
    """
    data = load()
    question = (question or "").strip()
    sql = (sql or "").strip().rstrip(";").strip()
    examples = [e for e in data["examples"] if e.get("question") != question]
    examples.insert(0, {
        "question": question,
        "sql": sql,
        "source": "corrected",          # 人工修正，权重最高
        "note": (note or "").strip()[:200],
        "original_sql": (original_sql or "").strip(),
        "corrected_at": datetime.now().isoformat(timespec="seconds"),
    })
    data["examples"] = examples[:200]
    save(data)
    return {"saved": True, "total": len(data["examples"])}


def find_correction(question: str) -> dict | None:
    """精确同问 + 人工修正过的，优先返回。"""
    q = (question or "").strip()
    for e in all_examples():
        if e.get("source") == "corrected" and (e.get("question") or "").strip() == q:
            return e
    return None


def all_examples() -> list[dict]:
    return load().get("examples", [])


_BUILTIN_CACHE: list[dict] | None = None


def builtin_examples() -> list[dict]:
    """随程序发布的已验证问答库（assets/ai_examples.json，由 tools/build_ai_corpus.py 生成）。

    这些是跑通过、能出数据的问句→SQL 对，用作 AI 的 few-shot。
    比公开的英文 text-to-SQL 数据集贴合得多——同一个库、同一套命名约定。
    """
    global _BUILTIN_CACHE
    if _BUILTIN_CACHE is not None:
        return _BUILTIN_CACHE
    # 冻结包里 __file__ 在临时解包目录，必须走 resources 统一解析
    from ..resources import resource_root
    path = resource_root() / "assets" / "ai_examples.json"
    try:
        _BUILTIN_CACHE = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _BUILTIN_CACHE = []
    return _BUILTIN_CACHE


def pick_examples(question: str, limit: int = 6, tables: list[str] | None = None) -> list[dict]:
    """挑最像的历史问答当 few-shot。

    按「二元组重叠」而不是单字重叠算相似度——
    「每个客户有多少项目」和「每个项目经理负责的项目数量」共享的字很多，
    但二元组只有「每个」几个，不该算像。

    打分 = 0.7 × 问句相似度 + 0.3 × 表重合度。
    表重合度很重要：问「工时」时，即便问句措辞不同，
    命中同一批表的示例也比措辞更像但换了表的示例有用得多。
    用户自己积累的示例优先于内置库（更贴近他的问法习惯）。
    """
    user_examples = all_examples()
    seen = {e.get("question") for e in user_examples}
    # 内置语料是跑通过的问答对，默认视为已验证；用户新增的要人工点「已验证」
    examples = list(user_examples) + [
        {**e, "verified": True}
        for e in builtin_examples() if e.get("question") not in seen
    ]
    if not examples:
        return []

    def bigrams(text: str) -> set[str]:
        t = "".join((text or "").split())
        return {t[i:i + 2] for i in range(len(t) - 1)} or {t}

    target = bigrams(question)
    want_tables = set(tables or [])
    scored = []
    for ex in examples:
        other = bigrams(ex.get("question", ""))
        if not other or not target:
            continue
        overlap = len(target & other)
        if overlap < 2:
            continue
        # 用 Jaccard 抑制「共享几个虚词就算像」的情况
        sim = overlap / (len(target) + len(other) - overlap)
        tb = 0.0
        if want_tables:
            ex_tables = set(ex.get("tables") or [])
            if ex_tables:
                tb = len(want_tables & ex_tables) / len(want_tables)
        score = 0.7 * sim + 0.3 * tb
        # 人工修正过的示例加权重：这是用户亲自改对的，比我们预置的和 AI 生成的都可信
        if ex.get("source") == "corrected":
            score += 0.5
        if ex.get("verified"):
            score += 0.8
        scored.append((score, ex))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:limit]]


def find_similar(question: str, threshold: float = 0.42) -> dict | None:
    """找一条**足够像**的历史问答，可以直接复用它的 SQL。

    阈值 0.42 是实测出来的：
        「每个项目经理负责的项目数量」vs「…负责多少项目」→ 0.50  ✓ 该命中
        「每个项目经理负责的项目数量」vs「每个客户有多少项目」→ 0.12  ✗ 不该命中
        「每个项目的预算总额」vs「每个项目有多少预算条目」→ 0.29    ✗ 不该命中
    取中间值，既能让换个说法的同类问题命中，又不至于张冠李戴。
    """
    hits = pick_examples(question, limit=1)
    if not hits:
        return None

    def bigrams(text: str) -> set[str]:
        t = "".join((text or "").split())
        return {t[i:i + 2] for i in range(len(t) - 1)} or {t}

    a, b = bigrams(question), bigrams(hits[0].get("question", ""))
    if not a or not b:
        return None
    sim = len(a & b) / (len(a) + len(b) - len(a & b))
    return hits[0] if sim >= threshold else None
