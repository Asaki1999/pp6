"""NL2SQL 准确率评测：跑真实库，逐题判分，给出可对比的数字。

为什么要有这个脚本
------------------
「准确率能不能和 DBX 相当」这种问题，靠嘴说没有意义。只有两种办法：
  1. 建题库 + 自动判分，跑出基线，改完再跑
  2. 凭感觉说「应该差不多」—— 然后每次改动都在赌

这个脚本走第 1 条。判分不看 SQL 文本是否字面相等（等价写法太多），
只看四件**确定性的**事：

  exec     能执行不报错           —— 报错就是 0 分，没有讨论空间
  tables   引用了该用的表         —— 选错表是最常见的失败，比语法错更隐蔽
  must     关键列/值出现在 SQL 里  —— 比如「高风险」必须落到 'High' 而不是 '高'
  nonempty 该有数据的题目有数据    —— 0 行往往是条件写歪，不报错但没用

四项全过才算「对」。汇总后能一眼看出瓶颈在哪一层：是选表、是取值映射、
还是条件写得太严。

用法：
    python tools/eval_nl2sql.py                    # 默认 hybrid
    python tools/eval_nl2sql.py --engine agent     # 查证模式
    python tools/eval_nl2sql.py --only 6,7,8       # 只跑指定题号（改完针对性复测）
    python tools/eval_nl2sql.py -v                 # 打印每题生成的 SQL
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------- 题库
#
# 出题原则：
#   - 覆盖真实使用里最常问的形态：过滤 / 计数 / 分组 / 跨表 / 时间 / 多值字段
#   - 每题都踩一个已知的坑（枚举中文→码值、软删除、多值外键、状态子查询）
#   - tables 只写「必须有」的表，多 join 了别的表不扣分
#   - must 是 SQL 里必须出现的字面量（列名或取值），防止「跑通了但查错东西」
CASES: list[dict] = [
    {
        "q": "执行中的项目有哪些",
        "tables": ["pp_project", "pp_project_state"],
        "must": ["state_id_"],
        "note": "状态必须走子查询映射，不能写 name_ = '执行中'",
    },
    {
        "q": "项目的名称和项目经理",
        "tables": ["pp_project"],
        "must": ["name_", "manager_ids_"],
        "note": "经理是多值字段，要用 FIND_IN_SET",
    },
    {
        "q": "有多少个项目",
        "tables": ["pp_project"],
        "must": ["COUNT"],
    },
    {
        "q": "每个项目有多少任务",
        "tables": ["pp_task", "pp_project"],
        "must": ["GROUP BY"],
    },
    {
        "q": "任务数量最多的前5个项目",
        "tables": ["pp_task", "pp_project"],
        "must": ["ORDER BY", "5"],
    },
    {
        "q": "高风险的风险有几个",
        "tables": ["pp_risk"],
        "must": ["rating_", "High"],
        "note": "「高风险」在库里是 'High'，写成 '高' 会查 0 行",
    },
    {
        "q": "每个风险等级有多少个风险",
        "tables": ["pp_risk"],
        "must": ["rating_", "GROUP BY"],
    },
    {
        "q": "跟踪中的风险有哪些",
        "tables": ["pp_risk"],
        "must": ["under_tracking"],
        "note": "枚举 under_tracking 也是个坑：库里码值是英文",
    },
    {
        "q": "进度低于50%的任务",
        "tables": ["pp_task"],
        "must": ["progress_", "50"],
    },
    {
        "q": "上个月创建的项目",
        "tables": ["pp_project"],
        "must": ["create_time_"],
        "note": "本月库里确实 0 条（数据只到 2026-10），所以用上个月出题",
    },
    {
        "q": "计划完成时间在2026年的项目",
        "tables": ["pp_project"],
        "must": ["finish_", "2026"],
    },
    {
        "q": "每个项目阶段的数量",
        "tables": ["pp_project_phase"],
        "must": ["GROUP BY"],
    },
    {
        "q": "没有责任人的任务",
        "tables": ["pp_task"],
        "must": ["assignee_ids_"],
        "note": "多值字段判空要处理空串，不能只写 IS NULL",
    },
    {
        "q": "工时最多的人是谁",
        "tables": ["pp_timesheet", "iform_party_employee"],
        "must": ["normal_hours_"],
    },
    {
        "q": "合同金额合计",
        "tables": ["pp_contract"],
        "must": ["amount_", "SUM"],
    },
    {
        "q": "每个客户有多少合同",
        "tables": ["pp_contract", "pp_customer"],
        "must": ["GROUP BY"],
    },
    {
        "q": "列出所有待办",
        "tables": ["pp_todo"],
        "must": [],
    },
    {
        "q": "进行中的问题有几个",
        "tables": ["pp_issue"],
        "must": ["in_progress"],
        "note": "问题表的状态列叫 status_，不叫 state_",
    },
    {
        "q": "按项目类型统计项目数量",
        "tables": ["pp_project", "pp_classification"],
        "must": ["GROUP BY"],
    },
    {
        "q": "预算超支的项目有哪些",
        # 「超支」有多种正解：预算条目的 调整后 vs 原始、项目预算 vs 实际成本。
        # 实测 agent 选的是 pp_project_budget_item 的 base_amount_ vs
        # original_base_amount_，比出题时设想的 pp_project_budget 更贴切。
        # 所以这里只要求「用到了某张预算类表」，不锁死具体哪一张。
        "tables_any": ["pp_project_budget_item", "pp_project_budget",
                       "pp_project_cost", "pp_project_budget_domain_sum"],
        "must": ["SUM"],
        "note": "报表类，规则引擎有模板，AI 要自己找预算表并做数值比较",
    },
]


def _score(case: dict, res: dict, err: str | None) -> dict:
    """判一道题。四个维度各自独立，最后取交集。"""
    from pp6kit.nlq import guard

    sql = (res or {}).get("sql") or ""
    rows = (res or {}).get("rows") or []

    exec_ok = bool(sql) and not err
    refs = set(guard.referenced_tables(sql)) if sql else set()
    if case.get("tables_any"):
        # 有多张可选正解时，命中任意一张就算对
        tables_ok = bool(refs & set(case["tables_any"]))
    else:
        tables_ok = all(t in refs for t in case["tables"])

    low = (sql or "").lower()
    must_ok = all(m.lower() in low for m in case.get("must", []))

    # 只有明确该有数据的题才查非空；报表类可能真的是 0 行
    nonempty_ok = len(rows) > 0 if case.get("nonempty", True) else True

    return {
        "exec": exec_ok,
        "tables": tables_ok,
        "must": must_ok,
        "nonempty": nonempty_ok,
        "ok": exec_ok and tables_ok and must_ok and nonempty_ok,
        "sql": sql,
        "rows": len(rows),
        "refs": sorted(refs),
        "err": err or "",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="NL2SQL 准确率评测")
    ap.add_argument("--engine", default="hybrid", choices=["hybrid", "llm", "agent", "local"])
    ap.add_argument("--only", default="", help="逗号分隔的题号（从 1 开始）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每题 SQL")
    args = ap.parse_args()

    from pp6kit import webui

    picked = range(len(CASES))
    if args.only:
        picked = [int(x) - 1 for x in args.only.split(",") if x.strip()]

    print(f"引擎：{args.engine}　题目：{len(list(picked))} 道\n" + "=" * 72)
    results = []
    t0 = time.time()

    for i in picked:
        case = CASES[i]
        err = None
        res = {}
        started = time.time()
        try:
            res = webui._nlq_ask_impl({"question": case["q"], "engine": args.engine})
        except Exception as exc:
            err = str(exc)[:160]
        cost = time.time() - started

        got = _score(case, res, err)
        results.append((i, case, got))

        mark = "✓" if got["ok"] else "✗"
        flags = []
        if not got["exec"]:
            flags.append("执行失败")
        if not got["tables"]:
            flags.append("表不对")
        if not got["must"]:
            flags.append("关键列/值缺失")
        if not got["nonempty"]:
            flags.append("0 行")
        print(f"[{i + 1:2d}] {mark} {case['q']}　({cost:.1f}s)　{('　← ' + '、'.join(flags)) if flags else ''}")
        if flags:
            if got["err"]:
                print(f"      错误：{got['err']}")
            else:
                print(f"      表：{', '.join(got['refs']) or '（无）'}　行数：{got['rows']}")
                want = case.get("tables") or case.get("tables_any") or []
                print(f"      需要：{', '.join(want)}" + ("（任一）" if case.get("tables_any") else ""))
        if args.verbose and got["sql"]:
            print(f"      SQL: {got['sql'][:220]}")

    total = len(results)
    def pct(k: str) -> str:
        n = sum(1 for _, _, g in results if g[k])
        return f"{n}/{total} ({n * 100 // max(total, 1)}%)"

    print("=" * 72)
    print(f"执行成功  {pct('exec')}")
    print(f"表命中    {pct('tables')}")
    print(f"关键列值  {pct('must')}")
    print(f"结果非空  {pct('nonempty')}")
    print(f"综合准确  {pct('ok')}")
    print(f"耗时 {time.time() - t0:.1f}s")

    bad = [(i, c, g) for i, c, g in results if not g["ok"]]
    if bad:
        print("\n失败明细（按瓶颈归类）：")
        for i, c, g in bad:
            why = ("执行失败" if not g["exec"] else
                   "表不对" if not g["tables"] else
                   "关键列/值缺失" if not g["must"] else "0 行")
            print(f"  [{i + 1}] {c['q']} → {why}")
            if c.get("note"):
                print(f"      出题意图：{c['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
