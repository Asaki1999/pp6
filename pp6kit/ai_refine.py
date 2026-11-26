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
2. 没提到的字段：name / code / type / length / choices / **cascadeParent** /
   分组 values 全部原样照抄，一个字都不改。
   **字段级 cascadeParent（级联父字段编码）尤其不能丢**——current 里给了的，
   输出里必须原样带回，哪怕这次修改跟它毫无关系。
   候选值里出现「父值：子1、子2」的，整行原样保留，不要拍平。
   current 里带 ``link: true`` 的字段是系统强制加的「XX选择」（项目选择 /
   合同选择）：**整条原样保留**，不要删、不要改名、也不要改类型。
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
11. **改对象编码**：用户说「编码改成 xxx / 换个编码 / 不要叫这个」时，改 bo.code，
    并保持 name 不变。编码只能是小写字母+数字+下划线、字母开头
    （例：project_apply、contract_v2）。**不要**只改 name 就算改了编码——
    两者是独立字段，改哪个就输出哪个

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
        if f.get("choiceGroups"):
            # 级联分组必须以「父：子1、子2」的分组写法给模型看——
            # 它回传什么我们就收什么。扁平给回去，模型照抄扁平，
            # 分组就永久丢了（2026-11-12 品种分类级联失效的根因之一）。
            item["values"] = [f"{p}：{'、'.join(kids)}"
                              for p, kids in f["choiceGroups"].items()]
            item["cascadeParent"] = f.get("cascadeParent") or ""
        elif f.get("choices"):
            # 用 values 命名，和 build_bo 的读取端一致（提示词也让模型写 values），
            # 避免「当前结果里叫 choices、输出要求叫 values」让模型犯迷糊
            item["values"] = f["choices"]
        if f.get("objectType") and f.get("type") == "object":
            item["objectType"] = f["objectType"]
        # 字段级级联必须让模型看见，否则它「原样照抄」时根本抄不到，
        # 每微调一次级联声明就被抹一遍（2026-11-12 实测：改一次编码
        # 整包 cascadeEnabled 全消失）
        if f.get("cascadeParent") and "cascadeParent" not in item:
            item["cascadeParent"] = f["cascadeParent"]
        if f.get("readonly"):
            item["readonly"] = True
        if f.get("isMultiple"):
            item["isMultiple"] = True
        if f.get("isLink"):
            # 「XX选择」是强加字段：告诉模型这条别动（改名/删掉都会让用户以为
            # 我们改了口径）；真正的 is_link 标记由我们重建时重新打。
            item["link"] = True
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


def refine(payload: dict, cfg: dict, timeout: int = 150,
           link_target=None, link_decisions: dict | None = None) -> dict:
    """在当前结果上按指令改。返回和 ai_generate_bo 同构的 dict。

    link_target / link_decisions：本次关联的目标业务对象（由路由层从
    X-Project-Link 解析，缺省 None = 不关联）。为什么微调也要传：``build_bo`` 按
    模型的 JSON 重建对象，而 ``isLink`` 是我们的内部口径、不在提示词里，模型抄不
    回来——不重新注入的话，「改一句话」之后「项目选择」就掉档成普通关联字段
    （不置顶、值来源不是全字段），用户会以为我们偷偷改了规则。
    """
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
    # 输出是完整 JSON（要回传全部字段），max_tokens 必须按字段数算。
    # 2026-11-12 用户截图实测：48 个字段时 6000 token 断在字符串中间
    # （「Unterminated string starting at...」），指令越小越显得莫名其妙。
    # 每字段约 120~200 token（编码+名称+类型+候选值），按 240 留余量，
    # 另加 2000 推理余量（deepseek-flash 一次推理烧 1800+）。
    _n_fields = len((current.get("bo") or {}).get("fields") or [])
    _budget = max(8000, min(32000, 4000 + _n_fields * 240))
    _msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ]
    text, used = llm.chat_any(ai_bo._fast_cfg(cfg), _msgs,
                              timeout=timeout, max_tokens=_budget)
    try:
        spec = ai_bo._extract_json(text)
    except json.JSONDecodeError:
        # 截断时重试一次：预算翻倍，并明确要求输出完整 JSON；再失败才报错
        text2, used2 = llm.chat_any(ai_bo._fast_cfg(cfg), [
            *_msgs,
            {"role": "assistant", "content": text[-2000:]},
            {"role": "user", "content":
                f"你上一次的输出被截断了，不是合法 JSON。请重新输出**完整**的 JSON"
                f"（共 {_n_fields} 个字段，一个都不能少，note 留空、写得紧凑些），"
                f"并照常遵守修改要求：{instruction}"},
        ], timeout=timeout, max_tokens=min(32000, _budget * 2))
        text, used = text2, used2
        try:
            spec = ai_bo._extract_json(text)
        except json.JSONDecodeError as exc:
            raise llm.LlmError(
                f"模型输出两次都被截断（当前 {_n_fields} 个字段太长）。"
                f"请再发一次试试；还不行就先在预览里删几个不要的字段，改完再加回来"
            ) from exc

    # 关联对象归一必须用**完整对象清单**，不能信前端传的 objects：
    # 前端 refine 时传的是 state.data.objects（parse 出的本包对象，可能就 1 个），
    # 拿它当 pool 会让「关联项目」resolve 不到 project，再被 strict_objects
    # 降级成「选项」字段——纠错一次就把关联字段改坏了。
    from . import objects as _objects
    known = _objects.ensure()
    # prefer_code=True：微调时显式给的编码必须照用。
    # 默认逻辑是「name 优先重新推导编码」（防止 AI 把整段标题当对象名生成拼音长串），
    # 但这条规则在微调场景下会把用户刚改的编码悄悄改回原名推导值——用户看到的就是
    # 「AI 说改好了，页面还是旧编码」（2026-11-12 实测复现）。
    bo, notices = ai_bo.build_bo(spec.get("bo") or {}, known,
                                 strict_objects=True, prefer_code=True)

    # 编码撞库硬校验（2026-11-26 用户要求）：微调改成（或本来就带着）一个
    # 系统已占用的编码时，这次修改不应用，报错让人先换编码。
    # 报错时前端不会覆盖 state.aiData，之前的修改都还在，链路不会卡死。
    # （旧的「⚠ 系统里已有同名对象」notice 被这道闸覆盖：能走到它的编码
    #   都已在这里报错，不再有「只提示不拦」的中间态。）
    _hit = _objects.code_taken(bo.code)
    if _hit:
        raise ValueError(_objects.code_conflict_message(_hit))

    # 编码变更要显式说出来：它会连带改物理表名 pp_<code> 和字典 key 前缀，
    # 用户需要知道这不是「只改个显示名」。
    _old_code = str((payload.get("bo") or {}).get("code") or "").strip()
    _code_change = ""
    if _old_code and bo.code != _old_code:
        _code_change = f"对象编码 {_old_code} → {bo.code}（物理表名 {bo.tbl_name}）"
        notices.insert(0, _code_change)

    # 微调也可能把级联子字段的候选值改拍平了（模型重排 values 时常见），
    # 补一轮归属映射，别让级联在纠错后失效
    notices.extend(ai_bo.fix_cascade_groups(bo, cfg))
    # 微调时新增的表格字段同样可能只写类型不写列，按修改要求补出来
    notices.extend(ai_bo.fill_table_columns(bo, cfg, description=instruction))

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

    # 「XX选择」链接字段：模型抄不回 isLink（内部口径），只能重新注入。
    # 与 ai_generate_bo / ai_apply_bo 同一份实现（projlink.ensure_link_field）：
    # 已有指向目标对象的字段就复用、只补标记，绝不加第二个。
    link_field = None
    if link_target is not None:
        from . import projlink as _pl
        if _pl.decisions_link_object(link_decisions, bo.code):
            _old_link = next((f for f in (payload.get("bo") or {}).get("fields") or []
                              if f.get("isLink")), None)
            link_field, _created, _note = _pl.ensure_link_field(
                bo, link_target, name=str((_old_link or {}).get("name") or ""))
            notices.append(_note)

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

    # 表单控件的取值来源（字典 key / 对象编码）是派生数据，永远以当前对象为准。
    # 对象编码一改，字典 key 全变（pp_<编码>_<字段>）；异常回退旧布局、或模型
    # 原样照抄旧值时，控件里留的就是失效 key，导出校验直接中止
    # （2026-11-12 用户截图：innovationPipeline 等四个控件引用的字典不存在）。
    if form_payload:
        _src = {x.code: (x.dict_key() if x.type_new == "dic" else x.object_type)
                for x in bo.fields if x.type_new in ("dic", "object")}
        for ff in form_payload.get("fields") or []:
            _nm = str(ff.get("name") or "").strip()
            if _nm in _src and ff.get("type") in ("select", "dictionary"):
                ff["valueSource"] = _src[_nm]
        # 链接字段的控件必须还在第一行、且是 linkdata：模型重排分组时常常把它
        # 挪到中间或降级成普通下拉，那样「先选项目、再联动」的顺序就没了。
        if link_field is not None:
            from . import ai_form as _af
            _af.prepend_control(form_payload, link_field.code, link_field.name,
                                link_field.object_type or "")

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
    # 编码变更由我们自己算（不依赖模型是否把它写进 changes）。
    # 否则会出现「代码确实改了、但对话里一条变化都没列」——用户仍然以为没生效。
    if _code_change and _code_change not in changes:
        changes.insert(0, _code_change)

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
        # 与 ai_generate_bo 同结构：前端要能一直显示「已关联：合同」
        "linkTarget": (link_target.to_dict() if link_target is not None else None),
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
