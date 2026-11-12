"""用一句话生成业务对象 + 字段。

JeecgBoot 的 codegen / onlform 就是这个思路：**AI 只产出结构化清单，
XML 由既有模板生成**。让模型直接写 35KB 的 bo XML 必错（转义、ID、字段顺序），
但让它写「对象名 + 10 个字段」很稳。

产出直接落到 Project 模型里，走原有的校验 + 生成链路，不另开一条路。
"""
from __future__ import annotations

import json
import re

from . import fieldtypes, objects
from .nlq import llm
from .model import Bo, BoField, Project, truthy

# 只让 AI 用这些类型——表/公式/国际化没做验证，别让它张口就来。
# 顺序照系统「添加字段 › 字段类型」下拉（用户截图给的 14 项，去掉没验证的 4 项）：
#   1 Str.字符串 2 数字 3 日期时间 4 大文本 5 用户 6 部门 7 附件列表
#   8 国际化   9 选项 10 对象  11 用户组 12 表格 13 自动编号 14 公式
ALLOWED_TYPES = (
    "varchar", "number", "datetime", "clob", "user", "department",
    "attachment", "dic", "object", "autoNumber",
)

SYSTEM = """你是 PowerProject V6 项目管理系统的实施顾问，负责设计业务对象（数据表）。

根据用户描述，输出一个业务对象及其字段清单。只输出 JSON，不要解释，不要 markdown 代码块。

JSON 结构：
{
  "code": "对象编码（英文小写+下划线，如 purchase_apply）",
  "name": "对象中文名（如 采购申请）",
  "desc": "一句话说明",
  "fields": [
    {"code": "", "name": "申请金额", "type": "number", "length": 20,
     "required": true, "objectType": "", "readonly": false, "note": ""}
  ]
}

**不要写 note 里有意义的说明，一律留空字符串**——说明是给做表的人看的，不写进系统。

字段类型只能从这 **14 种**里选（这是系统「添加字段 › 字段类型」下拉的完整清单，
顺序也一样，不要发明新类型）：

| 类型代码 | 界面叫法 | 什么时候用 |
|---|---|---|
| varchar | 字符串 | 单行文本，默认（长度 200） |
| number | 数字 | 金额、数量、百分比（长度 20） |
| datetime | 日期时间 | 日期、截止时间 |
| clob | 大文本 | 多行文本、备注、说明 |
| user | 用户 | 指向「人」（负责人、审批人、经办人） |
| department | 部门 | 指向系统组织树里的部门 |
| attachment | 附件列表 | 附件、图纸、文件 |
| i18n | 国际化 | 多语言文案（极少用） |
| dic | 选项 | 一组**固定取值**，人工维护（候选值写在 values 里；**objectType 一律留空**） |
| object | 对象 | 关联另一个业务对象的一条记录（项目/合同/客户/产品…） |
| userGroupCate | 用户组 | 用户组 |
| table | 表格 | 子表单（明细行） |
| autoNumber | 自动编号 | 单号 |
| formula | 公式 | 计算字段（没做验证，别用） |

**没有「数据字典」「数据集」这两种类型**——那是表单控件层的取数方式，不是字段类型，
下拉里根本选不到。需要给人选一串固定值，就用「选项」（dic）。

硬性规则：
1. **字段 code 必须由你按下面规范命名，不要留空、不要拼音。**
   code 是物理列名，导入后再改要建新表，所以必须一次给对。

   ▸ 命名规范（取自系统里 project / task / contract 等原生对象的真实字段）：
   - 小驼峰，全英文，语义化，**20 字符以内**
   - 「名称」类 → `name`（不要 itemName / xiangMuMingCheng）
   - 「编号/单号」→ `code`；「描述/说明」→ `description` 或 `notes`
   - 「关联某对象」→ `对象Id`，多个用 `对象Ids`
     （所属项目→projectId、项目状态→stateId、当前处理人→handlerIds）
   - 「是否XX」→ `isXxx`（是否里程碑→isMilestone）
   - 日期 → `xxxDate`；时间 → `xxxTime`；金额 → `xxxAmount`
   - 只能用小写字母开头 + 字母数字，**不含下划线**
   - 同一对象内不得重复

   ▸ 正例：项目名称=name、合同金额=amount、计划验收日期=planInspectDate、
     是否工艺技术类项目=isCraftTech、MAH持有人=mahHolder、申请人=applicant
   ▸ 反例：项目名称=xiangMuMingCheng（拼音）、是否工艺技术类项目=shiFouGongYiJiShuLeiXiangMu
     （27 字符）、MAH持有人=MAHChiYouRen（中英混杂）

   拿不准英文时，用简短准确的业务词，**绝不退回拼音长串**。
2. 不要生成 id / tenantId / ip / deleted / createBy / createTime / updateTime / version / updateBy
   —— 这 9 个系统字段生成时会自动补齐
3. 名称类字段用 varchar；金额用 number；审批人用 user；状态/类型/类别这类固定取值用 dic。
   **拿不准是 dic 还是 object 时一律 dic**：dic 带候选值，导入后能直接用；
   object 选错对象就是空下拉，用户什么也选不到。
4. 字段 8-16 个，覆盖业务实际要填的东西，不要凑数也不要漏
5. 「关联项目」「关联客户」「关联合同」这类**指向系统里别的业务对象**的，用 object（对象），
   objectType 填**下面给定的对象编码**（如 project / customer / contract）。
   编码必须从给定清单里选，不要自造，也不要填中文名——填错导入后是一个空下拉，什么也选不到。
   指向「人」的不要用 object，用 user；指向「部门」的用 department。
   **对象清单里没有对应条目的，不要硬套 object**——比如「需求单位」写了 20 个公司名，
   系统里没有这些组织，就该是 dic（选项）+ 候选值。
6. 带「单号」「编号」且要自动生成的，用 autoNumber
7. `readonly` 默认 false；只有字段是「只读展示/自动带出/自动生成」时才设 true，
   要人填写或选择的字段不要设只读。
8. 两个字段之间是「父级选了 A，子级只剩 A 下面那几项」的取值关系时，
   **在子字段上加 `cascadeParent` = 父字段的 code**（如 品种分类 → "productCategory"）。
   例：品种类别/品种分类、战略类别/项目注册类别、省市/区县。
   只在确实是父子取值关系时加；两个互不相干的下拉不要加，并列的也不要互相指。
   父字段可以是「选项」也可以是「关联对象」。加了之后，表单控件会自动继承，不用重复说明。
   ⚠️ 这条只声明「听谁的」，具体每个父值下有哪些子项由系统侧维护，
   所以 candidates 仍然要把子字段所有候选值列全。
"""


def _pick_provider(cfg: dict) -> bool:
    return bool(llm.active_channels(cfg))


# 生成类任务用「快模型优先」的链。
# 实测同一账号下：4-flash 3.3s / 4-air 4.0s / 4.5-flash 9.6s / 4.7-flash 频繁 429。
# 这类任务输出结构固定（10 来个字段的 JSON），小模型完全够用，
# 用大模型等 50 秒纯属浪费——用户填一句话不该等一分钟。
_FAST_CHAIN = ("glm-4-flash", "glm-4-air", "glm-4.5-flash", "glm-4.7-flash")


def _fast_cfg(cfg: dict) -> dict:
    """把渠道的模型换成快模型链，其余的（Key/地址）保持不动。"""
    out = []
    for ch in (cfg.get("channels") or []):
        if not (ch.get("api_key") and ch.get("base_url") and ch.get("enabled", True)):
            continue
        base_url = (ch.get("base_url") or "")
        if "bigmodel.cn" not in base_url:       # 只有智谱认这套模型名
            out.append(dict(ch))
            continue
        # 已确认没开通的模型别再试
        chain = [m for m in _FAST_CHAIN
                 if f"{base_url.rstrip('/')}|{(ch.get('api_key') or '')[-8:]}|{m}"
                 not in llm._NO_ACCESS]
        if not chain:
            out.append(dict(ch))
            continue
        out.append(dict(ch, model=chain[0], fallbacks=chain[1:]))
    return {"channels": out} if out else cfg


def _extract_json(text: str) -> dict:
    """模型输出经常带 ```json 包裹，或写到一半被 max_tokens 截断。

    截断是真实会发生的：字段多、模型啰嗦时 JSON 断在半个对象上。
    这里先剥 markdown，再尝试修复截断（补齐未闭合的括号），最后才报错。
    """
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    if not t.startswith("{"):
        i = t.find("{")
        if i >= 0:
            t = t[i:]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        fixed = _repair_truncated(t)
        if fixed is not None:
            return fixed
        raise


def _repair_truncated(text: str) -> dict | None:
    """截断的 JSON：砍掉最后一个不完整的元素，补齐括号后重试。

    两种输出结构都要认：
        业务对象   {"code":..., "fields":[{...}]}
        表单布局   {"groups":[{"title":..., "fields":[...]}]}   ← ai_form
    以前只判 "fields"，表单被截断后永远修不回来，
    表现就是「表单设计没成功（Unterminated string）」，白多跑一次 AI。
    """
    s = text.rstrip().rstrip(",")
    # 从后往前找「,]」或「,}」的位置，砍掉残缺的那一项
    for cut in range(len(s) - 1, max(len(s) - 3000, 0), -1):
        if s[cut] not in "}]":
            continue
        head = s[:cut + 1]
        # 补齐未闭合的 { 和 [
        depth_c = head.count("{") - head.count("}")
        depth_b = head.count("[") - head.count("]")
        if depth_c < 0 or depth_b < 0:
            continue
        candidate = head + "]" * depth_b + "}" * depth_c
        try:
            got = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(got, dict) and (got.get("fields") or got.get("groups")):
            return got
    return None


def _norm_code(raw: str, fallback: str = "biz", name: str = "") -> str:
    """对象编码：中文名 → 英文小写下划线，对齐系统真实对象（project_create）。

    AI 有时会把整个标题当名字（「项目立项信息表单」→ liuxianglingshixinxibiaodan），
    所以只要给了 name，就一律以 name 为准重新生成，不采信 raw。
    用 object_code 而不是拼音：对象编码会派生物理表名 pp_<code>，
    拼音出来的 pp_xiang_mu 在线上一个都找不到。
    """
    from . import naming
    label = (name or "").strip()
    if label:
        # 「XX表单」「XX表」这类尾巴对编码没意义
        clean = re.sub(r"(表单|表)$", "", label).strip() or label
        base = naming.object_code(clean)
        if base:
            return base
    s = re.sub(r"[^A-Za-z0-9_]", "_", (raw or "").strip())
    s = re.sub(r"_+", "_", s).strip("_").lower()
    if not s or not s[0].isalpha():
        s = fallback + "_" + s if s else fallback
    return s


_FIELD_CODE_RE = re.compile(r"^[a-z][a-zA-Z0-9]{0,39}$")


def _valid_ai_code(raw: str) -> str:
    """AI 给的字段编码是否合规（小驼峰、字母开头、无下划线、不超长）。"""
    s = (raw or "").strip()
    if not s or not _FIELD_CODE_RE.match(s):
        return ""
    # 全小写且超过 18 字符的，多半是拼音长串（shiFouGongYiJiShuLeiXiangMu 这类）
    # 单音节词如 name/code/description 不受影响
    if s.islower() and len(s) > 18:
        return ""
    return s


def _norm_field_code(raw: str, used: set[str], name: str = "") -> str:
    """字段编码：优先采纳 AI 给的语义化英文，不合规才退回中文名拼音。

    系统原生对象（project / task / contract）的字段是 name / start / stateId /
    handlerIds 这种短语义名，不是拼音。拼音串（shiFouGongYiJiShuLeiXiangMu）
    既长又不可读，只在 AI 没给出可用编码时兜底。
    """
    from . import naming
    ai = _valid_ai_code(raw)
    if ai and ai not in used and ai not in SYSTEM_CODES:
        used.add(ai)
        return ai
    if ai and (ai in used or ai in SYSTEM_CODES):
        # AI 的编码撞车或撞系统字段：加序号，比整串丢掉重来更好
        base, n = ai, 2
        while f"{base}{n}" in used or f"{base}{n}" in SYSTEM_CODES:
            n += 1
        got = f"{base}{n}"
        used.add(got)
        return got

    label = (name or "").strip()
    if label:
        base = naming.to_english(label)
        if base:
            s = base
            if len(s) > 2 and s.endswith("Id"):
                s = s[:-2]
            s = naming.unique(s, used)
            used.add(s)
            return s
    s = re.sub(r"[^A-Za-z0-9_]", "_", (raw or "").strip())
    s = re.sub(r"[_-]+([A-Za-z0-9])", lambda m: m.group(1).upper(), s)
    if s and s[0].isdigit():
        s = "f" + s
    s = s[:1].lower() + s[1:] if s else "field"
    if len(s) > 2 and s.endswith("Id") and s[:-2]:
        s = s[:-2]
    elif len(s) > 1 and s.endswith("ID"):
        s = s[:-2]
    base, n = s or "field", 2
    while s in used:
        s = f"{base}{n}"
        n += 1
    used.add(s)
    return s


SYSTEM_CODES = {"id", "tenantId", "ip", "deleted", "createBy", "createTime",
                "updateTime", "version", "updateBy"}

_DEFAULT_LEN = {"varchar": 200, "number": 20, "clob": None, "datetime": None,
                "user": 64, "department": 64, "autoNumber": 64, "attachment": None,
                "object": 64, "dic": 32}


def _as_pool(known) -> list[dict]:
    """兼容两种传参：对象清单（dict）或旧版的编码列表。"""
    if not known:
        return []
    if isinstance(known[0], dict):
        return [{"code": str(x.get("code") or ""), "name": str(x.get("name") or "")}
                for x in known if x.get("code")]
    return [{"code": str(c), "name": ""} for c in known]


def build_bo(spec: dict, known_objects: list | None = None,
             strict_objects: bool = False) -> tuple[Bo, list[str]]:
    """把 AI 给的 JSON 规整成 Bo。返回 (对象, 提示)。

    strict_objects=True（读非标准 Excel 时用）：object 字段的关联编码不在对象库里，
    不再"留着让人手工选"，而是**退回 dic（选项）**——
    依据是用户两次纠正：文档给的是一串固定取值、库里没有对应对象时，
    它就是个选项字段。留成 object 导入后是空下拉，比降级成文本更糟。
    """
    notices: list[str] = []
    pool = _as_pool(known_objects) or objects.catalog()
    known = {it["code"] for it in pool}

    name = (spec.get("name") or "").strip()
    code = _norm_code(spec.get("code") or name, name=name)
    name = name or code
    # 表名按手册规范取 PP_ + 编码。这里就补上，别等到 build 才发现为空——
    # 中间任何一步（预览/下载 Excel/直接出包）都要用到它。
    from .model import table_name_for
    bo = Bo(code=code, name=name, tbl_name=table_name_for(code),
            desc=(spec.get("desc") or "").strip())

    used: set[str] = set()
    for raw in (spec.get("fields") or []):
        ftype = (raw.get("type") or "varchar").strip()
        if ftype not in ALLOWED_TYPES:
            # 模型偶尔会回「单行文本」「下拉框」「数据字典」这种控件层叫法，
            # 先归一成类型代码；真不认识才降级——别把「数据字典」当不支持类型丢掉
            canon = fieldtypes.resolve(ftype)
            if canon in ALLOWED_TYPES:
                ftype = canon
            else:
                notices.append(
                    f"字段「{raw.get('name') or raw.get('code')}」类型 {ftype} 不支持，按字符串处理")
                ftype = "varchar"

        fcode = _norm_field_code(raw.get("code") or raw.get("name") or "", used,
                                 name=raw.get("name") or "")
        if fcode in SYSTEM_CODES:
            continue                      # 系统字段自动补，别重复

        object_type = (raw.get("objectType") or "").strip()
        if ftype == "dic" and not object_type:
            # 选项字段不要求模型给字典 key——按字段编码自动生成即可
            # （和模板说明一致：类型=选项 时「关联对象/字典」可留空）。
            # 早先这里跟 object 一起降级成文本，导致「状态」这种明显的枚举列被丢掉。
            object_type = fcode
        elif ftype == "object":
            if not object_type:
                # 关联对象没说指向谁，这个下拉没意义，降级成文本
                notices.append(
                    f"字段「{raw.get('name') or fcode}」是 object 类型但没说关联哪个对象，已按文本生成")
                ftype = "varchar"
            else:
                # AI 爱写 Project / 项目 / pp_project，统一归一到真实对象编码，
                # 否则导入后是个空下拉框——check 也只查非空，查不出来
                canon = objects.resolve(object_type, pool)
                if canon:
                    if canon != object_type:
                        label = objects.label(canon, pool) or canon
                        notices.append(
                            f"字段「{raw.get('name') or fcode}」的关联对象 {object_type} → {canon}（{label}）")
                    object_type = canon
                elif known and strict_objects:
                    # 库里有对象清单，但找不到这个编码 → 多半根本不是对象。
                    # 文档写的是一串固定取值（公司名/研究院/事业部），库里没有这些组织，
                    # 判成 object 导入后就是空下拉。退回「选项」，候选值由 _attach_values 补。
                    notices.append(
                        f"字段「{raw.get('name') or fcode}」按「下拉框」关联 {object_type} 在系统对象库里不存在，"
                        f"已改为「选项」——请在预览里确认候选值")
                    ftype = "dic"
                    object_type = fcode
                elif known:
                    notices.append(
                        f"字段「{raw.get('name') or fcode}」关联的对象 {object_type} 在系统对象清单里找不到，"
                        f"导入后需要手工选；如果它是新对象，请先把它建成对象再引用")

        length = raw.get("length")
        if not isinstance(length, int) or length <= 0:
            length = _DEFAULT_LEN.get(ftype)

        bo.fields.append(BoField(
            bo_code=code,
            code=fcode,
            name=(raw.get("name") or fcode).strip(),
            type_new=ftype,
            length=length,
            object_type=object_type,
            readonly=bool(raw.get("readonly")),
            choices=_clean_values(raw.get("values")),
            # 级联：AI 想表达「这个下拉只列父字段选过之后剩下的项」就填父字段编码
            cascade_parent=str(raw.get("cascadeParent") or "").strip(),
            sn=len(bo.fields) + 1,
        ))

    if not bo.fields:
        notices.append("这个对象一个字段都没生成出来，请把需求描述得更具体一点")
    return bo, notices


def _clean_values(raw) -> list[str]:
    """AI 给的候选值 → 干净的选项列表（选项字段才用得上）。

    放在 build_bo 里统一收口：以前只有走「读 Excel」那条路才 _attach_values，
    「一句话生成」和直接调 build_bo 的场景候选值会被丢掉。
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for v in raw:
        s = str(v).strip()[:40]
        if s and s not in out:
            out.append(s)
    return out[:30] if len(out) >= 2 else []


def generate(description: str, cfg: dict, known_objects: list | None = None,
             timeout: int = 90) -> dict:
    """一句话 → 业务对象。

    返回 {ok, bo, notices, model, seconds}；失败抛异常由调用方处理。
    """
    import time

    if not _pick_provider(cfg):
        raise llm.LlmError("还没配置大模型 API Key，无法用 AI 生成")

    pool = _as_pool(known_objects) or objects.ensure()
    extra = ""
    block = objects.prompt_block(None, pool)
    if block:
        extra = "\n\n" + block

    t0 = time.time()
    text, used = llm.chat_any(_fast_cfg(cfg), [
        {"role": "system", "content": SYSTEM + extra},
        {"role": "user", "content": description.strip()},
    ], timeout=timeout, max_tokens=3000)

    try:
        spec = _extract_json(text)
    except json.JSONDecodeError as exc:
        raise llm.LlmError(f"模型输出不是合法 JSON（{exc.msg}），请再试一次或把需求写简单些") from exc

    bo, notices = build_bo(spec, pool)
    return {
        "ok": True,
        "bo": bo,
        "notices": notices,
        "model": used.get("model", ""),
        "seconds": round(time.time() - t0, 2),
    }


def type_choices() -> list[dict]:
    """给前端下拉用的类型清单。

    label 用**字段层**的叫法（选项/下拉框），带上 dataTypeNew，
    和实施眼镜里看到的下拉一字不差；下拉里绝不出现「数据字典」「数据集」。
    """
    return [{"code": t, "label": fieldtypes.ui_label(t), "dataType": t}
            for t in ALLOWED_TYPES]


def to_dict(bo: Bo) -> dict:
    """给前端的结构，和 /api/parse 的 objects 保持一致，能直接复用预览渲染。"""
    return {
        "code": bo.code,
        "name": bo.name,
        "tbl": bo.tbl_name,
        "desc": bo.desc,
        "withLog": bo.with_log,
        "fields": [
            {
                "name": f.name,
                "code": f.code,
                "fieldName": f.resolved_field_name(),
                "type": f.type_new,
                "typeLabel": fieldtypes.ui_label(f.type_new),
                "length": f.length,
                "objectType": f.object_type,
                "index": f.is_index,
                "readonly": f.readonly,
                # 选项值：预览里要能编辑（从非标准 Excel 分析出来的 dic 字段会带上原表取值）
                "choices": list(f.choices or []),
                # 单选/多选：不给出去的话，前端预览和后续「再改一版」都会丢掉这个设置
                "isMultiple": bool(truthy((f.options or {}).get("isMultiple"))),
                # 级联父字段：不给出去，预览改一版就丢了
                "cascadeParent": f.cascade_parent or "",
            }
            for f in bo.fields
        ],
    }


def merge_into(project: Project, bo: Bo) -> list[str]:
    """把生成的对象并进现有 Project；对象编码重复时改名而不是覆盖。"""
    notices: list[str] = []
    if project.bo(bo.code):
        base, n = bo.code, 2
        while project.bo(f"{base}_{n}"):
            n += 1
        new_code = f"{base}_{n}"
        notices.append(f"对象编码 {base} 已存在，改用 {new_code}")
        for f in bo.fields:
            f.bo_code = new_code
        bo.code = new_code
    project.bos.append(bo)
    return notices
