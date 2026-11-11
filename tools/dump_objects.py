"""从目标库导出业务对象清单，存成随包快照 assets/objects.json。

PM 的机器不一定能连库，快照让「关联对象」这一栏在离线时也有正确候选。
用法：.venv/bin/python tools/dump_objects.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pp6kit import objects  # noqa: E402


def main() -> int:
    try:
        res = objects.refresh()
    except Exception as exc:
        print(f"✗ 连库失败：{exc}")
        return 1
    items = objects.catalog(force=True)
    out = ROOT / "assets" / "objects.json"
    out.write_text(json.dumps({"objects": items, "count": len(items)},
                              ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"✓ {res['count']} 个业务对象 → {out.relative_to(ROOT)}")
    for it in items[:6]:
        print(f"    {it['code']:<22} {it['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
