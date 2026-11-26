"""导出「项目对象字段快照」。

2026-11-27：随包快照这一档已从 projlink.load_snapshot 移除，产物不再随包分发。
现在只用于人工核对（当前用的是哪一档、字段数、字典），--asset 写文件仅为留档。

为什么需要快照
--------------
「关联项目」要能把本对象的字段对齐成项目对象的定义，但**不能**在出包时直连客户库
（安全红线：真实库只读、不联网）。所以项目字段清单要和 objects.json 一样冻结一份
随包快照，运行时只读 JSON（架构 D9：刷快照是运维动作，不藏在 web 请求路径里）。

取数优先级（与 projlink.load_snapshot 的三档对应）
--------------------------------------------------
① 目标库（只读 SELECT：iform_bo_def / iform_bo_attr / iform_bo_attr_column /
   iform_cat_type / iform_cat_dic，复用 ~/.pp6kit/nlq.json 的连接配置）
② 本机导出的「项目对象包」zip（PP6/业务对象/项目.zip，走 inspect 解析）

用法：
    .venv/bin/python tools/dump_project_fields.py                # 只打印摘要
    .venv/bin/python tools/dump_project_fields.py --asset        # 写 assets/project_fields.json
    .venv/bin/python tools/dump_project_fields.py --json out/x.json
    .venv/bin/python tools/dump_project_fields.py --zip PP6/业务对象/项目.zip --asset
    .venv/bin/python tools/dump_project_fields.py --no-db --asset   # 强制走 zip 兜底

⚠ 全程只 SELECT / DESC，绝不 INSERT / UPDATE / ALTER / CREATE。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pp6kit import projlink  # noqa: E402

DEFAULT_ZIP = ROOT / "PP6" / "业务对象" / "项目.zip"
ASSET = ROOT / "assets" / "project_fields.json"


def from_db() -> projlink.Snapshot | None:
    """连目标库抽一份快照（只读）。连不上/查不到返回 None，由调用方降级。"""
    try:
        from pp6kit.nlq import introspect, store
    except Exception as exc:                      # pragma: no cover - 依赖缺失时才走
        print(f"· 读不到数据库配置（{exc}），跳过目标库")
        return None
    db = (store.load().get("db") or {})
    if not db.get("host"):
        print("· 还没配置数据库连接（~/.pp6kit/nlq.json），跳过目标库")
        return None
    try:
        conn = introspect.connect(db["host"], db.get("port"), db.get("user"),
                                  db.get("password"), db.get("database"))
    except Exception as exc:
        print(f"· 连库失败：{exc}")
        return None
    try:
        snap = projlink.snapshot_from_cursor(conn.cursor())
    except Exception as exc:
        print(f"· 从库里抽快照失败：{exc}")
        return None
    finally:
        conn.close()
    if snap.source == "unavailable" or not snap.fields:
        print("· 库里没有 code=project 的业务对象")
        return None
    return snap


def from_zip(path: Path) -> projlink.Snapshot | None:
    if not path.is_file():
        print(f"· 没有随包样本 {path}")
        return None
    try:
        snap = projlink.snapshot_from_zip(path, source="package_snapshot")
    except Exception as exc:
        print(f"· 解析 {path.name} 失败：{exc}")
        return None
    if not snap.fields:
        print(f"· {path.name} 里没有可用的项目对象字段")
        return None
    return snap


def summarize(snap: projlink.Snapshot) -> None:
    project = snap.project or {}
    business = project.get("businessFieldCount", 0)
    print(f"✓ 快照来源 {snap.label()}（{snap.source}）")
    print(f"  项目对象 {project.get('code')} / {project.get('name')} / "
          f"{project.get('tbl')}：{len(snap.fields)} 个字段（业务字段 {business}）")
    print(f"  生成时间 {snap.generated_at}")
    dic = [f for f in snap.fields if f.dict is not None]
    for f in dic:
        items = "、".join(f"{i.key}({i.name}/{i.sn})" for i in f.dict.items[:8])
        print(f"  字典 {f.code} → {f.dict.ref}（{f.dict.scope}，{len(f.dict.items)} 项）：{items}")
    for w in snap.warnings:
        print(f"  {w}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出项目对象字段快照（只读）")
    ap.add_argument("--asset", action="store_true",
                    help="写到 assets/project_fields.json（随包快照）")
    ap.add_argument("--json", default="", help="写到指定路径")
    ap.add_argument("--zip", default="", help="从项目对象包解析（默认 PP6/业务对象/项目.zip）")
    ap.add_argument("--no-db", action="store_true", help="不连库，只解析 zip")
    args = ap.parse_args(argv)

    snap = None
    if not args.no_db and not args.zip:
        snap = from_db()
    if snap is None:
        zip_path = Path(args.zip) if args.zip else DEFAULT_ZIP
        snap = from_zip(zip_path)
        if snap is not None and args.zip:
            snap.source = "package_upload"
    if snap is None:
        print("✗ 目标库和项目包都拿不到快照")
        return 1

    payload = projlink.snapshot_payload(snap)
    targets: list[Path] = []
    if args.asset:
        targets.append(ASSET)
    if args.json:
        targets.append(Path(args.json))
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            shown = path.relative_to(ROOT)
        except ValueError:
            shown = path
        print(f"✓ 已写入 {shown}（{len(payload['fields'])} 个字段）")

    summarize(snap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
