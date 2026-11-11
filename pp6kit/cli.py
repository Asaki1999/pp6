"""命令行入口：

    python -m pp6kit template  [out.xlsx]        生成填写模板（带示例）
    python -m pp6kit check     <xlsx>            只校验
    python -m pp6kit build     <xlsx> [-o DIR]   生成可导入的 zip 包
    python -m pp6kit inspect   <zip|目录> [-o xlsx]  把已有导出包反解成 Excel
    python -m pp6kit capture   <样例目录>         重新学习控件/视图骨架
    python -m pp6kit ui                          启动可视化界面（推荐）
    python -m pp6kit learn     <导出zip或目录>   校准字段类型字典
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNIPPETS = Path(os.environ.get("PP6_SNIPPETS", ROOT / "assets" / "snippets.json"))


def load_snippets(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"缺少骨架文件 {path}\n请先运行: python -m pp6kit capture <系统导出样例目录>"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _print_issues(issues) -> tuple[int, int]:
    errors = sum(1 for i in issues if i.level == "error")
    warns = sum(1 for i in issues if i.level == "warn")
    for issue in issues:
        print(f"  {issue}")
    return errors, warns


def cmd_template(args) -> int:
    from .template import build_workbook, demo_project

    proj = demo_project()
    wb = build_workbook(proj)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"✓ 模板已生成: {out}")
    print("  已预置一套「采购申请」示例，可直接改字复用；改完执行 build 即可出包。")
    return 0


def _print_auto_notes(proj) -> None:
    """展示自动补齐的东西——用户没填/填成中文名的，得让他知道程序改成了什么。"""
    notes = getattr(proj, "auto_notes", None) or []
    if not notes:
        return
    print(f"\n自动补齐 {len(notes)} 项（编码按名称生成、关联对象按真实对象对照，可在 Excel 里改）:")
    for n in notes:
        print(f"  · {n}")


def cmd_check(args) -> int:
    from .reader import read_workbook
    from .validate import validate

    proj, read_errors, stats = read_workbook(Path(args.xlsx))
    print("读取概况: " + "  ".join(f"{k}={v}" for k, v in stats.items() if v))
    _print_auto_notes(proj)
    issues = []
    if read_errors:
        from .validate import Issue

        issues = [Issue("error", "读取", e) for e in read_errors]
    issues += validate(proj)
    errors, warns = _print_issues(issues)
    print(f"\n结果: {errors} 个错误, {warns} 个警告")
    return 1 if errors else 0


def cmd_build(args) -> int:
    from .builder import build
    from .reader import read_workbook
    from .validate import validate

    xlsx = Path(args.xlsx)
    proj, read_errors, stats = read_workbook(xlsx)
    print("读取概况: " + "  ".join(f"{k}={v}" for k, v in stats.items() if v))
    _print_auto_notes(proj)

    from .validate import Issue

    issues = [Issue("error", "读取", e) for e in read_errors] + validate(proj)
    errors, warns = _print_issues(issues)
    if errors:
        print(f"\n✗ 有 {errors} 个错误，已中止生成。修好后重跑。")
        return 1

    snippets = load_snippets(Path(args.snippets))
    out = Path(args.out) if args.out else ROOT / proj.out_dir
    codes = None
    if getattr(args, "modules", ""):
        codes = [x.strip() for x in args.modules.replace("，", ",").split(",") if x.strip()]
    res = build(proj, snippets, out, codes=codes)
    if not res.deliverables:
        print("\n✗ 选中的模块没有可生成的内容，检查一下 Excel 里对应的表是否填了。")
        return 1

    print(f"\n✓ 生成完成 -> {out / '按模块'}")
    for i, d in enumerate(res.deliverables, 1):
        print(f"  {i}. {d.label:<6} {d.filename:<24} {len(d.items):>3} 项  {d.bytes:>7} B   →  {d.menu}")
        for a in d.items:
            print(f"       · {a.kind} {a.key}")
    if res.notices:
        print("\n提示:")
        for n in dict.fromkeys(res.notices):
            print(f"  · {n}")
    allp = next((d for d in res.deliverables if d.module == "all"), None)
    if allp:
        print(f"\n共 {len(res.deliverables)} 个包。两种用法：")
        print(f"  · 一把导入：{allp.filename}（{out / '整包'}）—— 所有模块在一个 zip 里")
        print(f"  · 按模块导入：上面逐个上传，适合只更新某一块或排查问题")
    else:
        print(f"\n共 {len(res.deliverables)} 个模块包。系统里按上面顺序逐个模块导入即可。")
    if warns:
        print(f"（{warns} 条警告未阻断生成，建议按提示核对）")
    return 0


def cmd_inspect(args) -> int:
    from .inspect import inspect_path
    from .template import build_workbook

    src = Path(args.path)
    if not src.exists():
        raise SystemExit(f"路径不存在: {src}")
    proj, notes = inspect_path(src)
    wb = build_workbook(proj)
    out = Path(args.out) if args.out else ROOT / "out" / (src.stem + "_反解.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)

    print(f"✓ 已反解 -> {out}")
    print(f"  业务对象 {len(proj.bos)} / 表单 {len(proj.forms)} / 列表 {len(proj.lists)} / 数据集 {len(proj.datasets)}")
    for n in notes:
        print(f"  · {n}")
    print("  提示：系统内置字段（id/tenantId/createBy 等）已自动省略，build 时会补回。")
    return 0


def cmd_ui(args) -> int:
    from .webui import serve

    serve(args.port, open_browser=not args.no_browser)
    return 0


def cmd_audit(args) -> int:
    from .audit import main as audit_main
    argv = ["audit"]
    if getattr(args, "only", ""):
        argv += ["--only", args.only]
    if getattr(args, "sweep", False):
        argv.append("--sweep")
    return audit_main(argv)


def cmd_probe(args) -> int:
    from .probe import main as probe_main
    import sys as _sys

    argv = ["probe"]
    if getattr(args, "show_sql", False):
        argv.append("--show-sql")
    if getattr(args, "only", ""):
        argv += ["--only", args.only]
    return probe_main(argv)


def cmd_profile(args) -> int:
    from .dbprofile import main as profile_main

    return profile_main()


def cmd_dbmap(args) -> int:
    from .dbmap import main as dbmap_main
    import sys as _sys

    argv = _sys.argv
    _sys.argv = ["dbmap", "--out", args.out]
    try:
        return dbmap_main()
    finally:
        _sys.argv = argv


def cmd_learn(args) -> int:
    from .learn import learn

    src = Path(args.path)
    if not src.exists():
        raise SystemExit(f"路径不存在: {src}")
    verified, added, lines = learn(src)
    print(f"✓ 已用 {src} 校准字段类型")
    for line in lines:
        print(line)
    print(f"\n已校准 {verified} 种，新增 {added} 种。字典：assets/fieldtypes.json")
    return 0


def cmd_capture(args) -> int:
    sys.path.insert(0, str(ROOT / "tools"))
    from capture_snippets import capture  # type: ignore

    snip = capture(Path(args.sample_dir))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snip, ensure_ascii=False, indent=1), encoding="utf-8")
    m = snip["_meta"]
    print(f"✓ 骨架已更新: {out}")
    print(f"  控件 {len(m['fieldTypes'])} 种 / 视图 {', '.join(m['views'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pp6kit", description="PP6 Studio — PowerProject V6 配置同步工作台")
    ap.add_argument("--snippets", default=str(DEFAULT_SNIPPETS), help="骨架文件路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("template", help="生成填写模板")
    p.add_argument("out", nargs="?", default=str(ROOT / "out" / "模板.xlsx"))
    p.set_defaults(func=cmd_template)

    p = sub.add_parser("check", help="只校验 Excel")
    p.add_argument("xlsx")
    p.set_defaults(func=lambda a: cmd_check(a))

    p = sub.add_parser("build", help="生成 zip 包")
    p.add_argument("xlsx")
    p.add_argument("-o", "--out", help="输出目录")
    p.add_argument("-m", "--modules", default="",
                   help="只出这些模块，逗号分隔：bo,dataset,filter,form,list（默认全部）")
    p.set_defaults(func=lambda a: cmd_build(a))

    p = sub.add_parser("inspect", help="导出包反解成 Excel")
    p.add_argument("path")
    p.add_argument("-o", "--out")
    p.set_defaults(func=lambda a: cmd_inspect(a))

    p = sub.add_parser("ui", help="启动本地可视化界面")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=lambda a: cmd_ui(a))

    p = sub.add_parser("audit", help="审计规则引擎的盲区（需 AI 兜底的问法）")
    p.add_argument("--only", default="", help="只跑问题里含该关键词的")
    p.add_argument("--sweep", action="store_true", help="全表全列扫描（查幽灵列）")
    p.set_defaults(func=lambda a: cmd_audit(a))

    p = sub.add_parser("probe", help="批量问答自检（跑内置语料）")
    p.add_argument("--show-sql", action="store_true")
    p.add_argument("--only", default="", help="只跑问题里含该关键词的")
    p.set_defaults(func=lambda a: cmd_probe(a))

    p = sub.add_parser("profile", help="全库画像（行数/软删除/枚举/空表）")
    p.set_defaults(func=lambda a: cmd_profile(a))

    p = sub.add_parser("dbmap", help="生成数据库结构图谱")
    p.add_argument("--out", default="docs/数据库结构图谱.md")
    p.set_defaults(func=lambda a: cmd_dbmap(a))

    p = sub.add_parser("learn", help="用导出包校准字段类型字典")
    p.add_argument("path")
    p.set_defaults(func=lambda a: cmd_learn(a))

    p = sub.add_parser("capture", help="重新学习骨架")
    p.add_argument("sample_dir")
    p.add_argument("-o", "--out", default=str(DEFAULT_SNIPPETS))
    p.set_defaults(func=lambda a: cmd_capture(a))

    args = ap.parse_args(argv)
    if args.cmd == "template":
        # template 需要先有 out 目录
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
