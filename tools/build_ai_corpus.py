"""把已验证的问答语料导出成 AI few-shot 库。

为什么这么做
------------
Vanna 官方训练建议里的核心结论：
    「输出准确率的主要决定因素是训练数据质量，
      其中最有价值的是**已知正确的问题→SQL 对**；
      正确 SQL 越贴近新问题，模型改得越准。」

我们手上正好有 202 条**已经跑通验证**的问句（assets/probe_questions.json），
用本地规则引擎生成 SQL 再执行一遍，得到「问句 + 这段 schema 下确实能跑的 SQL」，
这就是质量最高的 few-shot 语料——比公开 English text-to-SQL 数据集贴合得多
（Spider 5183 条里 78% 是 0–2 分复杂度的 JOIN/COUNT/GROUP BY，
 结构上我们已覆盖，缺的是「中文问法 → 本库表」的对应关系）。

用法：
    .venv/bin/python tools/build_ai_corpus.py
产出：
    assets/ai_examples.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pp6kit.nlq import guard, introspect, local, store  # noqa: E402

OUT = ROOT / "assets" / "ai_examples.json"


def main() -> int:
    cfg = store.load()["db"]
    if not cfg.get("host"):
        print("✗ 还没配置数据库，先在界面里填连接信息")
        return 1

    conn = introspect.connect(cfg["host"], cfg["port"], cfg["user"],
                              cfg["password"], cfg["database"])
    pack = introspect.build_schema(conn, cfg["database"])
    introspect.annotate_liveness(conn, pack, list(pack.tables))
    introspect.annotate_columns(conn, pack)
    allowed = set(pack.tables) | guard.PARTY_TABLES | {"iform_cat_dic", "iform_cat_type"}

    cases = json.loads((ROOT / "assets" / "probe_questions.json").read_text(encoding="utf-8"))
    print(f"读入 {len(cases)} 条验证过的问句")

    pairs: list[dict] = []
    skipped: list[str] = []
    for case in cases:
        q = case["q"]
        try:
            sql, _note = local.build(pack, q)
            safe = guard.validate(sql, allowed)
            cur = conn.cursor()
            cur.execute(safe)
            rows = cur.fetchmany(1)
            cur.close()
            if not rows:
                skipped.append(f"{q} → 0 行")
                continue
        except Exception as e:
            skipped.append(f"{q} → {type(e).__name__}")
            continue
        pairs.append({"question": q, "sql": safe, "tables": case.get("tables", [])})

    conn.close()

    # 按问句去重，保留先出现的
    seen: set[str] = set()
    uniq = []
    for p in pairs:
        if p["question"] in seen:
            continue
        seen.add(p["question"])
        uniq.append(p)

    OUT.write_text(json.dumps(uniq, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ 导出 {len(uniq)} 条问答对 → {OUT.relative_to(ROOT)}")
    if skipped:
        print(f"  跳过 {len(skipped)} 条：")
        for s in skipped[:8]:
            print(f"    · {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
