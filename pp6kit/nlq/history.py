"""查询历史：最近 30 条问句与 SQL。

为什么不塞进 `nlq.json`：
  - 那份文件是配置 + 示例库，每次保存整体重写；历史是高频写入，
    混进去等于每查一次就要重写几百条示例。
  - 历史需要「一键清空」，配置不能跟着被清掉。

只存问句、SQL 和结果规模，**不存结果行**：几十万行的表格体量太大，
而且客户业务数据不该被复制到另一个文件里长期留着。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

MAX_ITEMS = 30

# 单条 SQL 的存储上限：超长的动态拼表语句截断即可，历史是用来回填的，
# 不是审计日志。截断后仍能重新执行（用户能看出来少了尾巴）。
MAX_SQL_CHARS = 4000
MAX_Q_CHARS = 300


def _file() -> Path:
    base = Path.home() / ".pp6kit"
    base.mkdir(parents=True, exist_ok=True)
    return base / "query_history.json"


def load() -> list[dict]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else data
    return [x for x in (items or []) if isinstance(x, dict) and x.get("sql")]


def add(question: str, sql: str, mode: str = "", action: str = "",
        rows: int = 0, ms: int = 0, origin: str = "") -> None:
    """记一条。同问句 + 同 SQL 只留最新，避免反复「重新执行」刷屏。"""
    q = (question or "").strip()
    s = (sql or "").strip()
    if not q or not s:
        return

    items = [x for x in load()
             if not (x.get("question") == q[:MAX_Q_CHARS]
                     and x.get("sql") == s[:MAX_SQL_CHARS])]
    items.insert(0, {
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "question": q[:MAX_Q_CHARS],
        "sql": s[:MAX_SQL_CHARS],
        "mode": mode,
        "action": action,
        "rows": int(rows or 0),
        "ms": int(ms or 0),
        "origin": origin,
    })
    _write(items[:MAX_ITEMS])


def clear() -> None:
    try:
        _file().unlink()
    except FileNotFoundError:
        pass


def _write(items: list[dict]) -> None:
    """原子写：先写 .tmp 再 replace，中途断电不会留下半截 JSON。"""
    path = _file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)
