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
from .model import (
    TABLE_COLUMN_TYPES,
    Bo,
    BoField,
    Project,
    TableColumn,
    truthy,
)

# 只让 AI 用这些类型——表/公式/国际化没做验证，别让它张口就来。
# 顺序照系统「添加字段 › 字段类型」下拉（用户截图给的 14 项，去掉没验证的 4 项）：
#   1 Str.字符串 2 数字 3 日期时间 4 大文本 5 用户 6 部门 7 附件列表
#   8 国际化   9 选项 10 对象  11 用户组 12 表格 13 自动编号 14 公式
# 2026-11-15：table（表格）拿到真实包核对后放开，前提是模型必须给出 columns
# （见 SYSTEM 第 10 条）；给不出列的子表导入后是一片空白，反而比没有更糟。
ALLOWED_TYPES = (
    "varchar", "number", "datetime", "clob", "user", "department",
    "attachment", "dic", "object", "table", "autoNumber",
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
10. 类型用 `table`（表格/子表）时，**必须同时给出 `columns`**，否则这字段会被降级成
   大文本（没有列的子表导入后是一片空白，比没有更糟）。每条列写：
   `{"name": "列名", "type": "varchar|number|date|datetime|clob|dic|user|department"}`
   可选 `precision`（数字列小数位）、`widthPercent`（列宽百分比）、`values`（选项列的候选值）。
   例：里程碑计划表 → `"columns": [{"name":"阶段","type":"varchar"},
   {"name":"计划开始时间","type":"date"},{"name":"是否里程碑","type":"dic","values":["是","否"]}]`。
   只有用户明确说「明细/多行/表格/子表」（如「里程碑计划」「预算明细」）时才用 table；
   普通的一对一字段不要用。
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
             description: str | None = None,
             trust_codes: bool = False) -> tuple[Bo, list[str]]:
    """把 AI 给的 JSON 规整成 Bo。返回 (对象, 提示)。

    strict_objects=True（读非标准 Excel 时用）：object 字段的关联编码不在对象库里，
    不再"留着让人手工选"，而是**退回 dic（选项）**——
    依据是用户两次纠正：文档给的是一串固定取值、库里没有对应对象时，
    它就是个选项字段。留成 object 导入后是空下拉，比降级成文本更糟。

    prefer_code=True（微调/显式改编码时用）：spec 里的 code 只要是合法英文编码就照用，
    不再按 name 重新推导。见 _norm_code 的说明。

    trust_codes=True（克隆时用）：字段编码来自线上真实对象包，一律保留——
    末尾的 pm_terms 原生口径改名（「状态」→ projectStatus 这种）会破坏
    「一模一样」的承诺，跳过。
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
        _flat, _groups, _tentative = _clean_values(
            raw.get("values"),
            cascade=bool(raw.get("cascadeParent")),
            groups_hint=raw.get("choiceGroups"),
        )

        # 表格字段（子表）：列定义由模型给，走和 Excel「2.1_表格列」同一条模型路径
        table_columns = _clean_columns(raw.get("columns"))
        if ftype == "table" and not table_columns:
            # 以前这里把字段直接改成 clob（大文本），理由是「空子表导入后一片空白」。
            # 但代价是**类型被悄悄改掉**：用户导入后拿到一个多行文本框，
            # 得删字段重建才能变成子表（2026-11-17 用户反馈
            # 「需求里有表格类型，导入 zip 后变成大文本」就是这条路径）。
            # 现在保留 table，先补一列占位顶住；真正的列由 fill_table_columns
            # 再问一次模型补出来（用户明确要求「列名你帮我加」）。
            _fname = (raw.get("name") or fcode).strip()
            table_columns = [TableColumn(
                name=_placeholder_column_name(_fname), input_type="varchar",
                sn=1, placeholder=True)]
        if ftype == "table":
            # 子表按 clob 落库，没有「长度」概念
            length = None
            object_type = ""

        bo.fields.append(BoField(
            bo_code=code,
            code=fcode,
            name=(raw.get("name") or fcode).strip(),
            type_new=ftype,
            length=length,
            object_type=object_type,
            readonly=bool(raw.get("readonly")),
            choices=_flat,
            choice_groups=(_groups or _tentative),
            # 级联：AI 想表达「这个下拉只列父字段选过之后剩下的项」就填父字段编码
            cascade_parent=str(raw.get("cascadeParent") or "").strip(),
            table_columns=table_columns,
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
    infer_cascade_parents(bo, notices)
    strip_parent_choices(bo, notices)
    # 最后再对齐原生口径：此时字段都已落定，改编码不会影响级联/字典的构造。
    # 克隆（trust_codes）例外：原对象编码是线上真实的，不改。
    if not trust_codes:
        from . import pm_terms
        notices.extend(pm_terms.apply(bo))
    return bo, notices


def infer_cascade_parents(bo: Bo, notices: list[str]) -> None:
    """候选值是分组写法、但没填 cascadeParent 时，按父值集合反查父字段。

    例（尹姣定立项表）：品种分类的分组键是 中药/化药/大健康/生物制品/N/A，
    同对象里「品种类别」的候选值正好是这 5 个 → 认定 品种类别 → 品种分类。
    只认「唯一一个字段的候选值覆盖全部父值」的情况；有歧义就不猜。
    """
    dic_fields = [f for f in bo.fields if f.type_new == "dic" and f.choices]
    for child in bo.fields:
        if child.type_new != "dic" or child.cascade_parent or not child.choice_groups:
            continue
        parents = set(child.choice_groups)
        if len(parents) < 2:
            continue
        hits = [p for p in dic_fields
                if p is not child and parents <= set(p.choices)]
        # 父字段一般定义在子字段前面；有多个候选时只保留之前的
        before = [p for p in hits if p.sn < child.sn]
        if before:
            hits = before
        if len(hits) != 1:
            # 认不到父字段：按普通候选值处理，别留下没有父级的分组
            child.choice_groups = {}
            continue
        parent = hits[0]
        child.cascade_parent = parent.code
        # 候选值改成子项（去重保序），父值只在分组映射里
        kids: list[str] = []
        for vals in child.choice_groups.values():
            for k in vals:
                if k not in kids:
                    kids.append(k)
        if kids:
            child.choices = kids
        notices.append(
            f"字段「{child.name}」的候选值是分组写法，"
            f"已按父值自动认到父字段「{parent.name}」（{parent.code}）")


def strip_parent_choices(bo: Bo, notices: list[str]) -> None:
    """子字段的候选值里混进了父字段的值 → 去掉。

    模型有时把「当品种类别=中药时，品种分类为 OTC/RX…」读成
    「品种分类 = 中药、OTC、RX…」，把父值也算成子字段的候选值。
    这里只在**重合很多**（≥2 个且覆盖父字段六成以上）时动手，避免误删
    恰好同名的单个业务值；有分组映射的字段不动（分组本身就说明清了）。
    """
    by_code = {f.code: f for f in bo.fields}
    for child in bo.fields:
        parent = by_code.get(child.cascade_parent or "")
        if parent is None or child.type_new != "dic" or child.choice_groups:
            continue
        if parent.type_new != "dic" or not parent.choices:
            continue
        overlap = [c for c in child.choices if c in parent.choices]
        if len(overlap) < 2 or len(overlap) < len(parent.choices) * 0.6:
            continue
        kept = [c for c in child.choices if c not in overlap]
        if not kept:
            continue
        child.choices = kept
        notices.append(
            f"字段「{child.name}」的候选值里混进了父字段「{parent.name}」的值"
            f"（{'、'.join(overlap[:4])}），已去掉；父值只在父字段里出现")


def _placeholder_column_name(field_name: str) -> str:
    """表格字段没拿到列定义时，给占位列取个像样的名字。

    「付款计划表」→「付款计划」、「预算明细」→「预算」，去掉的只是
    表明「这是个表」的后缀；实在啥都不剩就用「内容」。
    """
    name = re.sub(r"(表格|明细表|明细|子表|表)$", "", (field_name or "").strip()).strip()
    return (name or "内容")[:40]


def _clean_columns(raw) -> list[TableColumn]:
    """AI 给的表格列 → TableColumn 列表。

    宽容两种写法：`["名称","数量"]`（只给列名）与
    `[{"name":"名称","type":"varchar","precision":0,"values":[...]}]`。
    认不出的类型降级成字符串，不整条丢弃——丢列比降级更难发现。

    键名也要宽容：模型经常把 name/type 写成中文（列名/类型）或照数据库叫
    fieldName/dataType。只认英文键时整批列都会被丢掉，字段随后被判成
    「没给列」——2026-11-17 用户遇到的「表格变大文本」就有这一支。
    """
    if not isinstance(raw, list):
        return []
    out: list[TableColumn] = []
    for i, item in enumerate(raw, start=1):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(_first_of(item, "name", "label", "列名", "列名称", "字段名",
                            "fieldName", "title") or "").strip()[:40]
        if not name:
            continue
        typ = fieldtypes.resolve_column_type(
            _first_of(item, "type", "inputType", "列类型", "类型", "数据类型", "dataType")
            or "varchar")
        if typ not in TABLE_COLUMN_TYPES:
            typ = "varchar"
        values = [str(x).strip()[:40]
                  for x in (_first_of(item, "values", "choices", "选项值") or [])
                  if str(x).strip()]
        prec = item.get("precision")
        width = _first_of(item, "widthPercent", "宽度", "宽度%")
        out.append(TableColumn(
            name=name,
            input_type=typ,
            choices=values,
            precision=prec if isinstance(prec, int) else None,
            width_percent=float(width) if isinstance(width, (int, float)) else None,
            sn=i,
        ))
    return out


def _first_of(item: dict, *keys):
    """按顺序取第一个有值的键（AI 输出键名不稳定的兜底）。"""
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return v
    return None


_NA_RE = re.compile(r"(?<![A-Za-z])N/A(?![A-Za-z])", re.I)


def _split_choice_text(text: str) -> list[str]:
    """候选值分隔（、，,；;|/），但保留 N/A 这个完整取值。"""
    marks: list[str] = []

    def _hold(m):
        marks.append(m.group(0))
        return f"\x00{len(marks) - 1}\x00"

    held = _NA_RE.sub(_hold, text or "")
    return [re.sub(r"\x00(\d+)\x00", lambda m: marks[int(m.group(1))], x)
            for x in re.split(r"[,，;；|、/]+", held) if x.strip()]


def _clean_values(raw, cascade: bool = False, groups_hint=None
                  ) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    """AI 给的候选值 → (扁平列表, 分组映射, 试探性分组)。

    级联子字段（``cascade=True``）的分组写法「父值：子1、子2」：
      - 扁平列表**只放子项**，父值属于父字段，不能混进子字段的字典
        （2026-11-17 用户反馈：导入后子字段下拉里出现了「中药」「化药」这种父级值）；
      - 分组映射保留 父→子，供 gen_bo 写 dependValues。

    非级联字段不做分组：`L1：创新药` 这种带冒号的单条取值原样保留。
    但是「每一行都是 父：子、子」的形态会另存成试探性分组（第三个返回值），
    等所有字段建完后由 :func:`infer_cascade_parents` 用兄弟字段的候选值反查：
    父值全都出现在某个字段里 → 认定父子关系，子字段候选值改成子项。
    显式给了 ``choiceGroups`` 时以它为准（结构最可靠）。
    """
    groups: dict[str, list[str]] = {}
    tentative: dict[str, list[str]] = {}
    out: list[str] = []

    def _add(x: str) -> None:
        x = x.strip()[:40]
        if x and x not in out:
            out.append(x)

    def _kids(text: str) -> list[str]:
        return [k.strip()[:40] for k in _split_choice_text(str(text)) if k.strip()]

    # ① 模型直接给了 choiceGroups：扁平列表 = 所有子项
    if isinstance(groups_hint, dict) and groups_hint:
        for parent, kids in groups_hint.items():
            p = str(parent).strip()[:40]
            if not p or not isinstance(kids, (list, tuple)):
                continue
            bucket = groups.setdefault(p, [])
            for k in kids:
                k = str(k).strip()[:40]
                if k:
                    _add(k)
                    if k not in bucket:
                        bucket.append(k)
        return (out[:50] if len(out) >= 2 else []), groups, {}

    if not isinstance(raw, list):
        return [], {}, {}
    for v in raw:
        s = str(v).strip()
        if not s:
            continue
        m = re.match(r"^([^：:>＞]{1,20})[：:>＞](.+)$", s)
        if m:
            parent, kids = m.group(1).strip(), _kids(m.group(2))
            if kids:
                bucket = tentative.setdefault(parent, [])
                for k in kids:
                    if k not in bucket:
                        bucket.append(k)
        # 「L1：创新药、改良型新药项目」这种是**带代码前缀的单个取值**，
        # 不是「父：子」分组（父值 L1 不可能是另一个下拉的候选值）。
        code_prefix = bool(re.fullmatch(r"[A-Za-z]{1,3}\d{1,2}", m.group(1).strip())) if m else False
        if cascade and m and kids and not code_prefix:
            for k in kids:
                _add(k)
            continue
        _add(s)
    return (out[:50] if len(out) >= 2 else []), groups, tentative


_CASCADE_SENTENCE = re.compile(
    r"(?:当|若|如果)\s*"
    r"(?P<parent>[^，。；：:\s]{1,12}?)\s*(?:字段)?\s*(?:的)?\s*"
    r"(?:下拉)?(?:候选值|选项值|选项|取值)?\s*(?:为|是|选择|选)\s*"
    r"[“\"']?(?P<pval>[^”\"',，。；：:\s]{1,20}?)[”\"']?\s*(?:时|，|,)\s*[，,]?\s*"
    r"(?P<child>[^，。；：:\s]{1,12}?)\s*(?:字段)?\s*(?:的)?\s*"
    r"(?:下拉值|下拉|候选值|选项值|选项|取值)\s*(?:为|是|包括|有|[:：])\s*"
    r"(?P<kids>[^。；]+)")


def parse_conditional_cascades(text: str) -> list[dict]:
    """从自然语言里抽「当 A 为 X 时，B 的下拉值为 Y」。

    返回 [{parent, parentValue, child, kids}]。文档/PDF 里常见的写法：
      当「品种类别」为中药时，品种分类的下拉值为 OTC、RX、双跨、其他
    """
    out: list[dict] = []
    for m in _CASCADE_SENTENCE.finditer(str(text or "")):
        kids = [k.strip() for k in _split_choice_text(m.group("kids"))
                if k.strip()]
        if not kids:
            continue
        def _clean(x: str) -> str:
            return x.strip().strip("「」“”\"'")

        out.append({
            "parent": _clean(m.group("parent")),
            "parentValue": _clean(m.group("pval")),
            "child": _clean(m.group("child")),
            "kids": [_clean(k) for k in kids],
        })
    return out


def _apply_text_cascades(spec: dict, hints: list[dict]) -> None:
    """把句子级联盖到 spec：填 cascadeParent + 合并 choiceGroups。"""
    if not hints:
        return
    raw_fields = spec.get("fields") or (spec.get("bo") or {}).get("fields") or []
    by_name = {}
    for raw in raw_fields:
        if isinstance(raw, dict):
            nm = str(raw.get("name") or "").strip()
            if nm:
                by_name.setdefault(nm, raw)
    for h in hints:
        parent = by_name.get(h["parent"])
        child = by_name.get(h["child"])
        if parent is None or child is None:
            continue
        if not child.get("cascadeParent") and parent.get("code"):
            child["cascadeParent"] = parent["code"]
        groups = child.get("choiceGroups")
        if not isinstance(groups, dict):
            groups = {}
        bucket = groups.setdefault(h["parentValue"], [])
        for k in h["kids"]:
            if k not in bucket:
                bucket.append(k)
        child["choiceGroups"] = groups


def _cascade_prompt(hints: list[dict]) -> str:
    if not hints:
        return ""
    lines = ["", "===== 已识别的级联关系（必须照此生成）====="]
    for h in hints:
        lines.append(
            f"- 「{h['parent']}」= {h['parentValue']} → 「{h['child']}」只能是："
            f"{'、'.join(h['kids'])}")
    lines.append("子字段的 cascadeParent 填父字段编码；候选值只写子项，"
                 "父值不要写进子字段的 values。")
    return "\n".join(lines)


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
    hints = parse_conditional_cascades(description)
    from . import pm_terms
    text, used = llm.chat_any(_fast_cfg(cfg), [
        {"role": "system", "content": SYSTEM + "\n\n" + pm_terms.PROMPT + extra},
        {"role": "user", "content": description.strip() + _cascade_prompt(hints)},
    ], timeout=timeout, max_tokens=_budget)

    try:
        spec = _extract_json(text)
    except json.JSONDecodeError as exc:
        raise llm.LlmError(f"模型输出不是合法 JSON（{exc.msg}），请再试一次或把需求写简单些") from exc

    _apply_text_cascades(spec, hints)
    bo, notices = build_bo(spec, pool, description=description)
    # 模型往往只写「这是表格」不给列，按需求再问一次把列补出来
    notices.extend(fill_table_columns(bo, cfg, description=description))
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


# ---------------------------------------------------------------- 表格列补齐
#
# 用户 2026-11-17：「表格里面的列名你不会帮我添加吗」。模型（尤其快模型）
# 经常只回 `type: table` 而不写 columns。让用户自己到「2.1_表格列」一行行补
# 不合理——正确的做法是就着子表名 + 需求原文**再问一次模型要列**：
# 输入小、输出小，一次能把好几张子表一起补完。和 fix_cascade_groups 同一套路。

_COLUMN_SYSTEM = """你是 PowerProject 系统的表单设计师。用户给了几张「表格」字段（子表），\
你要按业务常识和需求原文，把每张子表的列设计出来。

只输出 JSON，不要解释，形如：
{"里程碑计划表": [{"name": "阶段", "type": "varchar"},
                 {"name": "计划开始时间", "type": "date"},
                 {"name": "金额", "type": "number", "precision": 2},
                 {"name": "是否里程碑", "type": "dic", "values": ["是", "否"]}]}

规则：
1. 列名用中文，2~8 个字，是业务上真实会填的列；不要写「内容」「列1」这种占位。
2. type 只能是 varchar / number / date / datetime / dic / user / department。
3. dic 列必须给 values（候选值数组）；number 列可给 precision（小数位）。
4. 每张子表 4~8 列，按填写顺序排列；不要加「序号」「操作」「合计」这类系统列。
5. 需求原文里明确提到的列，一个都不能漏；没提到的，按该子表的业务含义补齐。
6. 键用上面给你的子表名称原文。"""


def fill_table_columns(bo: Bo, cfg: dict, description: str = "",
                       hint: str = "", timeout: int = 60) -> list[str]:
    """表格字段缺列时，单独问一次模型把列补出来。

    只处理「一列都没有」或「只有占位列」的表格字段。已经有真实列的
    （模型自己给了 / Excel 里填了 / 反解出来的）一律不动。
    失败不影响出包——占位列兜着，只是提示用户手工补。
    """
    notices: list[str] = []
    targets = [f for f in bo.fields
               if f.type_new == "table" and _columns_missing(f)]
    if not targets:
        return notices
    if not _pick_provider(cfg):
        notices.append(
            "表格列没自动生成（还没配大模型）："
            + "、".join(f.name for f in targets)
            + " 请在「2.1_表格列」补列")
        return notices

    lines = [f"业务对象：{bo.name}（{bo.code}）", "需要设计列的表格字段："]
    for f in targets:
        lines.append(f"- {f.name}（编码 {f.code}）")
    ctx = (description or "").strip()
    if hint:
        ctx = (ctx + "\n" + hint).strip()
    if ctx:
        lines.append("\n需求原文：\n" + ctx[:2000])
    lines.append('\n请只输出 JSON：{"子表名称": [{"name": "列名", "type": "varchar"}, ...]}')
    prompt = "\n".join(lines)

    data = None
    # deepseek-flash 是推理模型，max_tokens 给小了 content 会是空串
    # （同 fix_cascade_groups 的实测），所以给 4000 + 空返回重试。
    for _ in range(3):
        try:
            text, _used = llm.chat_any(_fast_cfg(cfg), [
                {"role": "system", "content": _COLUMN_SYSTEM},
                {"role": "user", "content": prompt},
            ], timeout=timeout, max_tokens=4000)
        except Exception:
            break
        if not (text or "").strip():
            continue
        try:
            data = _extract_json(text)
            break
        except json.JSONDecodeError:
            continue
    if data is None:
        notices.append(
            "⚠ 表格列没自动生成（模型没返回可用结果）："
            + "、".join(f.name for f in targets) + " 请到「2.1_表格列」补列")
        return notices

    done: list[str] = []
    missed: list[str] = []
    for f in targets:
        got = _clean_columns(_columns_from_reply(data, f))
        if got:
            f.table_columns = got          # 占位列整批换掉
            head = "、".join(c.name for c in got[:5]) + ("…" if len(got) > 5 else "")
            done.append(f"「{f.name}」{len(got)} 列（{head}）")
        else:
            missed.append(f.name)
    if done:
        notices.append("表格列已按需求自动生成：" + "；".join(done)
                       + "——导入前可在预览里核对/增删")
    if missed:
        notices.append("⚠ 这些表格还是没生成出列：" + "、".join(missed)
                       + "，请到「2.1_表格列」补")
    return notices


def _columns_missing(field) -> bool:
    """这个表格字段是否还等着补列（无列，或只有一列占位）。"""
    cols = list(getattr(field, "table_columns", None) or [])
    return not cols or all(getattr(c, "placeholder", False) for c in cols)


def _columns_from_reply(data, field) -> list:
    """从模型回复里挑出这个子表的列。

    键可能是字段名、字段编码，也可能整包成 {"tables":[{name, columns}]}。
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in (field.name, field.code):
        got = data.get(key)
        if isinstance(got, list):
            return got
        if isinstance(got, dict) and isinstance(got.get("columns"), list):
            return got["columns"]
    for box in ("tables", "fields", "columns"):
        seq = data.get(box)
        if not isinstance(seq, list):
            continue
        for item in seq:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or item.get("code") or "") in (field.name, field.code):
                return item.get("columns") or []
    return []


def type_choices() -> list[dict]:
    """给前端下拉用的类型清单。

    label 用**字段层**的叫法（选项/下拉框），带上 dataTypeNew，
    和实施眼镜里看到的下拉一字不差；下拉里绝不出现「数据字典」「数据集」。
    """
    return [{"code": t, "label": fieldtypes.ui_label(t), "dataType": t}
            for t in ALLOWED_TYPES]


def to_dict(bo: Bo) -> dict:
    """给前端的结构，和 /api/parse 的 objects 保持一致，能直接复用预览渲染。

    两个「XX选择」相关口径（用户 2026-11-20 定的，改这里等于改产品）：
      - **这个字段排第一**：前端是按数组顺序渲染的（renderAiBo 直接 map），
        所以「置顶」只能在这里做排序，不能指望它天然在最前面——它是**最后**补的
        （sn = max+1），排在数组末尾才是它的自然位置；
        注意只改**预览顺序**，不动 ``sn``：sn 决定物理列顺序，存量包一改
        就等于要求线上 ALTER。
      - **带上 isLink**：前端据此显示「关联字段」标记；预览回传（导出 Excel）也认它，
        否则 ai_apply_bo 又把它当普通 object 字段，出包时丢掉置顶与值来源口径。
    """
    return {
        "code": bo.code,
        "name": bo.name,
        "tbl": bo.tbl_name,
        "desc": bo.desc,
        "withLog": bo.with_log,
        "projectLink": bool(getattr(bo, "project_link", False)),
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
                # 是不是「XX选择」链接字段（项目选择 / 合同选择）
                "isLink": bool(getattr(f, "is_link", False)),
                # 选项值：预览里要能编辑（从非标准 Excel 分析出来的 dic 字段会带上原表取值）
                "choices": list(f.choices or []),
                # 单选/多选：不给出去的话，前端预览和后续「再改一版」都会丢掉这个设置
                "isMultiple": bool(truthy((f.options or {}).get("isMultiple"))),
                # 级联父字段：不给出去，预览改一版就丢了
                "cascadeParent": f.cascade_parent or "",
                # 级联分组（父值→子值）：预览往返 / 再改一版同样不能丢，
                # 丢了出包就写不出 dependValues（2026-11-12 品种分类实测）
                "choiceGroups": dict(f.choice_groups or {}),
                # 表格字段的子列：预览里要能看到「这张子表有几列、分别是什么」，
                # 往返时也不能丢，否则「再改一版」后子表就空了
                "columns": [
                    {"name": c.name, "type": c.input_type,
                     "typeLabel": fieldtypes.COLUMN_LABELS.get(c.input_type, c.input_type),
                     "choices": list(c.choices or []),
                     "precision": c.precision,
                     "widthPercent": c.width_percent,
                     "required": bool(c.required),
                     "multiple": bool(c.multiple)}
                    for c in (f.table_columns or [])
                ],
            }
            # 链接字段置顶（只影响预览数组顺序，sn 原样不动，见上面的说明）
            for f in sorted(bo.fields,
                            key=lambda x: (0 if getattr(x, "is_link", False) else 1, x.sn))
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
