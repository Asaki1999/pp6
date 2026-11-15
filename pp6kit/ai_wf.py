"""一句话生成 / 修改审批流程。

为什么单开一条路
----------------
「普通模式」的 AI 只产出业务对象（对象 / 表单 / 列表），不产出流程三张表；
流程的节点、处理人、分支条件以前只能在 Excel 里手填，或者去线上设计器画。
但业务同事描述流程永远比画图快：「采购申请先部门经理，金额过 10 万再加
总经理，最后归档」——这句话里信息量足够生成一条能跑的流程。

做法：把当前工作簿的对象清单 + 现有流程结构压成紧凑上下文交给模型，
让它输出**结构化的流程 JSON**，再由这里转成 Workflow 写回 9/10/11 三张表。
写回只动这三张表，其它 sheet 原样保留（用户可能加了自己的批注列）。

不直接生成 workflowDefinition XML：XML 里 activity Id、Transition Id、
字段权限矩阵是联动的一套，走 Excel 中转能复用 reader → validate →
gen_workflow 那条已经被 300 多条回归钉住的链路。
"""
from __future__ import annotations

import io
import json
import re
import tempfile
from pathlib import Path

from openpyxl import load_workbook

from . import ai_bo, template
from .model import (
    WF_BRANCH,
    WF_ONGOING,
    WF_PASSED,
    WF_REJECTED,
    WF_START,
    WfEdge,
    WfNode,
    Workflow,
)
from .nlq import llm
from .reader import read_workbook

MAX_OBJECTS = 8          # 上下文里最多列几个对象（流程驱动优先）
MAX_FIELDS = 40          # 每个对象最多列多少个字段
MAX_EXISTING = 6         # 最多把几条现有流程塞进上下文

_KIND_LABEL = {"开始": WF_START, "审批": WF_ONGOING, "判断": WF_BRANCH,
               "通过": WF_PASSED, "驳回": WF_REJECTED}
_HANDLER_LABEL = {"角色", "人员", "无"}
_OPS = {"等于", "不等于", "大于", "大于等于", "小于", "小于等于",
        "属于", "为空", "不为空"}

SYSTEM = """你是 PowerProject（安科 PowerProject V6）的审批流程设计助手。
根据用户的描述，输出**可以直接导入系统**的流程定义 JSON。

【输出格式】只输出一个 JSON 对象，不要解释、不要 markdown：
{"workflows":[{"name":"流程名","boCode":"对象编码","nodes":[
  {"name":"开始","kind":"开始"},
  {"name":"部门经理审批","kind":"审批","handlerType":"角色","handlerNames":["部门经理"],"days":2},
  {"name":"判断_金额","kind":"判断"},
  {"name":"通过","kind":"通过"}],
 "edges":[
  {"src":"开始","dst":"部门经理审批","name":"提交"},
  {"src":"部门经理审批","dst":"判断_金额","name":"同意"},
  {"src":"判断_金额","dst":"通过","name":"金额小于10万","conditionField":"amount","conditionOp":"小于","conditionValue":"100000","order":1}]}],
 "notes":"一句话说明你做了什么"}

【硬规则】
1. 每条流程**恰好一个「开始」节点**、**至少一个「通过」节点**；节点名称在流程内不能重复。
2. 「判断」节点：kind=判断，出边至少 2 条且 order 从 1 起递增；判断节点不填处理人、不带表单。
   判断节点的出边都要给条件（conditionField/conditionOp/conditionValue），
   其中至少一条要留空作为兜底（无条件）。
3. conditionField 必须是该对象**真实存在的字段 code**（如 amount、applyType），不能写中文名；
   conditionOp 只能是：等于 / 不等于 / 大于 / 大于等于 / 小于 / 小于等于 / 属于 / 为空 / 不为空。
4. 流程图必须连通：从「开始」能走到「通过」；每条边的 src/dst 都必须出现在 nodes 里。
5. 处理人只写**角色名或人名**（handlerNames），不要编造 ID，handlerType 填「角色」。
6. 流程只能挂在上文列出的**流程驱动对象**上，boCode 用给出的英文编码，不要发明。
7. 用户说「改」时，以现有流程为基线做最小改动；**没提到的流程不要删、原样保留**。
8. 节点数量控制在 3~12 个；没有分支就用直线（开始→审批→…→通过）。
"""


def _obj_brief(proj) -> str:
    """对象清单：流程驱动优先，字段名+编码+类型，够模型判断条件字段就行。"""
    bos = sorted(proj.bos, key=lambda b: not b.flow_driven)[:MAX_OBJECTS]
    if not bos:
        return "（空）"
    lines: list[str] = []
    for bo in bos:
        tag = "流程驱动" if bo.flow_driven else "普通"
        lines.append(f"- {bo.code} {bo.name}（{tag}）")
        if not bo.flow_driven:
            continue                      # 普通对象不给字段，省 token
        for f in sorted(bo.fields, key=lambda x: x.sn)[:MAX_FIELDS]:
            extra = ""
            if f.type_new == "dic" and f.choices:
                extra = " 选项:" + "/".join(f.choices[:8])
            elif f.type_new == "number":
                extra = " 数字"
            lines.append(f"    {f.code} {f.name}（{f.type_new}）{extra}")
    return "\n".join(lines)


def _existing_brief(proj) -> str:
    if not proj.workflows:
        return "（还没有流程）"
    lines: list[str] = []
    for wf in proj.workflows[:MAX_EXISTING]:
        lines.append(f"流程「{wf.name}」→ 对象 {wf.bo_code}")
        for n in wf.nodes:
            who = ""
            if n.kind == WF_ONGOING and n.handler_names:
                who = " 处理人:" + ",".join(n.handler_names)
            lines.append(f"  {n.id} {n.name} [{n.kind.replace('Ongoing', '审批')}"
                         f"{who}]")
        if wf.edges:
            lines.append("  流转：" + "；".join(
                f"{e.src}→{e.dst}({e.name or '同意'}"
                + (f"，{e.condition_field} {e.condition_op} {e.condition_value}"
                   if e.condition_field else "")
                + ")" for e in wf.edges))
    return "\n".join(lines)


def _normalize(raw: dict, bo_codes: set[str], notes: list[str]) -> list[Workflow]:
    """把模型给的 JSON 修成能过 reader/validate 的形状，修了什么记进 notes。"""
    out: list[Workflow] = []
    for item in (raw.get("workflows") or []):
        name = str(item.get("name") or "").strip()
        bo_code = str(item.get("boCode") or "").strip()
        if not name:
            notes.append("有一条流程没给名字，已跳过")
            continue
        if bo_code not in bo_codes:
            notes.append(f"流程「{name}」挂的对象 {bo_code or '(空)'} 不在本表里，已跳过")
            continue

        nodes: list[WfNode] = []
        seen: set[str] = set()
        for i, n in enumerate(item.get("nodes") or [], start=1):
            nm = str(n.get("name") or "").strip() or f"节点{i}"
            if nm in seen:
                nm = f"{nm}_{i}"
                notes.append(f"流程「{name}」有重名节点，已改成「{nm}」")
            seen.add(nm)
            kind_raw = str(n.get("kind") or "审批").strip()
            kind = _KIND_LABEL.get(kind_raw, WF_ONGOING)
            if kind_raw not in _KIND_LABEL:
                notes.append(f"流程「{name}」节点「{nm}」的类型「{kind_raw}」认不出，按审批处理")
            htype = str(n.get("handlerType") or "角色").strip()
            if htype not in _HANDLER_LABEL:
                htype = "角色"
            names = n.get("handlerNames") or []
            if isinstance(names, str):
                names = [x for x in re.split(r"[,，、]", names) if x.strip()]
            try:
                days = int(n.get("days") or 2)
            except (TypeError, ValueError):
                days = 2
            nodes.append(WfNode(
                wf=name, name=nm, kind=kind, id=i,
                handler_type=htype if kind == WF_ONGOING else "无",
                handler_names=[str(x).strip() for x in names][:6],
                expect_days=max(0, min(days, 365)),
            ))

        kinds = [n.kind for n in nodes]
        if WF_START not in kinds:
            nodes.insert(0, WfNode(wf=name, name="开始", kind=WF_START, id=0))
            notes.append(f"流程「{name}」没有开始节点，已补一个")
        if WF_PASSED not in kinds:
            nodes.append(WfNode(wf=name, name="通过", kind=WF_PASSED, id=0))
            notes.append(f"流程「{name}」没有结束节点，已补一个「通过」")
        # 判断节点只能有一个、且在开始之后：多了就把第二个降级成审批
        if sum(1 for n in nodes if n.kind == WF_START) > 1:
            seen_start = False
            for n in nodes:
                if n.kind != WF_START:
                    continue
                if seen_start:
                    n.kind = WF_ONGOING
                    notes.append(f"流程「{name}」有多个开始节点，多出来的改成了审批")
                seen_start = True
        for i, n in enumerate(nodes, start=1):
            n.id = i

        by_name = {n.name: n for n in nodes}
        edges: list[WfEdge] = []
        for e in (item.get("edges") or []):
            src = str(e.get("src") or "").strip()
            dst = str(e.get("dst") or "").strip()
            if src not in by_name or dst not in by_name:
                notes.append(f"流程「{name}」的流转 {src or '?'}→{dst or '?'} 指向不存在的节点，已丢弃")
                continue
            op = str(e.get("conditionOp") or "").strip()
            edges.append(WfEdge(
                wf=name, src=src, dst=dst,
                name=str(e.get("name") or "").strip(),
                condition_field=str(e.get("conditionField") or "").strip(),
                condition_op=op if op in _OPS else "",
                condition_value=str(e.get("conditionValue") or "").strip(),
                value_label=str(e.get("conditionLabel") or "").strip(),
                order=int(e.get("order") or 0) if str(e.get("order") or "0").isdigit() else 0,
            ))
        # 判断节点出边不足两条：补一条到「通过」的兜底边，否则系统里是死路
        for n in nodes:
            if n.kind != WF_BRANCH:
                continue
            outs = [e for e in edges if e.src == n.name]
            if len(outs) < 2:
                target = next((x.name for x in nodes if x.kind == WF_PASSED), "通过")
                edges.append(WfEdge(wf=name, src=n.name, dst=target,
                                    name="其他情况", order=len(outs) + 1))
                notes.append(f"流程「{name}」的判断节点「{n.name}」只有 {len(outs)} 条出边，已补一条兜底")

        out.append(Workflow(name=name, bo_code=bo_code, nodes=nodes, edges=edges))
    return out


def _merge(existing: list[Workflow], generated: list[Workflow],
           focus: str) -> tuple[list[Workflow], list[str]]:
    """同名替换；focus 那条被改了名也按对象替换；其余追加。不删除用户已有流程。"""
    notes: list[str] = []
    out = list(existing)
    focus_wf = next((w for w in out if w.name == focus), None) if focus else None

    for g in generated:
        idx = next((i for i, w in enumerate(out) if w.name == g.name), None)
        if idx is not None:
            out[idx] = g
            notes.append(f"已更新流程「{g.name}」")
            continue
        # 改名场景：AI 给的新名字，但对象还是当前在改的那条
        if focus_wf is not None and g.bo_code == focus_wf.bo_code:
            i = out.index(focus_wf)
            out[i] = g
            notes.append(f"流程「{focus_wf.name}」已改名为「{g.name}」")
            focus_wf = g
            continue
        out.append(g)
        notes.append(f"已新增流程「{g.name}」")
    return out, notes


def generate(data: bytes, instruction: str, focus: str = "",
             cfg: dict | None = None, timeout: int = 90) -> tuple[bytes, dict]:
    """一句话生成/修改流程 → 返回新的 xlsx 字节 + 摘要。"""
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("先说清楚流程要怎么走，例如：采购申请先部门经理审批，金额≥10万再加总经理")

    tmp = Path(tempfile.mkdtemp()) / "ai-wf.xlsx"
    tmp.write_bytes(data)
    try:
        proj, read_errors, _stats = read_workbook(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    hard = [e for e in read_errors if "流程" in e]
    if hard:
        raise ValueError("这份 Excel 的流程表有错，先修好再让 AI 改：\n" + "\n".join(hard[:6]))

    driven = [b for b in proj.bos if b.flow_driven]
    if not driven:
        raise ValueError("这份 Excel 里没有流程驱动对象：到 1_业务对象 把「流程驱动」改成「是」，"
                         "否则流程没有可挂的对象")

    user = (
        f"【当前工作簿里的业务对象】\n{_obj_brief(proj)}\n\n"
        f"【现有流程】\n{_existing_brief(proj)}\n\n"
        f"{('【正在修改的流程】' + focus + chr(10)) if focus else ''}"
        f"【用户要求】\n{instruction}\n\n"
        "请输出完整 JSON。"
    )
    text, used = llm.chat_any(
        ai_bo._fast_cfg(cfg or {}),
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": user}],
        timeout=timeout, max_tokens=8000,
    )
    try:
        raw = ai_bo._extract_json(text)
    except json.JSONDecodeError:
        # 截断就再来一次，预算翻倍并明说
        text2, used = llm.chat_any(
            ai_bo._fast_cfg(cfg or {}),
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": user + "\n\n上次输出不是合法 JSON，请重新输出完整 JSON，"
                                                  "节点多时把 name 写短，不要省略任何字段。"}],
            timeout=timeout, max_tokens=16000,
        )
        raw = ai_bo._extract_json(text2)

    notes: list[str] = []
    gen = _normalize(raw, {b.code for b in driven}, notes)
    if not gen:
        raise ValueError("AI 没有给出可用的流程（可能是描述里没提流程细节）：" +
                         (str(raw.get("notes")) if isinstance(raw, dict) else ""))

    merged, mnotes = _merge(proj.workflows, gen, focus)
    proj.workflows = merged
    notes.extend(mnotes)

    # 只重写流程三张表，其它 sheet（含用户自己的批注/格式）原样保留
    wb = load_workbook(io.BytesIO(data))
    template.write_workflows(wb, proj)
    buf = io.BytesIO()
    wb.save(buf)

    summary = {
        "ok": True,
        "workflows": [
            {"name": w.name, "boCode": w.bo_code, "nodeCount": len(w.nodes),
             "edgeCount": len(w.edges)}
            for w in gen
        ],
        "notes": notes,
        "aiNote": str(raw.get("notes") or "") if isinstance(raw, dict) else "",
        "model": used.get("model", ""),
    }
    return buf.getvalue(), summary
