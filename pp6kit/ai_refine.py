"""AI 对话式微调：在已有结果上按用户新要求改，而不是从头重做。

和 ai_bo.generate 的区别：
  - generate 只吃一句话，从零设计
  - refine  吃「当前结果 + 新指令」，只动该动的部分

为什么要单独做：用户看到预览后往往只是想调几处（加个字段、改必填、换个类型），
让模型从零重新生成既不保稳定，也会把已经调好的东西推翻。
"""
from __future__ import annotations

import json
import time

from . import ai_bo
from .model import HIDE_OPERATORS, OP_BY_LABEL, HideRule
from .nlq import llm

SYSTEM = """你在帮用户**修改**一个已经设计好的业务对象。

用户会给你两样东西：
1. 当前对象/表单/列表的完整 JSON（current）
2. 一条修改要求（instruction）

你的任务：**只改要求里提到的部分，其余原样保留**。

规则（必须遵守）：
1. 输出**完整的**新 JSON，结构和 current 一致（不要只输出 diff）
2. 没提到的字段：name / code / type / length / choices 全部原样照抄，一个字都不改
3. 没提到的表单分组、列表列：原样保留
4. 新增字段：code 用**语义化英文小驼峰**，type 只能从这 14 种里选：
   varchar / number / datetime / clob / user / department / attachment /
   i18n / dic / object / userGroupCate / table / autoNumber / formula

   ▸ 新增字段的 code 规范（与系统原生对象 project/task/contract 一致）：
   - 「名称」→ name；「编号」→ code；「描述」→ description/notes
   - 「关联某对象」→ 对象Id，多个用 对象Ids（所属项目→projectId、处理人→handlerIds）
   - 「是否XX」→ isXxx（是否里程碑→isMilestone）
   - 日期 → xxxDate；金额 → xxxAmount；20 字符以内，不含下划线
   - **禁止拼音**：不要 xiangMuMingCheng，要 projectName
   正例：MAH持有人→mahHolder、是否工艺技术类项目→isCraftTech、申请人电话→applicantPhone
   反例：shiFouGongYiJiShuLeiXiangMu（27字符）、shenQingRenDianHua（18字符）
5. 新增选项字段：把候选值写进 values 数组；objectType 一律留空
6. **改单选/多选**：用 isMultiple 表示（true=多选，false=单选）写在字段上。
   用户说「改成多选」就把该字段的 isMultiple 设为 true；说「改成单选」设 false。
   没提到的字段，isMultiple 原样照抄
7. 删字段：从 fields 里去掉，同时从 form.fields 和 list.columns 里也去掉
8. 改类型：如果改成 dic 但原来没有候选值，在 choices 里给几个合理的默认值
9. 用户说"精简/合并"时，优先删掉重复含义的，不要动系统字段（id/创建人/创建时间等）
10. **每个字段都必须带上 values 字段**（非选项字段给空数组 []），不要省略

输出格式（只输出 JSON，不要解释）：
{
  "bo": {"code": "...", "name": "...", "fields": [...]},
  "form": {"key": "...", "name": "...", "fields": [...]},
  "list": {"key": "...", "name": "...", "columns": [...], "views": [...]},
  "changes": ["改了什么，一句一条，给用户看"]
}
"""


def _compact(payload: dict) -> dict:
    """把当前结果压成模型好看的形式，去掉预览用的冗余字段。"""
    bo = payload.get("bo") or {}
    fields = []
    for f in bo.get("fields") or []:
        item = {
            "code": f.get("code", ""),
            "name": f.get("name", ""),
            "type": f.get("type", ""),
        }
        if f.get("length"):
            item["length"] = f["length"]
        if f.get("choices"):
            # 用 values 命名，和 build_bo 的读取端一致（提示词也让模型写 values），
            # 避免「当前结果里叫 choices、输出要求叫 values」让模型犯迷糊
            item["values"] = f["choices"]
        if f.get("objectType") and f.get("type") == "object":
            item["objectType"] = f["objectType"]
        if f.get("readonly"):
            item["readonly"] = True
        if f.get("isMultiple"):
            item["isMultiple"] = True
        fields.append(item)

    out = {
        "bo": {"code": bo.get("code", ""), "name": bo.get("name", ""),
               "desc": bo.get("desc", ""), "fields": fields},
    }

    form = payload.get("form") or {}
    if form.get("fields"):
        out["form"] = {
            "key": form.get("key", ""),
            "name": form.get("name", ""),
            # 表单只需要「哪些字段、分在哪个组」，控件类型由字段类型决定
            "fields": [
                {"name": f.get("name", ""), "label": f.get("label", ""),
                 "type": f.get("type", ""), "group": f.get("group") or "",
                 "required": bool(f.get("required")),
                 "fullRow": bool(f.get("fullRow")),
                 # 级联/隐藏一起给模型看，否则它改表单时会把这两样漏掉
                 "cascadeParent": f.get("cascadeParent") or ""}
                for f in form["fields"]
            ],
            "hideRules": form.get("hideRules") or [],
        }

    lst = payload.get("list") or {}
    if lst.get("columns"):
        out["list"] = {
            "key": lst.get("key", ""),
            "name": lst.get("name", ""),
            "views": lst.get("views") or ["grid"],
            "columns": [{"code": c.get("code", ""), "label": c.get("label", "")}
                        for c in lst["columns"]],
        }
    return out


def refine(payload: dict, cfg: dict, timeout: int = 150) -> dict:
    """在当前结果上按指令改。返回和 ai_generate_bo 同构的 dict。"""
    instruction = " ".join((payload.get("instruction") or "").split())
    if not instruction:
        raise ValueError("说说要改哪里")
    if len(instruction) > 500:
        instruction = instruction[:500]

    current = _compact(payload)
    if not (current.get("bo") or {}).get("fields"):
        raise ValueError("还没有可修改的内容，先让它生成一版")

    t0 = time.time()
    user = (
        "===== 当前结果 =====\n"
        + json.dumps(current, ensure_ascii=False, indent=1)
        + "\n\n===== 修改要求 =====\n"
        + instruction
    )
    # 输出是完整 JSON，字段多时容易被 token 截断，给足额度
    text, used = llm.chat_any(
        ai_bo._fast_cfg(cfg),
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": user}],
        timeout=timeout, max_tokens=6000,
    )
    spec = ai_bo._extract_json(text)

    # 关联对象归一必须用**完整对象清单**，不能信前端传的 objects：
    # 前端 refine 时传的是 state.data.objects（parse 出的本包对象，可能就 1 个），
    # 拿它当 pool 会让「关联项目」resolve 不到 project，再被 strict_objects
    # 降级成「选项」字段——纠错一次就把关联字段改坏了。
    from . import objects as _objects
    known = _objects.ensure()
    bo, notices = ai_bo.build_bo(spec.get("bo") or {}, known, strict_objects=True)

    # 兜底：模型经常「改类型时把候选值弄丢」，或者干脆不回传 choices。
    # 按字段编码把旧候选值补回去，只有模型明确给了新值才覆盖。
    old_by_code = {f.get("code"): f for f in (payload.get("bo") or {}).get("fields") or []}
    new_by_code = {str(f.get("code") or "").strip(): f
                   for f in (spec.get("bo") or {}).get("fields") or []}
    for f in bo.fields:
        old = old_by_code.get(f.code)
        raw = new_by_code.get(f.code) or {}
        if f.type_new == "dic":
            if not f.choices and old and old.get("choices"):
                f.choices = list(old["choices"])
            # 多选：模型明确给了就用模型的，没给才沿用旧值
            want = raw.get("isMultiple")
            if want is None:
                want = bool(old.get("isMultiple")) if old else False
            f.options["isMultiple"] = ai_bo.truthy(want)
        # 长度：模型没给就沿用旧值，别因为回传丢失变成默认 200
        if f.length is None and old and old.get("length"):
            f.length = old["length"]

    # 表单：沿用原来的 key/name，只更新字段列表
    form_payload = None
    old_form = payload.get("form") or {}
    spec_form = spec.get("form") or {}
    if old_form.get("fields") or spec_form.get("fields"):
        try:
            from . import ai_form
            f_spec = {
                "groups": _regroup(spec_form, bo),
                "required": [f.get("name") for f in (spec_form.get("fields") or [])
                             if f.get("required")],
                "fullRow": [f.get("name") for f in (spec_form.get("fields") or [])
                            if f.get("fullRow")],
            }
            form, f_notes = ai_form.build_form(
                f_spec, bo,
                old_form.get("key") or f"create_{bo.code}",
                name=old_form.get("name") or f"新建{bo.name}",
            )
            _carry_over_logic(form, old_form)
            form_payload = ai_form.to_dict(form)
            notices.extend(f_notes)
        except Exception as exc:
            notices.append(f"表单没能同步更新（{str(exc)[:60]}），沿用上一版布局")

    # 列表：沿用原 key，列按新字段重算（被删掉的列要丢掉）
    list_payload = None
    old_list = payload.get("list") or {}
    if old_list.get("columns"):
        spec_list = spec.get("list") or {}
        try:
            from . import ai_list
            by_label = {f.name: f for f in bo.fields}
            by_code = {f.code: f for f in bo.fields}
            cols = []
            for c in (spec_list.get("columns") or []):
                key = str(c.get("code") or c.get("label") or "").strip()
                hit = by_code.get(key) or by_label.get(key)
                if hit is not None:
                    cols.append(hit.code)
            if not cols:      # 模型没给列就用全部字段
                cols = [f.code for f in bo.fields]
            # build_list 返回 (ListDef, notices)，不是单个对象
            lst, l_notes = ai_list.build_list(
                {"key": old_list.get("key") or f"{bo.code}_list",
                 "name": old_list.get("name") or f"{bo.name}列表",
                 "columns": cols,
                 "views": spec_list.get("views") or old_list.get("views") or ["grid"]},
                bo,
            )
            notices.extend(l_notes)
            list_payload = ai_list.to_dict(lst, bo)
        except Exception as exc:
            notices.append(f"列表没能同步更新（{str(exc)[:60]}），沿用上一版")
            list_payload = old_list

    changes = [str(x) for x in (spec.get("changes") or []) if str(x).strip()][:8]

    # 关系识别：纠错后字段可能变了（加了关联字段/删了字段），重新出建议。
    # 和 ai_generate_bo / ai_from_sheet 同构，保证前端预览页三块（对象下拉、
    # 对象库提示、关系确认区）在 refine 后依然可用。
    from . import relations as _relations
    from .model import Project as _Project
    _rel_proj = _Project()
    _rel_proj.bos.append(bo)
    try:
        relationships = _relations.analyze(_rel_proj)
    except Exception:
        relationships = {"status": "unavailable", "notice": "关系识别失败",
                         "parentOptions": [], "items": []}

    return {
        "ok": True,
        "bo": ai_bo.to_dict(bo),
        "form": form_payload,
        "list": list_payload,
        "notices": notices,
        "changes": changes,
        "types": ai_bo.type_choices(),
        "objects": _objects.ensure(),
        "objectSource": _objects.status(),
        "existObject": _objects.lookup(bo.name),
        "relationships": relationships,
        "model": used.get("model", ""),
        "seconds": round(time.time() - t0, 2),
        "instruction": instruction,
    }


def _regroup(form_spec: dict, bo) -> list[dict]:
    """把扁平的表单字段按 group 重新聚合成分组，供 build_form 用。"""
    order: list[str] = []
    bucket: dict[str, list[str]] = {}
    by_label = {f.name: f.code for f in bo.fields}
    by_code = {f.code: f.code for f in bo.fields}

    for f in form_spec.get("fields") or []:
        key = str(f.get("name") or f.get("label") or "").strip()
        code = by_code.get(key) or by_label.get(key)
        if not code:
            continue
        group = (f.get("group") or "").strip() or "基本信息"
        if group not in bucket:
            bucket[group] = []
            order.append(group)
        if code not in bucket[group]:
            bucket[group].append(code)

    if not order:      # 模型没给表单字段，就按对象字段一次性给
        return [{"title": "基本信息", "fields": [f.code for f in bo.fields]}]
    return [{"title": t, "fields": bucket[t]} for t in order]


def _carry_over_logic(form, old_form: dict) -> None:
    """微调是从零重建表单，级联和条件隐藏按「字段还在就继承」处理。

    这两样在微调提示词里没有对应表达（用户不会说「把 MAH 持有人的隐藏条件
    改成不等于其他」），让模型重写只会丢；不如原样继承，字段被删掉的自然失效。
    """
    parents = {f.get("name"): f.get("cascadeParent")
               for f in (old_form.get("fields") or []) if f.get("cascadeParent")}
    names = {f.name for f in form.fields}
    for ff in form.fields:
        if ff.name in parents and parents[ff.name] in names:
            ff.cascade_parent = parents[ff.name]

    for raw in (old_form.get("hideRules") or []):
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("target") or "").strip()
        source = str(raw.get("source") or "").strip()
        if target not in names or source not in names:
            continue
        op = OP_BY_LABEL.get(str(raw.get("operator") or "等于"), str(raw.get("operator") or "equal"))
        if op not in HIDE_OPERATORS:
            continue
        form.hide_rules.append(HideRule(
            form_key=form.key, target=target, source=source, operator=op,
            value=str(raw.get("value") or "").strip(),
            value_label=str(raw.get("valueLabel") or "").strip(),
        ))
