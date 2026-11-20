"""业务对象「关联项目」与项目字段对齐。

与 relations.py 同构的三段式：analyze（只读分析）→ parse_decisions（请求头）
→ apply（服务端重校验后写模型）。红线：
  1. analyze 绝不修改 Project；
  2. 相似字段**默认也照项目定义覆盖**（2026-11-14 用户口径：勾了「关联项目」就按
     pp_project 的定义原样覆盖，不再逐条弹窗问）；差异在 analyze 里算成 diff，
     由前端事后出一个「pp_project ↔ 你的 Excel」对照弹窗让用户确认；
  3. 快照不可用时只补链接字段、不阻断出包（识别是增强，不是门槛）。

为什么单独一个模块（架构 D1）
-----------------------------
builder.py 已经承担 6 件事（补系统字段/补流程字段/展开表单/出包/命名/打包），
再塞「跨对象字段池比对 + 改名 + 引用重写」进去没人敢动。这里只做一件事：
把「某个对象的字段定义」对齐成「项目对象对应字段的定义」，并把按编码引用的地方
一起改掉。

两处硬约束（写死在实现里，勿改）
-------------------------------
1. **存量包永不改 code / fieldName / sn**（patch.py 铁律：改列名等于要求线上 ALTER）。
   同步包模式下 code/fieldName 会被对齐成项目侧的值——那是**新表**，不涉及 ALTER。
2. **GLOBAL 字典照抄项目侧 options，不合成 optionSource**（架构 R4）。
   线上真实字段 project.huiBaoLeiXing 的 options 就是
   ``{"isMultiple":false,"isDefault":false}``，合成 ``optionSource`` 会造出一个
   线上不存在的形态（实测证据见 docs/架构-项目关联字段-20261114.md §0.3 F-C）。
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import fieldtypes as _ft
from .model import Bo, BoField, camel_to_snake
from .resources import resource_root, work_dir

# ---------------------------------------------------------------- 常量

PROJECT_CODE = "project"
PROJECT_TBL = "pp_project"

# 链接字段（指向项目对象的 object 字段）。10 个结构键与系统原生
# ``project_create.projectId`` 逐键一致（PRD F3 实证：sn=74 那条）。
# 只有 name 按用户 2026-11-20 的口径改成「项目选择」：
#   「强制在他的字段里加一个『XX 选择』控件；关联项目 = 项目选择，
#     关联其他对象 = 合同选择」。
# ⚠ code / fieldName **不跟着改**（仍是 projectId / project_id_）：存量包
#   一改列名就等于要求线上 ALTER，用户原话只要求显示名。
LINK_CODE = "projectId"
LINK_FIELD_NAME = "project_id_"
LINK_DEFAULT_NAME = "项目选择"
# 6 键与 project_create.projectId 逐键一致（dataTypeNew=object / datatype=varchar /
# objectType=project / length=64 / isnull=Y / isindex=N / 无 format）
LINK_OPTIONS: dict = {
    "isMultiple": False,
    "isDefault": False,
    "uniqueCheck": False,
    "optionSource": "BO_ATTR_DIC_TYPE",
    "columns": [],
    "identities": [],
}

# 系统字段 + 流程字段：一律不参与对齐（PRD B3-2）。
# currencyId / baseTotalBudget / baseTotalCost / isDraft 是项目对象的平台级字段
# （PRD F1 的系统字段清单里点名了它们），把合同里的字段对齐成「本位币总预算」
# 没有业务意义，所以一并排除。这四项排除后，项目对象 51 个 attribute 里
# 业务字段正好 36 个（51 − 14 系统字段 − pinyin）。
SYSTEM_CODES = {
    "id", "tenantId", "ip", "deleted", "createBy", "createTime", "updateTime",
    "version", "updateBy", "baselineVersion", "isDraft", "isDefault",
    "currencyId", "baseTotalBudget", "baseTotalCost",
    "processNode", "processHandler", "processDefId", "processInstanceId",
}

# 语义可疑名单（主理人 Q1 拍板后的**最终**三项）：
# 项目名称 / 项目简称 / 项目经理——这些在非项目对象上多半是「手抄的快照文本」，
# 对齐成项目主数据字段会把两种语义混成一列，事后拆不开，必须人工拍。
# 项目类型 / 项目级别 / 项目状态 / 项目阶段 / 项目编号 是项目维度字段，
# 同名且同族一律自动对齐（用户核心诉求），不进弹窗。
SEMANTIC_SUSPECT = ("项目名称", "项目简称", "项目经理")

# 同义词归一（架构 R3）：同一目标字段只认一个我方字段（规则等级 + 项目 sn 排序）
SYNONYM_GROUPS = (
    ("类型", "类别", "分类"),
    ("级别", "等级"),
    ("编号", "编码", "代码"),
    ("状态", "状况"),
)

# 快照可用性下限（架构 §2.2）：少于这个数视为不完整，降级但仍用
MIN_SNAPSHOT_FIELDS = 30

# 规则等级：数字越小证据越强（架构 §3.5）
# C = 仅候选（名称包含、但不是自动可判的强关系）：不自动覆盖，只供用户手选。
RANK = {"E0": 0, "S3": 1, "S4": 2, "S2": 3, "S1": 4, "C": 5}

_SOURCE_LABEL = {
    "target_db": "目标库",
    "package_snapshot": "随包快照",
    "package_upload": "上传的项目包",
    "unavailable": "无",
}

_SNAPSHOT_VERSION = 1
# 快照里除去业务字段之外的那批编码（含平台自动生成的 pinyin）。
# 项目对象实测 51 个 attribute：37 个 sn 1..37 + 14 个系统字段，
# 其中 pinyin 是平台自动生成的拼音列，统计「业务字段」时不算（36 个）。
_EXTRA_NON_BUSINESS = {"pinyin"}


# ---------------------------------------------------------------- 目标业务对象
# 「关联项目」原来只认 code=project / pp_project。2026-11-18 起用户可以选择
# 「其他业务对象」：手输对象名称 → 连库时按 iform_bo_def.name_ 精确匹配（并读取
# iform_bo_table.comment_ 的表说明）；没连库时上传该对象的 zip 解析。
# 除了链接字段的命名与文案，后续「字段对齐 / 改名 / 引用重写」与关联项目完全同构，
# 所以这里只把「目标对象是谁」抽成一个值对象，其余逻辑继续复用。
@dataclass(frozen=True)
class TargetObject:
    """要对齐的目标业务对象（默认项目）。

    link_code / link_field / link_label 是**新增链接字段**的范式：
      项目 → projectId / project_id_ / 项目选择
      其它 → {code}Id / {code}_id_ / {name}选择（合同选择 / 客户选择…）
    「XX选择」是用户 2026-11-20 定的显示名口径（原「所属项目 / 关联合同」），
    code 与物理列保持系统同族不变——那两样一改就要线上 ALTER。
    """

    code: str = PROJECT_CODE
    name: str = "项目"
    tbl: str = PROJECT_TBL
    link_code: str = LINK_CODE
    link_field: str = LINK_FIELD_NAME
    link_label: str = LINK_DEFAULT_NAME
    table_label: str = ""          # 物理表说明（iform_bo_table.comment_）
    matched_by: str = ""           # name / code / tableLabel / snapshot / default

    @property
    def is_project(self) -> bool:
        return self.code == PROJECT_CODE

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "tbl": self.tbl,
            "tableLabel": self.table_label,
            "linkCode": self.link_code,
            "linkField": self.link_field,
            "linkLabel": self.link_label,
            "matchedBy": self.matched_by,
            "isProject": self.is_project,
        }


def project_target() -> TargetObject:
    """项目对象的固定范式（保持 2026-11-14 的行为逐字节不变）。"""
    return TargetObject()


def target_for(code: str, name: str = "", tbl: str = "",
               table_label: str = "", matched_by: str = "") -> TargetObject:
    """按真实对象编码/名称构造目标。项目永远走固定范式，避免被库里的别名带偏。"""
    code = str(code or "").strip()
    if not code or code == PROJECT_CODE:
        got = project_target()
        if table_label or matched_by:
            return replace(got, table_label=table_label,
                           matched_by=matched_by or "default")
        return got
    name = str(name or "").strip() or code
    tbl = str(tbl or "").strip() or f"pp_{code}"
    link_code = code[:1].lower() + code[1:] + "Id"
    link_field = camel_to_snake(code) + "_id_"
    return TargetObject(
        code=code, name=name, tbl=tbl,
        link_code=link_code, link_field=link_field,
        # 「XX选择」：关联其它对象时显示名是「合同选择」「客户选择」，
        # 与「项目选择」同一套口径（用户 2026-11-20 确认）。
        link_label=f"{name}选择", table_label=str(table_label or "").strip(),
        matched_by=str(matched_by or "").strip(),
    )


def target_of(snap: "Snapshot | None") -> TargetObject:
    """从快照的 project 元信息还原目标对象（上传 token 的路径用）。"""
    if snap is None:
        return project_target()
    meta = dict(snap.project or {})
    return target_for(
        str(meta.get("code") or PROJECT_CODE),
        str(meta.get("name") or "项目"),
        str(meta.get("tbl") or ""),
        str(meta.get("tableLabel") or meta.get("table_label") or ""),
        "snapshot",
    )


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class DictItem:
    """项目侧字典的一个候选值（key 是**固定业务码**，不可重算）。"""

    key: str
    name: str
    sn: int = 0


@dataclass(frozen=True)
class DictMeta:
    """项目侧字典（目前只承载 GLOBAL；BO_ATTR 的不落快照）。"""

    ref: str
    name: str = ""
    stru_type: str = "1"
    scope: str = "GLOBAL"
    type_sn: int = 0
    items: tuple[DictItem, ...] = ()


@dataclass(frozen=True)
class ProjectField:
    """项目对象的一个字段（对齐的目标定义）。"""

    code: str
    name: str
    type_new: str = "varchar"
    datatype: str = ""
    field_name: str = ""
    object_type: str = ""
    length: int | None = None
    precision: int | None = None
    is_null: bool = True
    is_index: bool = False
    default_value: str = ""
    format: str | None = None
    sn: int = 0
    options: dict = field(default_factory=dict)
    dict: DictMeta | None = None
    desc: str = ""


@dataclass
class Snapshot:
    """目标对象字段快照。三档来源：目标库 / 随包 / 上传的 zip。"""

    source: str = "unavailable"
    generated_at: str = ""
    project: dict = field(default_factory=dict)
    fields: list[ProjectField] = field(default_factory=list)
    system_codes: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    # 快照文件里声明的字段总数（用来判断「解析到的比预期少」）
    declared_total: int = 0
    # 字段数下限：项目对象要求 30+；其它业务对象可能只有 3~10 个字段，
    # 拿项目的 30 去卡会把正常快照误判成 degraded。
    min_fields: int = MIN_SNAPSHOT_FIELDS

    @property
    def status(self) -> str:
        """ok / degraded / unavailable（前端只读这个字段）。"""
        if self.source == "unavailable" or not self.fields:
            return "unavailable"
        total = self.declared_total or len(self.fields)
        if len(self.fields) < self.min_fields or total > len(self.fields):
            return "degraded"
        return "ok"

    def field(self, code: str) -> ProjectField | None:
        for f in self.fields:
            if f.code == code:
                return f
        return None

    def pool(self) -> list[ProjectField]:
        """候选池，已按 sn 排序（候选排序依赖它）。"""
        return sorted(self.fields, key=lambda f: f.sn)

    def label(self) -> str:
        """快照来源的人话说明。

        上传包这一档要带上目标名：关联合同时还写「上传的项目包」会误导用户
        以为是项目——文案跟项目一样就没法判断这份快照是谁的。
        """
        if self.source == "package_upload":
            name = (target_of(self).name or "").strip()
            if name and name != "项目":
                return f"上传的{name}包"
        return _SOURCE_LABEL.get(self.source, self.source)


@dataclass
class LinkPlan:
    """链接字段（指向项目对象的 object 字段）的处置计划。"""

    action: str                      # add / reuse / conflict / reject_self
    code: str = LINK_CODE
    field_name: str = LINK_FIELD_NAME
    name: str = LINK_DEFAULT_NAME
    existing: dict | None = None     # reuse 时的原字段描述
    note: str = ""


@dataclass
class AlignAction:
    """一条「我方字段 ↔ 项目字段」的关系（E0 直接对应 / S1-S4 相近）。

    ``auto`` 只对**直接对应**为 True：``rule == "E0"`` 且同族、不在可疑名单、
    候选唯一。2026-11-15 用户口径——只有直接对应才自动覆盖；语义相近（S3/S4）、
    可能相关（S2/S1）、E0 但可疑/跨族/多候选，一律进 ``ObjectPlan.pending``，
    弹窗逐条确认后才覆盖。

    ``diff`` 是「覆盖前的差异明细」（pp_project ↔ 你的 Excel 两列对照），
    两侧完全一致时为空 dict。
    """

    field_code: str                  # 对齐前我方 code
    field_name: str                  # 对齐前我方 name（给报告看）
    target: ProjectField
    rule: str                        # E0 / S3 / S4 / S2 / S1
    rank: int = 0
    family_ok: bool = True
    suspect: bool = False
    auto: bool = False               # 直接对应 → 自动覆盖；其余要用户勾
    confirmed: bool = False          # 用户在「确认相近字段」里勾了「按项目覆盖」
    ambiguous: bool = False          # 同分候选 ≥2：不自动覆盖，交用户挑
    candidates: list[ProjectField] = field(default_factory=list)  # 同分/次优候选（≤3）
    rename_code: str = ""
    rename_field: str = ""
    diff: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    reason: str = ""                 # 一句话结论（报告 / 弹窗标题行）
    basis: str = ""                  # 判断依据（人话，不带规则码）
    confidence: str = "low"          # high / medium / low
    status: str = ""                 # auto / applied / pending / ambiguous / conflict
    note: str = ""                   # 附注（多候选 / 跨族 / 可疑 / 通用字段名…）

    def to_dict(self) -> dict:
        return {
            "fieldCode": self.field_code,
            "fieldName": self.field_name,
            "name": self.field_name,
            "rule": self.rule,
            "suspect": self.suspect,
            "auto": self.auto,
            "confirmed": self.confirmed,
            "ambiguous": self.ambiguous,
            "status": self.status,
            "reason": self.reason,
            "basis": self.basis,
            "confidence": self.confidence,
            "note": self.note,
            "renameCode": self.rename_code,
            "renameField": self.rename_field,
            "target": _target_payload(self.target),
            "candidates": [_candidate_payload(t) for t in self.candidates],
            "diff": dict(self.diff) if self.diff else None,
            "warnings": list(self.warnings),
        }


@dataclass
class ObjectPlan:
    object_code: str
    object_name: str
    linked: bool = False
    update_mode: bool = False
    link: LinkPlan | None = None
    # 本次**要应用**的字段对齐：直接对应（auto）+ 用户已确认的相近字段
    aligned: list[AlignAction] = field(default_factory=list)
    # 未确认的相近字段：只报告、不应用（用户勾了「按项目覆盖」才进 aligned）
    pending: list[AlignAction] = field(default_factory=list)
    # 每个我方字段一行关系（前端「项目对齐」页签与确认弹窗都渲染它）
    relations: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _target_payload(t: ProjectField | None) -> dict | None:
    if t is None:
        return None
    return {
        "code": t.code,
        "name": t.name,
        "typeNew": t.type_new,
        "datatype": t.datatype,
        "objectType": t.object_type,
        "length": t.length,
        "precision": t.precision,
        "fieldName": t.field_name,
        "sn": t.sn,
        "dict": ({"ref": t.dict.ref, "name": t.dict.name, "scope": t.dict.scope,
                  "itemCount": len(t.dict.items)} if t.dict else None),
    }


def _candidate_payload(t: ProjectField | None) -> dict | None:
    """多候选行用：只要弹窗里够看/够选的 4 个键（不给定义，避免前端自己判规则）。"""
    if t is None:
        return None
    return {
        "code": t.code,
        "name": t.name,
        "fieldName": t.field_name,
        "type": _type_label(t.type_new),
    }


# ---------------------------------------------------------------- 名称归一


_FULLWIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_BRACKETS = re.compile(r"[（(【\[]([^（）()【\[\]]*)[）)】\]]")


def _to_halfwidth(text: str) -> str:
    out = []
    for ch in text:
        if ch in _FULLWIDTH:
            out.append(chr(_FULLWIDTH[ch]))
        elif ch == "\u3000":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def normalize(name: str) -> str:
    """字段名归一：全角→半角、去空白、去括号内容（最多 2 层）、去尾部「字段」。

    为什么不去掉「的」：那会把「项目的名称」与「项目名称」混同，
    而这正是语义可疑名单（项目名称）要防的场景。
    """
    raw = str(name or "")
    text = _to_halfwidth(raw)
    text = re.sub(r"\s+", "", text)
    for _ in range(2):
        new = _BRACKETS.sub("", text)
        if new == text:
            break
        text = new
    if len(text) > 2 and text.endswith("字段"):
        text = text[: -len("字段")]
    return text or raw.strip()


def synonym_canon(text: str) -> str:
    """同义词归一到组内首项：类别/分类→类型、等级→级别、编码/代码→编号、状况→状态。"""
    out = str(text or "")
    for group in SYNONYM_GROUPS:
        head = group[0]
        for word in group[1:]:
            out = out.replace(word, head)
    return out


# 通用字段名：几乎每个对象都有，撞上项目侧同名/近义字段多半是巧合。
# 不拦对齐（可能是真的想对齐「状态」），只在弹窗里提示「匹配可能不准」。
GENERIC_FIELD_NAMES = ("备注", "名称", "标题", "状态", "类型", "描述", "说明", "id")
_GENERIC_NAMES: frozenset[str] = frozenset(normalize(x) for x in GENERIC_FIELD_NAMES)


def is_generic_name(name: str) -> bool:
    """是不是「备注/名称/状态…」这类通用字段名（归一后比较）。"""
    return normalize(name) in _GENERIC_NAMES


_FAMILY = {
    "varchar": "varchar",
    "number": "number",
    "datetime": "datetime",
    "date": "date",
    "clob": "clob",
}
# datatype → 5 个物理族（架构 §3.3）。同族互换不 ALTER，可自动；跨族一律弹窗。
_DATATYPE_FAMILY = {
    "varchar": "varchar", "user": "varchar", "department": "varchar",
    "attachment": "varchar", "i18n": "varchar", "dic": "varchar",
    "object": "varchar", "userGroupCate": "varchar", "autoNumber": "varchar",
    "formula": "varchar", "file": "varchar",
    "number": "number",
    "datetime": "datetime",
    "date": "date",
    "clob": "clob", "table": "clob",
}
_FAMILY_CACHE: dict[str, str] = {}


def family(type_new: str) -> str:
    """dataTypeNew → 物理族。

    family("dic") == family("object") == family("varchar") == "varchar"：
    这三类的物理列都是 varchar，互换不 ALTER（README「导入之后：线上还能改什么」
    实证 7 项可改），所以可以自动对齐。
    """
    key = str(type_new or "varchar")
    if key in _FAMILY_CACHE:
        return _FAMILY_CACHE[key]
    cfg = _ft.specs().get(key) or {}
    datatype = cfg.get("datatype") or "varchar"
    got = _FAMILY.get(datatype) or _DATATYPE_FAMILY.get(datatype, "varchar")
    _FAMILY_CACHE[key] = got
    return got


# ---------------------------------------------------------------- 相似判定


def _name_of(x) -> str:
    if isinstance(x, str):
        return x
    return str(getattr(x, "name", "") or "")


def _code_of(x) -> str:
    if isinstance(x, str):
        return ""
    return str(getattr(x, "code", "") or "")


def _col_of(x) -> str:
    """物理列名：BoField 走 resolved_field_name()，ProjectField 直接取 field_name。"""
    if isinstance(x, str):
        return ""
    getter = getattr(x, "resolved_field_name", None)
    if callable(getter):
        return str(getter() or "")
    return str(getattr(x, "field_name", "") or "")


def _lev(a: str, b: str, limit: int = 2) -> int:
    """编辑距离（带早停：某一行最小值已超 limit 就返回 limit+1）。"""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def similar(own, target) -> str | None:
    """相似判定，返回规则码（E0/S3/S4/S2/S1）或 None。**纯函数、可单测、离线。**

    own / target 可以是 ProjectField / BoField / 纯字符串（只比名称）。

    判定顺序固定（返回第一个命中者），排在前面的视为更强的证据：
      E0 精确同名 → S3 编码或物理列同源 → S4 同义词归一 → S2 编辑距离 → S1 包含

    S2 只认**编辑距离 ≤ 1**（主理人 2026-11-14 复审修正）。曾经放宽到 ≤2，
    实测把「合同名称/项目名称」「合同编号/项目编号」「合同类型/项目类型」
    「合同金额/项目金额」全判成相似——任何带「XX名称/XX编号」的对象都会弹窗，
    弹窗一多用户就一路点「全部不对齐」，真正的相似项反而被埋掉。
    差 2 次编辑的组合交给 S1（包含关系）兜：项目阶段名称/项目阶段 仍是相似，
    只是规则码不同，两者都 auto=False。

    S1 另加一条收敛「多出来的部分是 2 个字以上，或较短者本身 ≥ 3 字」：
    否则「金额/总金额」这种只差一个限定字的组合会被判成同一字段
    （PL04 明确要求它是 None）。
    """
    a = normalize(_name_of(own))
    b = normalize(_name_of(target))
    if not a or not b:
        return None
    if a == b:
        return "E0"
    code_a, code_b = _code_of(own), _code_of(target)
    if code_a and code_a == code_b:
        return "S3"
    col_a, col_b = _col_of(own), _col_of(target)
    if col_a and col_a == col_b:
        return "S3"
    if synonym_canon(a) == synonym_canon(b):
        return "S4"
    if min(len(a), len(b)) >= 3 and _lev(a, b, 2) <= 1:
        return "S2"
    short, longer = (a, b) if len(a) <= len(b) else (b, a)
    extra = len(longer) - len(short)
    if short in longer and len(short) >= 2 and extra <= 3 and (extra >= 2 or len(short) >= 3):
        return "S1"
    return None


def match(own, target) -> tuple[str, int] | None:
    """(规则, 等级) 或 None。等级小者优先。"""
    rule = similar(own, target)
    return (rule, RANK[rule]) if rule else None


# 项目侧编码集合（模块级缓存）。用途：validate 的「编码别带 Id 后缀」检查要看
# 一眼这个编码是不是**系统原生**的——stateId / classificationId / gradeId 是
# 项目对象本来就叫这个名，报「建议去掉 Id 后缀」等于让用户跟系统对着干。
_PROJECT_CODES_CACHE: set[str] | None = None


def project_field_codes() -> set[str]:
    """项目侧（快照里）的全部字段编码 + 系统字段编码 + 链接字段编码。

    快照不可用时退回系统字段与链接字段，保证校验不会因为没快照就失效。
    带模块级缓存：每次出包都会问一遍，逐个对象读 JSON 不值当。
    """
    global _PROJECT_CODES_CACHE
    if _PROJECT_CODES_CACHE is None:
        codes = set(SYSTEM_CODES) | {LINK_CODE}
        try:
            snap = load_snapshot()
            codes |= {f.code for f in snap.fields if f.code}
        except Exception:
            pass
        _PROJECT_CODES_CACHE = codes
    return _PROJECT_CODES_CACHE


def is_project_code(code: str) -> bool:
    """这个编码是不是项目对象/系统约定里本来就有的（对齐后不算「命名不规范」）。"""
    return str(code or "") in project_field_codes()


def is_target_code(code: str, target: TargetObject | None = None) -> bool:
    """编码是不是「当前目标对象」里本来就有的（含本轮新增的链接字段）。

    和 is_project_code 的区别：这里只认**本次选中的对象**，不把库里的
    contractId 全局加白——自建的 contractId 在关联项目时仍应提示去掉 Id 后缀。
    """
    code = str(code or "")
    if not code:
        return False
    target = target or project_target()
    if code == target.link_code:
        return True
    if target.is_project:
        return is_project_code(code)
    try:
        snap = load_snapshot(target=target)
    except Exception:                      # noqa: BLE001
        return False
    return any(f.code == code for f in snap.fields)


def clear_code_cache() -> None:
    """快照刷新后调用，否则新加的字段名永远进不了白名单。"""
    global _PROJECT_CODES_CACHE
    _PROJECT_CODES_CACHE = None


# ---------------------------------------------------------------- 快照读写


def _data_home() -> Path:
    return Path.home() / ".pp6kit"


def snapshot_cache_path() -> Path:
    return _data_home() / "project_fields.json"


def target_cache_path(target: TargetObject | None = None) -> Path:
    """目标对象快照的落盘位置。

    项目继续用 ``~/.pp6kit/project_fields.json``（兼容老版本与 objects.refresh）；
    其它对象放在 ``~/.pp6kit/target_fields/<code>.json``，互不覆盖。
    """
    target = target or project_target()
    if target.is_project:
        return snapshot_cache_path()
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", target.code).strip("_") or "object"
    return _data_home() / "target_fields" / f"{safe}.json"


def upload_dir() -> Path:
    return work_dir() / "project-link-snapshots"


_TOKEN_RE = re.compile(r"^[0-9a-f]{20}$")


def _as_int(value) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _as_bool_yn(value, default: bool = True) -> bool:
    text = str(value if value is not None else "").strip().upper()
    if not text:
        return default
    return text in ("Y", "YES", "TRUE", "1", "是")


def _field_from_payload(row: dict) -> ProjectField | None:
    code = str(row.get("code") or "").strip()
    if not code:
        return None
    raw_dict = row.get("dict") or None
    meta = None
    if isinstance(raw_dict, dict) and str(raw_dict.get("ref") or "").strip():
        items = tuple(
            DictItem(key=str(it.get("key") or ""), name=str(it.get("name") or ""),
                     sn=_as_int(it.get("sn")) or 0)
            for it in (raw_dict.get("items") or [])
            if str(it.get("key") or "").strip()
        )
        meta = DictMeta(
            ref=str(raw_dict.get("ref") or ""),
            name=str(raw_dict.get("name") or ""),
            stru_type=str(raw_dict.get("struType") or raw_dict.get("stru_type") or "1"),
            scope=str(raw_dict.get("scope") or "GLOBAL"),
            type_sn=_as_int(raw_dict.get("typeSn") or raw_dict.get("type_sn")) or 0,
            items=items,
        )
        if meta.scope != "GLOBAL":
            meta = None      # BO_ATTR 字典不落快照（架构 §2.2）
    options = row.get("options")
    return ProjectField(
        code=code,
        name=str(row.get("name") or code),
        type_new=str(row.get("typeNew") or row.get("type_new") or "varchar"),
        datatype=str(row.get("datatype") or ""),
        field_name=str(row.get("fieldName") or row.get("field_name") or ""),
        object_type=str(row.get("objectType") or row.get("object_type") or ""),
        length=_as_int(row.get("length")),
        precision=_as_int(row.get("precision")),
        is_null=_as_bool_yn(row.get("isNull", row.get("is_null")), True),
        is_index=_as_bool_yn(row.get("isIndex", row.get("is_index")), False),
        default_value=str(row.get("defaultValue") or row.get("default_value") or ""),
        format=(str(row["format"]) if row.get("format") not in (None, "") else None),
        sn=_as_int(row.get("sn")) or 0,
        options=dict(options) if isinstance(options, dict) else {},
        dict=meta,
        desc=str(row.get("desc") or ""),
    )


def _field_payload(f: ProjectField) -> dict:
    return {
        "code": f.code,
        "name": f.name,
        "desc": f.desc,
        "typeNew": f.type_new,
        "datatype": f.datatype,
        "fieldName": f.field_name,
        "objectType": f.object_type,
        "length": f.length,
        "precision": f.precision,
        "isNull": f.is_null,
        "isIndex": f.is_index,
        "defaultValue": f.default_value,
        "format": f.format,
        "sn": f.sn,
        "options": dict(f.options),
        "dict": (None if f.dict is None else {
            "ref": f.dict.ref,
            "name": f.dict.name,
            "struType": f.dict.stru_type,
            "typeSn": f.dict.type_sn,
            "scope": f.dict.scope,
            "items": [{"key": i.key, "name": i.name, "sn": i.sn} for i in f.dict.items],
        }),
    }


def snapshot_payload(snap: Snapshot) -> dict:
    """Snapshot → 落盘 JSON（schema 见架构 §2.2）。"""
    fields = sorted(snap.fields, key=lambda f: f.sn)
    business = [f for f in fields
                if f.code not in SYSTEM_CODES and f.code not in _EXTRA_NON_BUSINESS]
    project = dict(snap.project or {})
    project.setdefault("code", PROJECT_CODE)
    project.setdefault("name", "项目")
    project.setdefault("tbl", PROJECT_TBL)
    project["attributeCount"] = len(fields) or project.get("attributeCount", 0)
    project["businessFieldCount"] = len(business) or project.get("businessFieldCount", 0)
    return {
        "version": _SNAPSHOT_VERSION,
        "source": snap.source,
        "generatedAt": snap.generated_at,
        "project": project,
        "systemCodes": sorted(snap.system_codes or SYSTEM_CODES),
        "fields": [_field_payload(f) for f in fields],
    }


def _snapshot_from_payload(data: dict, source: str, where: str) -> Snapshot | None:
    if not isinstance(data, dict):
        return None
    rows = data.get("fields")
    if not isinstance(rows, list):
        return None
    fields = [f for f in (_field_from_payload(r) for r in rows if isinstance(r, dict)) if f]
    if not fields:
        return None
    declared = _as_int((data.get("project") or {}).get("attributeCount")) or 0
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    obj_code = str(project.get("code") or PROJECT_CODE)
    obj_name = str(project.get("name") or ("项目" if obj_code == PROJECT_CODE else obj_code))
    min_fields = MIN_SNAPSHOT_FIELDS if obj_code == PROJECT_CODE else 1
    warnings: list[str] = []
    if len(fields) < min_fields:
        warnings.append(
            f"⚠ {obj_name}字段快照只解析到 {len(fields)} 个字段（预期 {min_fields}+），"
            f"对齐结果可能不完整")
    elif declared and declared > len(fields):
        warnings.append(
            f"⚠ {obj_name}字段快照声明 {declared} 个字段，实际解析到 {len(fields)} 个，对齐结果可能不完整")
    system = data.get("systemCodes")
    return Snapshot(
        source=str(data.get("source") or source),
        generated_at=str(data.get("generatedAt") or ""),
        project=dict(project),
        fields=fields,
        system_codes=set(system) if isinstance(system, list) and system else set(SYSTEM_CODES),
        warnings=warnings,
        declared_total=declared,
        min_fields=min_fields,
    )


# 结果按「路径 + mtime」缓存（架构 §3.7）。force=True 清缓存（前端「重新分析」用）
_CACHE: dict[str, tuple[float, Snapshot]] = {}


def _load_file(path: Path, source: str, force: bool = False) -> Snapshot | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    if not force and key in _CACHE:
        cached_mtime, cached = _CACHE[key]
        if cached_mtime == mtime:
            return cached
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # 文件存在但读不出来：当作没有这一档，继续往下走（不能让一个坏 JSON 拦死出包）
        return None
    snap = _snapshot_from_payload(data, source, str(path))
    if snap is None:
        return None
    if snap.source == source and source in ("target_db", "package_snapshot", "package_upload"):
        snap.source = str(data.get("source") or source)
    _CACHE[key] = (mtime, snap)
    return snap


def clear_cache() -> None:
    _CACHE.clear()


def load_snapshot_file(upload_token: str, force: bool = False) -> Snapshot:
    """只按 token 读快照文件（不校验目标对象），供 resolve_target 还原目标用。"""
    token = str(upload_token or "").strip()
    if not _TOKEN_RE.match(token):
        raise ValueError("字段快照标识无效，请重新上传业务对象 zip")
    path = upload_dir() / f"{token}.json"
    if not path.is_file():
        raise ValueError("字段快照标识无效，请重新上传业务对象 zip")
    snap = _load_file(path, "package_upload", force)
    if snap is None:
        raise ValueError("字段快照文件读不出来，请重新上传业务对象 zip")
    return snap


def _snapshot_mismatch(snap: Snapshot, target: TargetObject) -> str:
    meta = dict(snap.project or {})
    code = str(meta.get("code") or PROJECT_CODE)
    name = str(meta.get("name") or ("项目" if code == PROJECT_CODE else code))
    if code == target.code:
        return ""
    return (f"这份快照是「{name}」（{code}），不是「{target.name}」"
            f"（{target.code}）；请上传对应业务对象的 zip")


def load_snapshot(upload_token: str | None = None, force: bool = False,
                  target: TargetObject | None = None) -> Snapshot:
    """三档优先级：上传 token → 目标库缓存 → 随包（仅项目）→ unavailable。"""
    target = target or project_target()
    if force:
        clear_cache()
    token = str(upload_token or "").strip()
    if token:
        snap = load_snapshot_file(token, force)
        bad = _snapshot_mismatch(snap, target)
        if bad:
            raise ValueError(bad)
        return snap
    snap = _load_file(target_cache_path(target), "target_db", force)
    if snap is not None:
        bad = _snapshot_mismatch(snap, target)
        if not bad:
            return snap
    if target.is_project:
        snap = _load_file(resource_root() / "assets" / "project_fields.json",
                          "package_snapshot", force)
        if snap is not None:
            # 随包那份 JSON 是 `dump_project_fields.py` 从库里导出来的，文件里
            # "source" 写的是 target_db；但走到这一档就说明它不是「当前连上的库」，
            # 而是打进包里的冻结副本（_load_file 会把文件里的 source 盖回来）。
            # 不纠正的话界面会显示「当前快照：目标库 · 51 个字段」，
            # 同时又要求用户导入项目 zip —— 自相矛盾，用户以为库是连着的。
            snap.source = "package_snapshot"
            return snap
    label = "项目" if target.is_project else target.name
    return Snapshot(
        source="unavailable",
        fields=[],
        project={"code": target.code, "name": target.name, "tbl": target.tbl,
                 "tableLabel": target.table_label},
        warnings=[f"{label}字段快照不可用（未连库刷新、无随包快照、未上传{label}包），"
                  f"本次只补「{target.link_label}」链接字段、不做字段对齐"],
        min_fields=1 if not target.is_project else MIN_SNAPSHOT_FIELDS,
    )


def save_snapshot(snap: Snapshot, path: Path | None = None) -> Path:
    """把快照写成 JSON（原子替换），返回落盘路径。"""
    target = Path(path) if path else snapshot_cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot_payload(snap), ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(target)
    clear_cache()
    clear_code_cache()      # 快照换了 → 白名单跟着换
    return target


def resolve_target_from_zip(data: bytes, name: str = "",
                            code: str = "") -> TargetObject:
    """没连库时：先从上传的 zip 里按名称/编码认出目标对象（不落快照）。

    用户只输入了名称、库里又查不到时走这里；找到对象后用它的真实 code/tbl
    作为字段池。找不到就抛 ValueError，并列出包里有哪些对象。
    """
    import tempfile

    from .inspect import inspect_path

    name = str(name or "").strip()
    code = str(code or "").strip()
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "target.zip"
        src.write_bytes(bytes(data))
        proj, _notes = inspect_path(src)
    bo = proj.bo(code) if code else None
    if bo is None and code:
        bo = next((b for b in proj.bos if (b.code or "") == code), None)
    if bo is None and not code:
        exact = [b for b in proj.bos if (b.name or "").strip() == name]
        if len(exact) == 1:
            bo = exact[0]
        elif len(exact) > 1:
            raise ValueError(
                "包里同名对象不止一个：" +
                "、".join(f"{b.name}（{b.code}）" for b in exact[:6]) +
                "；请改用对象编码")
    if bo is None:
        names = "、".join(f"{b.name or b.code}（{b.code}）" for b in proj.bos[:8])
        raise ValueError(
            f"这份 zip 里没有名称完全一致的业务对象「{name or code}」"
            + (f"；包内对象：{names}" if names else ""))
    return target_for(bo.code, bo.name, bo.tbl_name, "",
                      "code" if code else "name")


def save_snapshot_upload(data: bytes, target: TargetObject | None = None) -> dict:
    """把一份「目标业务对象包」zip 存成第③档快照，返回 token 与摘要。"""
    import hashlib

    target = target or project_target()
    snap = snapshot_from_zip(data, source="package_upload", target=target)
    if not snap.fields:
        label = "项目" if target.is_project else target.name
        raise ValueError(
            f"这份 zip 里找不到「{label}」业务对象。请上传该对象导出的包"
            f"（bo/*.xml 里 code={target.code} 或物理表 {target.tbl}）")
    token = hashlib.sha1(target.code.encode("utf-8") + data).hexdigest()[:20]
    upload_dir().mkdir(parents=True, exist_ok=True)
    save_snapshot(snap, upload_dir() / f"{token}.json")
    # 再按**对象编码**存一份（target_fields/<code>.json）：
    # 值来源 / 联动那条链（links._target_from_snapshot）只按对象编码找快照，
    # 不认 token。只写 token 那一份的话，「关联其他业务对象 + 用户只上传了 zip」
    # 的值来源里永远只有主键和名称两列——而用户要的是「返回全部业务字段」，
    # 偏偏这种缺失在界面上完全看不出来（下拉能点、联动配不出来）。
    # 库里刷新出来的快照也是存这个位置（resolve_target），两档来源同一个入口。
    save_snapshot(snap, target_cache_path(target))
    got = target_of(snap)
    return {"ok": True, "token": token, "source": snap.source,
            "project": snap.project, "fieldCount": len(snap.fields),
            "target": got.to_dict()}


# ---------------------------------------------------------------- 从包 / 从库抽快照


def _field_from_bo_field(f: BoField, sn: int) -> ProjectField:
    meta = None
    if f.dict_ref:
        meta = DictMeta(
            ref=f.dict_ref,
            name=f.dict_name or f.dict_ref,
            stru_type=f.dict_stru or "1",
            scope="GLOBAL",
            items=tuple(DictItem(key=str(it.get("key") or ""),
                                 name=str(it.get("name") or ""),
                                 sn=_as_int(it.get("sn")) or 0)
                        for it in (f.dict_items or []) if str(it.get("key") or "").strip()),
        )
    return ProjectField(
        code=f.code,
        name=f.name,
        type_new=f.type_new,
        datatype=f.datatype or "",
        field_name=f.resolved_field_name(),
        object_type=f.object_type or "",
        length=f.length,
        precision=f.precision,
        is_null=f.is_null,
        is_index=f.is_index,
        default_value=f.default_value or "",
        sn=f.sn or sn,
        options=dict(f.options or {}),
        dict=meta,
        desc=f.desc or "",
    )


def _pick_target_bo(proj, target: TargetObject) -> tuple[Bo | None, str]:
    """在一份导出包里找到目标对象：编码 → 物理表 → 名称完全一致。"""
    bo = proj.bo(target.code)
    if bo is not None:
        return bo, "code"
    bo = next((b for b in proj.bos
               if target.tbl and (b.tbl_name or "").lower() == target.tbl.lower()), None)
    if bo is not None:
        return bo, "table"
    exact = [b for b in proj.bos if (b.name or "").strip() == target.name]
    if len(exact) == 1:
        return exact[0], "name"
    if len(exact) > 1:
        # 同名对象不止一个：只有编码能区分，别猜
        return None, "ambiguous:" + "、".join(f"{b.name}({b.code})" for b in exact[:6])
    return None, ""


def snapshot_from_zip(path_or_bytes, source: str = "package_snapshot",
                      target: TargetObject | None = None) -> Snapshot:
    """从一份业务对象导出包解析快照。**纯函数**，供 tools 与测试使用。

    target 默认项目对象；其它对象按 code → 物理表 → 名称完全一致 找。
    """
    import tempfile

    from .inspect import inspect_path

    target = target or project_target()
    data: bytes | None = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else None
    with tempfile.TemporaryDirectory() as td:
        if data is not None:
            src = Path(td) / "target.zip"
            src.write_bytes(bytes(data))
        else:
            src = Path(path_or_bytes)
        proj, notes = inspect_path(src)

    if target.is_project:
        bo = proj.bo(PROJECT_CODE)
        if bo is None:
            # 编码被改过：按物理表名认（项目对象永远是 pp_project）。
            bo = next((b for b in proj.bos if (b.tbl_name or "").lower() == PROJECT_TBL), None)
        if bo is None and len(proj.bos) == 1:
            # 单对象包：只有这一个对象，基本可断定是用户要的项目包。
            # 但「项目立项（project_create）」也是单对象包，它的表是 pp_project_create，
            # 不能当项目字段池用——名字里带「立项」的直接排除。
            only = proj.bos[0]
            if "立项" not in (only.name or "") and "project_create" not in (only.code or ""):
                bo = only
        if bo is None:
            # 以前这里取「字段最多」的对象，结果一份项目立项包会被当成项目对象，
            # 拿 88 个立项字段去对齐——比不可用更糟。宁可报错让用户换包。
            return Snapshot(
                source="unavailable",
                warnings=["这份 zip 里没有「项目」对象（bo/*.xml 里 code=project "
                          "或物理表 pp_project），无法当作项目字段池"])
        if (bo.tbl_name or "").lower() != PROJECT_TBL and bo.code != PROJECT_CODE:
            return Snapshot(
                source="unavailable",
                warnings=[f"这份 zip 里最像项目的是「{bo.name or bo.code}」"
                          f"（{bo.tbl_name or '无表名'}），不是项目对象 pp_project；"
                          f"请上传「业务对象 › 项目」导出的包"])
    else:
        bo, how = _pick_target_bo(proj, target)
        if bo is None:
            detail = ("包里同名对象不止一个：" + how.split(":", 1)[1]
                      if how.startswith("ambiguous:") else
                      "包里没有 code=" + target.code +
                      ("、物理表 " + target.tbl if target.tbl else "") +
                      "、名称「" + target.name + "」完全一致的对象")
            names = "、".join(f"{b.name or b.code}（{b.code}）" for b in proj.bos[:8])
            return Snapshot(
                source="unavailable",
                project={"code": target.code, "name": target.name, "tbl": target.tbl},
                warnings=[f"这份 zip 不能作为「{target.name}」的字段池：{detail}"
                          + (f"；包内对象：{names}" if names else "")],
                min_fields=1)
        if target.name and (bo.name or "").strip() != target.name:
            notes = list(notes) + [
                f"⚠ 包里的对象名称是「{bo.name or bo.code}」，与输入的「{target.name}」"
                f"不完全一致；已按编码 {bo.code} 继续"]

    fields = [_field_from_bo_field(f, i) for i, f in enumerate(bo.fields, start=1)]
    business = [f for f in fields
                if f.code not in SYSTEM_CODES and f.code not in _EXTRA_NON_BUSINESS]
    min_fields = MIN_SNAPSHOT_FIELDS if target.is_project else 1
    label = "项目" if target.is_project else target.name
    warnings: list[str] = []
    if len(fields) < min_fields:
        warnings.append(
            f"⚠ {label}字段快照只解析到 {len(fields)} 个字段（预期 {min_fields}+），"
            f"对齐结果可能不完整")
    import datetime

    return Snapshot(
        source=source,
        generated_at=datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        project={"code": bo.code, "name": bo.name,
                 "tbl": bo.tbl_name or target.tbl or f"pp_{bo.code}",
                 "tableLabel": target.table_label,
                 "attributeCount": len(fields),
                 "businessFieldCount": len(business)},
        fields=fields,
        system_codes=set(SYSTEM_CODES),
        warnings=warnings + [n for n in notes if "失败" in n][:3],
        declared_total=len(fields),
        min_fields=min_fields,
    )


def snapshot_from_cursor(cur, target: TargetObject | None = None) -> Snapshot:
    """用一个**只读** cursor 抽目标对象字段快照（SELECT only，绝不写库）。

    由 objects.refresh() 复用同一个连接调用，避免「刷新对象清单」和
    「刷新项目字段」各开一次连接。表结构见 docs/数据库结构图谱.md：
        iform_bo_def         对象（code_ / name_ / id_）
        iform_bo_attr        属性（data_type_new_ / object_type_ / is_null_ …）
        iform_bo_attr_column 属性 → 物理列（field_name_）
        iform_bo_table       物理表（def_code_ = 对象编码，name_ = 表名，comment_ = 表说明）
        iform_cat_type/dic   字典（type_key_ / type_scope_ / key_ / name_ / sn_）
    """
    import datetime

    target = target or project_target()
    cur.execute("SELECT id_, code_, name_ FROM iform_bo_def "
                "WHERE code_ = %s AND COALESCE(state_, '') <> 'deleted' LIMIT 1",
                (target.code,))
    row = cur.fetchone()
    if not row:
        label = "项目" if target.is_project else target.name
        return Snapshot(
            source="unavailable",
            project={"code": target.code, "name": target.name, "tbl": target.tbl},
            warnings=[f"目标库里没有 code={target.code} 的业务对象（{label}），"
                      f"无法刷新字段快照"],
            min_fields=1 if not target.is_project else MIN_SNAPSHOT_FIELDS)
    def_id, code, name = str(row[0] or ""), str(row[1] or target.code), str(row[2] or target.name)
    tbl, table_label = target.tbl, target.table_label
    try:
        cur.execute("SELECT name_, COALESCE(comment_, '') FROM iform_bo_table "
                    "WHERE def_code_ = %s LIMIT 1", (code,))
        got = cur.fetchone()
        if got:
            tbl = str(got[0] or tbl)
            table_label = str(got[1] or table_label)
    except Exception:
        pass          # 老库没有 iform_bo_table 时退回 pp_{code}

    cur.execute(
        "SELECT code_, name_, COALESCE(desc_, ''), data_type_new_, COALESCE(data_type_, ''), "
        "       attr_length_, precision_, COALESCE(is_null_, 'Y'), COALESCE(is_index_, 'N'), "
        "       sn_, COALESCE(object_type_, ''), COALESCE(def_value_, ''), format_, options_ "
        "FROM iform_bo_attr WHERE def_id_ = %s ORDER BY sn_", (def_id,))
    attrs = cur.fetchall()

    cols: dict[str, str] = {}
    try:
        cur.execute("SELECT attr_code_, field_name_ FROM iform_bo_attr_column "
                    "WHERE def_id_ = %s", (def_id,))
        cols = {str(a): str(b or "") for a, b in cur.fetchall() if a}
    except Exception:
        cols = {}

    # 字典块：只取 GLOBAL，且只查属性里真的引用到的 typeKey
    dic_keys = sorted({str(a[10]) for a in attrs if str(a[3]) == "dic" and a[10]})
    dicts: dict[str, DictMeta] = {}
    if dic_keys:
        try:
            placeholders = ",".join(["%s"] * len(dic_keys))
            cur.execute(
                "SELECT id_, type_key_, name_, COALESCE(stru_type_, '1'), sn_, "
                "       COALESCE(type_scope_, '') FROM iform_cat_type "
                f"WHERE type_key_ IN ({placeholders}) AND category_key_ = 'DIC_TYPE'",
                tuple(dic_keys))
            types = {str(tid): (key, nm, stru, sn, scope)
                     for tid, key, nm, stru, sn, scope in cur.fetchall()}
            if types:
                tids = tuple(types)
                cur.execute("SELECT type_id_, key_, name_, sn_ FROM iform_cat_dic "
                            f"WHERE type_id_ IN ({','.join(['%s'] * len(tids))}) ORDER BY sn_",
                            tids)
                items: dict[str, list[DictItem]] = {}
                for tid, key, nm, sn in cur.fetchall():
                    if str(tid) not in types or not key:
                        continue
                    items.setdefault(str(tid), []).append(
                        DictItem(key=str(key), name=str(nm or key), sn=int(sn or 0)))
                for tid, (key, nm, stru, sn, scope) in types.items():
                    if str(scope).upper() != "GLOBAL":
                        continue          # BO_ATTR 字典不落快照（架构 §2.2）
                    dicts[key] = DictMeta(ref=key, name=nm or key, stru_type=str(stru or "1"),
                                          scope="GLOBAL", type_sn=int(sn or 0),
                                          items=tuple(items.get(tid, ())))
        except Exception:
            dicts = {}          # 老库没有字典表，不影响对齐其它字段

    fields: list[ProjectField] = []
    for sn, a in enumerate(attrs, start=1):
        f_code = str(a[0] or "").strip()
        if not f_code:
            continue
        type_new = str(a[3] or "varchar").strip() or "varchar"
        obj_type = str(a[10] or "").strip()
        opts: dict = {}
        if a[13]:
            try:
                got = json.loads(a[13])
                opts = got if isinstance(got, dict) else {}
            except (json.JSONDecodeError, TypeError):
                opts = {}
        fields.append(ProjectField(
            code=f_code,
            name=str(a[1] or f_code),
            type_new=type_new,
            datatype=str(a[4] or ""),
            field_name=cols.get(f_code, "") or (camel_to_snake(f_code) + "_"),
            object_type=obj_type,
            length=_as_int(a[5]),
            precision=_as_int(a[6]),
            is_null=_as_bool_yn(a[7], True),
            is_index=_as_bool_yn(a[8], False),
            default_value=str(a[11] or ""),
            format=(str(a[12]) if a[12] not in (None, "") else None),
            sn=_as_int(a[9]) or sn,
            options=opts,
            dict=dicts.get(obj_type) if type_new == "dic" else None,
            desc=str(a[2] or ""),
        ))

    business = [f for f in fields
                if f.code not in SYSTEM_CODES and f.code not in _EXTRA_NON_BUSINESS]
    min_fields = MIN_SNAPSHOT_FIELDS if target.is_project else 1
    label = "项目" if target.is_project else (name or target.name)
    warnings: list[str] = []
    if len(fields) < min_fields:
        warnings.append(
            f"⚠ {label}字段快照只解析到 {len(fields)} 个字段（预期 {min_fields}+），"
            f"对齐结果可能不完整")
    return Snapshot(
        source="target_db",
        generated_at=datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        project={"code": code, "name": name, "tbl": tbl,
                 "tableLabel": table_label,
                 "attributeCount": len(fields),
                 "businessFieldCount": len(business)},
        fields=fields,
        system_codes=set(SYSTEM_CODES),
        warnings=warnings,
        declared_total=len(fields),
        min_fields=min_fields,
    )


def save_snapshot_from_cursor(cur, target: TargetObject | None = None) -> dict:
    """抽快照并写到目标对象的缓存文件，返回摘要 dict。

    失败**不要**往外抛得太狠：调用方是 objects.refresh()，它只把这里当附带动作。
    """
    target = target or project_target()
    snap = snapshot_from_cursor(cur, target)
    if snap.source == "unavailable":
        return {"ok": False, "reason": snap.warnings[0] if snap.warnings else "不可用"}
    path = save_snapshot(snap, target_cache_path(target))
    return {"ok": True, "path": str(path), "source": snap.source,
            "fieldCount": len(snap.fields),
            "generatedAt": snap.generated_at, "project": snap.project,
            "target": target_of(snap).to_dict()}


# ---------------------------------------------------------------- 目标对象解析
# 用户手输业务对象名称后：
#   连了库 → iform_bo_def.name_ 精确匹配（code_ 也认），顺带读 iform_bo_table
#            的 name_（物理表）与 comment_（表说明）；
#   没连库 → 由调用方让用户上传该对象的 zip，再按 code/表名/名称解析。
# 只做**精确**匹配：模糊匹配会把「合同台账」绑到「合同」上，对齐错对象比不对齐更糟。


def db_config() -> dict:
    try:
        from .nlq import store
        return dict(store.load().get("db") or {})
    except Exception:                      # noqa: BLE001
        return {}


def db_is_configured(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else db_config()
    return bool(str(cfg.get("host") or "").strip()
                and str(cfg.get("user") or "").strip()
                and str(cfg.get("database") or "").strip())


def _db_connect(cfg: dict | None = None):
    from .nlq import introspect
    cfg = cfg or db_config()
    return introspect.connect(cfg.get("host"), cfg.get("port"), cfg.get("user"),
                              cfg.get("password"), cfg.get("database"))


def find_targets(name: str = "", code: str = "") -> list[dict]:
    """按名称/编码精确查业务对象，返回 [{id,code,name,desc,tbl,tableLabel,...}]。

    名称匹配失败时再按「物理表说明 / 对象说明」精确兜底（用户可能记的是表标签）。
    **不做包含匹配**。没连库返回空列表。
    """
    name = str(name or "").strip()
    code = str(code or "").strip()
    if not (name or code):
        return []
    cfg = db_config()
    if not db_is_configured(cfg):
        return []
    conn = _db_connect(cfg)
    try:
        cur = conn.cursor()
        base = (
            "SELECT d.id_, d.code_, d.name_, COALESCE(d.desc_, ''), "
            "       COALESCE(t.name_, ''), COALESCE(t.comment_, ''), "
            "       COALESCE(d.is_main_, 'N') "
            "FROM iform_bo_def d "
            "LEFT JOIN iform_bo_table t ON t.def_code_ = d.code_ "
            "WHERE COALESCE(d.state_, '') <> 'deleted' AND ({where}) "
            "ORDER BY (COALESCE(d.is_main_, 'N') = 'Y') DESC, d.code_ LIMIT 20"
        )
        where: list[str] = []
        args: list = []
        if name:
            where.append("d.name_ = %s")
            args.append(name)
        if code:
            where.append("d.code_ = %s")
            args.append(code)
        cur.execute(base.format(where=" OR ".join(where)), tuple(args))
        rows = cur.fetchall()
        matched_by = "name" if name else "code"
        if not rows and name:
            cur.execute(base.format(where="COALESCE(t.comment_, '') = %s "
                                          "OR COALESCE(d.desc_, '') = %s"),
                        (name, name))
            rows = cur.fetchall()
            matched_by = "tableLabel"
        return [{
            "id": str(r[0] or ""),
            "code": str(r[1] or ""),
            "name": str(r[2] or r[1] or ""),
            "desc": str(r[3] or ""),
            "tbl": str(r[4] or ""),
            "tableLabel": str(r[5] or ""),
            "isMain": str(r[6] or "N"),
            "matchedBy": matched_by,
        } for r in rows if str(r[1] or "").strip()]
    finally:
        conn.close()


def _suggest_targets(name: str, limit: int = 5) -> list[dict]:
    """没精确命中时给「相近的名称」提示，但绝不自动选中。"""
    name = str(name or "").strip()
    if not name:
        return []
    cfg = db_config()
    if not db_is_configured(cfg):
        return []
    conn = _db_connect(cfg)
    try:
        cur = conn.cursor()
        like = f"%{name}%"
        cur.execute(
            "SELECT d.code_, d.name_, COALESCE(t.comment_, '') "
            "FROM iform_bo_def d LEFT JOIN iform_bo_table t ON t.def_code_ = d.code_ "
            "WHERE COALESCE(d.state_, '') <> 'deleted' "
            "  AND (d.name_ LIKE %s OR d.code_ LIKE %s OR COALESCE(t.comment_, '') LIKE %s) "
            "ORDER BY (COALESCE(d.is_main_, 'N') = 'Y') DESC, d.code_ LIMIT %s",
            (like, like, like, int(limit)))
        return [{"code": str(r[0] or ""), "name": str(r[1] or r[0] or ""),
                 "tableLabel": str(r[2] or "")} for r in cur.fetchall()]
    except Exception:                      # noqa: BLE001
        return []
    finally:
        conn.close()


def _pick_target(matches: list[dict]) -> dict | None:
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    mains = [m for m in matches if str(m.get("isMain") or "").upper() == "Y"]
    if len(mains) == 1:
        return mains[0]
    return None


def resolve_target(name: str = "", code: str = "", token: str = "",
                   force: bool = False) -> TargetObject:
    """把「用户输入的名称/编码」解析成 TargetObject；必要时从库抽快照。

    优先级：上传 token（快照自带目标元信息）→ 项目 → 库精确匹配。
    解析不了就抛 ValueError（API 层转 400），不要静默退回项目——那会把字段对齐到
    一个用户根本没选的对象上。
    """
    name = str(name or "").strip()
    code = str(code or "").strip()
    token = str(token or "").strip()

    if token:
        snap = load_snapshot_file(token, force=force)
        got = target_of(snap)
        if code and got.code != code:
            raise ValueError(f"上传的快照是「{got.name}」（{got.code}），与选择的对象编码不一致")
        if name and got.name != name and got.code != code:
            raise ValueError(f"上传的快照是「{got.name}」，与输入的「{name}」不一致")
        return got

    if (not code or code == PROJECT_CODE) and (not name or name == "项目"):
        return project_target()

    if code == PROJECT_CODE or name == "项目":
        return project_target()

    matches = find_targets(name=name, code=code)
    pick = _pick_target(matches)
    if pick is None:
        if len(matches) > 1:
            names = "、".join(f"{m['name']}（{m['code']}）" for m in matches[:6])
            raise ValueError(f"名称「{name}」匹配到多个业务对象：{names}；请改用对象编码")
        if not db_is_configured():
            raise ValueError(f"未连接数据库，无法核对业务对象「{name}」；"
                             f"请上传该对象的 zip 作为字段池")
        extra = ""
        got = _suggest_targets(name)
        if got:
            extra = "；相近的有：" + "、".join(
                f"{x['name']}（{x['code']}）" for x in got)
        raise ValueError(f"数据库里没有名称完全一致的业务对象「{name}」{extra}")

    target = target_for(pick["code"], pick["name"], pick["tbl"],
                        pick["tableLabel"], pick["matchedBy"])
    path = target_cache_path(target)
    snap = _load_file(path, "target_db", force=force)
    if snap is None or _snapshot_mismatch(snap, target):
        conn = _db_connect()
        try:
            snap = snapshot_from_cursor(conn.cursor(), target)
            if snap.source == "unavailable":
                raise ValueError(snap.warnings[0] if snap.warnings else
                                 f"读取业务对象 {target.code} 的字段失败")
            save_snapshot(snap, path)
        finally:
            conn.close()
    return target


def target_status(name: str = "", code: str = "") -> dict:
    """给界面用的目标对象状态：连库精确匹配结果 + 是否有可用字段快照。"""
    name = str(name or "").strip()
    code = str(code or "").strip()
    configured = db_is_configured()
    if (not code or code == PROJECT_CODE) and (not name or name == "项目"):
        snap = load_snapshot()
        tgt = project_target()
        return {"ok": True, "dbConfigured": configured, "name": name or "项目",
                "matches": [], "target": tgt.to_dict(), "resolved": True,
                "fieldCount": len(snap.fields), "source": snap.source,
                "sourceLabel": snap.label(), "status": snap.status,
                "generatedAt": snap.generated_at, "warnings": list(snap.warnings)[:3]}
    matches = find_targets(name=name, code=code)
    pick = _pick_target(matches)
    base = {"ok": True, "dbConfigured": configured, "name": name,
            "matches": matches[:6], "resolved": False, "target": None,
            "fieldCount": 0, "source": "unavailable", "sourceLabel": "无",
            "status": "unavailable", "generatedAt": "", "warnings": []}
    if pick is None:
        base["suggestions"] = _suggest_targets(name) if configured and name else []
        return base
    try:
        tgt = resolve_target(name=name, code=pick["code"])
    except Exception as exc:                # noqa: BLE001
        base["error"] = str(exc)
        return base
    snap = load_snapshot(target=tgt)
    base.update({
        "resolved": True, "target": tgt.to_dict(),
        "fieldCount": len(snap.fields), "source": snap.source,
        "sourceLabel": snap.label(), "status": snap.status,
        "generatedAt": snap.generated_at,
        "warnings": list(snap.warnings)[:3],
    })
    return base





# ---------------------------------------------------------------- 链接字段


def plan_link_field(bo: Bo, pool: list[ProjectField],
                    target: TargetObject | None = None) -> LinkPlan:
    """该对象的「关联目标对象」链接字段怎么处置：新增 / 复用 / 冲突 / 拒绝。"""
    target = target or project_target()
    label = target.link_label
    if bo.code == target.code:
        return LinkPlan("reject_self", code=target.link_code,
                        field_name=target.link_field, name=label,
                        note=f"对象 {target.code} 不能关联自己（自关联不允许）")
    cands = [f for f in bo.fields
             if f.type_new == "object" and (f.object_type or "") == target.code]
    if cands:
        first = min(cands, key=lambda f: f.sn)
        col = first.resolved_field_name()
        if first.code == target.link_code and col == target.link_field:
            # 已经是系统范式那个链接字段（本轮刚补的，或上一轮「确认版 Excel」带回来的）
            # → 对用户就是「这个对象会/已经有一个 projectId」，不重复新增
            return LinkPlan("add", code=first.code, field_name=col, name=first.name,
                            existing={"code": first.code, "fieldName": col,
                                      "name": first.name, "length": first.length,
                                      "type": first.type_new},
                            note=f"指向{target.name}对象（{col} varchar {first.length or 64}）")
        note = f"= 复用已有链接字段 {first.name}（{first.code}）"
        if col != target.link_field:
            note += f"：物理列 {col}（{target.name}范式为 {target.link_field}），未改动"
        if first.length not in (None, 64) or str(
                (first.options or {}).get("isMultiple")).lower() == "true":
            note += "：定义与系统范式不同（长度/isMultiple），未改动"
        return LinkPlan("reuse", code=first.code, field_name=col, name=first.name,
                        existing={"code": first.code, "fieldName": col,
                                  "name": first.name, "length": first.length,
                                  "type": first.type_new},
                        note=note)
    same_code = bo.field_by_code(target.link_code)
    if same_code is not None:
        # 编码被别的字段占着（不是指向目标对象）→ 不能再补一个同编码字段
        return LinkPlan(
            "conflict", code=same_code.code, field_name=same_code.resolved_field_name(),
            name=same_code.name,
            existing={"code": same_code.code, "name": same_code.name,
                      "type": same_code.type_new},
            note=f"编码 {target.link_code} 已被字段（{same_code.name}）占用"
                 f"且未关联{target.name}")
    for f in bo.fields:
        if f.resolved_field_name() == target.link_field and not (
                f.type_new == "object" and (f.object_type or "") == target.code):
            return LinkPlan(
                "conflict", code=f.code, field_name=f.resolved_field_name(), name=f.name,
                existing={"code": f.code, "name": f.name, "type": f.type_new},
                note=f"物理列 {target.link_field} 已被字段 {f.code}（{f.name}）占用"
                     f"且未关联{target.name}")
    return LinkPlan("add", code=target.link_code, field_name=target.link_field,
                    name=label,
                    note=f"新增：指向{target.name}对象（{target.link_field} varchar 64）")


def ensure_link_field(bo: Bo, target: TargetObject | None = None,
                      pool: list[ProjectField] | None = None,
                      name: str = "") -> tuple[BoField, bool, str]:
    """在对象上**就地**确保「XX选择」链接字段存在并打上 is_link。

    返回 ``(字段, 是否新建, 一条给用户看的提示)``。

    为什么需要它（而不是只靠 ``builder.ensure_project_links``）
    ----------------------------------------------------------
    ``ensure_project_links`` 挂在 ``build()`` 里，是**出包**时的兜底。而 AI 这条路
    （一句话生成 / 需求文档解析 → 确认版 Excel）里有两种产物根本不走 ``build()``：
      ① ``/api/ai/bo``、``/api/ai/sheet`` 返回的**预览**；
      ② ``ai_apply_bo`` 导出的那份 Excel（用户拿着它回头出包，也可能线下改）。
    两处都不补的后果就是 2026-11-20 用户报的那句「我选择关联项目时，生成的字段
    里面没有项目选择字段」：开关亮了、预览和 Excel 里却一个字段都没多出来。
    ``build()`` 里那道兜底保留不动——确认版 Excel、存量包修订页还要靠它。

    幂等口径与 ``ensure_project_links`` 完全一致（同一份 ``plan_link_field``）：
      - 已有指向目标对象的 object 字段 → **复用**，只打 is_link、绝不加第二个；
      - 编码/物理列被别人占着 → 报错（不硬塞，塞进去线上就是两列同名）；
      - 目标对象就是自己 → 报错（自关联）；
      - 需要新建时 sn = 当前最大 sn + 1（链接字段排在业务字段之后）。
    """
    target = target or project_target()
    plan = plan_link_field(bo, pool or [], target)
    if plan.action == "reject_self":
        raise ValueError(f"业务对象 {bo.code}: 对象 {target.code} 不能关联自己"
                         f"（自关联不允许）")
    if plan.action == "conflict":
        raise ValueError(
            f"业务对象 {bo.code}: 物理列 {target.link_field} 已被字段 "
            f"{plan.code}（{plan.name}）占用且未关联{target.name}，"
            f"请先改字段名或改用现有字段")
    want_name = str(name or "").strip()
    existing = bo.field_by_code(plan.code)
    if existing is None:
        sn = max([f.sn for f in bo.fields], default=0) + 1
        field = link_field(name=want_name, sn=sn, target=target)
        field.bo_code = bo.code
        bo.fields.append(field)
        created = True
        note = (f"业务对象 {bo.code}: 关联{target.name}，已补链接字段 {field.code}"
                f"（{field.resolved_field_name()} varchar 64，sn={sn}）")
    else:
        field = existing
        created = False
        # 用户（或 AI）在预览里改过的显示名尊重它；没给才沿用原值。
        # 存量包铁律只保护 code / fieldName / sn，显示名是可以改的。
        if want_name:
            field.name = want_name
        note = (f"业务对象 {bo.code}: 关联{target.name}，复用链接字段 {field.code}"
                f"（{field.resolved_field_name()}，未新增）")
    field.is_link = True
    # 1_业务对象 的「关联项目」列必须跟着变「是」：否则用户拿这份 Excel 回头出包，
    # ensure_project_links 连门都不会进（它只看 project_link）。
    bo.project_link = True
    return field, created, note


def link_field(name: str = "", sn: int = 1,
               target: TargetObject | None = None) -> BoField:
    """按系统范式构造链接字段（10 个结构键逐键对齐 project_create.projectId）。

    field_name 显式给出：``resolved_field_name()`` 本来就会按编码推出同一个值
    （XML 产物不变），但「确认版 Excel」的「物理列名」列取的是
    ``BoField.field_name``——留空那一格是空的，用户看不到「数据库列也一致」。

    ``is_link=True`` 是给下游的口径标记：预览与三张表单里置顶、值来源按
    「全部业务字段 + 主键、不默认过滤」出（见 model.BoField.is_link）。
    """
    target = target or project_target()
    return BoField(
        bo_code="",
        code=target.link_code,
        name=(name or "").strip() or target.link_label,
        type_new="object",
        object_type=target.code,
        length=64,
        precision=None,
        is_null=True,
        is_index=False,
        field_name=target.link_field,
        options=copy.deepcopy(LINK_OPTIONS),
        sn=sn,
        is_link=True,
    )


# ---------------------------------------------------------------- 分析


def _norm_suspects(target: TargetObject | None = None) -> set[str]:
    """可疑快照字段：与目标对象同名/同义的文本列，可能是手抄的快照。

    项目侧固定三项（项目名称/项目简称/项目经理）；其它对象按同样的口径
    加上「{对象名}名称 / {对象名}简称」——例如在采购申请里写「合同名称」
    多半只是文本快照，不应该自动改名成 contract.name 变成对象主标题。
    """
    names = list(SEMANTIC_SUSPECT)
    target = target or project_target()
    if not target.is_project and target.name:
        names += [f"{target.name}名称", f"{target.name}简称"]
    return {normalize(x) for x in names}


def _type_label(type_new: str) -> str:
    return _ft.ui_label(type_new) or type_new


# 差异表的行标签。前端按 label 原样渲染，不再自己猜 key 的含义（零业务规则）。
_CHANGE_LABELS = {
    "name": "显示名",
    "code": "字段编码",
    "fieldName": "物理列名",
    "type": "类型",
    "length": "长度",
    "precision": "小数位",
    "required": "必填",
    "index": "索引",
    "format": "格式",
    "objectType": "关联对象/字典",
}


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _own_format(own: BoField) -> str:
    """我方字段的格式：BoField 不存 format，按类型从 fieldtypes.json 反推。"""
    key = own.type_new if _ft.is_known(own.type_new) else "varchar"
    cfg = _ft.specs().get(key) or {}
    return str(cfg.get("format") or "")


def _own_object_type(own: BoField) -> str:
    """我方「关联对象/字典」：dic 看全局字典 ref，object 看关联对象编码。"""
    if own.type_new == "dic":
        return own.dict_ref or own.object_type or ""
    return own.object_type or ""


def _target_object_type(t: ProjectField) -> str:
    if t.type_new == "dic":
        return (t.dict.ref if t.dict else "") or t.object_type or ""
    return t.object_type or ""


def _own_options(own: BoField) -> list[dict]:
    """我方候选值 → ``[{"key","name"}]``。

    key 可能是空串：Excel 里手填的「选项值」只有名字，业务码是出包时才生成的，
    所以「我方 key 未知」不能当成「与项目不同」。有 dict_items 时以它为准
    （确认版 Excel / 存量包会把项目侧的业务码带回来）。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for it in (own.dict_items or []):
        name = str(it.get("name") or "").strip()
        key = str(it.get("key") or "").strip()
        label = name or key
        if not label or label in seen:
            continue
        seen.add(label)
        out.append({"key": key, "name": label})
    for choice in (own.choices or []):
        label = str(choice or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        out.append({"key": "", "name": label})
    return out


def _target_options(t: ProjectField) -> list[dict]:
    items = t.dict.items if t.dict is not None else ()
    return [{"key": str(i.key or ""), "name": str(i.name or i.key or "")} for i in items]


def _options_diff(own: BoField, t: ProjectField) -> list[dict]:
    """候选值差异。优先按名称配对，没有名称时按 key 配对。

    只回**有差异**的项，每条一行（前端逐行列出，用户要能一眼看出少/多了哪个值）：
      changed      两侧都有，key 或 name 不同（某一侧 key 未知时不比 key）
      only_project 只在项目侧 → 覆盖后我方会多出这个选项值
      only_mine    只在我方 → 覆盖后会被项目侧替换掉（会丢）
    """
    mine = _own_options(own)
    theirs = _target_options(t)
    if not mine and not theirs:
        return []
    rows: list[dict] = []
    used_m: set[int] = set()
    used_t: set[int] = set()

    def push(a: dict | None, b: dict | None, state: str) -> None:
        rows.append({
            "name": (a or b or {}).get("name", ""),
            "mineKey": (a or {}).get("key") or None,
            "mineName": (a or {}).get("name") or None,
            "projectKey": (b or {}).get("key") or None,
            "projectName": (b or {}).get("name") or None,
            "state": state,
        })

    # ① 同名配对（用户看的是名字；同名不同码只是「码不一致」）
    for i, a in enumerate(mine):
        if i in used_m or not a["name"]:
            continue
        for j, b in enumerate(theirs):
            if j in used_t or b["name"] != a["name"]:
                continue
            used_m.add(i)
            used_t.add(j)
            changed = bool(a["key"] and b["key"] and a["key"] != b["key"])
            push(a, b, "changed" if changed else "same")
            break
    # ② 剩下按 key 配对（名字被改过，但业务码没变）
    for i, a in enumerate(mine):
        if i in used_m or not a["key"]:
            continue
        for j, b in enumerate(theirs):
            if j in used_t or not b["key"] or b["key"] != a["key"]:
                continue
            used_m.add(i)
            used_t.add(j)
            push(a, b, "changed")
            break
    # ③ 只在一侧
    for j, b in enumerate(theirs):
        if j not in used_t:
            push(None, b, "only_project")
    for i, a in enumerate(mine):
        if i not in used_m:
            push(a, None, "only_mine")
    return [r for r in rows if r["state"] != "same"]


def _diff_changes(own: BoField, t: ProjectField, update_mode: bool) -> list[dict]:
    """逐项定义差异（只留 mine != project 的项）。"""
    out: list[dict] = []

    def add(key: str, mine, project) -> None:
        left = "" if mine is None else str(mine)
        right = "" if project is None else str(project)
        if left == right:
            return
        out.append({"key": key, "label": _CHANGE_LABELS[key],
                    "mine": left, "project": right})

    add("name", own.name, t.name)
    if not update_mode:
        # 更新模式（对象已有 ID）下 code / 物理列**不会被覆盖**：列在这里等于
        # 骗用户「导入后会改名」。存量包铁律见 patch.py。
        add("code", own.code, t.code)
        add("fieldName", own.resolved_field_name(), t.field_name)
    mine_type = _type_label(own.type_new)
    project_type = _type_label(t.type_new)
    add("type", mine_type, project_type)
    add("length", own.length, t.length)
    add("precision", own.precision, t.precision)
    add("required", _yes_no(not own.is_null), _yes_no(not t.is_null))
    add("index", _yes_no(own.is_index), _yes_no(t.is_index))
    if mine_type == project_type and (t.format or own.type_new in ("datetime", "date")):
        # 格式只对日期/时间有业务含义。varchar 的 fieldtypes.json 里带了一个
        # 占位的 yyyy-MM-dd，照它比对会给每个字符串字段都造一条假差异，所以
        # 只有项目侧真有格式、或字段本身是日期时才比较。
        add("format", _own_format(own), t.format or "")
    add("objectType", _own_object_type(own), _target_object_type(t))
    return out


def _diff_payload(own: BoField, t: ProjectField, rule: str, update_mode: bool,
                  bo: Bo | None = None) -> dict:
    """对齐前的差异明细（前端弹窗「pp_project ↔ 你的 Excel」两列对照用）。

    两侧定义完全一致时返回空 dict —— 前端据此判断「这个字段没有差异」，
    不会为了凑数弹一个空表。
    """
    changes = _diff_changes(own, t, update_mode)
    options = _options_diff(own, t)
    if not changes and not options:
        return {}
    return {
        "objectCode": (bo.code if bo is not None else own.bo_code) or "",
        "objectName": (bo.name if bo is not None else ""),
        "fieldCode": own.code,
        "fieldName": own.name,
        "targetCode": t.code,
        "targetName": t.name,
        "rule": rule,
        "updateMode": update_mode,
        "changes": changes,
        "options": options,
        "count": len(changes) + len(options),
    }


def _warnings_for(own: BoField, t: ProjectField) -> list[str]:
    """对齐会带来什么损失——**分析阶段就算出来**，前端才能在确认前看到。

    这条很容易被漏：warning 原本只在 `_apply_definitions` 里追加，而
    `/api/parse` 只跑 analyze 不跑 apply，于是「自定义候选值将丢失（2 项：甲、乙）」
    只出现在 zip 注释里——没人会去读注释，用户以为对齐是无损的（主理人复验）。
    所以改成 analyze 与 apply 共用本函数，两处文案天然一致。

    只算「用户可预见、可决策」的两类：候选值被替换、多选变单选。
    跨族已经在 reason 里说过了，这里不重复。
    """
    out: list[str] = []
    if t.dict is not None:
        kept = {i.name for i in t.dict.items}
        dropped = [c for c in (own.choices or []) if c not in kept]
        if dropped:
            out.append(
                f"自定义候选值将丢失（{len(dropped)} 项：{'、'.join(dropped[:6])}）")
    multi_own = str((own.options or {}).get("isMultiple")).lower() == "true"
    multi_t = str((t.options or {}).get("isMultiple")).lower() == "true"
    if multi_own and not multi_t and own.length and t.length and own.length > t.length:
        out.append(f"⚠ 多选→单选，长度 {own.length}→{t.length}")
    return out


def _basis_for(rule: str, own, target) -> str:
    """判断依据（人话）。前端只显示这句话，不再自己翻译规则码。"""
    if rule == "E0":
        return "字段名完全一致"
    if rule == "S3":
        code_a, code_b = _code_of(own), _code_of(target)
        if code_a and code_a == code_b:
            return f"字段编码一致（{code_b}）"
        col = _col_of(target)
        return f"物理列一致（{col}）" if col else "字段编码或物理列一致"
    if rule == "S4":
        same = synonym_canon(normalize(_name_of(own)))
        return f"同义词（{normalize(_name_of(own))}≈{normalize(_name_of(target))}，" \
               f"归一后都是「{same}」）"
    if rule == "S2":
        dist = _lev(normalize(_name_of(own)), normalize(_name_of(target)), 2)
        return f"名称只差 {dist} 个字"
    if rule == "S1":
        return "名称包含关系（短名被长名包含）"
    if rule == "C":
        return (f"手动指定：名称包含（{normalize(_name_of(own))} ⊂ "
                f"{normalize(_name_of(target))}）")
    return ""


_CONFIDENCE = {"E0": "high", "S3": "high", "S4": "medium", "S2": "medium",
               "S1": "low", "C": "low"}


def _decide(own: BoField, pool: list[ProjectField], update_mode: bool,
            suspects: set[str], bo: Bo | None = None,
            target: TargetObject | None = None) -> AlignAction | dict:
    """单个我方字段 → AlignAction（找到对应目标字段）或 skipped 记录。"""
    target = target or project_target()
    if own.code in SYSTEM_CODES:
        return {"fieldCode": own.code, "fieldName": own.name, "kind": "system",
                "message": "系统字段/流程字段不参与对齐"}
    # 对象自身的「名称/标题」字段绝不能对齐成项目侧的「项目名称」：那是每个
    # 对象的主标题，改名后列表标题、详情标题、待办标题全变，而用户的「项目名称」
    # 多半只是想存一份快照文本。
    #
    # 为什么**按字段名**判而不只看 code：Excel 里编码留空时，"名称" 会被
    # naming.to_english 翻成 mingCheng（词典里没有「名称→name」这条），
    # 只看 code == "name" 会漏掉最常见的那条路径（主理人 2026-11-14 复验抓到）。
    # 为什么**不用包含匹配**："项目名称" 归一后仍是「项目名称」，它必须进
    # B10 可疑名单弹窗，不能被这条守卫吞掉——所以只认两个完整等价写法。
    own_label = normalize(own.name)
    if own.code == "name" or own_label in ("名称", "标题"):
        where = "项目字段" if target.is_project else f"{target.name}字段"
        return {"fieldCode": own.code, "fieldName": own.name, "kind": "own_name",
                "message": f"本对象自身的名称/标题字段，不参与{where}对齐"}
    if own.type_new == "object" and (own.object_type or "") == target.code:
        return {"fieldCode": own.code, "fieldName": own.name, "kind": "self_link",
                "message": f"已是「{target.link_label}」链接字段，跳过"}
    cands: list[tuple[int, int, str, ProjectField]] = []
    for t in pool:
        rule = similar(own, t)
        if rule:
            cands.append((RANK[rule], t.sn, rule, t))
    # 用户口径（2026-11-15）：像「预算」这种短名，项目里有「预算状态」「总预算」时
    # 都要列出来给他选。similar 的 S1 有收敛条件（短名 2 字、多 1 个字不判相似），
    # 所以这里再补一层「名称包含」候选，只用于下拉/手选，不参与自动判定。
    for t in pool:
        if any(c[3].code == t.code for c in cands):
            continue
        a, b = normalize(own.name), normalize(t.name)
        if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
            cands.append((RANK["C"], t.sn, "C", t))
    if not cands:
        where = "项目对象" if target.is_project else f"{target.name}对象"
        return {"fieldCode": own.code, "fieldName": own.name, "kind": "no_match",
                "message": f"{where}里没有对应字段，按原规则生成"}
    cands.sort(key=lambda x: (x[0], x[1]))
    rank, _sn, rule, best = cands[0]
    # 同分候选（同一条规则命中两个项目字段）→ 多候选：不自动应用，交给用户挑
    ties = [c for c in cands if c[0] == rank]
    ambiguous = len(ties) > 1
    others = [c[3] for c in cands if c[3] is not best]
    suspect = normalize(own.name) in suspects
    family_ok = family(own.type_new) == family(best.type_new)
    action = AlignAction(
        field_code=own.code,
        field_name=own.name,
        target=best,
        rule=rule,
        rank=rank,
        family_ok=family_ok,
        suspect=suspect,
        ambiguous=ambiguous,
        candidates=others[:6],
        rename_code=(own.code if update_mode else best.code),
        rename_field=(own.resolved_field_name() if update_mode else best.field_name),
        diff=_diff_payload(own, best, rule, update_mode, bo),
        # 分析阶段就带上「会丢什么」，前端在弹窗里就能提醒（apply 阶段同源再算一次）
        warnings=_warnings_for(own, best),
        basis=_basis_for(rule, own, best),
        confidence=("low" if ambiguous else _CONFIDENCE.get(rule, "low")),
    )
    # 2026-11-15 用户口径（改死了）：**只有直接对应自动覆盖**。
    #   · E0 精确同名 + 同族 + 不在可疑名单 + 候选唯一 → 自动，不弹窗；
    #   · 其余（S3/S4/S2/S1、E0 可疑、E0 跨族、E0 多候选）→ 一律 pending，
    #     弹窗逐条列出来，默认保留我方，勾了「按项目覆盖」才覆盖。
    action.auto = bool(rule == "E0" and family_ok and not suspect and not ambiguous)
    if action.diff and action.warnings:
        # 「会丢什么」跟着差异一起走：前端在同一个弹窗里就能提醒（不用再拼一条规则）
        action.diff["warnings"] = list(action.warnings)

    notes: list[str] = []
    if update_mode:
        notes.append("更新模式保留编码列名")
    if suspect:
        notes.append(f"可疑快照：可能只是抄{target.name}名称的文本")
    if not family_ok:
        notes.append(f"类型跨族（{_type_label(own.type_new)}→"
                     f"{_type_label(best.type_new)}）")
    if ambiguous:
        notes.append(f"多候选（{len(ties)} 个同分）")
    if is_generic_name(own.name):
        notes.append("通用字段名，匹配可能不准")
    action.note = "；".join(dict.fromkeys(notes))

    side = "项目侧" if target.is_project else f"{target.name}侧"
    if suspect:
        action.reason = (f"同名但可能只是快照文本（{side} {best.code}），"
                         f"不自动覆盖")
    elif not family_ok:
        action.reason = (f"类型跨族（{_type_label(own.type_new)}→"
                         f"{_type_label(best.type_new)}），对齐会改物理类型")
    elif ambiguous:
        action.reason = f"同分候选 {len(ties)} 个（{best.code} 最匹配），需你确认"
    elif rule == "E0":
        action.reason = f"精确同名且同族（{side} {best.code}）"
    else:
        action.reason = (f"{action.basis}（{rule}）：{own.name} ↔ {target.name} "
                         f"{best.name}（{best.code}）")
    return action


def _skip_relation(own: BoField, rec: dict,
                   target: TargetObject | None = None) -> dict:
    """skipped 记录 → 关系行（系统字段 / 本对象名称 / 无匹配）。"""
    target = target or project_target()
    kind = rec.get("kind") or ""
    status = "unrelated" if kind == "no_match" else "unknown"
    notes: list[str] = []
    if kind == "no_match":
        notes.append(rec.get("message") or f"{target.name}对象里没有对应字段")
    elif rec.get("message"):
        notes.append(rec["message"])
    if is_generic_name(own.name):
        notes.append("通用字段名，匹配可能不准")
    return _relation_row(
        field_code=own.code, field_name=own.name, status=status, rule="",
        basis="", confidence="low", target=None, candidates=[],
        diff=None, note="；".join(dict.fromkeys(notes)),
    )


_REL_STATUSES = ("auto", "pending", "applied", "ambiguous", "conflict",
                 "unrelated", "unknown")

# 每个状态对应的「处理」文案（前端原样显示，零业务规则）
_REL_ACTION = {
    "auto": "已自动覆盖",
    "applied": "已按你的选择覆盖",
    "pending": "待确认",
    "ambiguous": "待确认（多候选）",
    "conflict": "保留我方",
    "unrelated": "按原规则生成",
    "unknown": "不参与对齐",
}


def _relation_row(field_code: str, field_name: str, status: str, rule: str,
                  basis: str, confidence: str, target: ProjectField | None,
                  candidates: list, diff: dict | None, note: str) -> dict:
    """一个字段一行的关系表（契约见架构 §2.4 relations）。"""
    return {
        "fieldCode": field_code,
        "fieldName": field_name,
        "status": status,
        "rule": rule or "",
        "basis": basis or "",
        "confidence": confidence or "low",
        "target": _candidate_payload(target),
        "candidates": [c for c in (_candidate_payload(t) for t in candidates) if c],
        "diff": dict(diff) if diff else None,
        "action": _REL_ACTION.get(status, ""),
        "note": note or "",
    }


def _action_relation(action: AlignAction, status: str, note: str = "") -> dict:
    """AlignAction + 最终状态 → 关系行。"""
    note = "；".join(dict.fromkeys([x for x in (action.note, note) if x]))
    return _relation_row(
        field_code=action.field_code, field_name=action.field_name, status=status,
        rule=action.rule, basis=action.basis, confidence=action.confidence,
        target=action.target, candidates=action.candidates,
        diff=action.diff or None, note=note,
    )


def relation_summary(relations: list[dict]) -> dict:
    """关系表按状态计数（前端摘要行、报告合计都用这一份）。"""
    out = {"total": len(relations)}
    for status in _REL_STATUSES:
        out[status] = sum(1 for r in relations if r.get("status") == status)
    # 需要用户确认的 = 未确认的相近/多候选/冲突（冲突也要让用户看见并保留我方）
    out["needsConfirm"] = out["pending"] + out["ambiguous"] + out["conflict"]
    return out



def _conflict_of(action: AlignAction, own: BoField, taken: dict[str, str],
                 used_codes: dict[str, BoField], used_cols: dict[str, BoField],
                 update_mode: bool, title_refs: set[str],
                 target: TargetObject | None = None) -> dict | None:
    """这条对齐会不会撞车（目标 / 编码 / 物理列 / 主身份）。撞了就返回冲突记录。

    四种冲突都按「跳过该字段、出一行报告」处理，绝不中止出包：
      target_taken   目标字段已被别的我方字段认领（同一目标只认一个）
      code_taken     对齐后的编码被本对象另一个字段占着
      fieldname_taken 对齐后的物理列被占着（更新模式不会改列名，所以不查）
      identity       是本对象的标题/主身份字段，改名会改列表与详情的主标题
    """
    target = target or project_target()
    target_code = action.target.code
    if target_code in taken:
        return {
            "fieldCode": own.code, "kind": "target_taken", "target": target_code,
            "message": f"{target.name}字段 {target_code} 已被字段"
                       f"「{taken[target_code]}」认领，已跳过",
        }
    if action.rename_code != own.code:
        other = used_codes.get(action.rename_code)
        if other is not None and other is not own:
            return {
                "fieldCode": own.code, "kind": "code_taken",
                "target": action.rename_code,
                "message": f"编码 {action.rename_code} 已被字段「{other.name}」占用，已跳过",
            }
    if not update_mode and action.rename_field != own.resolved_field_name():
        other = used_cols.get(action.rename_field)
        if other is not None and other is not own:
            return {
                "fieldCode": own.code, "kind": "fieldname_taken",
                "target": action.rename_field,
                "message": f"物理列 {action.rename_field} 已被字段「{other.name}」占用，已跳过",
            }
    if action.rename_code == "name" and own.code in title_refs:
        # B3-4：不要因为对齐把本对象自己的主身份字段改掉
        return {
            "fieldCode": own.code, "kind": "identity", "target": "name",
            "message": f"该字段是本对象的标题/主身份字段，对齐成{target.name} name "
                       f"会改变列表与详情的主标题，已跳过",
        }
    return None


def _manual_rule(own: BoField, picked: ProjectField) -> str:
    """用户手选了一个「仅名称包含」的候选（如 预算 → 总预算）。

    similar() 对「多 1 个字 + 短名 2 字」会刻意返回 None（PL04 的金额/总金额），
    但用户明确选了它，就必须能落；规则记为 C，依据写明是手动指定。
    """
    a, b = normalize(own.name), normalize(picked.name)
    if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
        return "C"
    return ""


def _retarget(action: AlignAction, own: BoField, picked: ProjectField,
              update_mode: bool, bo: Bo | None) -> None:
    """把这条关系改指到**用户选中的**候选项目字段（多候选场景）。

    多候选时「最匹配」只是排序第一，可能是错的；用户在下拉里选了另一个，就得按
    那一个覆盖。规则 / 依据 / 置信度 / 差异 / 会丢什么**全部按新目标重算**——
    沿用原来那套说明会让弹窗里写的依据和实际覆盖的字段对不上。
    """
    rule = similar(own, picked) or _manual_rule(own, picked) or action.rule
    # 选了 A 之后，「其它候选」就是除 A 之外的那些（含原来的最匹配）——
    # 不清理会留下 target 自己，前端下拉里出现两个一样的选项
    others = [t for t in ([action.target] + list(action.candidates or []))
              if t is not None and t.code != picked.code]
    action.target = picked
    action.candidates = others[:6]
    action.rule = rule
    action.rank = RANK.get(rule, action.rank)
    action.family_ok = family(own.type_new) == family(picked.type_new)
    action.rename_code = own.code if update_mode else picked.code
    action.rename_field = (own.resolved_field_name() if update_mode
                           else picked.field_name)
    action.diff = _diff_payload(own, picked, rule, update_mode, bo)
    action.warnings = _warnings_for(own, picked)
    if action.diff and action.warnings:
        action.diff["warnings"] = list(action.warnings)
    action.basis = _basis_for(rule, own, picked)
    action.confidence = _CONFIDENCE.get(rule, "low")
    notes = [n for n in (action.note or "").split("；")
             if n and not n.startswith("多候选")]
    notes.append(f"多候选：已按你选的 {picked.code} 覆盖")
    action.note = "；".join(dict.fromkeys(notes))
    action.ambiguous = False          # 已经人工拍过了，不再是「不敢替你决定」


def _picked_target(action: AlignAction, want) -> ProjectField | None:
    """align 的值是字符串时 → 用户指定的项目字段（必须是本字段的候选之一）。

    只认 ``[最匹配] + candidates``：前端传来的 code 一律当**选择**看，不当定义看
    （服务端仍自己算规则/差异），所以选一个不在候选里的字段没有任何意义，
    直接返回 None 由调用方忽略。
    """
    if not isinstance(want, str) or not want:
        return None
    for t in [action.target] + list(action.candidates or []):
        if t is not None and t.code == want:
            return t
    return None


def _plan_object(bo: Bo, pool: list[ProjectField], decision: dict | None = None,
                 mode: str = "excel", project=None,
                 target: TargetObject | None = None) -> ObjectPlan:
    """一个对象的对齐计划。纯计算，不改模型。

    ``decision`` 是本对象的决策（``{"linked":…, "align":{字段: true/编码/false}}``）：
      align[field] = true        → 按最匹配的目标字段覆盖（相近字段的「按项目覆盖」）
      align[field] = "targetCode"→ 按指定的候选覆盖（多候选下拉里挑的那个）
      align[field] = false / 缺省 → 保留我方（直接对应仍然自动覆盖）
    """
    target = target or project_target()
    decision = decision or {}
    update_mode = mode == "package" or bool(getattr(bo, "exist_id", "") or "")
    plan = ObjectPlan(
        object_code=bo.code,
        object_name=bo.name,
        linked=bool(getattr(bo, "project_link", False)),
        update_mode=update_mode,
        link=plan_link_field(bo, pool, target),
    )
    if not pool:
        # 快照不可用：一个字段都没法判断 → 全部 unknown（不冒充「无关字段」，
        # 否则报告会告诉用户「目标对象里没有这些字段」，而事实是**我们不知道**）
        label = "项目" if target.is_project else target.name
        for own in sorted(bo.fields, key=lambda f: f.sn):
            plan.relations.append(_relation_row(
                field_code=own.code, field_name=own.name, status="unknown",
                rule="", basis="", confidence="low", target=None, candidates=[],
                diff=None, note=f"{label}字段快照不可用，无法判断对应关系",
            ))
        return plan

    suspects = _norm_suspects(target)
    decided: list[tuple[AlignAction, BoField]] = []
    for own in sorted(bo.fields, key=lambda f: f.sn):
        got = _decide(own, pool, update_mode, suspects, bo, target)
        if isinstance(got, dict):
            plan.skipped.append(got)
            plan.relations.append(_skip_relation(own, got, target))
            continue
        decided.append((got, own))

    align_decision = decision.get("align") or {}

    # 同一目标只认一个我方字段（B5）：按 (规则等级, 我方 sn) 升序认领
    taken: dict[str, str] = {}
    used_codes = {f.code: f for f in bo.fields}
    used_cols = {f.resolved_field_name(): f for f in bo.fields}
    title_refs = set()
    if project is not None:
        for lst in project.lists:
            if lst.bo_code == bo.code:
                for v in (lst.title_field, lst.sort_field, lst.parent_field,
                          lst.detail_field, lst.board_group):
                    if v:
                        title_refs.add(v)

    for action, own in sorted(decided, key=lambda p: (p[0].rank, p[1].sn)):
        want = align_decision.get(action.field_code)
        # 多候选：align 的值可以直接写项目字段编码（用户在弹窗下拉里挑的那个）。
        # **必须在冲突检查之前**改指，否则目标/编码撞车是按原来那个字段算的。
        picked = _picked_target(action, want)
        if picked is not None and picked is not action.target:
            _retarget(action, own, picked, update_mode, bo)
        elif isinstance(want, str) and want and picked is None:
            where = "项目" if target.is_project else target.name
            msg = f"决策里指定的{where}字段 {want} 不在候选里，已忽略"
            plan.warnings.append(
                f"决策里给字段 {own.code} 指定的{where}字段 {want} 不在候选里，已忽略")
            action.note = "；".join(x for x in (action.note, msg) if x)
        conflict = _conflict_of(action, own, taken, used_codes, used_cols,
                                update_mode, title_refs, target)
        if conflict is not None:
            # 冲突不进 pending：用户不能选「按项目覆盖」（会撞车），只能保留我方
            plan.conflicts.append(conflict)
            action.status = "conflict"
            plan.relations.append(_action_relation(
                action, "conflict", conflict["message"]))
            continue
        taken[action.target.code] = own.name or own.code
        if picked is not None:
            # 已经人工拍了（含「换成另一个候选」）→ 视同确认
            action.confirmed = True
            action.status = "applied"
            plan.aligned.append(action)
            plan.relations.append(_action_relation(action, "applied"))
            continue
        if want is False:
            # 决策（向后兼容 CLI / 存量客户端）：显式「不对齐」仍然尊重
            plan.skipped.append({
                "fieldCode": own.code, "fieldName": own.name, "kind": "user_skip",
                "message": "决策里明确选了不对齐，保持原样",
            })
            action.status = "pending"
            plan.pending.append(action)
            plan.relations.append(_action_relation(
                action, "pending", "决策里明确选了不对齐，保持原样"))
            continue
        if action.auto:
            # 直接对应：自动覆盖，不进弹窗
            action.status = "auto"
            plan.aligned.append(action)
            plan.relations.append(_action_relation(action, "auto"))
            continue
        if want is True:
            # 用户在「确认相近字段」里勾了「按项目覆盖」→ 视同确认（多候选也对齐了）
            action.confirmed = True
            action.ambiguous = False
            action.status = "applied"
            plan.aligned.append(action)
            plan.relations.append(_action_relation(
                action, "applied", "已按你的勾选覆盖"))
            continue
        action.status = "ambiguous" if action.ambiguous else "pending"
        plan.pending.append(action)
        plan.relations.append(_action_relation(action, action.status))

    known = {f.code for f in bo.fields}
    for key in (decision.get("align") or {}):
        if key not in known:
            plan.warnings.append(f"决策里的字段 {key} 不在对象 {bo.code} 内，已忽略（Excel 可能改过）")
    # 关系表按我方字段原有顺序排列（Excel 里从上到下，用户对得上号）
    order = {f.code: f.sn for f in bo.fields}
    plan.relations.sort(key=lambda r: order.get(r.get("fieldCode"), 0))
    return plan


def analyze(project, snap: Snapshot | None = None,
            decisions: dict | None = None,
            target: TargetObject | None = None) -> dict:
    """只读分析，返回 view model（契约见架构 §2.4）。绝不修改 Project。

    ``decisions``：解析出来的 X-Project-Link（可以是 None）。带上它，用户勾过的
    「按项目覆盖」才会体现在预览里——**预览必须等于产物**，否则用户在弹窗里勾完，
    页签还写着「待确认」，出包却已经改成了项目定义。``align`` 的值是**选择**
    （true / "项目字段编码" / false），规则与差异一律由服务端重算。

    除了「每个对象对齐了什么」，这里还产出**给弹窗用**的清单：

    * ``relations``   —— 每个我方字段一行关系（状态/依据/项目字段/候选/差异），
      弹窗与页签都只渲染它，前端零业务规则。
    * ``diffs``       —— 有差异的字段一份差异明细（pp_project ↔ 你的 Excel），
      含每个选项值的变化；两侧都一样的字段不出现。**含未确认的相近字段**：
      用户要先看到「勾了会变成什么」才能决定勾不勾。
    * ``addedLinks``  —— 缺「所属项目」而被自动补上的链接字段（Excel 里没这列）。
    * ``newFields``   —— Excel 里有、项目里没有的字段（按原规则生成，不覆盖、不弹窗）。

    2026-11-15 口径：只有直接对应（E0 同族唯一）自动覆盖；其余进 ``pending``，
    等用户在弹窗里逐条勾。``pendingCount`` = 需要用户看一眼的条数。
    """
    # 快照 token：用户上传目标对象包后，preview（analyze）和 build（apply）必须用同一份，
    # 否则页面上看到的是「随包快照」的对齐结果，出包却按上传包改字段——两边对不上。
    target = target or project_target()
    snap = snap if snap is not None else load_snapshot(
        (decisions or {}).get("snapshot") or None, target=target)
    pool = snap.pool()
    items: list[dict] = []
    diffs: list[dict] = []
    relations: list[dict] = []
    added_links: list[dict] = []
    new_fields: list[dict] = []
    summary = {"linkedObjects": 0, "exact": 0, "similar": 0, "skipped": 0,
               "linkFields": 0, "diffs": 0, "addedLinks": 0, "newFields": 0}
    warnings = list(snap.warnings)
    notices: list[str] = []

    for bo in project.bos:
        if not getattr(bo, "project_link", False):
            items.append({"objectCode": bo.code, "objectName": bo.name, "linked": False,
                          "updateMode": bool(getattr(bo, "exist_id", "") or ""),
                          "linkField": None, "exact": [], "similar": [],
                          "pending": [], "relations": [], "relationSummary": {},
                          "pendingCount": 0,
                          "conflicts": [], "skipped": [], "diffCount": 0})
            continue
        plan = _plan_object(bo, pool, ((decisions or {}).get("objects") or {}).get(bo.code),
                            "excel", project, target)
        summary["linkedObjects"] += 1
        # summary 里的 exact / similar 仍然按**证据强度**分（E0 vs 相近规则），
        # 但只统计**这次真的要应用**的那些（auto + 已确认）
        summary["exact"] += sum(1 for a in plan.aligned if a.rule == "E0")
        summary["similar"] += sum(1 for a in plan.aligned if a.rule != "E0")
        summary["skipped"] += len(plan.skipped)
        if plan.link and plan.link.action == "add":
            summary["linkFields"] += 1
        if plan.link and plan.link.action == "conflict":
            warnings.append(f"业务对象 {bo.code}：{plan.link.note}，出包会被中止")

        # 差异同时给「要覆盖的」和「还没确认的」：未确认的字段也必须能让用户
        # 先看清「勾了会变成什么」，否则弹窗里只剩一句「待确认」
        obj_diffs = [a.diff for a in plan.aligned + plan.pending if a.diff]
        diffs.extend(obj_diffs)
        obj_rel = plan.relations
        relations.extend(obj_rel)
        rel_sum = relation_summary(obj_rel)
        # 真正「自动补」的才记：plan_link 的 add 分支里带 existing 表示这个字段
        # 本来就长成系统范式（上一轮补过 / 确认版 Excel 带回来），不是这次补的
        if plan.link and plan.link.action == "add" and plan.link.existing is None:
            added_links.append({
                "objectCode": bo.code,
                "objectName": bo.name,
                "fieldName": plan.link.name or target.link_label,
                "code": plan.link.code or target.link_code,
            })
        new_fields.extend({
            "objectCode": bo.code,
            "objectName": bo.name,
            "fieldCode": s.get("fieldCode", ""),
            "fieldName": s.get("fieldName") or s.get("fieldCode", ""),
        } for s in plan.skipped if s.get("kind") == "no_match")

        items.append({
            "objectCode": bo.code,
            "objectName": bo.name,
            "linked": True,
            "updateMode": plan.update_mode,
            "linkField": (None if plan.link is None else {
                "action": plan.link.action,
                "code": plan.link.code,
                "fieldName": plan.link.field_name,
                "name": plan.link.name,
                "type": "object",
                "objectType": target.code,
                "length": 64,
                "note": plan.link.note,
            }),
            # exact / similar 按规则强度分（都已在 aligned 里 = 会应用）
            "exact": [a.to_dict() for a in plan.aligned if a.rule == "E0"],
            "similar": [a.to_dict() for a in plan.aligned if a.rule != "E0"],
            # 未确认的相近字段：不应用，等弹窗里逐条勾
            "pending": [a.to_dict() for a in plan.pending],
            "pendingCount": rel_sum["needsConfirm"],
            "relations": obj_rel,
            "relationSummary": rel_sum,
            "conflicts": plan.conflicts,
            "skipped": plan.skipped,
            "warnings": plan.warnings,
            "diffs": obj_diffs,
            "diffCount": len(obj_diffs),
        })

    rel_sum = relation_summary(relations)
    summary["diffs"] = len(diffs)
    summary["addedLinks"] = len(added_links)
    summary["newFields"] = len(new_fields)
    summary.update(rel_sum)
    summary["pendingCount"] = rel_sum["needsConfirm"]

    if plans := [i for i in items if i["linked"]]:
        if target.is_project:
            notices.append(
                "关联项目的对象只做「字段定义对齐」；如需把数据归属挂到项目下，"
                "请在「所属关系」页签勾选父子关系")
        else:
            notices.append(
                f"关联「{target.name}」的对象只做「字段定义对齐」；"
                f"如需把数据归属挂到{target.name}下，请在「所属关系」页签勾选父子关系")
    status = snap.status
    notice = None
    if status == "unavailable":
        if target.is_project:
            notice = ("项目字段快照不可用（未连库刷新、无随包快照、未上传项目包），"
                      "本次只补「所属项目」链接字段、不做字段对齐")
        else:
            notice = (f"{target.name}字段快照不可用（未连库刷新、未上传{target.name}包），"
                      f"本次只补「{target.link_label}」链接字段、不做字段对齐")
    elif not plans:
        notice = None
    return {
        "status": status,
        "snapshotSource": snap.source,
        "snapshotSourceLabel": snap.label(),
        "snapshotGeneratedAt": snap.generated_at,
        "target": target.to_dict(),
        "project": {**dict(snap.project or {}),
                    "fieldCount": len(snap.fields)} if snap.fields else {},
        "notice": notice,
        "warnings": warnings,
        "summary": summary,
        "items": items,
        "notices": notices,
        # ---- 弹窗/页签用的清单（前端零业务规则，直接渲染）
        "relations": relations,
        "relationSummary": rel_sum,
        "pendingCount": rel_sum["needsConfirm"],
        "diffs": diffs,
        "diffCount": len(diffs),
        "addedLinks": added_links,
        "addedLinkCount": len(added_links),
        "newFields": new_fields,
        "newFieldCount": len(new_fields),
    }


# ---------------------------------------------------------------- 决策头


def parse_decisions(raw: str | None, project=None) -> dict | None:
    """解析 X-Project-Link。None = 没带；非法 → ValueError（API 层转 400）。

    决策里的值只当**选择**用，不当定义用：服务端一律重新分析，不采信前端给的
    编码/类型/长度（前端可能拿旧 Excel 的 code 提交，信任它等于允许静默改名）。

    ``objects[code].align[字段编码]`` 有三种取值（2026-11-15）：
      true            按最匹配的项目字段覆盖
      "targetCode"    按**指定的**项目字段覆盖（多候选时用户在弹窗下拉里挑的那个；
                      服务端会校验它是不是本字段的候选，不是就忽略并记一条 note）
      false / 缺省     保留我方
    """
    if raw is None or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"X-Project-Link 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("X-Project-Link 需要 {\"version\":1,\"objects\":{...}}")
    target = data.get("target") or {}
    if target and not isinstance(target, dict):
        raise ValueError("X-Project-Link.target 必须是 {name, code} 对象")
    t_name = str(target.get("name") or "").strip()
    t_code = str(target.get("code") or "").strip()
    if t_name and len(t_name) > 100:
        raise ValueError("X-Project-Link.target.name 过长")
    if t_code and not re.match(r"^[A-Za-z0-9_.-]{1,64}$", t_code):
        raise ValueError("X-Project-Link.target.code 不合法")
    objects = data.get("objects")
    if not isinstance(objects, dict):
        raise ValueError("X-Project-Link.objects 必须是对象")
    pkg_codes = {b.code for b in getattr(project, "bos", []) or []}
    for bo_code, d in objects.items():
        if not isinstance(d, dict):
            raise ValueError(f"决策 {bo_code} 必须是对象")
        if pkg_codes and bo_code not in pkg_codes:
            raise ValueError(f"决策里的对象 {bo_code} 不在本包内")
        if "linked" in d and not isinstance(d["linked"], bool):
            raise ValueError(f"决策 {bo_code}.linked 必须是 true/false")
        if "linkField" in d and not isinstance(d["linkField"], bool):
            raise ValueError(f"决策 {bo_code}.linkField 必须是 true/false")
        align = d.get("align")
        if align is not None:
            if not isinstance(align, dict):
                raise ValueError(f"决策 {bo_code}.align 必须是对象")
            for key, val in align.items():
                if isinstance(val, bool):
                    continue
                if isinstance(val, str) and val.strip():
                    continue
                raise ValueError(
                    f"决策 {bo_code}.align[{key}] 必须是 true/false 或项目字段编码")
    token = str(data.get("snapshot") or "").strip()
    if token and not _TOKEN_RE.match(token):
        raise ValueError("字段快照标识无效，请重新上传业务对象 zip")
    return {"version": 1, "snapshot": token or "",
            "target": {"name": t_name, "code": t_code} if (t_name or t_code) else {},
            "objects": objects}


def decisions_link_object(decisions: dict | None, code: str) -> bool:
    """决策头里这个对象要不要「强制补 XX选择」。

    AI 路径（一句话生成 / 需求文档解析 / 微调）的常态是「生成前还没有对象编码」，
    所以 ``objects`` 往往是空的，或者只有别的对象 —— 「头在、但没提到我」按
    **要补**处理：头本身表达的就是「这次导入/生成要关联业务对象」。

    头里明确写了 ``linked: false``（用户在开关上关掉了）→ 不补，尊重它。
    与 ``apply_linked`` 同一个语义，避免「预览补了、出包又说没勾」的两套判断。
    """
    if decisions is None:
        return False
    entry = (decisions.get("objects") or {}).get(code)
    if isinstance(entry, dict) and "linked" in entry:
        return bool(entry["linked"])
    return True


# ---------------------------------------------------------------- 应用


def apply_linked(project, decisions: dict | None = None,
                 target: TargetObject | None = None) -> list[str]:
    """只把「关联项目」这个**布尔开关**写进模型，一个字段都不动。

    为什么单独一个函数（而不是复用 ``apply``）：解析页要把开关的结果**先**落到
    模型上，才能让 ``analyze`` 把刚勾上的对象算进对齐清单；而 ``analyze`` 必须
    在看得到「对齐前」的模型时跑，所以这一段不能顺手把字段也改了——字段对齐、
    改名、引用重写仍然只由 ``apply`` 负责（顺序见 webui.parse_excel 的四条约束）。

    decisions 的 ``align`` 一律忽略（那是 ``apply`` 的事）；对象不存在时不抛，
    只在返回值里给一条忽略提示——Excel 是用户手改的，页面上一个过期的 code
    不该让整次解析 400。

    Returns:
        给用户看的提示（「哪个对象：关联项目=是/否」），可直接 extend 进 notes。
    """
    notices: list[str] = []
    target = target or project_target()
    label = "关联项目" if target.is_project else f"关联{target.name}"
    objects = (decisions or {}).get("objects") if isinstance(decisions, dict) else None
    if not isinstance(objects, dict):
        return notices
    known = {bo.code: bo for bo in getattr(project, "bos", []) or []}
    for bo_code, d in objects.items():
        if not isinstance(d, dict) or "linked" not in d:
            continue
        bo = known.get(bo_code)
        if bo is None:
            notices.append(f"决策里的对象 {bo_code} 不在本次 Excel 内，已忽略")
            continue
        want = bool(d["linked"])
        # 值没变就不刷提示：用户只是重开一次页面，不该满屏「已设置」
        if bool(getattr(bo, "project_link", False)) == want:
            continue
        bo.project_link = want
        notices.append(f"对象 {bo_code}：{label}={'是' if want else '否'}（来自界面开关）")
    return notices


def _apply_definitions(bo: Bo, actions: list[AlignAction]) -> list[dict]:
    """把对齐结果写进字段定义（**不改 code/field_name**，改名由下一阶段做）。"""
    written: list[dict] = []
    target_names = {a.target.name for a in actions}
    for a in actions:
        own = bo.field_by_code(a.field_code)
        if own is None:
            continue
        t = a.target
        length_before = own.length
        name_before = own.name
        # 「会丢什么」与 analyze 阶段同一份逻辑（同一函数），去重后挂到报告上
        for w in _warnings_for(own, t):
            if w not in a.warnings:
                a.warnings.append(w)

        own.type_new = t.type_new
        # datatype 由 resolved() 按 type_new 从 fieldtypes.json 推（架构 §3.9）
        own.datatype = ""
        if t.type_new == "dic":
            own.object_type = t.dict.ref if t.dict else ""
        elif t.type_new == "object":
            own.object_type = t.object_type
        else:
            own.object_type = ""
        own.length = t.length
        own.precision = t.precision
        own.is_null = t.is_null
        own.is_index = t.is_index
        own.default_value = t.default_value or ""
        # options **深拷贝项目侧**，禁止合成 optionSource（架构 R4）
        own.options = copy.deepcopy(dict(t.options or {}))
        if t.dict is not None:
            # 丢弃提示已由 _warnings_for 统一算过，这里只负责落值
            own.dict_ref = t.dict.ref
            own.dict_name = t.dict.name or t.dict.ref
            own.dict_stru = t.dict.stru_type or "1"
            own.dict_items = [{"key": i.key, "name": i.name, "sn": i.sn}
                              for i in t.dict.items]
            own.choices = [i.name for i in t.dict.items]
            own.choice_groups = {}
        elif t.type_new == "dic":
            own.dict_ref = ""
            own.dict_name = ""
            own.dict_stru = ""
            own.dict_items = []
        else:
            own.dict_ref = own.dict_name = own.dict_stru = ""
            own.dict_items = []
        # 显示名跟着项目侧走（主理人拍板：让「一致」名副其实）
        if own.name != t.name:
            clash = [f for f in bo.fields if f is not own and f.name == t.name]
            if clash and t.name not in target_names:
                a.warnings.append(
                    f"本对象已有字段也叫「{t.name}」，显示名会重复（不阻断，建议手动改名）")
            own.name = t.name
        written.append({
            "objectCode": bo.code,
            "objectName": bo.name,
            "fieldCode": a.field_code,
            "fieldName": name_before,
            "newCode": a.rename_code,
            # column = **我方**物理列（update_mode 下就是「保留」的那一列），
            # projectField = 项目侧物理列。以前这里只有 newField（= rename_field，
            # 也是我方列），报告就把它当成「项目侧」写出来，还拿显示名当列名——
            # QA D1 抓到的文案错位。
            "column": own.resolved_field_name(),
            "projectField": t.field_name,
            "newField": a.rename_field,
            "rule": a.rule,
            "type": t.type_new,
            "objectType": own.object_type or "",
            "length": t.length,
            "lengthBefore": length_before,
            "warnings": list(a.warnings),
            "updateMode": False,
        })
    return written


def _apply_renames(bo: Bo, actions: list[AlignAction]) -> dict[str, str]:
    """两阶段改名（架构 D7）：先改成临时码，再改成终码。

    为什么必须两阶段：互换场景（A→B、B→A）单阶段会互相覆盖。
    统一走一条路径比「检测到交换才两阶段」更容易验证。
    """
    pairs: list[tuple[BoField, str, str, str, str]] = []
    for a in actions:
        own = bo.field_by_code(a.field_code)
        if own is None:
            continue
        old_col = own.resolved_field_name()
        new_code = a.rename_code or own.code
        new_col = a.rename_field or old_col
        if new_code == own.code and new_col == old_col:
            # 名字没变也要把物理列写进模型：确认版 Excel 的「物理列名」列取的是
            # BoField.field_name，留空那一格就是空的（QA O2），用户看不到
            # 「数据库列已与项目一致」这件事实
            own.field_name = new_col
            continue
        pairs.append((own, own.code, old_col, new_code, new_col))
    if not pairs:
        return {}
    # 第一阶段：全部挪到临时码，保证第二阶段不会撞车
    for i, (own, _old, old_col, _nc, new_col) in enumerate(pairs, start=1):
        own.code = f"__pl_tmp_{i}"
        own.field_name = f"__pl_tmp_{i}_"
        _ = old_col, new_col
    mapping: dict[str, str] = {}
    for own, old_code, old_col, new_code, new_col in pairs:
        own.code = new_code
        own.field_name = new_col
        if old_code != new_code:
            mapping[old_code] = new_code
        if old_col != new_col:
            mapping[old_col] = new_col
    # 自检：临时码不许泄漏，code/field_name 必须唯一（早失败，不留半成品）
    left = [f.code for f in bo.fields if f.code.startswith("__pl_tmp_")]
    if left:
        raise ValueError(f"对齐改名未完成，残留临时编码 {left}，已中止")
    codes = [f.code for f in bo.fields]
    if len(codes) != len(set(codes)):
        dup = {c for c in codes if codes.count(c) > 1}
        raise ValueError(f"对齐后出现重复编码 {'、'.join(sorted(dup))}，已中止")
    cols = [f.resolved_field_name() for f in bo.fields]
    if len(cols) != len(set(cols)):
        dup = {c for c in cols if cols.count(c) > 1}
        raise ValueError(f"对齐后出现重复物理列名 {'、'.join(sorted(dup))}，已中止")
    return mapping


def _map_value(value: str, mapping: dict[str, str]) -> str:
    return mapping.get(str(value or ""), value)


def _rewrite_refs(project, bo_code: str, mapping: dict[str, str],
                  aligned_codes: set[str] | None = None
                  ) -> tuple[dict[str, int], list[str], list[str]]:
    """把按字段编码引用的 10 处落点全部改掉（表单脚本只检测不修改）。

    aligned_codes：本次对齐过的字段（**对齐后的 code**）。这些字段的类型可能
    从「选项」变成「对象」或反过来，表单控件的 field_type/取值来源必须跟着换，
    否则包里会留下一个引用「已经不存在的字典」的控件（导入校验直接拦死）。
    只处理这几个字段，用户手工配的其它控件一律不动。
    """
    hits: dict[str, int] = {}
    script_keys: list[str] = []
    notes: list[str] = []
    aligned_codes = set(aligned_codes or ())
    if not mapping and not aligned_codes:
        return hits, script_keys, notes

    def bump(where: str, n: int = 1) -> None:
        if n:
            hits[where] = hits.get(where, 0) + n

    bo = project.bo(bo_code)
    if bo is not None:
        for f in bo.fields:
            if f.cascade_parent:
                new = _map_value(f.cascade_parent, mapping)
                if new != f.cascade_parent:
                    f.cascade_parent = new
                    bump("字段级联父字段")
        if bo.parent_field:
            new = _map_value(bo.parent_field, mapping)
            if new != bo.parent_field:
                bo.parent_field = new
                bump("父子关系关联字段")

    forms = [f for f in project.forms if f.bo_code == bo_code]
    for form in forms:
        for ff in form.fields:
            for attr in ("name", "cascade_parent", "value_source"):
                cur = getattr(ff, attr)
                if not cur:
                    continue
                new = _map_value(cur, mapping)
                if new != cur:
                    setattr(ff, attr, new)
                    bump("表单控件")
        for rule in form.hide_rules:
            for attr in ("target", "source"):
                cur = getattr(rule, attr)
                if not cur:
                    continue
                new = _map_value(cur, mapping)
                if new != cur:
                    setattr(rule, attr, new)
                    bump("隐藏规则")

    # 对齐过的字段：控件类型/取值来源要跟着新类型走
    if aligned_codes and bo is not None:
        # lazy import：blueprint 依赖 model/modules，放模块顶层会让 import 图更绕
        from .blueprint import control_for
        for form in forms:
            for ff in form.fields:
                if ff.name not in aligned_codes:
                    continue
                bf = bo.field_by_code(ff.name)
                if bf is None:
                    continue
                want = bf.value_source()
                if bf.type_new == "dic":
                    if ff.field_type != "dictionary":
                        ff.field_type = "dictionary"
                        bump("表单控件（改字典）")
                elif bf.type_new == "object":
                    # 字段对齐成「关联对象」后，原来那个文本框就是个死控件：
                    # 既选不到关联对象，也没有取值来源，校验还会报「object 字段
                    # 没有下拉」。所以**强制**改掉（与 dic 的口径一致，主理人拍板）：
                    #   · select / linkdata（树形关联）保持原样，只补取值来源；
                    #   · 其他类型（text / dictionary / number…）换成对象该有的控件，
                    #     并记一条 notice，让用户知道界面上发生了什么。
                    if ff.field_type not in ("select", "linkdata"):
                        old_type = ff.field_type
                        ff.field_type = control_for(bf)
                        ff.value_source = bf.object_type or ""
                        notes.append(
                            f"表单 {form.key} 控件「{ff.label or bf.name}」{old_type} → "
                            f"{ff.field_type}（字段对齐后为关联对象 {bf.object_type}）")
                        bump("表单控件（改下拉）")
                        continue
                else:
                    if ff.field_type in ("dictionary", "select", "linkdata"):
                        ff.field_type = "text"
                        ff.value_source = ""
                        bump("表单控件（改文本）")
                    continue
                if want and ff.value_source != want:
                    ff.value_source = want
                    bump("表单控件（取值来源）")

    for lst in project.lists:
        if lst.bo_code != bo_code:
            continue
        changed = 0
        lst.columns = [_map_value(c, mapping) for c in lst.columns]
        changed += 0
        for attr in ("sort_field", "parent_field", "title_field", "board_group",
                     "gantt_start", "gantt_end", "gantt_progress", "detail_field"):
            cur = getattr(lst, attr)
            if not cur:
                continue
            new = _map_value(cur, mapping)
            if new != cur:
                setattr(lst, attr, new)
                changed += 1
        bump("列表", changed + (0 if lst.columns else 0))

    for wf in project.workflows:
        if wf.bo_code != bo_code:
            continue
        for edge in wf.edges:
            if not edge.condition_field:
                continue
            new = _map_value(edge.condition_field, mapping)
            if new != edge.condition_field:
                edge.condition_field = new
                bump("流程条件")

    # 表单脚本（7_表单脚本）：JS 里的字段编码无法静态解析 → 只检测、列清单给人工核对
    for fkey, script in (project.form_scripts or {}).items():
        if not any(f.key == fkey for f in forms):
            continue
        for old in mapping:
            if len(old) < 3:
                continue
            if re.search(rf"(?<![\w$]){re.escape(old)}(?![\w$])", str(script or "")):
                script_keys.append(fkey)
                break
    return hits, sorted(set(script_keys)), notes


def apply(project, decisions: dict | None = None, snap: Snapshot | None = None,
          mode: str = "excel", target: TargetObject | None = None) -> dict:
    """把对齐结果写进模型。服务端重新分析，不信任前端给的 code/定义。"""
    decisions = decisions or {}
    target = target or project_target()
    snap = snap if snap is not None else load_snapshot(
        decisions.get("snapshot") or None, target=target)
    pool = snap.pool()
    dec_objects = decisions.get("objects") if isinstance(decisions, dict) else None
    report = {
        "applied": 0, "objects": {}, "aligned": [], "kept": [], "skipped": [],
        "pending": [], "conflicts": [], "linkFields": [], "rewrites": {},
        "scriptFormKeys": [], "notices": [], "noop": True,
    }
    if snap.status == "unavailable":
        if target.is_project:
            report["notices"].append(
                "项目字段快照不可用（未连库刷新、无随包快照、未上传项目包），"
                "本次只补「所属项目」链接字段、不做字段对齐")
        else:
            report["notices"].append(
                f"{target.name}字段快照不可用（未连库刷新、未上传{target.name}包），"
                f"本次只补「{target.link_label}」链接字段、不做字段对齐")

    for bo in project.bos:
        d = (dec_objects or {}).get(bo.code) or {}
        if "linked" in d:
            bo.project_link = bool(d["linked"])
        if not bo.project_link:
            continue
        if bo.code == target.code:
            raise ValueError(f"对象 {target.code} 不能关联自己（自关联不允许）")
        plan = _plan_object(bo, pool, d, mode, project, target)
        entry = {
            "objectCode": bo.code, "objectName": bo.name, "linked": True,
            "updateMode": plan.update_mode, "aligned": [], "kept": [],
            "pending": [a.to_dict() for a in plan.pending],
            "relations": plan.relations,
            "conflicts": plan.conflicts, "skipped": plan.skipped,
            "warnings": plan.warnings,
        }
        if plan.link is not None:
            if plan.link.action == "reject_self":
                raise ValueError(f"对象 {target.code} 不能关联自己（自关联不允许）")
            if plan.link.action == "conflict":
                raise ValueError(
                    f"物理列 {target.link_field} 已被字段 {plan.link.code}"
                    f"（{plan.link.name}）占用且未关联{target.name}，"
                    f"请先改字段名或改用现有字段")
            # 决策里 linkField=false = 只对齐字段、不补目标链接
            # （用户自己已经有别的关联方式）。builder 侧读这个标记跳过补字段。
            bo.skip_link_field = d.get("linkField") is False
            if plan.link.action == "reuse":
                report["linkFields"].append({
                    "objectCode": bo.code, "objectName": bo.name,
                    "action": "reuse", "code": plan.link.code,
                    "fieldName": plan.link.field_name, "name": plan.link.name,
                    "type": "object", "objectType": target.code,
                    "length": (plan.link.existing or {}).get("length"),
                    "note": plan.link.note,
                })
            elif not bo.skip_link_field:
                report["linkFields"].append({
                    "objectCode": bo.code, "objectName": bo.name,
                    "action": plan.link.action, "code": target.link_code,
                    "fieldName": target.link_field, "name": plan.link.name,
                    "type": "object", "objectType": target.code, "length": 64,
                    "note": plan.link.note,
                })

        if plan.aligned:
            written = _apply_definitions(bo, plan.aligned)
            for row in written:
                row["updateMode"] = plan.update_mode
                if plan.update_mode:
                    row["note"] = "保留编码与物理列（对象为更新模式）"
            report["aligned"].extend(written)
            entry["aligned"] = written
            mapping = _apply_renames(bo, plan.aligned)
            aligned_codes = {a.rename_code or a.field_code for a in plan.aligned}
            rewrites, scripts, ref_notes = _rewrite_refs(
                project, bo.code, mapping, aligned_codes)
            if rewrites:
                report["rewrites"].setdefault(bo.code, {}).update(rewrites)
            if scripts:
                report["scriptFormKeys"] = sorted(
                    set(report["scriptFormKeys"]) | set(scripts))
            for note in ref_notes:
                if note not in report["notices"]:
                    report["notices"].append(note)
            entry["rewrites"] = rewrites
            entry["notices"] = list(ref_notes)
            if plan.update_mode:
                side = "项目侧" if target.is_project else f"{target.name}侧"
                report["kept"].extend({
                    "objectCode": bo.code, "objectName": bo.name,
                    "fieldCode": row["fieldCode"], "fieldName": row["fieldName"],
                    "note": "保留编码与物理列（对象为更新模式）",
                    "message": f"保留 {row['fieldName']}（{side} {row['newCode']}）",
                } for row in written)
        for item in plan.skipped:
            report["skipped"].append({
                "objectCode": bo.code, "objectName": bo.name,
                "fieldCode": item.get("fieldCode", ""),
                "fieldName": item.get("fieldName") or item.get("fieldCode", ""),
                "kind": item.get("kind", "skip"),
                "message": item.get("message", ""),
            })
        report["conflicts"].extend({**c, "objectCode": bo.code,
                                    "objectName": bo.name} for c in plan.conflicts)
        report["objects"][bo.code] = entry

    report["applied"] = len(report["aligned"])
    report["pending"] = [{
        "objectCode": code, "objectName": (obj.get("objectName") or code),
        **{k: a.get(k) for k in ("fieldCode", "fieldName", "rule", "reason")},
        "target": (a.get("target") or {}).get("code"),
    } for code, obj in report["objects"].items() for a in (obj.get("pending") or [])]
    report["noop"] = not (report["aligned"] or report["linkFields"]
                          or any(report["rewrites"].values()))
    report["summary"] = {
        "linkedObjects": sum(1 for b in project.bos if getattr(b, "project_link", False)),
        "aligned": len(report["aligned"]),
        "pending": len(report["pending"]),
        "linkFields": len(report["linkFields"]),
        "skipped": len(report["skipped"]),
        "conflicts": len(report["conflicts"]),
    }
    return report


# ---------------------------------------------------------------- 存量包


def _bo_from_package(file: str, text: str) -> Bo | None:
    """用包里的 bo XML 造一个「伪 Bo」（不读 Excel），供存量包侧复算计划。"""
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    ent = next((c for c in root if local(c.tag) == "bodefentity"), None)
    if ent is None:
        return None
    bo = Bo(
        code=ent.attrib.get("code", ""),
        name=ent.attrib.get("name", ""),
        tbl_name=ent.attrib.get("tblName", "") or PROJECT_TBL,
        exist_id=ent.attrib.get("id", "") or "",
        flow_driven=ent.attrib.get("mode", "").strip().lower() == "workflow",
        project_link=True,
    )
    for i, attr in enumerate((c for c in ent if local(c.tag) == "attribute"), start=1):
        options: dict = {}
        if attr.attrib.get("options"):
            try:
                got = json.loads(attr.attrib["options"])
                options = got if isinstance(got, dict) else {}
            except json.JSONDecodeError:
                options = {}
        bo.fields.append(BoField(
            bo_code=bo.code,
            code=attr.attrib.get("code", ""),
            name=attr.attrib.get("name", ""),
            type_new=attr.attrib.get("dataTypeNew", "varchar") or "varchar",
            datatype=attr.attrib.get("datatype", "") or "",
            object_type=attr.attrib.get("objectType", "") or "",
            length=_as_int(attr.attrib.get("length")),
            precision=_as_int(attr.attrib.get("precision")),
            is_null=(attr.attrib.get("isnull") or "Y") != "N",
            is_index=(attr.attrib.get("isindex") or "N") == "Y",
            default_value=attr.attrib.get("defaultvalue", "") or "",
            options=options,
            field_name=attr.attrib.get("fieldName", "") or "",
            sn=_as_int(attr.attrib.get("sn")) or i,
        ))
    return bo


def _bo_field_from_raw(raw: dict, bo_code: str) -> BoField:
    """用户手填的「新增字段」→ BoField（只为参与对齐计算，不写盘）。

    为什么存量包侧也要认这些行：勾了「关联项目」时用户新加的「汇报类型」必须
    按项目侧的全局字典落地；不参与计算就会按自定义字典造一个 `对象_字段`
    的 key，导入端报「引用的字典不存在」（架构 §0.3 F-D）。
    """
    type_new = str(raw.get("type") or raw.get("type_new") or "varchar").strip() or "varchar"
    choices = raw.get("choices")
    if isinstance(choices, str):
        choices = [c.strip() for c in re.split(r"[,，;；、/\n\r]+", choices) if c.strip()]
    return BoField(
        bo_code=bo_code,
        code=str(raw.get("code") or "").strip(),
        name=str(raw.get("name") or raw.get("code") or "").strip(),
        type_new=type_new,
        object_type=str(raw.get("objectType") or raw.get("object_type") or "").strip(),
        length=_as_int(raw.get("length")),
        precision=_as_int(raw.get("precision")),
        is_null=_as_bool_yn(raw.get("isnull", raw.get("is_null")), True),
        is_index=_as_bool_yn(raw.get("index", raw.get("is_index")), False),
        default_value=str(raw.get("default") or raw.get("defaultvalue") or ""),
        options=dict(raw.get("options") or {}) if isinstance(raw.get("options"), dict) else {},
        choices=list(choices or []),
        field_name=str(raw.get("fieldName") or raw.get("field_name") or ""),
        sn=_as_int(raw.get("sn")) or 0,
    )


def analyze_package(data: bytes, chosen: dict, spec: dict | None = None,
                    snap: Snapshot | None = None,
                    target: TargetObject | None = None) -> dict:
    """存量包侧的对齐分析：每个文件一份 ObjectPlan（patch 再翻成 raw 行）。"""
    import zipfile

    spec = spec or {}
    target = target or project_target()
    snap = snap if snap is not None else load_snapshot(
        spec.get("snapshot") or None, target=target)
    pool = snap.pool()
    link_files = [str(x) for x in (spec.get("linkProject") or [])]
    align_dec = spec.get("align") or {}
    out: dict = {"objects": {}, "warnings": list(snap.warnings), "notices": [],
                 "snapshotSource": snap.source, "snapshotSourceLabel": snap.label(),
                 "snapshotGeneratedAt": snap.generated_at}
    if not link_files:
        return out
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as zin:
        for file in link_files:
            info = chosen.get(file)
            if info is None:
                raise ValueError(f"包里找不到这些 XML：{file}")
            text = zin.read(file).decode("utf-8", "replace")
            bo = _bo_from_package(file, text)
            if bo is None:
                raise ValueError(f"{file}: 不是可解析的业务对象 XML")
            if bo.code == target.code:
                raise ValueError(f"{file}: 对象 {target.code} 不能关联自己（自关联不允许）")
            # 用户手填的新字段也进候选池：它们同样要对齐成目标侧定义
            # （包里已存在的 code 不重复加，那种情况由「替换」分支处理）
            for raw in _user_rows(spec, file):
                code = str(raw.get("code") or "").strip()
                if not code or bo.field_by_code(code) is not None:
                    continue
                bo.fields.append(_bo_field_from_raw(raw, bo.code))
            decision = {"align": align_dec.get(file) or {}}
            plan = _plan_object(bo, pool, decision, "package", None, target)
            if plan.link is not None and plan.link.action == "conflict":
                raise ValueError(
                    f"{file}: 物理列 {target.link_field} 已被字段 {plan.link.code}"
                    f"（{plan.link.name}）占用且未关联{target.name}，"
                    f"请先改字段名或改用现有字段")
            out["objects"][file] = plan
    return out


def _user_rows(spec: dict, file: str) -> list[dict]:
    """取某个文件对应的「用户手填字段行」（与 patch._fields_for 同一套口径）。"""
    by_target = spec.get("byTarget") or {}
    if isinstance(by_target, dict) and file in by_target:
        return list(by_target[file] or [])
    return list(spec.get("fields") or [])


def plan_to_raw_rows(plan: ObjectPlan,
                     target: TargetObject | None = None) -> tuple[list[dict], dict | None]:
    """ObjectPlan → patch 的两类 raw 行：(覆盖定义行[], 新增链接字段行|None)。"""
    target = target or project_target()
    aligns: list[dict] = []
    for a in plan.aligned:
        t = a.target
        aligns.append({
            "code": a.field_code,          # 存量包永不改 code/fieldName
            "name": t.name,
            "type": t.type_new,
            "objectType": (t.dict.ref if (t.type_new == "dic" and t.dict) else t.object_type),
            "length": t.length,
            "precision": t.precision,
            "isnull": "是" if t.is_null else "否",
            "index": bool(t.is_index),
            "default": t.default_value or "",
            "options": copy.deepcopy(dict(t.options or {})),
            # 存量包永不改 code/fieldName：这里的物理列是**我方**的
            # （新字段随自己的编码走，已有字段保持原列），项目侧的列名只用于报告
            "fieldName": a.rename_field or t.field_name,
            "align": True,
            "dict": t.dict,
            "rule": a.rule,
            "warnings": list(a.warnings),
        })
    link_raw = None
    if plan.link is not None and plan.link.action == "add":
        link_raw = {
            "code": target.link_code,
            "name": plan.link.name or target.link_label,
            "type": "object",
            "objectType": target.code,
            "length": 64,
            "isnull": "是",
            "index": False,
            "options": copy.deepcopy(LINK_OPTIONS),
        }
    return aligns, link_raw


# ---------------------------------------------------------------- 报告


def package_report(plans: dict, target: TargetObject | None = None) -> dict:
    """把 analyze_package 的结果翻成与 apply() 同构的 report（共用报告文案）。"""
    target = target or project_target()
    report = {
        "applied": 0, "objects": {}, "aligned": [], "kept": [], "skipped": [],
        "pending": [], "conflicts": [], "linkFields": [], "rewrites": {},
        "scriptFormKeys": [], "notices": [], "noop": True,
    }
    for file, plan in (plans.get("objects") or {}).items():
        link = plan.link
        if link is not None:
            report["linkFields"].append({
                "objectCode": plan.object_code, "objectName": plan.object_name,
                "action": link.action, "code": link.code, "fieldName": link.field_name,
                "name": link.name, "type": "object", "objectType": target.code,
                "length": 64, "note": link.note,
            })
        for a in plan.aligned:
            t = a.target
            # 行里同时给「人看的字段名」与「物理列名」：报告要用列名说明
            # 「为什么没跟项目侧一致」，用字段名会被误读成列名
            report["aligned"].append({
                "objectCode": plan.object_code, "objectName": plan.object_name,
                "fieldCode": a.field_code, "fieldName": a.field_name,
                "column": a.rename_field, "newCode": a.field_code,
                "newField": t.field_name,
                "rule": a.rule, "type": t.type_new, "objectType": t.object_type,
                "length": t.length, "lengthBefore": None,
                "warnings": list(a.warnings), "updateMode": True,
                "note": "保留编码与物理列（存量包不改列名）",
            })
        for item in plan.skipped:
            report["skipped"].append({
                "objectCode": plan.object_code, "objectName": plan.object_name,
                "fieldCode": item.get("fieldCode", ""),
                "fieldName": item.get("fieldName") or item.get("fieldCode", ""),
                "kind": item.get("kind", "skip"), "message": item.get("message", ""),
            })
        report["conflicts"].extend({**c, "objectCode": plan.object_code,
                                    "objectName": plan.object_name}
                                   for c in plan.conflicts)
        # 存量包：未确认的相近字段不改包，只进报告（用户在「确认相近字段」里
        # 用 align 勾过之后才会变成 aligned 行）
        report["pending"].extend({
            "objectCode": plan.object_code, "objectName": plan.object_name,
            "fieldCode": a.field_code, "fieldName": a.field_name,
            "rule": a.rule, "reason": a.reason,
            "target": a.target.code,
        } for a in plan.pending)
    report["applied"] = len(report["aligned"])
    report["noop"] = not (report["aligned"] or report["linkFields"])
    report["summary"] = {
        "linkedObjects": len(plans.get("objects") or {}),
        "aligned": len(report["aligned"]),
        "pending": len(report["pending"]),
        "linkFields": sum(1 for it in report["linkFields"] if it["action"] == "add"),
        "skipped": len(report["skipped"]),
        "conflicts": len(report["conflicts"]),
    }
    return report


def report_lines(report: dict, target: TargetObject | None = None) -> list[str]:
    """给 web 报告 / zip comment / patch 报告共用的「目标对象对齐」小节。"""
    target = target or project_target()
    title = "项目对齐" if target.is_project else f"{target.name}对齐"
    label = "关联项目" if target.is_project else f"关联{target.name}"
    if not report or report.get("noop"):
        return [title, f"  （本包没有对象勾「{label}」，未做对齐）"]
    lines = [title]
    for it in report.get("linkFields") or []:
        name = it.get("objectName") or it.get("objectCode") or ""
        code = it.get("objectCode") or ""
        if it.get("action") == "add":
            lines.append(f"  ✓ {name} {code}：{label}=是，新增 {it.get('code')}"
                         f"（{it.get('fieldName')} varchar {it.get('length')}）")
        else:
            lines.append(f"  = {name} {code}：{it.get('note') or '复用已有链接字段'}")
    for it in report.get("aligned") or []:
        name = it.get("objectName") or it.get("objectCode") or ""
        span = ""
        if it.get("lengthBefore") not in (None, it.get("length")):
            span = f"，长度 {it['lengthBefore']}→{it.get('length')}"
        tail = ""
        if it.get("updateMode"):
            col = it.get("column") or it.get("fieldName")
            # 目标侧列名取 projectField；老数据没有这个键时退回 newField
            theirs = it.get("projectField") or it.get("newField") or ""
            side = "项目侧" if target.is_project else f"{target.name}侧"
            tail = f"  ← 保留物理列 {col}（{side} {theirs}，存量包不改列名）"
        lines.append(
            f"  ✓ {name} {it.get('fieldName')} → {it.get('newCode')}"
            f"（{it.get('type')}"
            f"{' ' + it['objectType'] if it.get('objectType') else ''}{span}）{tail}")
        for w in it.get("warnings") or []:
            lines.append(f"      · {w}")
    shown = {(it.get("objectCode"), it.get("fieldCode")) for it in report.get("aligned") or []}
    for it in report.get("kept") or []:
        if (it.get("objectCode"), it.get("fieldCode")) in shown:
            continue      # 对齐行行尾已经写了「保留物理列 X（项目侧 Y）」，不重复列一行
        lines.append(f"  = {it.get('objectName')} {it.get('fieldName')}："
                     f"{it.get('note') or '保留原样'}")
    # 只列「用户能操作」的跳过项：系统字段、本对象名称这类是**正常**的，
    # 逐个列会把报告刷成几十行（一个 51 字段的对象就有十几行系统字段），
    # 真正的冲突反而被埋掉。其余汇总成一行。
    quiet_kinds = {"system", "no_match", "self_link"}
    quiet = 0
    for it in report.get("skipped") or []:
        if it.get("kind") in quiet_kinds:
            quiet += 1
            continue
        lines.append(f"  - {it.get('objectName')} {it.get('fieldName')}：{it.get('message')}")
    if quiet:
        lines.append(f"  （另有 {quiet} 个字段无需对齐：系统字段 / 本对象名称 / "
                     f"项目对象里没有对应字段）")
    for it in report.get("pending") or []:
        lines.append(
            f"  ? {it.get('objectName') or it.get('objectCode')} "
            f"{it.get('fieldName')} ↔ 项目 {it.get('target')}（{it.get('rule')}）："
            f"{it.get('reason') or '相近字段'} —— 未覆盖，需在「确认相近字段」里勾选")
    for it in report.get("conflicts") or []:
        lines.append(f"  ✗ {it.get('objectName')}：{it.get('message')}")
    summary = report.get("summary") or {}
    total = (f"  合计：新增 {summary.get('linkFields', 0)} / "
             f"对齐 {summary.get('aligned', 0)} / "
             f"保留 {len(report.get('kept') or [])} / "
             f"跳过 {summary.get('skipped', 0)}")
    if summary.get("pending"):
        total += f" / 待确认 {summary['pending']}"
    lines.append(total)
    scripts = report.get("scriptFormKeys") or []
    if scripts:
        lines.append("  需人工核对：表单 " + "、".join(scripts)
                     + " 的脚本里引用了已改名的字段编码（JS 无法静态改写）")
    return lines


def similar_notices(project, snap: Snapshot | None = None) -> list[str]:
    """CLI 用：列出「和项目字段相近但**没有**自动覆盖」的字段，提示用户去确认。

    2026-11-15 用户口径：只有直接对应（E0 同族唯一）自动覆盖，语义相近 /
    可能相关必须逐条勾。所以这里不再说「已覆盖」，而是明确说「没覆盖、等你确认」。
    """
    vm = analyze(project, snap)
    out: list[str] = []
    for item in vm.get("items") or []:
        for r in item.get("relations") or []:
            if r.get("status") not in ("pending", "ambiguous"):
                continue
            target = (r.get("target") or {}).get("code") or ""
            out.append(
                f"⚠ 对象「{item['objectName']}」的字段「{r.get('fieldName')}」与项目字段 "
                f"{target} 相近（{r.get('rule')}：{r.get('basis')}），"
                f"本次按你的选择生成、未覆盖；如需照项目定义覆盖，请在预览页"
                f"「确认相近字段」里勾选它")
    return out


# ---------------------------------------------------------------- 校验


def validate(project, snap: Snapshot | None = None,
             target: TargetObject | None = None) -> list[dict]:
    """出包前专项校验。返回 [{level, where, message}]（validate.py 负责合并）。"""
    out: list[dict] = []
    target = target or getattr(project, "link_target", None) or project_target()
    snap = snap if snap is not None else load_snapshot(target=target)
    label = "关联项目" if target.is_project else f"关联{target.name}"
    # 非法填法的检查对**所有**对象都要做：填了认不出的值时 project_link 是 False
    # （按否处理），如果只在勾上的对象里查，这条提示永远不会出现。
    for bo in project.bos:
        raw = str(getattr(bo, "project_link_raw", "") or "").strip()
        if raw and raw not in ("是", "否", "Y", "N", "YES", "NO", "TRUE", "FALSE",
                               "1", "0", "√", "T", "F"):
            out.append({
                "level": "warn", "where": f"业务对象 {bo.code}",
                "message": f"「{label}」填了「{raw}」，认不出，按「否」处理"
                           f"（该列只能填 是 / 否）"})
    linked = [b for b in project.bos if getattr(b, "project_link", False)]
    for bo in linked:
        where = f"业务对象 {bo.code}"
        if bo.code == target.code:
            out.append({"level": "error", "where": where,
                        "message": f"对象 {target.code} 不能勾「{label}」："
                                   f"自己关联自己没有意义，请改成否"})
        for f in bo.fields:
            if f.resolved_field_name() != target.link_field:
                continue
            if f.type_new == "object" and (f.object_type or "") == target.code:
                continue
            out.append({
                "level": "error", "where": where,
                "message": f"物理列 {target.link_field} 已被字段 {f.code}（{f.name}）占用"
                           f"且未关联{target.name}，请先改字段名或改用现有字段"})
        links = [f for f in bo.fields
                 if f.type_new == "object" and (f.object_type or "") == target.code]
        if len(links) > 1:
            out.append({
                "level": "warn", "where": where,
                "message": f"对象里有 {len(links)} 个指向{target.name}的关联字段"
                           f"（{'、'.join(f.code for f in links)}），只会复用第一个"})
    if linked:
        if snap.status == "unavailable":
            if target.is_project:
                out.append({
                    "level": "info", "where": "1_业务对象",
                    "message": "项目字段快照不可用，本次只补「所属项目」链接字段、不做字段对齐"})
            else:
                out.append({
                    "level": "info", "where": "1_业务对象",
                    "message": f"{target.name}字段快照不可用，本次只补"
                               f"「{target.link_label}」链接字段、不做字段对齐"})
        elif snap.status == "degraded":
            for w in snap.warnings[:2]:
                out.append({"level": "warn", "where": "1_业务对象", "message": w})
    return out
