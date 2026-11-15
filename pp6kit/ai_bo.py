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
4. **只生成用户描述里明确提到的业务字段。** 用户没提的字段一个都不要加——
   不要默认补「合同类型、状态、备注、附件、生效日期、到期日期、承办部门、
   关联项目」这类常见字段；字段数量不设下限，用户说几个就是几个。
   系统字段由生成器自动补，不要输出。
   **字段名用用户的原词，不要扩写/拆写：** 用户说「预算」就生成「预算」，
   不要生成「预算名称/预算金额」；说「合同」就生成「合同」，不要补
   「合同名称/合同编号」。只有描述里明确写了「名称/编号/金额/日期」等后缀，
   才生成带后缀的字段。
5. **不要自动生成「关联项目 / 所属项目」这类链接字段。** 只有用户明确说
   「关联项目 / 所属项目」时才生成 object 字段；应用在勾选「关联项目」时
   会自动补 projectId，不需要 AI 生成。
6. 「关联项目」「关联客户」「关联合同」这类**指向系统里别的业务对象**的，用 object（对象），
   objectType 填**下面给定的对象编码**（如 project / customer / contract）。
   编码必须从给定清单里选，不要自造，也不要填中文名——填错导入后是一个空下拉，什么也选不到。
   指向「人」的不要用 object，用 user；指向「部门」的用 department。
   **对象清单里没有对应条目的，不要硬套 object**——比如「需求单位」写了 20 个公司名，
   系统里没有这些组织，就该是 dic（选项）+ 候选值。
7. 带「单号」「编号」且要自动生成的，用 autoNumber
8. `readonly` 默认 false；只有字段是「只读展示/自动带出/自动生成」时才设 true，
   要人填写或选择的字段不要设只读。
9. 两个字段之间是「父级选了 A，子级只剩 A 下面那几项」的取值关系时，
   **在子字段上加 `cascadeParent` = 父字段的 code**（如 品种分类 → "productCategory"）。
   例：品种类别/品种分类、战略类别/项目注册类别、省市/区县。
   只在确实是父子取值关系时加；两个互不相干的下拉不要加，并列的也不要互相指。
   父字段可以是「选项」也可以是「关联对象」。加了之后，表单控件会自动继承，不用重复说明。
   **子字段的候选值按分组写**：一条 value = 「父值：子1、子2」，
   例 `"values": ["中药：OTC、RX、双跨、其他", "化药：OTC、RX、双跨、其他", "大健康：大健康"]`。
   这样出包时每个子项会带上自己属于哪些父值的级联值（dependValues），
   导入后选「中药」子级就只剩 OTC/RX/双跨/其他。**不要把分组拍平**——
   拍平后级联声明还在，但过滤不出任何东西。
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
        流程定义   {"workflows":[{"nodes":[...], "edges":[...]}]} ← ai_wf
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
        if isinstance(got, dict) and (got.get("fields") or got.get("groups")
                                      or got.get("workflows")):
            return got
    return None


def _norm_code(raw: str, fallback: str = "biz", name: str = "",
               prefer_explicit: bool = False) -> str:
    """对象编码：中文名 → 英文小写下划线，对齐系统真实对象（project_create）。

    AI 有时会把整个标题当名字（「项目立项信息表单」→ liuxianglingshixinxibiaodan），
    所以**首次生成**时只要给了 name，就一律以 name 为准重新生成，不采信 raw。
    用 object_code 而不是拼音：对象编码会派生物理表名 pp_<code>，
    拼音出来的 pp_xiang_mu 在线上一个都找不到。

    prefer_explicit=True（微调场景）时反过来：raw 只要语法合法就照用。
    原因（2026-11-12 用户反馈「让 AI 改编码，每次都说改好了但页面还是旧编码」）：
    微调时模型会返回 {"code": "想改成的编码", "name": "对象名"}，而 name 非空时
    上面的规则会把 code 重新按 name 推一遍——用户改成什么都会被悄悄改回旧值，
    模型却回答「改好了」。同一个字段编码（_norm_field_code）早就采纳显式值，
    对象编码没道理例外。
    """
    from . import naming
    if prefer_explicit:
        s = _clean_code(raw)
        if s:
            return s
    label = (name or "").strip()
    if label:
        # 「XX表单」「XX表」这类尾巴对编码没意义
        clean = re.sub(r"(表单|表)$", "", label).strip() or label
        base = naming.object_code(clean)
        if base:
            return base
    return _clean_code(raw) or fallback


_OBJECT_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _clean_code(raw: str) -> str:
    """把 raw 规整成合法对象编码；不像人写的英文编码就返回空。"""
    s = (raw or "").strip()
    if not s:
        return ""
    # 驼峰写法先转下划线（ContractV2 → contract_v2），否则只能整串小写成 contractv2
    if "_" not in s and any(c.isupper() for c in s):
        from .model import camel_to_snake
        s = camel_to_snake(s)
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    if not s or not s[0].isalpha() or not _OBJECT_CODE_RE.match(s):
        return ""
    # 拼音长串：一长串下落字母、中间没有下划线（liuxianglingshixinxibiaodan）。
    # 真实对象编码几乎都带下划线（project_create / contract / eq_ledger），
    # 而 AI 把整段中文标题当名字时产出的就是这种串——判为不合规，交给 name 推导。
    if "_" not in s and len(s) > 18:
        return ""
    # 超长的（带下划线也算）同样不可信：物理表名会跟着变长
    if len(s) > 40:
        return ""
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


_FIELD_SUFFIXES = (
    "名称", "编号", "金额", "日期", "时间", "状态", "备注", "附件", "类型",
    "部门", "人员", "数量", "说明", "描述", "单号", "类别", "分类",
)


def _norm_cn(value: str) -> str:
    """只留中文/字母/数字，去掉空格和标点，用于「描述里到底提没提这个词」。"""
    return re.sub(r"[^\u4e00-\u9fa5a-z0-9]", "", str(value or "").lower())


def _mentioned(name: str, description: str) -> bool:
    """字段名是否真的出现在用户描述里。

    只认**完整名字**，或「描述里既有词根、也明确出现了后缀」的组合。
    这样用户说「记录预算」时，AI 生成的「预算名称/预算金额」会被丢掉——
    用户没提名称/金额，不能替他把一个词拆成两个字段。
    """
    a, d = _norm_cn(name), _norm_cn(description)
    if not a or not d:
        return True
    if a in d:
        return True
    for suffix in _FIELD_SUFFIXES:
        if len(a) > len(suffix) and a.endswith(suffix):
            base = a[:-len(suffix)]
            if base in d and suffix in d:
                return True
    return False


def _filter_mentioned(fields: list[dict], description: str) -> tuple[list[dict], list[str]]:
    """只保留用户描述里明确提到的字段；返回 (保留, 被丢弃的字段名)。"""
    kept: list[dict] = []
    dropped: list[str] = []
    for raw in fields:
        name = str(raw.get("name") or "").strip()
        if not name or _mentioned(name, description):
            kept.append(raw)
        else:
            dropped.append(name)
    return kept, dropped


def build_bo(spec: dict, known_objects: list | None = None,
             strict_objects: bool = False,
             prefer_code: bool = False,
             description: str | None = None) -> tuple[Bo, list[str]]:
    """把 AI 给的 JSON 规整成 Bo。返回 (对象, 提示)。

    strict_objects=True（读非标准 Excel 时用）：object 字段的关联编码不在对象库里，
    不再"留着让人手工选"，而是**退回 dic（选项）**——
    依据是用户两次纠正：文档给的是一串固定取值、库里没有对应对象时，
    它就是个选项字段。留成 object 导入后是空下拉，比降级成文本更糟。

    prefer_code=True（微调/显式改编码时用）：spec 里的 code 只要是合法英文编码就照用，
    不再按 name 重新推导。见 _norm_code 的说明。
    """
    notices: list[str] = []
    pool = _as_pool(known_objects) or objects.catalog()
    known = {it["code"] for it in pool}

    name = (spec.get("name") or "").strip()
    code = _norm_code(spec.get("code") or name, name=name,
                      prefer_explicit=prefer_code)
    name = name or code
    # 表名按手册规范取 PP_ + 编码。这里就补上，别等到 build 才发现为空——
    # 中间任何一步（预览/下载 Excel/直接出包）都要用到它。
    from .model import table_name_for
    bo = Bo(code=code, name=name, tbl_name=table_name_for(code),
            desc=(spec.get("desc") or "").strip())

    raw_fields = list(spec.get("fields") or [])
    if description:
        raw_fields, dropped = _filter_mentioned(raw_fields, description)
        if dropped:
            notices.append(
                f"按你的描述只保留明确提到的字段，已忽略 AI 额外建议的 {len(dropped)} 个："
                + "、".join(dropped[:8])
                + ("…" if len(dropped) > 8 else "")
            )
    used: set[str] = set()
    for raw in raw_fields:
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
        if ftype == "dic":
            # 选项字段不要求模型给字典 key——按字段编码自动生成即可
            # （和模板说明一致：类型=选项 时「关联对象/字典」可留空）。
            # 早先这里跟 object 一起降级成文本，导致「状态」这种明显的枚举列被丢掉。
            #
            # ⚠ 这里必须是**完整字典 key**（对象编码_字段编码），不能只放字段编码：
            # object_type 会被 to_dict 带到预览、再被导出当 valueSource 用。
            # 2026-11-12 用户截图：放裸字段编码导致整包 dic 控件的取值来源全错
            # （报「引用的字典在对象里不存在」，正确 key 应为 project_apply_applyUnit）。
            # dict_key() == bo_code + "_" + code，与 _dict_blocks 生成字典用同一规则。
            object_type = f"{code}_{fcode}"
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

        # 选项值：AI 从需求文本里抽候选值时，分组写法会原样带过来
        # （「中药：OTC、RX、双跨、其他」一行一条），必须同时保留扁平列表和分组映射，
        # 否则出包只能出成 6 个不相关联的候选值，级联永远不会过滤
        # （2026-11-12 用户实测：导入后品种分类随品种类别不联动）。
        _flat, _groups = _clean_values(raw.get("values"))

        bo.fields.append(BoField(
            bo_code=code,
            code=fcode,
            name=(raw.get("name") or fcode).strip(),
            type_new=ftype,
            length=length,
            object_type=object_type,
            readonly=bool(raw.get("readonly")),
            choices=_flat,
            choice_groups=_groups,
            # 级联：AI 想表达「这个下拉只列父字段选过之后剩下的项」就填父字段编码
            cascade_parent=str(raw.get("cascadeParent") or "").strip(),
            sn=len(bo.fields) + 1,
        ))

    if not bo.fields:
        notices.append("这个对象一个字段都没生成出来，请把需求描述得更具体一点")

    # 级联父级归一：模型给的 cascadeParent 可能是中文字段名（「品种类别」）
    # 或自造编码（varietyCategory），只要能在本对象里按**字段名**对上就修正。
    # 对不上也留原值——validate 会拦「父级不在本表单」，但这里先给条 notice。
    codes = {f.code for f in bo.fields}
    by_name = {f.name: f.code for f in bo.fields}
    for f in bo.fields:
        p = (f.cascade_parent or "").strip()
        if not p or p in codes:
            continue
        hit = by_name.get(p)
        if hit:
            f.cascade_parent = hit
            notices.append(f"字段「{f.name}」的级联父级「{p}」按字段名对到 {hit}")
        else:
            notices.append(
                f"⚠ 字段「{f.name}」的级联父级「{p}」在本对象里不存在"
                f"（既不是字段编码也不是字段名），请预览里改成正确编码")
    return bo, notices


def _clean_values(raw) -> tuple[list[str], dict[str, list[str]]]:
    """AI 给的候选值 → (扁平列表, 分组映射)。

    扁平列表是所有候选值（供 choices 用）；分组映射只在条目里出现
    「父：子1、子2」时才非空，给 choice_groups 用。

    放在 build_bo 里统一收口：以前只有走「读 Excel」那条路才 _attach_values，
    「一句话生成」和直接调 build_bo 的场景候选值会被丢掉。
    分组解析与 reader._parse_choice_groups 同一套规则（用户 2026-11-12 的
    「品种分类随品种类别联动」就是靠它保住分组）。
    """
    if not isinstance(raw, list):
        return [], {}
    out: list[str] = []
    groups: dict[str, list[str]] = {}

    def _add(x: str) -> None:
        x = x.strip()[:40]
        if x and x not in out:
            out.append(x)

    for v in raw:
        s = str(v).strip()
        if not s:
            continue
        m = re.match(r"^([^：:>＞]{1,20})[：:>＞](.+)$", s)
        if m:
            parent, kids = m.group(1).strip(), m.group(2)
            _add(parent)
            bucket = groups.setdefault(parent, [])
            for k in re.split(r"[,，;；|、/]+", kids):
                k = k.strip()[:40]
                if k:
                    _add(k)
                    if k not in bucket:
                        bucket.append(k)
            continue
        _add(s)
    return (out[:30] if len(out) >= 2 else []), groups


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
    # 一句话生成也可能要 30+ 字段，3000 会截断成半个 JSON（同 ai_sheet/ai_refine
    # 的教训）。按描述长度估预算：中文描述里字段数大约是顿号/分句数的量级，
    # 每 8 个字 ≈ 一个字段 × 240 token，再带 2000 推理余量。
    _budget = max(8000, min(32000, 4000 + len(description) // 8 * 240))
    text, used = llm.chat_any(_fast_cfg(cfg), [
        {"role": "system", "content": SYSTEM + extra},
        {"role": "user", "content": description.strip()},
    ], timeout=timeout, max_tokens=_budget)

    try:
        spec = _extract_json(text)
    except json.JSONDecodeError as exc:
        raise llm.LlmError(f"模型输出不是合法 JSON（{exc.msg}），请再试一次或把需求写简单些") from exc

    bo, notices = build_bo(spec, pool, description=description)
    return {
        "ok": True,
        "bo": bo,
        "notices": notices,
        "model": used.get("model", ""),
        "seconds": round(time.time() - t0, 2),
    }


def fix_cascade_groups(bo: Bo, cfg: dict, timeout: int = 60) -> list[str]:
    """级联子字段的候选值被模型拍平（没有分组）时，单独问一次模型要归属。

    为什么需要这一步（2026-11-12 尹姣定表实测）：提示词要求「候选值按分组写」，
    但模型仍可能输出 ["中药","OTC","RX",...] 的拍平数组——分组一丢，
    出包就写不出 dependValues，导入后级联不联动。与其再赌一次完整重生成，
    不如就着已有候选值单独要一份映射，输入小、输出小、基本不会截断。

    只处理「有 cascadeParent、有父子双方候选值、但没有 choice_groups」的字段；
    输出的 groups 会过滤到父子候选值的交集上，防止模型编造。
    """
    notices: list[str] = []
    for f in bo.fields:
        if f.type_new != "dic" or not f.cascade_parent or f.choice_groups:
            continue
        if not f.choices:
            continue
        parent = bo.field_by_code(f.cascade_parent)
        if parent is None or not parent.choices:
            continue

        prompt = (
            "下面是同一表单里两个下拉字段的候选值。\n"
            f"父字段《{parent.name}》：{'、'.join(parent.choices)}\n"
            f"子字段《{f.name}》：{'、'.join(f.choices)}\n\n"
            "业务规则：选中父字段的某个值后，子字段只显示属于该父值的候选值；"
            "一个子值可以同时属于多个父值。子值里如果混进了与父值同名的项"
            "（常见于把父值也当成子值列了一遍），忽略它，不要输出。\n"
            '请只输出 JSON：{"groups": {"父值原文": ["子值1", "子2"]}}，'
            "键必须是父字段候选值的原文，值必须是子字段候选值的原文，不要编造。"
        )
        try:
            text = ""
            spec = {}
            # deepseek-flash 是推理模型：实测一次调用 reasoning_tokens 就烧了
            # 1853，max_tokens 给小了 content 直接是空串（2026-11-12 实测三次
            # 两空一成）。预算 4000 + 失败重试，双保险。
            for attempt in range(3):
                text, _ = llm.chat_any(_fast_cfg(cfg), [
                    {"role": "system", "content": "你是配置助手，只输出 JSON，不要解释。"},
                    {"role": "user", "content": prompt},
                ], timeout=timeout, max_tokens=4000)
                if not (text or "").strip():
                    continue
                try:
                    spec = _extract_json(text)
                    break
                except json.JSONDecodeError:
                    continue
            raw_groups = (spec or {}).get("groups") or {}
            valid_parents = set(parent.choices)
            valid_children = set(f.choices)
            clean: dict[str, list[str]] = {}
            for p, kids in raw_groups.items():
                p = str(p).strip()
                # 键必须是父字段的候选值。注意**不能**额外要求「p 不在子候选值里」：
                # 拍平场景下子字段 choices 混着父值，这个条件会把所有合法组键
                # 全部误杀（2026-11-12 实测：修复一直空转就是因为这个）。
                if p not in valid_parents:
                    continue
                ks: list[str] = []
                for k in kids or []:
                    k = str(k).strip()
                    if k in valid_children and k not in ks:
                        ks.append(k)
                if ks:
                    clean[p] = ks
            if clean:
                f.choice_groups = clean
                covered = {k for ks in clean.values() for k in ks}
                orphan = [c for c in f.choices
                          if c not in covered and c not in valid_parents]
                msg = (f"字段「{f.name}」的级联分组已按候选值补齐"
                       f"（{len(clean)} 组，覆盖 {len(covered)} 个子项）")
                if orphan:
                    msg += f"；这些子项没归属，导入后任何父值下都不显示：{'、'.join(orphan)}"
                notices.append(msg)
            else:
                notices.append(
                    f"⚠ 字段「{f.name}」的级联分组没问到（模型没有返回可用的归属关系），"
                    f"候选值按平铺生成，导入后父级选完子级仍会全量显示；"
                    f"可在预览里把候选值改成「父值：子1、子2」的分组写法后重试")
        except Exception:
            notices.append(
                f"⚠ 字段「{f.name}」的级联分组补齐失败，候选值按平铺生成，"
                f"导入后级联不会过滤；可在预览里手动改成「父值：子1、子2」分组写法")
    return notices


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
                # 级联分组（父值→子值）：预览往返 / 再改一版同样不能丢，
                # 丢了出包就写不出 dependValues（2026-11-12 品种分类实测）
                "choiceGroups": dict(f.choice_groups or {}),
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
