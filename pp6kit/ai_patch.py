"""存量包修订的两个 AI 入口：任意 Excel 认字段、对话调整草稿。

为什么单独做（而不是复用 ai_sheet.analyze）：
- `analyze` 产出的是「一整个业务对象 + 表单 + 列表」，用于新建；存量包里只加几列，
  让模型设计一整套表单既慢又容易越界（PAT10 的红线是「只加字段、改名称」）。
- 这里只回答两件事：表里有哪些字段、这次对话要加减什么。

两个函数都不落库、不碰包，纯函数式：入参出参见 `/api/patch/fields/free` 与
`/api/ai/patch-fields` 的契约（README「导入前改包」）。
"""
from __future__ import annotations

import json
import re

from . import ai_bo, ai_sheet
from .nlq import llm

# 新增字段允许的类型（与后台「添加字段」的字段层类型一致；表格/公式这类需要
# 额外结构定义，界面上也没做配置，这里不开放，避免生成导不进去的包）
ALLOWED = ("varchar", "number", "date", "datetime", "clob", "dic",
           "user", "department", "attachment", "object", "autoNumber")

_LABELS = {
    "字符串": "varchar", "文本": "varchar", "单行文本": "varchar", "字符": "varchar",
    "数字": "number", "数值": "number", "金额": "number", "整数": "number",
    "日期": "date", "日期时间": "datetime", "时间": "datetime",
    "大文本": "clob", "多行文本": "clob", "备注": "clob",
    "选项": "dic", "枚举": "dic", "字典": "dic",
    "用户": "user", "人员": "user", "部门": "department",
    "附件": "attachment", "附件列表": "attachment",
    "对象": "object", "关联对象": "object",
    "自动编号": "autoNumber", "编号": "autoNumber",
}

FREE_SYSTEM = """你在帮实施顾问从一张 Excel 里识别「要往已有业务对象里新增哪些字段」。

只输出 JSON，不要解释。规则：
1. 表可能是任意形态：一行一个字段、多级表头、字段名和类型/说明混排、甚至带样例数据。
   自己判断哪行/哪列是字段名；不要被标题行、序号列、示例行带偏。
2. 每个字段给这些键：
   name       中文名，照表里的写法（没有就按含义起一个，别加序号）
   code       语义化英文小驼峰，禁止拼音，字母开头，20 字符以内
              （例：风险等级→riskLevel、期望到货日期→expectDate、采购说明→purchaseNote）
   type       只能是 varchar / number / date / datetime / clob / dic / user /
              department / attachment / object / autoNumber
   choices    选项值用「、」分隔；非选项字段给空串
   length     数字或空串；precision 同理
   required   true / false（表里写「必填/是/*」才为 true）
   note       说明，没有就空串
   source_column  来自表里哪一列（或哪一行），给用户核对用
3. 表里明确写了类型就照它映射：字符串/文本→varchar、数字/金额→number、
   日期→date/datetime、选项/枚举→dic、人员→user、部门→department。
4. 不要编造表里没有的字段；同一个字段出现多次只保留一次。
5. 识别不确定时宁可少给，并在 notes 里写一句（最多 3 条）。

输出格式：
{"fields": [...], "notes": ["..."]}"""

ADJUST_SYSTEM = """你在帮实施顾问调整「本次要新增的字段草稿」。

只输出 JSON。输入：draft（本次草稿，可改可删）、existing（包里已有字段，只能看）、
history（前几轮对话）、message（这次的要求）。

红线（必须遵守）：
1. 用户要删字段：只能删 draft 里的。existing 里的字段**一律不能删**，
   reply 里要直接说「包里已有的字段不能删除」，并指出只能从这次草稿里去掉。
2. 用户要改 existing 的字段名或类型：拒绝，告诉他去「已有字段改名」页签。
3. 新加字段：code 用语义化英文小驼峰（禁止拼音，20 字符以内）；
   type 只能是 varchar / number / date / datetime / clob / dic / user /
   department / attachment / object / autoNumber；选项值写 choices（用「、」分隔）。
   与 existing 同名的，换一个更具体的名字，并在 reply 里说明。
4. reply 用中文，祈使句、无主语，不超过 3 句；不要复述整张表。

输出格式：
{"reply": "给用户看的一句话",
 "add":    [{"code": "...", "name": "...", "type": "...", "choices": "...",
             "length": "", "precision": "", "required": false, "note": ""}],
 "modify": [{"code": "...", 其余键只给要改的}],
 "remove": ["code", "..."],
 "refused": false}"""


def _norm_type(value) -> str:
    text = str(value or "").strip()
    if text in ALLOWED:
        return text
    return _LABELS.get(text, "")


def _clean_code(value) -> str:
    code = re.sub(r"[^0-9A-Za-z_]", "", str(value or "").strip())
    if not code or not code[0].isalpha():
        return ""
    return code[:30]


def _text(value, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clean_field(raw: dict) -> dict | None:
    """模型/表里的一行 → 前端草稿字段；认不出类型就丢掉。"""
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name"), 60)
    code = _clean_code(raw.get("code"))
    type_code = _norm_type(raw.get("type") or raw.get("datatype"))
    if not name and not code:
        return None
    if not code:
        return None
    if not type_code:
        type_code = "varchar"
    item = {
        "code": code,
        "name": name or code,
        "type": type_code,
        "choices": _text(raw.get("choices") if raw.get("choices") is not None
                         else raw.get("options"), 300),
        "required": bool(raw.get("required")),
        "note": _text(raw.get("note") if raw.get("note") is not None else raw.get("desc"), 200),
        "source_column": _text(raw.get("source_column"), 40),
    }
    for key in ("length", "precision"):
        value = str(raw.get(key) or "").strip()
        item[key] = value if value.isdigit() else ""
    return item


def free_fields(data: bytes, cfg: dict, filename: str = "", sheet: str = "",
                timeout: int = 120) -> dict:
    """任意 Excel → 候选字段。不落库，识别完由前端勾选再进草稿。"""
    if not data:
        raise ValueError("没收到文件")
    summary = ai_sheet.summarize(data, only=sheet or None)
    if not summary.get("sheets"):
        names = "、".join(it["name"] for it in (summary.get("inventory") or [])[:6])
        tip = f"（读到了这些表：{names}）" if names else ""
        raise ValueError("这张表里没识别到可用的字段内容" + tip)

    text = ai_sheet.render_summary(summary)
    if len(text) > 8000:
        text = text[:8000] + "\n…（表格太长，已截断）"
    user = (f"文件名：{filename or '未命名'}\n表名：{summary['sheets'][0]['name']}\n\n"
            f"===== 表格内容 =====\n{text}")

    raw, _ = llm.chat_any(ai_bo._fast_cfg(cfg), [
        {"role": "system", "content": FREE_SYSTEM},
        {"role": "user", "content": user},
    ], timeout=timeout, max_tokens=4000)
    try:
        spec = ai_bo._extract_json(raw)
    except json.JSONDecodeError as exc:
        raise llm.LlmError("模型没能把这张表整理成字段清单，请换一张表或改用模板导入") from exc

    items: list[dict] = []
    conflicts: list[dict] = []
    seen_code: dict[str, str] = {}
    seen_name: dict[str, str] = {}
    for row in (spec.get("fields") or []):
        item = _clean_field(row)
        if not item:
            conflicts.append({"code": str((row or {}).get("code") or ""),
                              "reason": "缺字段编码或名称，已跳过"})
            continue
        low = item["code"].lower()
        if low in seen_code:
            conflicts.append({"code": item["code"], "reason": "编码重复，已跳过"})
            continue
        if item["name"] in seen_name:
            conflicts.append({"code": item["code"],
                              "reason": f"与「{item['name']}」重名，已跳过"})
            continue
        seen_code[low] = item["code"]
        seen_name[item["name"]] = item["code"]
        items.append(item)

    if not items:
        raise ValueError("这张表里没识别出可用的字段，请确认表头/列名是否清楚")
    notes = [_text(x, 120) for x in (spec.get("notes") or []) if _text(x, 120)][:3]
    return {"ok": True, "fields": items, "conflicts": conflicts, "notes": notes,
            "sheet": summary["sheets"][0]["name"],
            "sheetCount": summary.get("sheetCount") or 1}


def _compact_draft(draft: list[dict]) -> list[dict]:
    out = []
    for f in draft or []:
        if not isinstance(f, dict) or not str(f.get("code") or "").strip():
            continue
        out.append({k: f.get(k) for k in
                    ("code", "name", "type", "choices", "length", "precision",
                     "required", "note") if f.get(k) not in (None, "")})
    return out[:80]


def adjust(payload: dict, cfg: dict, timeout: int = 120) -> dict:
    """对话调整草稿；服务端强制红线（越界的删除/修改一律丢弃）。"""
    message = _text(payload.get("message"), 500)
    if not message:
        raise ValueError("说说要加、去掉或改哪个字段")
    draft = _compact_draft(payload.get("draft") or [])
    existing = []
    for f in (payload.get("existing") or [])[:400]:
        if isinstance(f, dict) and str(f.get("code") or "").strip():
            existing.append({"code": str(f.get("code")).strip(),
                             "name": _text(f.get("name"), 60)})
    draft_codes = {str(f["code"]) for f in draft}
    existing_codes = {f["code"] for f in existing}
    existing_names = {f["name"] for f in existing if f["name"]}

    history = []
    for turn in (payload.get("history") or [])[-6:]:
        if isinstance(turn, dict) and turn.get("role") in ("user", "assistant"):
            history.append({"role": turn["role"], "content": _text(turn.get("content"), 400)})

    body = json.dumps({"draft": draft, "existing": existing, "message": message},
                      ensure_ascii=False, indent=1)
    if len(body) > 12000:
        body = body[:12000] + "\n…（草稿太长，已截断）"
    msgs = [{"role": "system", "content": ADJUST_SYSTEM}, *history,
            {"role": "user", "content": body}]
    raw, _ = llm.chat_any(ai_bo._fast_cfg(cfg), msgs, timeout=timeout, max_tokens=4000)
    try:
        spec = ai_bo._extract_json(raw)
    except json.JSONDecodeError as exc:
        raise llm.LlmError("模型这次的输出不是合法 JSON，请再发一次") from exc

    refused = bool(spec.get("refused"))
    notes: list[str] = []

    add: list[dict] = []
    for row in (spec.get("add") or []):
        item = _clean_field(row)
        if not item:
            continue
        if item["code"] in existing_codes or item["name"] in existing_names:
            refused = True
            notes.append(f"「{item['name']}」包里已有同名字段，已跳过（改个更具体的名字再试）")
            continue
        if item["code"] in draft_codes:
            notes.append(f"草稿里已有编码 {item['code']}，已跳过")
            continue
        draft_codes.add(item["code"])
        add.append(item)

    modify: list[dict] = []
    for row in (spec.get("modify") or []):
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if code not in draft_codes:
            # 指向包里已有字段（或不存在的编码）：红线，丢弃并说明
            refused = True
            notes.append(f"「{code or '未指明'}」不在本次草稿里，不能修改（已有字段请用「改名」页签）")
            continue
        patch = {k: row[k] for k in
                 ("name", "choices", "length", "precision", "required", "note")
                 if k in row}
        if "type" in row:
            type_code = _norm_type(row.get("type"))
            if type_code:
                patch["type"] = type_code
        if patch:
            modify.append({"code": code, **patch})

    remove: list[str] = []
    for code in (spec.get("remove") or []):
        code = str(code or "").strip()
        if not code:
            continue
        if code in existing_codes:
            refused = True
            notes.append(f"包里已有的字段「{code}」不能删除；只能从这次草稿里去掉")
            continue
        if code in draft_codes:
            remove.append(code)
        else:
            notes.append(f"草稿里没有 {code}，已忽略")
    for code in remove:
        draft_codes.discard(code)

    reply = _text(spec.get("reply"), 300) or "已按你的要求调整草稿"
    if refused and not spec.get("refused"):
        reply = "包里已有的字段不能删除或改类型；只能从这次草稿里去掉。"
    return {"ok": True, "reply": reply, "add": add, "modify": modify,
            "remove": remove, "refused": refused, "notes": notes}
