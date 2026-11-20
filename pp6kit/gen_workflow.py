"""流程定义（workflowDefinition）生成器：9_流程 / 10_流程节点 / 11_流程流转 → 一份裸 XML。

## 产物形态

流程定义**不进同步包**：它是裸 XML，导入位置是 管理 › 流程设计 › 导入，
所以 builder 侧按 ``modules.Module.zipped = False`` 分流成单文件交付。

## 格式基准（逐字对齐，不凭记忆）

    PP6/流程/项目立项_20261113145550.xml   的 **v30** 那一版
    PP6/流程/config-sync-20261113150255.zip  配套的 bo + 2 张 form

v30 = ``DefaultFlowChartVersion="30"``，是用户线上正在跑的那张 13 节点图；
v1–v10 只有 5 节点旧图，仅用来判断「某个键是必需还是可选」。

七段结构：``id / name / moduleId / categoryId / businessType / workflowType /
titleFormat`` → ``designer/FlowDefinition/FlowCharts/FlowChart``（活动与流转）→
``layout``（画布坐标）→ ``nodePermissions``（节点字段权限）。

## 与样本的三处已知偏离（都是「无法复现」而不是「写错了」）

1. ``nodePermissions`` **不含 fieldset/card 容器项**，``column`` 恒 ``[]``。
   容器名是 ``gen_form`` 构建期 ``short_id()`` 随机生成的（``fieldset_abc1234``），
   而 ``short_id()`` 是雪花 id（时间驱动）——QA 实测**同一份 Excel 连续两次构建，
   表单 XML 本身就不一致**，所以流程侧无论如何都对不上那张表单里的容器名。
   明细表列的权限来自列表模板，同理。
   若真机导入后发现审批节点的分组标题异常 / 明细表列不可见，改动点只有一个：
   让 ``gen_form`` 的容器短 id 可注入（按 form_key + 标签 md5），再把这两类项加回来。
2. 只出**一版** ``FlowChart Version="1"``（样本的 30 版是编辑器历史，不是格式要求）。
3. ``activity Id`` 用连续的 ``1..N``、``Transition Id`` 用独立的 ``1..M``。
   样本 v30 的 activity Id 不连续（缺 5，删过节点）、Transition Id 也是 1..18 跳号，
   说明系统不要求连号；我们出自己的连续序列，diff 时更好读。

## 防御性

``builder.build()`` 可以被直接调用（测试里就是这么用的），所以本模块**不假设
validate 已经跑过**：未知节点名、悬空流转、缺开始/缺通过节点一律
「跳过 + notice」，绝不抛 KeyError / StopIteration —— 抛了整包就出不来。
"""
from __future__ import annotations

import hashlib
import uuid

from . import gen_bo
from . import links as _links
from .gen_form import Snippets, node_field_meta
from .model import (
    WF_BRANCH,
    WF_COLOR,
    WF_COND_LABEL,
    WF_EMPTY_OPS,
    WF_ONGOING,
    WF_PASSED,
    WF_REJECTED,
    WF_START,
    WF_STATE_BY_KIND,
    PROCESS_STATE_CODES,
    WF_STATE_OPTIONS,
    Bo,
    Form,
    Project,
    WfNode,
    Workflow,
)
from .xmlw import el, esc_attr, json_attr, wrap

# 业务状态落在对象上的字段编码（builder.ensure_workflow_fields 补的就是它）
PROCESS_STATE_FIELD = "processState"

# 画布配色兜底值：Ongoing 的默认色（WF_COLOR 里故意没有 Start，见 model 注释）
_DEFAULT_COLOR = "1"

# 不写进 nodePermissions 的容器控件。
# 注意与 validate.CONTAINER_TYPES 不同：那里把 label/attachment 也算容器，
# 而这里是「表单上的布局容器」，tableField（明细表的列）属于叶子控件要保留。
_SKIP_TYPES = {"fieldset", "card", "grid", "grid_layout", "tabs", "tablelayout"}

# nodePermissions 里三项固定权限（样本 v30 各节点完全一致）
_READ_ALL = [{"type": "all", "subjectAssignments": [{"value": "allUsers"}]}]
_EDIT_START = [{"type": "BpmRole", "rightsId": "NodeTodoHandler,BpmCreator",
                "rightsName": "节点待办人,流程发起人"}]
_EDIT_NONE = [{"type": "none"}]
_PERM_NONE = [{"type": "none"}]

# 人工挑下一处理人时的 NextHandler 写法（样本 node3 / node13 出边）
_NEXT_HANDLER_PICK = ("None;SelectMode=Referenced;"
                      "AllowSelectWhenNextHandlersEmpty=false;AllowMultiple=true")
# 处理人类型填了角色/人员但既没名字也没 id：提交时人工挑（设计 §3.4）
_HANDLER_EMPTY = ("None;SelectMode=Referenced;"
                  "AllowSelectWhenNextHandlersEmpty=false;AllowMultiple=true")

# 画布间距（设计 §3.5：层间 220 / 行间 90）
_LAYER_GAP = 220
_ROW_GAP = 90


# ---------------------------------------------------------------- 公开入口

def wf_id(wf: Workflow, bo: Bo | None) -> str:
    """流程定义的 id。

    系统里流程 id 是 **UUID**（样本 8eea2a6d-f67a-4da8-8d6d-b2f13c21fd22），
    而业务对象是 19 位雪花号——两种 id 不能混用，所以不能拿 bo 的 id 顶。

    用 uuid5 而不是 uuid4：同一份 Excel 重复出包必须同 id，
    否则第二次导入会「新增一份流程」而不是更新原来那份。
    """
    exist = (wf.exist_id or "").strip()
    if exist:
        return exist
    code = (bo.code if bo is not None else wf.bo_code) or ""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pp6kit/workflow/{code}/{wf.name}"))


def state_codes(bo: Bo) -> dict[str, str]:
    """「业务状态名 → processState 字典短码」。

    优先走 ``gen_bo.dict_choice_codes``（对象里有 processState 字段时，那条路
    与 bo XML 的字典块同源，天然一致）。**只选流程模块时对象不出包、字段也没补**，
    那张表是空的，所以退到同一套 md5 算法自己算：
    scope = ``{对象编码}_processState``（= BoField.dict_key()）、sn 从 1 起、
    used 从空集开始 —— 与 ``_dict_blocks`` 对该字段的算法逐字相同，
    因此两种情形算出的短码必然一样。
    """
    if bo is None or not bo.code:
        return {}
    found = gen_bo.dict_choice_codes(bo, PROCESS_STATE_FIELD)
    if found:
        return found
    # 对象没有 processState 字段时（只出流程模块，字段没补），
    # 直接用平台的系统常量——这两个值必须是同一组，不能各算各的。
    # 见 model.PROCESS_STATE_CODES（2026-11-13 在 8112 真实库核对过）。
    return dict(PROCESS_STATE_CODES)


def render(wf: Workflow, bo: Bo, project: Project, snip: Snippets,
           wf_id: str) -> tuple[str, list[str]]:
    """返回 (XML 文本, 提示列表)。XML 为空串表示「这条流程生成不了」，由调用方跳过。"""
    notices: list[str] = []

    def note(text: str) -> None:
        notices.append(f"流程 {wf.name}: {text}")

    # 节点权限里的控件快照必须和表单产物逐键一致——关联对象的那几个键
    # （linkdata/link_linkage/link_condition）要靠同一份计划才对齐。
    link_plan = _links.LinkPlan(project)

    nodes = _ordered_nodes(wf, note)
    if not nodes:
        note("没有可用的节点（10_流程节点 是空的，或节点重复），已跳过这条流程")
        return "", notices

    kinds = {n.kind for n in nodes}
    if WF_START not in kinds:
        note("没有「开始」节点，导入后无法发起；已按现有节点生成，请补一个开始节点")
    if WF_PASSED not in kinds and WF_REJECTED not in kinds:
        note("没有「通过」或「驳回」节点，流程跑不到终点；已按现有节点生成")

    ordered = _resolve_edges(wf, nodes, note)
    tid_of = _assign_transition_ids(nodes, ordered)
    pairs = [(e["src"].name, e["dst"].name) for e in ordered]
    levels, has_cycle = _layer(nodes, pairs)
    if has_cycle:
        note("流程里有回到上游的流转（回边），画布分层可能不紧凑，"
             "导入后可在流程设计器里微调位置")

    # 节点用的表单：FormKey 与 nodePermissions 都靠它，一次算好避免两边不一致
    form_of: dict[str, Form | None] = {}
    for node in nodes:
        # 判断节点没有 Properties/FormKey，也不进 nodePermissions——不必为它推导表单
        form_of[node.name] = (None if node.kind == WF_BRANCH
                              else _pick_form(node, bo, project, note))

    states = state_codes(bo)
    if not states:
        note("对象没有可用的「流程状态」字典，节点的 BusinessState 会留空"
             "（导入后流程状态不显示，但流程能跑）")

    activities: list[str] = []
    for node in nodes:
        outs = [e for e in ordered if e["src"].name == node.name]
        activities.append(_activity_xml(
            node, outs, nodes, pairs, tid_of, states, form_of.get(node.name),
            bo, wf, note))

    chart = wrap("FlowChart", activities, {"IsPublished": "true", "Version": "1"}, indent=4)
    flow_def = wrap(
        "FlowDefinition",
        [
            wrap("FlowCharts", [chart], indent=3),
            el("Interceptors", "", None, indent=3),
        ],
        {
            "DefaultFlowChartVersion": "1",
            "Id": wf_id,
            "IsActive": "true" if wf.is_active else "false",
            "Name": wf.name,
            # 这三个是退回策略，样本 v30 固定这组值（退回给自己、可退回给全部处理人）
            "ParallelGoBackMode": "Self",
            "SendBackHandlerMode": "AllHandlers",
            "SendBackMode": "Allowed",
        },
        indent=2,
    )

    module_id = (wf.module_id or bo.module_id or project.module_id or "").strip()
    if not module_id:
        note("未指定模块ID（moduleId 为空），导入后流程可能挂不到任何模块下")
    category_id = (wf.category_id or project.wf_category_id or "").strip()
    if not category_id:
        note("未指定流程分类ID（categoryId 为空），导入后流程会落在「未分类」")

    body = [
        "<workflowDefinition>",
        el("id", "", esc_attr(wf_id), indent=1),
        el("name", "", esc_attr(wf.name), indent=1),
        el("moduleId", "", esc_attr(module_id), indent=1),
        el("categoryId", "", esc_attr(category_id), indent=1),
        el("businessType", "", esc_attr(bo.code), indent=1),
        el("workflowType", "", esc_attr(wf.workflow_type or "normal"), indent=1),
        el("titleFormat", "", esc_attr(
            wf.title_format or "${name}-${bo.name}-${bo.createBy}-${bo.createTime}"), indent=1),
        wrap("designer", [flow_def], indent=1),
        el("layout", "", _esc_text(_layout_json(nodes, ordered, tid_of, levels)), indent=1),
        el("nodePermissions", "",
           _esc_text(_permissions_json(nodes, bo, snip, form_of, note, link_plan)), indent=1),
        "</workflowDefinition>",
    ]
    return "\n".join(body) + "\n", notices


# ---------------------------------------------------------------- 节点与流转

def _ordered_nodes(wf: Workflow, note) -> list[WfNode]:
    """按行序取有效节点，并把 activity Id 定成 1..N（判断节点也占号）。

    样本 v30 的 activity Id 是 1,2,3,4,6,7…（缺 5，删过节点）——系统不要求连号。
    我们出自己的连续序列：用户填了「节点序号」就尊重它（流转表按序号引用），
    留空/重复才按行序补号。
    """
    out: list[WfNode] = []
    seen: set[str] = set()
    used_ids: set[int] = set()
    next_id = 1
    for raw in wf.nodes:
        name = (raw.name or "").strip()
        if not name:
            note("有一个节点没填名称，已跳过")
            continue
        if name in seen:
            note(f"节点名称「{name}」重复，后一条已跳过"
                 f"（流转按名字找节点，重名会连错线）")
            continue
        seen.add(name)
        nid = raw.id if raw.id and raw.id > 0 else 0
        if nid and nid in used_ids:
            note(f"节点「{name}」的序号 {nid} 已被占用，已改按行序重新编号")
            nid = 0
        if not nid:
            nid = next_id
            while nid in used_ids:
                nid += 1
        used_ids.add(nid)
        next_id = max(next_id, nid + 1)
        raw.id = nid
        out.append(raw)
    return out


def _resolve_edges(wf: Workflow, nodes: list[WfNode], note) -> list[dict]:
    """把流转表整理成「一条出边 = 一个 dict」，含多条件的合并。

    三件事：
      1. 悬空流转（起点/终点不存在）跳过并提示；
      2. 流转表整表为空 → 按节点行序自动串成直线，**跳过判断节点**（设计 §1.3）；
      3. 同 src 同 dst 的多行合并成一条出边的多个 rules（AND），
         这样 Excel 不用加「条件组」列。
    """
    by_name = {n.name: n for n in nodes}
    groups: dict[tuple[str, str], dict] = {}
    order_seen: dict[tuple[str, str], int] = {}
    for edge in wf.edges:
        src = (edge.src or "").strip()
        dst = (edge.dst or "").strip()
        if src not in by_name or dst not in by_name:
            missing = src if src not in by_name else dst
            note(f"流转「{src or '?'} → {dst or '?'}」引用了不存在的节点「{missing}」，已跳过")
            continue
        key = (src, dst)
        rule = (edge.condition_field or "", edge.condition_op or "",
                edge.condition_value or "", edge.value_label or "")
        if key in groups:
            # 多行同边：合并条件（AND）。没条件的行不重复加，否则 Condition 里会出现空规则
            if rule[0]:
                groups[key]["rules"].append(rule)
            else:
                groups[key]["blank"] += 1
            order_seen[key] = order_seen.get(key, 0) + 1
            continue
        groups[key] = {
            "src": by_name[src],
            "dst": by_name[dst],
            "name": (edge.name or "").strip(),
            "order": int(edge.order or 0),
            "inform": edge.inform,
            "rules": [rule] if rule[0] else [],
            "blank": 0 if rule[0] else 1,
        }
        order_seen[key] = 1

    out = list(groups.values())
    for key, count in order_seen.items():
        if count > 1 and not groups[key]["rules"]:
            note(f"流转「{key[0]} → {key[1]}」有 {count} 行且都没有条件，已合并成一条出边")
        elif groups[key]["rules"] and groups[key]["blank"]:
            # 同一对节点既有条件行又有无条件行：按条件合并后，那行无条件等于没写。
            # 「条件不成立时也要能走」的兜底应该连到**另一个**节点，否则流程会卡死。
            note(f"流转「{key[0]} → {key[1]}」既有条件行又有无条件行，已合并成带条件的出边"
                 f"（无条件那行被忽略；要兜底请把它连到别的节点）")

    if not out:
        chain = [n for n in nodes if n.kind != WF_BRANCH]
        skipped = [n.name for n in nodes if n.kind == WF_BRANCH]
        for a, b in zip(chain, chain[1:]):
            out.append({"src": a, "dst": b, "name": "", "order": 0,
                        "inform": None, "rules": []})
        if out:
            why = ("11_流程流转 是空的" if not wf.edges
                   else "11_流程流转 里没有一条能连上的流转")
            note(why + "，已按节点行序自动串成一条直线（"
                 + " → ".join(n.name for n in chain) + "）")
            if skipped:
                note("自动连线时跳过了判断节点：" + "、".join(skipped)
                     + "（判断节点要靠条件流转，请在 11_流程流转 里手工连）")

    # 流转按钮文案：起点是开始节点 → 提交，其余 → 同意
    for e in out:
        if not e["name"]:
            e["name"] = "提交" if e["src"].kind == WF_START else "同意"

    # Order：判断节点的出边才 1..N；普通节点恒 0（更正设计：样本 v30 的
    # T1/T2/T6/T7/T10/T13/T14/T15/T18 全是 Order="0"）
    counters: dict[str, int] = {}
    for e in out:
        if e["src"].kind != WF_BRANCH:
            e["order"] = 0
            continue
        if e["order"] > 0:
            counters[e["src"].name] = max(counters.get(e["src"].name, 0), e["order"])
        else:
            counters[e["src"].name] = counters.get(e["src"].name, 0) + 1
            e["order"] = counters[e["src"].name]

    # 同一判断节点内 Order 不能重复（系统靠它排出边按钮顺序）。
    # ⚠ 只能查判断节点：普通节点的 Order 上面已被强制成 0（更正 #3），
    # 只要有 2 条出边就恒有 dup={0}，会对着「审批」节点报「判断节点顺序重复」，
    # 而且用户去填 1、2 也会被改回 0 —— 那条建议永远无法满足。
    for node in nodes:
        if node.kind != WF_BRANCH:
            continue
        orders = [e["order"] for e in out if e["src"].name == node.name]
        dup = {o for o in orders if orders.count(o) > 1}
        if dup:
            note(f"判断节点「{node.name}」的出边顺序重复：{sorted(dup)}，"
                 f"请在「顺序」列改成 1、2…")

    return out


def _assign_transition_ids(nodes: list[WfNode], edges: list[dict]) -> dict[int, int]:
    """Transition Id 是**独立序列**（与 activity Id 不同源），allocated 按
    「源节点顺序 → 出边 Order」走，保证同一份 Excel 每次出包编号一致。

    返回 {id(edge dict) -> transition Id}；用 id() 做键是因为 dict 不可哈希且
    同一对 src/dst 可能有多条出边（不同 Order）。
    """
    seq = 1
    out: dict[int, int] = {}
    pos = {id(e): i for i, e in enumerate(edges)}      # 保持行序，用于稳定排序
    for node in nodes:
        outs = [e for e in edges if e["src"].name == node.name]
        outs.sort(key=lambda e: (e["order"], pos[id(e)]))
        for e in outs:
            out[id(e)] = seq
            seq += 1
    return out


def _layer(nodes: list[WfNode], pairs: list[tuple[str, str]]) -> tuple[dict[str, int], bool]:
    """拓扑分层：``level[dst] = max(level[dst], level[src] + 1)``。

    松弛轮数上限 = 节点数：有回边时能保证退出来（否则会把自己绕死）。
    返回 (levels, 是否没收敛)；没收敛说明存在回边，画布可能挤在一起。
    """
    level = {n.name: 0 for n in nodes}
    rounds = max(len(nodes), 1)
    for _ in range(rounds):
        changed = False
        for a, b in pairs:
            if a == b:           # 自环直接忽略，否则每轮都会把自己顶上一层
                continue
            if level.get(b, 0) < level.get(a, 0) + 1:
                level[b] = level[a] + 1
                changed = True
        if not changed:
            return level, False
    return level, True


# ---------------------------------------------------------------- 处理人 / 条件

def _handler_chain(node: WfNode, note) -> str:
    """处理人字符串（样本 v30 的紧凑 JSON + 中文原样）。

    ``handler_names`` 与 ``handler_ids`` 长度不一致时用 id 顶上名字：
    名字只影响界面显示，id 才是待办真正派的谁，宁可有名字缺失也不能错位。
    """
    ids = [x for x in (node.handler_ids or []) if x]
    names = [x for x in (node.handler_names or []) if x]
    if not ids:
        if node.handler_type in ("角色", "人员"):
            note(f"节点「{node.name}」没填处理人，已出成「提交时人工选择下一处理人」")
        return _HANDLER_EMPTY
    if len(names) != len(ids):
        if len(names) < len(ids):
            names = names + ids[len(names):]
        else:
            names = names[:len(ids)]
    head = {
        "label": "",
        "texts": names,
        "ids": ids,
        "type": "RoleSelection" if node.handler_type == "角色" else "UserSelection",
    }
    return ("Chain:[" + json_attr({"header": head, "nexts": []}) + "]"
            ";SelectMode=Referenced;AllowSelectWhenNextHandlersEmpty=false"
            ";AllowSkipWhenNextHandlersEmpty=false"
            ";AllowMultiple=" + ("true" if len(ids) > 1 else "false"))


def _rule_value(bf, op: str, raw: str, label: str, bo: Bo, note, node_name: str):
    """把「条件值」换成系统认的形态，返回 (value, value_label)。

    - 选项字段：中文选项名 → 字典短码（gen_bo.dict_choice_codes 同源换算）
    - 数字字段：``"300"`` → ``300``（样本是数字不是字符串）
    - 属于/不为空：属于 → 数组；为空/不为空 → None
    """
    text = (raw or "").strip()
    if op in WF_EMPTY_OPS:
        return None, (label or "")
    if op == "in":
        items = [x.strip() for x in text.replace("，", ",").split(",") if x.strip()]
        if bf.type_new == "dic":
            codes = gen_bo.dict_choice_codes(bo, bf.code)
            return [codes.get(x, x) for x in items], (label or text)
        return items, (label or text)
    if bf.type_new == "number":
        try:
            number = float(text)
            return (int(number) if number.is_integer() else number), (label or text)
        except (TypeError, ValueError):
            note(f"节点「{node_name}」的条件值「{text}」不是数字，已按字符串写入，"
                 f"导入后条件可能不成立")
            return text, (label or text)
    if bf.type_new == "dic":
        codes = gen_bo.dict_choice_codes(bo, bf.code)
        if text and text not in codes:
            note(f"节点「{node_name}」的条件值「{text}」不在字段「{bf.name}」的选项里，"
                 f"已原样写入（导入后条件可能永远不成立）")
        return codes.get(text, text), (label or text)
    return text, (label or text)


def _rule_field_snapshot(bf) -> tuple[str, str, str, dict, str | None]:
    """条件规则的字段快照：(type, input, field_data_type, field_options, field_object_type)。

    键集逐字对齐样本 v30 node13 的出边（``projectTotalBudgetTaxIncluded`` 那条）。
    选项字段的 field_options 里 objectType / dictionary 都等于字典 key；
    数字字段是同一份去掉这两个键。
    """
    base = {"isMultiple": False, "isDefault": False, "uniqueCheck": False,
            "optionSource": "BO_ATTR_DIC_TYPE", "columns": [], "identities": [],
            "format": None}
    if bf.type_new == "dic":
        key = bf.dict_key()
        options = dict(base)
        options["objectType"] = key
        options["dictionary"] = key
        return "string", "dictionary", "dic", options, key
    if bf.type_new == "object":
        ot = (bf.object_type or "").strip()
        options = dict(base)
        options["objectType"] = ot
        return "string", "linkdata", "object", options, (ot or None)
    if bf.type_new == "number":
        return "number", "number", "number", dict(base), None
    # 其余类型样本里没有实例（无证据实现）：按字符串处理并让调用方提示核对
    return "string", "text", (bf.type_new or "varchar"), dict(base), None


def _rules_of(edge: dict, bo: Bo, wf: Workflow, note) -> list[dict]:
    """一条出边的条件规则列表（同 src 同 dst 的多行 = 多条 rule，条件为 AND）。"""
    rules: list[dict] = []
    for field_raw, op_raw, value_raw, value_label in edge["rules"]:
        field_code = (field_raw or "").strip()
        bf = bo.field_by_code(field_code) if bo is not None else None
        if bf is None:
            note(f"流转「{edge['src'].name} → {edge['dst'].name}」的条件字段"
                 f"「{field_code}」在对象 {bo.code if bo else wf.bo_code} 里不存在，"
                 f"该条件已忽略")
            continue
        op_label = (op_raw or "等于").strip()
        op = WF_COND_LABEL.get(op_label, "")
        if not op:
            note(f"条件运算「{op_label}」认不出，已按「等于」处理"
                 f"（可选：{' / '.join(WF_COND_LABEL)}）")
            op, op_label = "equal", "等于"
        if bf.type_new not in ("dic", "object", "number"):
            note(f"条件字段「{bf.name}」是 {bf.type_new} 类型，样本里没有这类条件的写法，"
                 f"已按字符串条件生成，导入后请在流程设计器里核对")
        value, value_label_text = _rule_value(
            bf, op, value_raw, value_label, bo, note, edge["src"].name)
        rtype, rinput, rdatatype, roptions, robject = _rule_field_snapshot(bf)
        field_expr = (f"({robject}){bf.code}"
                      if bf.type_new in ("dic", "object") and robject else bf.code)
        rules.append({
            "rule_id": _rule_id(wf.name, edge["src"].name, edge["dst"].name,
                                bf.code, value),
            "field": field_expr,
            "label": bf.name,
            "type": rtype,
            "input": rinput,
            "field_data_type": rdatatype,
            "field_object_type": robject,
            "field_options": roptions,
            "operator": op,
            "operator_label": op_label,
            "value": value,
            "value_label": value_label_text,
            "boCode": bo.code if bo is not None else wf.bo_code,
            "format": "",
        })
    return rules


def _rule_id(*parts) -> str:
    """规则的短 id：8 位十六进制、确定性（样本是编辑器随机生成的 7 位字符）。

    用 md5 而不是 ``ids.short_id()``：后者基于雪花 id（时间驱动），
    同一份 Excel 两次出包会得到不同的 rule_id，diff 就失效了。
    """
    seed = "/".join(str(p) for p in parts)
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:8]


def _condition_text(edge: dict, bo: Bo, wf: Workflow, note) -> str:
    """``<Condition>`` 的文本：无条件写 ``True``，有条件写双层 JSON。

    外层 ``{"$type": …, "conditionJson": "<内层 JSON 字符串>"}`` —— 样本就是这么
    套两层的，别拍平成一层。
    """
    rules = _rules_of(edge, bo, wf, note)
    if not rules:
        return "True"
    tree = {"condition": "AND", "rules": rules}
    outer = {"$type": "WorkflowConditionTreeTransitionCondition",
             "conditionJson": json_attr(tree)}
    return json_attr(outer)


# ---------------------------------------------------------------- 表单 / 节点

def _pick_form(node: WfNode, bo: Bo, project: Project, note) -> Form | None:
    """节点用哪张表单：显式填了就用；否则开始节点取新建表单、其余取详情表单。

    都没有（比如只填了对象没填表单）就退回该对象的第一个表单 —— 有表单总比
    nodePermissions 空着强，至少字段权限能配出来。
    """
    own = [f for f in project.forms if f.bo_code == bo.code]
    if node.form_key:
        hit = next((f for f in project.forms if f.key == node.form_key), None)
        if hit is None:
            note(f"节点「{node.name}」填的表单 {node.form_key} 不在本包内"
                 f"（若系统里已有这张表可忽略这条）")
        return hit
    want = "create" if node.kind == WF_START else "detail"
    hit = next((f for f in own if f.kind == want), None)
    if hit is None and own:
        hit = own[0]
        note(f"节点「{node.name}」没找到{('新建' if want == 'create' else '详情')}表单，"
             f"已用表单 {hit.key}（字段权限按它生成）")
    if hit is None:
        note(f"节点「{node.name}」的对象 {bo.code} 没有任何表单，"
             f"该节点的 FormKey 与字段权限会是空的")
    return hit


def _go_back_ids(node: WfNode, nodes: list[WfNode], pairs: list[tuple[str, str]],
                 wf: Workflow, note) -> list[int]:
    """本节点允许退回的 activity Id 列表。

    - 用户填了「退回节点」→ 用他的（「无」= 不允许退回）；
    - 留空 → **本节点之前所有 NormalActivity**（含「开始」节点、**不含判断节点**）。

    更正设计的那条「本节点之前所有 Ongoing」：样本 v30 里 node2=[1]（只有开始节点）、
    node10=[1,2,3,8]（跳过 Branch 7）、node14=[1,2,3,6,12]（跳过 Branch 13）——
    开始节点是算的，判断节点是不算的。
    """
    if node.go_back:
        if len(node.go_back) == 1 and node.go_back[0] == "无":
            return []
        ids: list[int] = []
        for ref in node.go_back:
            hit = wf.node_by_name(ref)
            if hit is None or hit.id not in [n.id for n in nodes]:
                note(f"节点「{node.name}」的退回节点「{ref}」不在流程里，已忽略")
                continue
            if hit.kind == WF_BRANCH:
                note(f"节点「{node.name}」的退回节点「{ref}」是判断节点，已忽略"
                     f"（判断节点没有待办，退不回去）")
                continue
            ids.append(hit.id)
        return sorted(set(ids))

    # 反向 BFS：谁（直接或间接）能走到本节点
    parents: dict[str, list[str]] = {}
    for a, b in pairs:
        parents.setdefault(b, []).append(a)
    seen: set[str] = set()
    stack = list(parents.get(node.name, []))
    while stack:
        cur = stack.pop()
        if cur in seen or cur == node.name:
            continue
        seen.add(cur)
        stack.extend(parents.get(cur, []))
    by_name = {n.name: n for n in nodes}
    return sorted({by_name[x].id for x in seen
                   if x in by_name and by_name[x].kind != WF_BRANCH})


def _activity_xml(node: WfNode, outs: list[dict], nodes: list[WfNode],
                  pairs: list[tuple[str, str]], tid_of: dict[int, int],
                  states: dict[str, str], form: Form | None, bo: Bo,
                  wf: Workflow, note) -> str:
    """一个节点：活动元素 + 它的全部出边 + Properties。

    ``pairs`` 是**全图**的 (src, dst) —— 算「退回节点」要沿入边反查祖先，
    只给本节点的出边是算不出来的。
    """
    go_back = _go_back_ids(node, nodes, pairs, wf, note)

    transitions = [
        _transition_xml(e, tid_of.get(id(e), 0), node.kind, bo, wf, note)
        for e in outs
    ]

    if node.kind == WF_BRANCH:
        # BranchActivity 没有 State / 没有 ExpectHandleDays，Properties 只有 ColorCode
        attrs = {"Id": str(node.id), "Name": node.name}
        props = [("ColorCode", node.color or WF_COLOR.get(WF_BRANCH, "2"))]
        return wrap("BranchActivity",
                    transitions + [wrap("Properties", _rows(props, 7), indent=6)],
                    attrs, indent=5)

    form_key = node.form_key or (form.key if form is not None else "")
    state_value = node.business_state or WF_STATE_BY_KIND.get(node.kind, "")
    state_code = states.get(state_value, "")
    if state_value and not state_code:
        note(f"节点「{node.name}」的业务状态「{state_value}」不在对象的流程状态选项里"
             f"（{'/'.join(WF_STATE_OPTIONS)}），BusinessState 已留空")
    color_default = WF_COLOR.get(node.kind, _DEFAULT_COLOR)

    props: list[tuple[str, str]] = []
    if node.kind == WF_START:
        # 更正设计：v30 的开始节点**没有** ColorCode（只有 v1–v10 旧图写了 7）
        props = [
            ("BeforeCompleteEvents", "[]"),
            ("IfShowButtonOfConsult", "false"),
            ("AfterEnterEvents", "[]"),
            ("IfShowButtonOfSaveForm", "true"),
            ("BusinessState", state_code),
            ("FormKey", form_key),
            ("BeforeEnterEvents", "[]"),
            ("AfterCompleteEvents", "[]"),
        ]
    elif node.kind == WF_PASSED:
        props = [
            ("BeforeCompleteEvents", "[]"),
            ("AfterEnterEvents", "[]"),
            ("IfShowButtonOfSaveForm", "false"),
            ("BusinessState", state_code),
            ("FormKey", form_key),
            ("ColorCode", node.color or color_default),
            ("BeforeEnterEvents", "[]"),
            ("AfterCompleteEvents", "[]"),
            ("IfShowButtonOfAddReader", "false"),
        ]
    elif node.kind == WF_REJECTED:
        # 更正设计（原文档写反了）：Rejected **有** IfShowButtonOfAddReader，
        # **没有** IfShowButtonOfSaveForm —— 证据是 v1–v10 的
        # <NormalActivity Id="5" Name="不通过" State="Rejected">
        props = [
            ("BeforeCompleteEvents", "[]"),
            ("AfterEnterEvents", "[]"),
            ("BusinessState", state_code),
            ("FormKey", form_key),
            ("ColorCode", node.color or color_default),
            ("BeforeEnterEvents", "[]"),
            ("AfterCompleteEvents", "[]"),
            ("IfShowButtonOfAddReader", "false"),
        ]
    else:      # Ongoing（以及任何未知 kind 都按审批节点出）
        props = [
            ("BeforeCompleteEvents", "[]"),
            ("AfterEnterEvents", "[]"),
            ("GoBackActivityIds", json_attr(go_back)),
            ("BusinessState", state_code),
            ("Handler", _handler_chain(node, note)),
            ("FormKey", form_key),
            ("ColorCode", node.color or color_default),
            ("BeforeEnterEvents", "[]"),
            ("AfterCompleteEvents", "[]"),
        ]
        if go_back:
            # 键序是**字符串字典序**（样本 node14 实证 {"1","12","2","3","6"}），
            # 不是数字序 —— sorted(key=str) 才能与样本逐字节一致
            comment = {str(i): True for i in sorted(go_back, key=str)}
            props.append(("GoBackConfig",
                          json_attr({"name": "退回", "commentRequired": comment})))
        # GoBackConfig / ReviewBusinessParams 都是**可选**键（30 版里有 56 个
        # Ongoing 两个都没有）。默认都写，若某些环境不认这个键，删掉这一行即可。
        props.append(("ReviewBusinessParams",
                      '{"enabled":false,"reviewNodeTypes":[]}'))

    attrs = {"AllowHandover": "true" if node.allow_handover else "false"}
    if node.kind == WF_ONGOING:
        attrs["ExpectHandleDays"] = str(node.expect_days or 2)
    attrs["Id"] = str(node.id)
    attrs["Name"] = node.name
    attrs["State"] = node.kind

    return wrap("NormalActivity",
                transitions + [wrap("Properties", _rows(props, 7), indent=6)],
                attrs, indent=5)


def _transition_xml(edge: dict, tid: int, src_kind: str, bo: Bo, wf: Workflow,
                    note) -> str:
    """一条出边（Transition）。

    ``Properties`` 的子元素按「源节点种类」出。证据是 30 个 FlowChart 版本里
    365 条出边的全量统计（QA 2026-11-13 逐个版本核过）：
      源 = Start      30/30   有 IfHideAttachment、**没有** NotAllowManualTransform
      源 = Ongoing   215/215   两个都有（NotAllowManualTransform 插在 IfHideCC 之后）
      源 = Branch    120/120   两个都没有
    """
    props: list[tuple[str, str]] = [("IfHideComment", "false"), ("PreTransformEvents", "[]")]
    if src_kind != WF_BRANCH:
        props.append(("IfHideAttachment", "false"))
    props.append(("IfHideCC", "false"))
    if src_kind == WF_ONGOING:
        # 只有 Ongoing 的出边带这个键：开始节点没有待办，写了也只是噪音，
        # 而样本里 30/30 个 Start 出边都没有它（差一个键就会让 diff 处处不等）
        props.append(("NotAllowManualTransform", "false"))
    props.append(("PostTransformEvents", "[]"))

    inform = edge["inform"]
    if inform is None:
        # 判断节点的出边默认不通知（条件分支是自动流转），其余默认通知
        inform = src_kind != WF_BRANCH

    attrs = {
        "AllowAutoTransform": "false",
        "AutoTransformReferActivityIds": "",
        "CommentRequired": "false",
        "Id": str(tid),
        "Inform": "true" if inform else "false",
        "Name": edge["name"],
        "Order": str(edge["order"]),
        "Target": str(edge["dst"].id),
        "Type": "Forward",
    }
    next_handler = (_NEXT_HANDLER_PICK if edge["src"].pick_next else "ActivityHandler")
    return wrap("Transition", [
        el("Condition", "", esc_attr(_condition_text(edge, bo, wf, note)), indent=7),
        el("Countersign", "", "Any", indent=7),
        el("NextHandler", "", next_handler, indent=7),
        wrap("Properties", _rows(props), indent=7),
    ], attrs, indent=6)


# ---------------------------------------------------------------- layout / 权限

def _layout_json(nodes: list[WfNode], edges: list[dict], tid_of: dict[int, int],
                 levels: dict[str, int]) -> str:
    """画布坐标。设计器一打开会按自己的算法重排，这里只求「不重叠、能看」。

    - node key 与 link key 是**两套独立**的 ``-1..-N`` 序列（样本实证）；
    - ``points`` 是 4 个数字的扁平数组 ``[x1,y1,x2,y2]``，不是点对数组；
    - ``loc`` 是 ``"x y"`` 字符串。
    """
    index = {n.name: i for i, n in enumerate(nodes)}
    by_level: dict[int, list[int]] = {}
    for i, n in enumerate(nodes):
        by_level.setdefault(levels.get(n.name, 0), []).append(i)
    pos: dict[str, tuple[float, float]] = {}
    for lv, idxs in by_level.items():
        for k, i in enumerate(idxs):
            # 同层竖排、整层垂直居中（样本的设计器也是这么排的）
            x = round(float(lv * _LAYER_GAP), 2)
            y = round(float((k - (len(idxs) - 1) / 2) * _ROW_GAP), 2)
            pos[nodes[i].name] = (x, y)

    node_array = []
    for i, n in enumerate(nodes):
        x, y = pos.get(n.name, (0.0, 0.0))
        node_array.append({
            "activityId": n.id,
            "key": -(i + 1),
            "text": n.name,
            "category": "Branch" if n.kind == WF_BRANCH else n.kind,
            "loc": f"{x} {y}",
        })

    link_array = []
    for j, e in enumerate(edges):
        sx, sy = pos.get(e["src"].name, (0.0, 0.0))
        tx, ty = pos.get(e["dst"].name, (0.0, 0.0))
        link_array.append({
            "key": -(j + 1),
            "transitionId": tid_of.get(id(e), 0),
            "text": e["name"],
            "points": [sx, sy, tx, ty],
            "from": -(index[e["src"].name] + 1),
            "to": -(index[e["dst"].name] + 1),
        })

    return json_attr([{
        "nodeDataArray": node_array,
        "linkDataArray": link_array,
        "version": 1,
    }])


def _permissions_json(nodes: list[WfNode], bo: Bo, snip: Snippets,
                      form_of: dict[str, Form | None], note, link=None) -> str:
    """nodePermissions：每个 NormalActivity 一份字段权限。

    - 键 = 非判断节点的 activity Id（样本 v30 是 10 个键，判断节点不在里面）；
    - 第一项固定 ``_p_title``（表单标题），开始节点可编辑、其余只读；
    - 其余项 = 该节点表单里的**叶子控件**，field_type / field_options 走
      ``gen_form.node_field_meta``（与表单产物逐键一致，见那个函数的说明）。

    **已知偏离**：样本 58 项里含 6 个 ``fieldset_xxx`` 容器项，开始节点还有 22 条
    明细表列权限（``column``）。容器名是 gen_form 构建期 ``short_id()`` 随机生成的，
    流程侧无法复现同一个名字；明细表列来自列表模板，同理。所以这两类我们不出
    （见模块 docstring 第 1 条，附回滚方法）。
    """
    perms: dict[str, dict] = {}
    for node in sorted(nodes, key=lambda n: n.id):
        if node.kind == WF_BRANCH:
            continue
        edit = _EDIT_START if node.kind == WF_START else _EDIT_NONE
        fields = [_perm_item(bo, "_p_title", "表单标题", "text", {}, edit)]
        form = form_of.get(node.name)
        if form is not None:
            for ff in form.fields:
                if ff.field_type in _SKIP_TYPES:
                    continue
                ftype, options, fallback = node_field_meta(form, ff, snip, link, bo)
                if fallback:
                    note(f"节点「{node.name}」的控件 {ff.name}：样例里没有 {ftype} 骨架，"
                         f"字段权限用了 text 兜底，导入后请核对")
                fields.append(_perm_item(bo, ff.name, ff.label, ftype, options, edit))
        if len(fields) < 2:
            note(f"节点「{node.name}」的表单没有可配权限的控件，"
                 f"该节点只带「表单标题」一项权限")
        perms[str(node.id)] = {"button": [], "field": fields,
                               "column": [], "row": [], "opinion": []}
    return json_attr(perms)


def _perm_item(bo: Bo, name: str, label: str, field_type: str, options: dict,
               edit: list) -> dict:
    """nodePermissions.field 里的单项（键序照样本 v30 第一项逐字）。"""
    return {
        "field_options": options,
        "hide": _PERM_NONE,
        "code": bo.code,
        "read": _READ_ALL,
        "edit": edit,
        "name": name,
        "label": label,
        "field_type": field_type,
        "required": _PERM_NONE,
    }


# ---------------------------------------------------------------- 小工具

def _rows(props: list[tuple[str, str]], indent: int = 8) -> list[str]:
    """``(标签, 文本)`` 列表 → 已转义的行（Properties 的子元素）。

    缩进照样本：活动级 Properties 在 12 空格、子元素 14（indent=6/7）；
    流转级 Properties 在 14 空格、子元素 16（indent=7/8）。
    """
    return [el(name, "", esc_attr(text), indent=indent) for name, text in props]


def _esc_text(text: str) -> str:
    """文本节点转义：只转 ``& < >``，**保留双引号原样**。

    样本里 ``<layout>`` / ``<nodePermissions>`` 的 JSON 引号是裸的，
    而 ``<Condition>`` 被转成了 ``&quot;``（编辑器逐字写出来的差异）。
    两者 XML 解析结果相同，这里照样本各自的形态写，diff 才干净。
    """
    return (str(text).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))
