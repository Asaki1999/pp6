"""用一句话设计表单布局。

对应 JeecgBoot 的 desform 思路：AI 只产出**布局清单**，
XML 由 gen_form 的既有模板渲染。让模型直接写表单 XML 必错
（一个控件就是一坨 30+ 键的 fieldOptions）。

AI 要回答的三个问题：
  1. 字段怎么分组（基本信息 / 业务信息 / 审批信息…）
  2. 哪些字段占整行（长文本、附件）
  3. 哪些必填、占位提示写什么
"""
from __future__ import annotations

import json
import re

from . import fieldtypes
from .model import (
    EMPTY_OPERATORS,
    FORM_CREATE,
    FORM_CONDITION,
    FORM_DETAIL,
    HIDE_OPERATORS,
    OP_BY_LABEL,
    Bo,
    Form,
    FormField,
    HideRule,
)
from .nlq import llm
from .ai_bo import _extract_json, _fast_cfg

SYSTEM = """你是 PowerProject V6 的实施顾问，负责设计数据录入表单。

根据业务对象的字段清单，设计表单布局。只输出 JSON，不要解释，不要 markdown。

JSON 结构：
{
  "groups": [
    {"title": "基本信息", "fields": ["applyNo", "applyDate", "applicant"]},
    {"title": "采购内容", "fields": ["itemName", "quantity", "unitPrice"]},
    {"title": "补充说明", "fields": ["remark"]}
  ],
  "required": ["applyNo", "applicant", "itemName"],
  "fullRow": ["remark", "attachment"],
  "placeholders": {"applyNo": "系统自动生成"},
  "cascades": {"itemType": "category"},
  "hideRules": [
    {"target": "budget", "source": "needBudget", "operator": "不等于", "value": "是"}
  ]
}

字段说明：
- groups：按字段数决定组数，**组数 ≈ ceil(字段数 ÷ 6)**，最少 2 组、最多 8 组，
  每组 3~8 个字段。按**业务含义**分，不要按字段类型分。
  例：采购申请 = 基本信息 / 采购内容 / 时间与说明；
  字段多时（30+ 个）拆细一些，如 基本信息 / 分类与归属 / 研发属性 / 组织与承接 /
  预算与执行 / 目标与进展 / 变更与风险
- required：必填的字段编码（单号、名称、金额、申请人这类）
- fullRow：需要独占一行的字段（长文本、附件、说明类）
- placeholders：个别字段的输入提示（可选，绝大多数不用写）
- cascades：级联下拉，{"子字段": "父字段"}。父字段必须是同一张表单里
  更靠前的下拉/字典字段，**只有确实存在上下级关系时才写**（如 品种分类 级联 品种类别）
- hideRules：条件隐藏，条件成立时把 target 藏起来。想表达「等于X时才显示」，
  就反过来写「不等于X 时隐藏」。operator 只能取 等于/不等于/为空/不为空；
  同一条规则里只能用单条件，多条件请拆成多条。
  value 填**选项名原文**（如「是」，不要填 Y/N 这种码），为空类不填 value

硬性规则：
1. 每个字段编码必须来自给定的字段清单，**不能编造**
2. 每个字段**只能出现一次**，且必须全部用上
3. 分组标题 2~6 个字，不要起「其他」「杂项」这种
4. 组内字段顺序按填写习惯排（先填什么后填什么）
"""


def _norm_groups(spec: dict, bo: Bo) -> tuple[list[dict], list[str]]:
    """把 AI 的分组规整到对象字段上。对不上的剔除，重复的只留第一次。"""
    notices: list[str] = []
    by_code = {f.code: f for f in bo.fields}
    seen: set[str] = set()
    groups: list[dict] = []

    for g in (spec.get("groups") or []):
        title = (g.get("title") or "").strip() or "基本信息"
        codes = []
        for c in (g.get("fields") or []):
            code = str(c).strip()
            if code not in by_code:
                notices.append(f"分组「{title}」里的字段 {code} 不存在，已跳过")
                continue
            if code in seen:
                continue
            seen.add(code)
            codes.append(code)
        if codes:
            groups.append({"title": title, "codes": codes})

    # AI 漏掉的字段补到最后一组，不能丢——丢了等于数据录不进去
    missing = [f.code for f in bo.fields if f.code not in seen]
    if missing:
        if groups:
            groups[-1]["codes"].extend(missing)
        else:
            groups = [{"title": "基本信息", "codes": missing}]
        notices.append(f"这些字段 AI 没安排位置，已补到最后一组：{', '.join(missing[:8])}")
    return groups, notices


def build_form(spec: dict, bo: Bo, form_key: str, kind: str = FORM_CREATE,
               name: str = "", layout: int = 2) -> tuple[Form, list[str]]:
    """把 AI 的布局 JSON 落成 Form 模型。"""
    groups, notices = _norm_groups(spec, bo)
    by_code = {f.code: f for f in bo.fields}

    required = {str(x).strip() for x in (spec.get("required") or [])}
    full_row = {str(x).strip() for x in (spec.get("fullRow") or [])}
    placeholders = {str(k): str(v) for k, v in (spec.get("placeholders") or {}).items()}

    form = Form(
        key=form_key,
        name=name or f"新建{bo.name}",
        bo_code=bo.code,
        kind=kind,
        form_type="form",
        layout=layout,
    )

    from . import blueprint  # 控件映射复用 blueprint 的规则

    for gi, group in enumerate(groups):
        for ci, code in enumerate(group["codes"]):
            f = by_code[code]
            ftype = blueprint.control_for(f)
            # 长文本/附件天然占整行；AI 点名的也占整行
            is_full = f.type_new in ("clob", "attachment") or code in full_row
            form.fields.append(FormField(
                form_key=form_key,
                name=code,
                label=f.name,
                field_type=ftype,
                # dic 的取值来源必须是字典 typeKey（对象编码_字段编码），
                # 用 f.object_type 会拿到 AI 塞进来的字段编码，控件就找不到字典
                value_source=(f.dict_key() if f.type_new == "dic"
                              else (f.object_type if f.type_new == "object" else "")),
                placeholder=_clean_placeholder(placeholders.get(code)) or _default_placeholder(f.type_new),
                required=code in required,
                readonly=bool(getattr(f, "readonly", False)),
                full_row=is_full,
                # AI 给的分组名直接就是「布局 V1.0」的分区标题，
                # 生成时每个分区出一个卡片/分组框 + 一个表格布局
                group=group["title"],
            ))

    _apply_cascades(form, spec, notices)
    _apply_hide_rules(form, spec, notices)
    return form, notices


def _apply_cascades(form: Form, spec: dict, notices: list[str]) -> None:
    """落 cascades：{"子字段": "父字段"}。AI 偶尔会给成 [[子, 父]] 也兼容。"""
    raw = spec.get("cascades") or {}
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        pairs = [(str(k).strip(), str(v).strip()) for k, v in raw.items()]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                pairs.append((str(item.get("child") or "").strip(),
                              str(item.get("parent") or "").strip()))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((str(item[0]).strip(), str(item[1]).strip()))
    by_name = {f.name: f for f in form.fields}
    for child, parent in pairs:
        cf = by_name.get(child)
        if cf is None:
            notices.append(f"AI 给的级联 {child}→{parent}：控件 {child} 不在表单里，已跳过")
            continue
        if parent not in by_name:
            notices.append(f"AI 给的级联 {child}→{parent}：父级 {parent} 不在表单里，已跳过")
            continue
        cf.cascade_parent = parent


def _apply_hide_rules(form: Form, spec: dict, notices: list[str]) -> None:
    """落 hideRules，顺带把中文算子翻译成系统里的英文码。"""
    by_name = {f.name: f for f in form.fields}
    for item in (spec.get("hideRules") or []):
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "").strip()
        source = str(item.get("source") or "").strip()
        if target not in by_name or source not in by_name:
            notices.append(f"AI 给的隐藏规则（{target} ← {source}）引用了不存在的控件，已跳过")
            continue
        op_raw = str(item.get("operator") or "等于").strip()
        operator = OP_BY_LABEL.get(op_raw, op_raw)
        if operator not in HIDE_OPERATORS:
            notices.append(f"AI 给的条件算子「{op_raw}」不支持，该条已按「等于」处理")
            operator = "equal"
        value = str(item.get("value") or "").strip()
        if operator not in EMPTY_OPERATORS and not value:
            notices.append(f"AI 给的隐藏规则（{target} ← {source}）缺条件值，已跳过")
            continue
        form.hide_rules.append(HideRule(
            form_key=form.key,
            target=target,
            source=source,
            operator=operator,
            value=value,
            value_label=str(item.get("valueLabel") or "").strip(),
        ))


def _clean_placeholder(text: str, limit: int = 10) -> str:
    """占位符要短，且不能截成半句话。

    手册的抽屉只有 45% 宽，一行两列时输入框实际只有 300px 出头，
    放不下长句。AI 常给出「请描述项目涉及的靶点或作用机制」这种，
    截到 10 个字会变成「请描述项目涉及的靶点或作…」，比长句更难看。

    所以：能整句保留就保留；超长且截不出完整短语，就返回空，
    由调用方退回该类型的标准提示（请输入 / 请选择）。
    """
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) <= limit:
        return t
    # 优先在标点处断开，保留前半句
    for sep in ("，", ",", "；", ";", "。", "、"):
        head = t.split(sep)[0].strip()
        if head and len(head) <= limit:
            return head
    return ""


def _default_placeholder(type_new: str) -> str:
    return {
        "number": "请输入数字",
        "datetime": "请选择日期",
        "user": "请选择人员",
        "department": "请选择部门",
        "object": "请选择",
        "dic": "请选择",
        "clob": "请输入",
        "attachment": "点击上传附件",
        "autoNumber": "系统自动生成",
    }.get(type_new, "请输入")


def _compact_fields(bo: Bo) -> list[dict]:
    """给模型看的字段清单。只给必要信息，省 token 也少出错。

    选项类字段要带上候选值：判断「谁能级联谁」靠的就是选项内容
    （「化学药/中药」显然是「药品」的下级，「已通过/未通过」显然不是）。
    只给前 10 个，避免选项多的字段把 token 撑爆。
    """
    out: list[dict] = []
    for f in bo.fields:
        item = {"code": f.code, "name": f.name,
                "type": fieldtypes.labels().get(f.type_new, f.type_new)}
        if f.type_new == "dic" and f.choices:
            item["选项"] = list(f.choices)[:10]
        out.append(item)
    return out


def generate(bo: Bo, cfg: dict, form_name: str = "", timeout: int = 90) -> dict:
    """给一个业务对象设计表单布局。"""
    import time

    if not llm.active_channels(cfg):
        raise llm.LlmError("还没配置大模型 API Key")

    payload = {
        "对象名称": bo.name,
        "对象编码": bo.code,
        "字段": _compact_fields(bo),
    }
    t0 = time.time()
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    # 表格布局的 JSON 很小（分组 + 字段名清单），但推理型模型会先消耗一大段思考 token，
    # 1500 经常不够——截断后就是 "Unterminated string starting at"。
    # 给足 4000，并且失败自动重试一次（换更"短"的指令，减少模型发挥）。
    text, used = llm.chat_any(_fast_cfg(cfg), msgs, timeout=timeout, max_tokens=4000)
    try:
        spec = _extract_json(text)
    except json.JSONDecodeError as exc:
        retry_msgs = [
            {"role": "system", "content": SYSTEM + "\n\n只输出 JSON，字段名照抄，不要任何多余内容。"},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            text2, used2 = llm.chat_any(_fast_cfg(cfg), retry_msgs,
                                        timeout=timeout, max_tokens=4000)
            spec = _extract_json(text2)
            used = used2
        except json.JSONDecodeError:
            raise llm.LlmError(f"模型输出不是合法 JSON（{exc.msg}）")
        except Exception as rexc:
            raise llm.LlmError(f"模型输出不是合法 JSON（{exc.msg}）；重试也失败：{str(rexc)[:80]}")

    name = form_name or f"新建{bo.name}"
    form, notices = build_form(spec, bo, f"create_{bo.code}", FORM_CREATE, name)
    return {
        "form": form,
        "groups": [{"title": g["title"], "fields": g["codes"]}
                   for g in _norm_groups(spec, bo)[0]],
        "notices": notices,
        "model": used.get("model", ""),
        "seconds": round(time.time() - t0, 2),
    }


def to_dict(form: Form) -> dict:
    """给前端预览用，结构对齐 /api/parse 的 forms。"""
    rows: list[list[dict]] = []
    cur: list[dict] = []
    for ff in form.fields:
        if ff.newline and cur:
            rows.append(cur)
            cur = []
        cur.append({
            "label": ff.label,
            "name": ff.name,
            "type": ff.field_type,
            # 下拉类控件的取值来源（对象编码/字典 key）。漏了它预览再导出，
            # 「所属项目」这种控件就成了没有数据源的空下拉——check 只会轻轻提一句。
            "valueSource": ff.value_source,
            "required": ff.required,
            "readonly": ff.readonly,
            "placeholder": ff.placeholder,
            "fullRow": ff.full_row,
            "group": ff.group,
        })
        if ff.full_row:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)

    return {
        "key": form.key,
        "name": form.name,
        "boCode": form.bo_code,
        "kind": form.kind,
        "layout": form.layout,
        "layoutStyle": form.layout_style,
        "labelWidth": form.resolved_label_width(),
        "fieldCount": len(form.fields),
        "fields": [
            {"name": ff.name, "label": ff.label, "type": ff.field_type,
             "valueSource": ff.value_source, "readonly": ff.readonly,
             "required": ff.required, "fullRow": ff.full_row,
             "cascadeParent": ff.cascade_parent,
             "placeholder": ff.placeholder, "group": ff.group}
            for ff in form.fields
        ],
        "hideRules": [
            {"target": r.target, "source": r.source, "operator": r.operator,
             "value": r.value, "valueLabel": r.value_label}
            for r in form.hide_rules
        ],
        # 级联两种形状都给：字段上的 cascadeParent 方便前端逐行展示，
        # 这里的 {子: 父} 方便别的消费者（导出 Excel、微调回传）整体拿走
        "cascades": {ff.name: ff.cascade_parent for ff in form.fields if ff.cascade_parent},
        "rows": rows,
    }
