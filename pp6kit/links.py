"""关联对象 -> 值来源 + 联动 + 过滤：**一处推导，三处复用**。

依据 ``docs/参考-关联对象值来源-20261119.md``（线上导出包
config-sync-20261119184322 的逐键拆解），凡是形状都以那份样本为准：

    新建表单控件   fieldType=linkdata    linkdata=<值来源key>
                   link_config / link_linkage / link_condition / init_linkage ...
    详情表单控件   fieldType=selectObject boCode=<目标对象编码> store=id（不带 value_source）
    值来源         dataTemplate type=valueSource unique=id_，
                   dataTemplateTpl 的 filterConditions 与表单 link_condition
                   用**同一个** rule_id

为什么单独成模块（而不是散在 blueprint / gen_tpl / gen_form 里）：

  「本对象的哪个字段能过滤目标对象」「值来源该返回哪几列」「哪些返回列该联动到
  哪个控件」这三件事，三个生成器必须给出同一个答案。rule_id 只要算两次就会
  分叉，线上表现是值来源不按表单条件过滤——下拉里永远是全量数据，而表单上
  配置看着是对的，很难查。

目标对象的信息来源三级降级，任何一级拿不到都不能让出包失败：

  1. 本包里的业务对象（Bo，字段名/物理列名/选项全都有）
  2. 目标库的元数据（``objects.db_object_info``，连线时才有）
  3. 离线兜底：``pp_<编码>`` 表 + 只有 id_ / name_ 两列

第 3 级是必须的：回归、离线机器、以及「系统对象但没连库」都靠它出货，
产出的 link_config 仍然是合法的（主键/名称列），只是没法让用户挑返回列。
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from .model import Bo, BoField, DataSet, camel_to_snake, table_name_for

# 目标对象的主键 / 名称物理列。线上所有业务表都是这两个名字，
# 值来源的 unique/uniqueField 也必须是主键，否则下拉取不到值。
ID_COLUMN = "id_"
NAME_COLUMN = "name_"

# 系统里给控件显示值用的列（link_config.text）。值来源不返回它就只剩一串 id。
_LABEL_COLUMN = NAME_COLUMN

_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_NS = uuid.UUID("8f1d6a52-3c47-4b9e-9a10-6d2f0c5b7e41")

# 「XX选择」值来源要排掉的系统列（用户口径：返回结果 = 全部业务字段 + 主键）。
# 以 ai_bo.SYSTEM_CODES 为底（id/tenantId/ip/deleted/createBy/createTime/
# updateTime/version/updateBy），再补三处：
#   · isDraft / isDefault —— 平台内部标记，业务人员从不看；
#   · process*（processState/processNode/…）—— 流程引擎反写的冗余列；
#   · baselineVersion / currencyId / baseTotal* —— 目标库元数据里确实是系统列，
#     但不在 ai_bo 那份「AI 别自己编这些编码」的清单里（那份是另一件事）。
# 三档目标来源（本包 bo / 目标库元数据 / 字段快照）共用这一份判定，
# 否则同一个对象在「连库」和「只有快照」两种环境下返回列会不一样。
_SYSTEM_EXTRA = {"isDraft", "isDefault", "baselineVersion", "currencyId",
                 "baseTotalBudget", "baseTotalCost"}
_SYSTEM_CODES_CACHE: set[str] | None = None


def system_codes() -> set[str]:
    """目标对象里不算「业务字段」的编码集合（见 ``_SYSTEM_EXTRA`` 的说明）。"""
    global _SYSTEM_CODES_CACHE
    if _SYSTEM_CODES_CACHE is None:
        from . import ai_bo           # lazy：ai_bo 会拉 objects/nlq，别在导入期绕圈

        _SYSTEM_CODES_CACHE = set(ai_bo.SYSTEM_CODES) | set(_SYSTEM_EXTRA)
    return _SYSTEM_CODES_CACHE


def is_system_code(code: str) -> bool:
    """这一列是不是系统字段（含 process* 流程冗余列）。"""
    text = str(code or "")
    return text in system_codes() or text.startswith("process")


# ---------------------------------------------------------------- 短码


def stable_code(seed: str, length: int = 7) -> str:
    """稳定的 7 位小写短码（filterConditions 与 link_condition 的 rule_id）。

    为什么不能用 ``ids.short_id()``：那个基于雪花 id（时间驱动），同一份 Excel
    出两次包会得到两个不同的 rule_id，值来源与表单就对不上了——线上表现是
    过滤条件静默失效。这里用 md5 派生：同样的输入永远同样的短码。
    """
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    n = int(digest[:12], 16)
    out: list[str] = []
    for _ in range(max(1, length)):
        n, rest = divmod(n, len(_ALPHABET))
        out.append(_ALPHABET[rest])
    return "".join(out)


def filter_key(seed: str) -> str:
    """filterConditions[].key 的 uuid（系统要一个 uuid，不是短码）。

    同样要确定：uuid5 由 seed 派生，反复出包不变。
    """
    return str(uuid.uuid5(_NS, seed))


# ---------------------------------------------------------------- 目标对象字段


@dataclass
class LinkField:
    """目标对象里的一列（值来源的候选返回列 / 过滤列）。"""

    code: str
    label: str
    column: str
    datatype: str = "varchar"
    type_new: str = "varchar"
    object_type: str = ""
    sn: int = 0
    options: dict = field(default_factory=dict)
    choices: list[str] = field(default_factory=list)
    choice_codes: dict[str, str] = field(default_factory=dict)
    # 目标对象的系统字段（id/createTime/tenantId/process*…）。
    # 值来源「返回全部业务字段」时要按它排掉：把这些列带进 resultColumns，
    # 设计器的「返回结果」面板里就是一屏没人要的审计列，联动还会把主键/名称
    # 塞进 id/name 这种隐藏控件（样本里这两列是 `field: ""`，只回显）。
    is_system: bool = False

    def needs_picker(self) -> bool:
        """这一列在设计器里需不需要「选择控件 + 候选值」。

        样本里只有选项/关联列带 fieldType/fieldOptions，其余纯文本列不带。
        """
        return self.type_new in ("object", "dic")

    def rule_types(self) -> tuple[str, str, str]:
        """过滤规则里的 (type, input, controlType)。

        样本里关联对象是 ``string / select / text``（值来自表单控件，所以
        input 仍是 select、controlType 仍是 text）。数字与日期按各自类型出，
        否则设计器里算子下拉是「等于（文本）」，数字比较会变成字符串比较。
        """
        if self.type_new == "object":
            return "string", "select", "text"
        if self.type_new == "dic":
            return "string", "select", "text"
        if self.type_new == "number":
            return "number", "number", "number"
        if self.type_new == "datetime":
            return "date", "date", "datePicker"
        return "string", "text", "text"

    def picker_options(self, target_code: str = "") -> dict:
        """dataTemplateField 的 fieldOptions：选项列/关联列才需要。

        选项值的短码与对象 XML（gen_bo.dict_choice_codes）同源，同一个选项在
        对象里和值来源里是同一个码——否则值来源列出来的选项选中后存不进对象。
        """
        if self.type_new == "dic":
            opts = []
            for name in self.choices:
                code = self.choice_codes.get(name, "")
                opts.append({"name": name, "label": name,
                             "val": code or name, "id": code or name})
            return {"datasource": "static", "options": opts,
                    "metadata": {"boCode": target_code, "fieldKey": self.column}}
        if self.type_new == "object":
            return {
                "datasource": "valuesource",
                "value_source": f"{self.object_type}_value" if self.object_type else "",
                "metadata": {"boCode": target_code, "fieldKey": self.column,
                             "tree": bool(self.options.get("tree"))},
            }
        return {}


@dataclass
class LinkTarget:
    """值来源背后要取数的**目标对象**。"""

    code: str
    label: str
    table: str
    fields: list[LinkField] = field(default_factory=list)
    source: str = ""            # package / system / offline

    def field(self, key: str) -> LinkField | None:
        """按 字段编码 / 物理列名 / 显示名 找一列（Excel 里三种都有人写）。"""
        text = str(key or "").strip()
        if not text:
            return None
        for f in self.fields:
            if f.code == text or f.column == text:
                return f
        for f in self.fields:
            if f.label == text:
                return f
        return None

    def id_field(self) -> LinkField:
        got = self.field("id") or self.field(ID_COLUMN)
        return got or LinkField(code="id", label="主键", column=ID_COLUMN)

    def name_field(self) -> LinkField:
        got = self.field("name") or self.field(NAME_COLUMN)
        return got or LinkField(code="name", label="名称", column=NAME_COLUMN)

    @property
    def id_column(self) -> str:
        return self.id_field().column

    @property
    def name_column(self) -> str:
        return self.name_field().column

    def business_fields(self) -> list[LinkField]:
        """目标对象的**业务**字段（排掉系统字段，按目标对象 sn 顺序）。

        「XX选择」的值来源要「全部业务字段 + 主键」，这份清单就是那个「全部」。
        子表列（type_new=table）在 target_from_bo 里已经排除——它们不是物理列，
        值来源选进去取不到值。
        """
        return [f for f in sorted(self.fields, key=lambda x: x.sn)
                if not f.is_system]

    def result_list(self, wanted: list[str] | None,
                    all_fields: bool = False) -> tuple[list[LinkField], bool]:
        """值来源的返回列：主键 + 名称强制在前，其余按用户顺序去重。

        为什么名称也强制：``link_config.text`` 指向名称列，值来源不返回它，
        控件里只会显示一串 id（线上「关联项目」就变成一列乱码数字）。

        ``all_fields=True``（只给「XX选择」链接字段用）：主键第一位 → 名称
        （控件显示列）→ **其余全部非系统业务字段**，按目标对象 sn 顺序去重。
        用户口径是「返回结果 = 全部业务字段 + 主键」，这样新建表单的联动才能
        把目标对象里与需求字段同名的列一一带过来——不返回的列是联动不了的。

        ``wanted`` 非空时**永远以用户为准**（6_数据集「返回字段」/app 面板勾的
        那几列），all_fields 让位：显式配置优先于任何默认口径。
        返回 (列, 用户是否显式指定过)。
        """
        picked = [str(w).strip() for w in (wanted or []) if str(w).strip()]
        out: list[LinkField] = []
        seen: set[str] = set()
        for base in (self.id_field(), self.name_field()):
            if base.column not in seen:
                out.append(base)
                seen.add(base.column)
        if all_fields and not picked:
            for f in self.business_fields():
                if f.is_system or f.column in seen:
                    continue
                out.append(f)
                seen.add(f.column)
        for want in picked:
            f = self.field(want)
            if f is None or f.column in seen:
                continue
            out.append(f)
            seen.add(f.column)
        return out, bool(picked)

    def column_of(self, key: str) -> str:
        f = self.field(key)
        return f.column if f is not None else ""


@dataclass
class LinkFilter:
    """值来源的过滤条件 = 表单某个控件 -> 目标对象某一列。"""

    source_field: str        # 本对象字段编码（与表单控件编码同名）
    source_label: str        # 本对象字段显示名
    column: str              # 目标对象被过滤的物理列
    label: str               # 目标列显示名
    rule_id: str
    type: str = "string"
    input: str = "text"
    control: str = "text"
    # 配置里写的过滤字段已经不存在（对齐改名/反解包换了编码），实际按推导的字段
    # 绑定 —— 存的是那个失效的原编码，用来出 notice 让用户知道被自动纠正了
    rebound_from: str = ""

    @property
    def field_name(self) -> str:
        """样本形状：``<rule_id>_1``。"""
        return f"{self.rule_id}_1"


@dataclass
class LinkInfo:
    """一个「关联对象」字段在生成时需要的全部信息。"""

    field: str
    target: LinkTarget
    dataset_key: str
    results: list[LinkField]
    linkage: list[dict]
    filter: LinkFilter | None = None
    results_explicit: bool = False
    has_dataset: bool = False


# ---------------------------------------------------------------- 目标对象解析


def _field_of(bo: Bo, bf: BoField) -> LinkField:
    from . import gen_bo        # lazy：gen_bo 也会 import model，避免顶层绕一圈

    codes: dict[str, str] = {}
    if bf.type_new == "dic" and bf.choices:
        try:
            codes = gen_bo.dict_choice_codes(bf)
        except Exception:        # noqa: BLE001 —— 短码算不出来不该拖垮出包
            codes = {}
    labels = bf.sub_choices() if bf.choice_groups else list(bf.choices)
    datatype = "varchar"
    try:
        datatype = bf.resolved().get("datatype") or "varchar"
    except Exception:            # noqa: BLE001
        datatype = bf.datatype or "varchar"
    return LinkField(
        code=bf.code, label=bf.name, column=bf.resolved_field_name(),
        datatype=datatype, type_new=bf.type_new,
        object_type=bf.object_type or "", sn=bf.sn,
        options=dict(bf.options or {}), choices=labels, choice_codes=codes,
        # is_system 以 BoField 为准（builder 补的系统字段都带这个标记）；
        # 再兜一层编码判定——用户 Excel 里手写一行 createTime 时没有这个标记，
        # 那也还是系统列，不该出现在「全部业务字段」里。
        is_system=bf.is_system or is_system_code(bf.code),
    )


def target_from_bo(bo: Bo) -> LinkTarget:
    """本包业务对象 -> LinkTarget。子表（table）不能当返回列，其余都留着。"""
    fields = [_field_of(bo, f) for f in sorted(bo.fields, key=lambda x: x.sn)
              if f.type_new != "table"]
    return LinkTarget(code=bo.code, label=bo.name, table=bo.tbl_name,
                      fields=fields, source="package")


def offline_target(code: str, label: str = "") -> LinkTarget:
    """离线兜底：只有主键与名称两列。

    系统对象（project / customer…）不在本包里，没连库时拿不到字段清单。
    这时仍然要能出包：``pp_<编码>`` 是平台的物理表命名规则，主键/名称列
    全系统统一，够 link_config 与结果列用。
    """
    return LinkTarget(
        code=code, label=label or code, table=table_name_for(code),
        fields=[LinkField(code="id", label="主键", column=ID_COLUMN),
                LinkField(code="name", label="名称", column=NAME_COLUMN)],
        source="offline",
    )


def _target_from_snapshot(code: str) -> LinkTarget | None:
    """目标对象的字段快照（关联其它业务对象时刷的那份）。

    为什么要有这一级：目标对象不在本包里、又没连库时，值来源只剩「主键+名称」
    两列可用——用户上传过该对象的 zip、或在界面上点过「刷新对象清单」之后，
    ``~/.pp6kit/target_fields/<code>.json`` 里是有完整字段池的（专业、项目类型
    这些）。不读它就等于把用户已经准备好的配置丢掉，值来源的「返回结果」里
    永远选不到业务列。

    项目对象还多一档 ``assets/project_fields.json``（随包快照），所以完全离线
    也能按真实字段返回。
    """
    from . import projlink as _pl

    try:
        tgt = _pl.TargetObject(code=code, name=code, tbl=table_name_for(code))
        snap = _pl.load_snapshot(target=tgt)
    except Exception:            # noqa: BLE001 —— 快照坏了不该让出包失败
        return None
    if snap.source == "unavailable" or not snap.fields:
        return None
    fields = [
        LinkField(code=f.code, label=f.name or f.code,
                  column=f.field_name or (camel_to_snake(f.code) + "_"),
                  datatype=f.datatype or "varchar",
                  type_new=f.type_new or "varchar",
                  object_type=f.object_type or "", sn=f.sn or i,
                  options=dict(f.options or {}),
                  # 快照只有字段定义、没有 is_system 标记 → 按编码判
                  # （SYSTEM_CODES 之外还有 createTime/isDraft/process* 这批）
                  is_system=is_system_code(f.code))
        for i, f in enumerate(snap.fields, start=1)
    ]
    label = (snap.project or {}).get("tableLabel") or (snap.project or {}).get("name") or code
    return LinkTarget(code=code, label=label, table=table_name_for(code),
                      fields=fields, source="snapshot")


def _target_from_system(code: str) -> LinkTarget | None:
    """目标库里的对象（连线时）。离线/查不到返回 None。"""
    from . import objects as _objects

    info = _objects.db_object_info(code)
    fields_meta = (info or {}).get("fields") or {}
    if not fields_meta:
        return None
    labels = {it["code"]: (it.get("name") or it["code"])
              for it in _objects.catalog()}
    fields: list[LinkField] = []
    for i, (c, meta) in enumerate(sorted(fields_meta.items()), start=1):
        fields.append(LinkField(
            code=c,
            # 库里有 name_ 就用中文名，否则退化成编码（老版本元数据没这一列）
            label=str(meta.get("name") or c),
            column=camel_to_snake(c) + "_",
            datatype=str(meta.get("datatype") or "varchar"),
            type_new=str(meta.get("type_new") or "varchar"),
            sn=i,
            # 目标库元数据的列清单里混着 id_/tenant_id_/create_time_/process_*，
            # 按 ai_bo.SYSTEM_CODES（+ links 侧补充的 isDraft/isDefault/process*）
            # 判掉，否则「返回全部业务字段」会把审计列全带上。
            is_system=is_system_code(c),
        ))
    return LinkTarget(code=code, label=labels.get(code, code),
                      table=table_name_for(code), fields=fields, source="system")


# ---------------------------------------------------------------- 过滤条件推导


def auto_filter(bo: Bo | None, field: BoField | None,
                target: LinkTarget | None) -> tuple[str, str] | None:
    """本对象的哪个字段能用来过滤目标对象：返回 (本对象字段编码, 目标列)。

    默认规则（对照样本 config-sync-20261119184322）：
    本对象里有一个**显示名相同**的关联字段指向同一类对象，就用它过滤——
    「项目类型」筛「项目」（项目表里那列叫 classification_id_）就是这么来的。

    为什么按显示名而不是按编码：两侧编码通常不同（xiangMuLeiXing /
    classificationId），显示名是实施人员唯一能对上的东西。

    找不到就返回 None：不加过滤，值来源出全量。这比猜错一列强得多
    （猜错的表现是下拉里永远空，且没有任何报错）。
    """
    if bo is None or field is None or target is None:
        return None
    for sf in sorted(bo.fields, key=lambda x: x.sn):
        if sf.code == field.code:
            continue
        if sf.type_new != "object" or not sf.object_type:
            continue
        tf = target.field(sf.name)
        if tf is None or not tf.needs_picker():
            continue
        # 两侧都指同一类对象才敢绑（避免「项目类型」被绑到「项目级别」那列）
        if sf.object_type and tf.object_type and sf.object_type != tf.object_type:
            continue
        return sf.code, tf.column
    return None


def filter_of(ds: DataSet | None, target: LinkTarget,
              source_bo: Bo | None = None,
              field: BoField | None = None) -> LinkFilter | None:
    """按数据集配置算出过滤条件；配置不全时按同名推导。

    ``ds.auto_filter=False`` 表示「这份值来源默认不加过滤」（「XX选择」链接字段
    就是这种）：既不推导、也不写 filterConditions，表单侧 link_condition 为空。
    但 ``ds.filter_field / ds.filter_target`` 显式填过的仍然照旧生效——
    显式配置（6_数据集 / app 面板）永远优先于默认口径。

    2026-11-19 补一个会静默失效的边界：``6_数据集`` 里写的「过滤字段」是**出包
    当时**的字段编码，而 ``objects.resolve_object_fields`` 会在后面把「项目类型」
    对齐改名成目标对象的编码（xiangMuLeiXing → classificationId）。于是
    ``ds.filter_field`` 指向一个已经没有的字段，按编码判等就永远不等 → 过滤条件
    整条丢掉，用户拿到的是「不过滤」的值来源（下拉全量，界面看着都对）。
    所以判等之外再放行一种情况：**原编码在本对象里已经不存在**时采纳推导结果，
    并把这件事记在 ``rebound_from`` 上，由调用方出 notice 说明自动纠正了什么。
    用户明确选了另一列（编码存在且与推导不同）时仍然尊重他的选择，不悄悄换列。
    """
    src_code = (ds.filter_field if ds is not None else "") or ""
    column = (ds.filter_target if ds is not None else "") or ""
    if not src_code and not column:
        # 数据集明确写了「不要自动推导」（DataSet.auto_filter=False）时，
        # 这里必须**什么都不加**：这是「XX选择」链接字段的用户口径——
        # 默认不过滤，等他在 app 面板里自己选了过滤字段再写进数据集。
        # 判定放在「显式配置」之后：ds.filter_field/filter_target 填过的仍然
        # 走下面的显式分支（用户的配置永远优先于任何默认值）。
        if ds is not None and not ds.auto_filter:
            return None
        pair = auto_filter(source_bo, field, target)
        if pair is None:
            return None
        src_code, column = pair
    tf = target.field(column) if column else None
    rebound_from = ""
    if tf is None:
        pair = auto_filter(source_bo, field, target)
        # 原编码还在对象里 → 以用户的选择为准（哪怕推导出的是另一列）
        stale = (bool(src_code) and source_bo is not None
                 and source_bo.field_by_code(src_code) is None)
        if pair is not None and (not src_code or pair[0] == src_code or stale):
            if stale and pair[0] != src_code:
                rebound_from = src_code
            src_code, column = pair[0], pair[1]
            tf = target.field(column)
    if tf is None:
        if not column:
            return None
        # 目标列只在数据集里以物理列名出现（反解/手写 Excel）→ 造一个占位列
        tf = LinkField(code=column, label=column, column=column)
    if not src_code:
        return None
    src_field = source_bo.field_by_code(src_code) if source_bo is not None else None
    rtype, rinput, rcontrol = tf.rule_types()
    seed = f"{(ds.key if ds is not None else target.code)}:{tf.column}"
    return LinkFilter(
        source_field=src_code,
        source_label=src_field.name if src_field is not None else src_code,
        column=tf.column,
        label=tf.label if tf.label and tf.label != tf.column else (
            src_field.name if src_field is not None else src_code),
        rule_id=stable_code(seed),
        type=rtype, input=rinput, control=rcontrol,
        rebound_from=rebound_from,
    )


# ---------------------------------------------------------------- 计划


class LinkPlan:
    """一次出包内的「关联对象」解析上下文（目标对象信息带缓存）。"""

    def __init__(self, project=None) -> None:
        self.project = project
        self._targets: dict[str, LinkTarget] = {}

    # ---- 目标对象
    def target(self, code: str) -> LinkTarget | None:
        text = str(code or "").strip()
        if not text:
            return None
        got = self._targets.get(text)
        if got is not None:
            return got
        resolved = self._from_project(text) or _target_from_system(text) \
            or _target_from_snapshot(text) or offline_target(text)
        self._targets[text] = resolved
        return resolved

    def _from_project(self, code: str) -> LinkTarget | None:
        if self.project is None:
            return None
        bo = self.project.bo(code)
        if bo is None:
            bo = next((b for b in self.project.bos if b.tbl_name == code), None)
        return target_from_bo(bo) if bo is not None else None

    # ---- 数据集
    def dataset(self, key: str) -> DataSet | None:
        if self.project is None or not key:
            return None
        return next((d for d in self.project.datasets if d.key == key), None)

    def dataset_for(self, target: LinkTarget, value_source: str = "") -> DataSet | None:
        """控件该用哪份值来源：先看它自己指的 key，再按目标对象找。"""
        ds = self.dataset(value_source)
        if ds is not None and _is_for_target(ds, target):
            return ds
        if self.project is None:
            return None
        for d in self.project.datasets:
            if _is_for_target(d, target):
                return d
        return None

    def source_of(self, ds_key: str) -> tuple[Bo | None, BoField | None]:
        """哪张表单的哪个字段在用这份值来源：返回 (源对象, 源字段)。

        值来源 XML 本身不知道「谁在用我」，而过滤条件（项目类型筛项目）必须
        从**使用者**那一侧推导。生成数据集时按 key 回查一遍表单就够了：
        同一份值来源被多张表单共用时取第一个（配置是一致的才会共用，见
        blueprint._ensure_value_sources 的分组规则）。

        以前 render_dataset 直接调 filter_of(ds, target, None, None)，源对象
        传空 —— 显式写了「过滤字段」也只能靠目标的同名列硬猜，猜不到就静默
        出全量：界面上下拉可选，但筛选条件没生效，属于最难查的一类问题。
        """
        if self.project is None or not ds_key:
            return None, None
        for form in self.project.forms:
            bo = self.project.bo(form.bo_code)
            if bo is None:
                continue
            for ff in form.fields:
                if ff.value_source != ds_key:
                    continue
                bf = bo.field_by_code(ff.name)
                if bf is not None and bf.type_new == "object" and bf.object_type:
                    return bo, bf
        return None, None

    # ---- 单个控件
    def for_field(self, bo: Bo | None, ff, form=None) -> LinkInfo | None:
        """关联对象控件 -> LinkInfo；不是关联对象 / 目标不明返回 None。"""
        bf = bo.field_by_code(ff.name) if bo is not None else None
        if bf is None or bf.type_new != "object" or not bf.object_type:
            return None
        target = self.target(bf.object_type)
        if target is None:
            return None
        ds = self.dataset_for(target, ff.value_source)
        # 返回列只从数据集这一处读（result_fields / result_all 两个开关）：
        # 表单侧 link_linkage 与值来源侧 resultColumns 必须是同一份，各算一遍
        # 就会指向一条值来源没返回的列（选了值带不出来）。
        results, explicit = target.result_list(
            ds.result_fields if ds is not None else [],
            all_fields=bool(ds is not None and ds.result_all))
        results = with_linked_columns(target, bo, bf, results, explicit)
        linkage = _linkage(bo, bf, ff, results, form)
        return LinkInfo(
            field=ff.name,
            target=target,
            dataset_key=ds.key if ds is not None else (ff.value_source or bf.object_type),
            results=results,
            linkage=linkage,
            filter=filter_of(ds, target, bo, bf),
            results_explicit=explicit,
            has_dataset=ds is not None,
        )


def _is_for_target(ds: DataSet, target: LinkTarget) -> bool:
    table = (ds.from_table or "").strip()
    return table in (target.code, target.table) or (bool(table) and table == target.code)


def with_linked_columns(target: LinkTarget, bo: Bo | None, bf: BoField | None,
                        results: list[LinkField], explicit: bool) -> list[LinkField]:
    """把「能联动到本对象同名字段」的目标列补进返回列。

    联动（``link_linkage``）只能引用值来源**返回**的列。用户没在 6_数据集 里
    勾过返回列时，返回列只有主键 + 名称，于是「专业 ``zhuan_ye_`` → 本表单
    ``zhuanYe``」这类自动映射永远配不出来——样本的返回列里专门带了
    ``zhuan_ye_``，就是为了这条联动。

    所以按「目标对象的物理列名 == 本对象某字段的物理列名」把它们自动补上
    （跳过系统字段：``id_``/``name_``/``create_time_`` 几乎每份值来源都有，
    带过去只会把主键塞进隐藏控件）。显式配过返回列的以用户为准，不擅自加。

    调用点有两处，必须**同一份**结果：表单侧（link_linkage）与值来源侧
    （resultColumns）。两边各算一遍的话，联动会指向一个值来源根本没返回的列，
    界面上看着配好了、选了值却带不出来。
    """
    if explicit or bo is None or not results:
        return results
    out = list(results)
    have = {f.column for f in out}
    for sf in sorted(bo.fields, key=lambda x: x.sn):
        # 判定用「标记 or 编码」：``is_system`` 只活在模型里，用户把确认版 Excel
        # 再读回来时（2_字段 没有这一列）它就丢了，而 create_time_/process_node_
        # 这些列名照样是系统列——只认标记的话，往返一次返回列里就多出一串审计列。
        if (sf.is_system or is_system_code(sf.code)
                or (bf is not None and sf.code == bf.code)):
            continue
        tf = target.field(sf.resolved_field_name())
        if tf is None or tf.column in have:
            continue
        out.append(tf)
        have.add(tf.column)
    return out


def _linkage(bo: Bo | None, bf: BoField, ff, results: list[LinkField],
             form=None) -> list[dict]:
    """值来源返回列 -> 本表单控件（link_linkage）。

    用户显式配过（Excel「联动字段」列 / app 上的联动映射）就以他为准；
    没配就按「返回列的物理列名 == 本对象某字段的物理列名」自动对上——
    样本里项目的 ``zhuan_ye_`` 自动带到表单的 ``zhuanYe``（专业）。
    """
    explicit: dict[str, str] = {}
    labels: dict[str, str] = {}
    for item in (getattr(ff, "linkage", None) or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("column") or "").strip()
        if not name:
            continue
        explicit[name] = str(item.get("field") or "").strip()
        if item.get("label"):
            labels[name] = str(item["label"]).strip()

    auto: dict[str, str] = {}
    if bo is not None:
        for sf in bo.fields:
            # 系统字段（id/name/createTime/updateTime…）不参与联动：
            # 它们的物理列名全系统一样（id_/name_/create_time_），几乎每个
            # 值来源都会返回，自动映射过去只会把主键/名称塞进 id/name 这种
            # 隐藏控件——样本里这几列都是 `field: ""`（只回显）。
            # 编码也一起判：`is_system` 标记在确认版 Excel 往返后会丢
            # （2_字段 没这一列），而 createTime/processNode 仍是系统字段。
            if sf.code == bf.code or sf.is_system or is_system_code(sf.code):
                continue
            auto.setdefault(sf.resolved_field_name(), sf.code)

    controls = {f.name for f in form.fields} if form is not None else None
    out: list[dict] = []
    for rf in results:
        name = rf.column
        if labels.get(name):
            label = labels[name]
        else:
            label = rf.label if rf.label and rf.label != rf.code else (rf.label or name)
        field_code = explicit.get(name)
        if field_code is None:
            field_code = auto.get(name, "")
        if controls is not None and field_code and field_code not in controls:
            # 本表单没有这个控件 → 只回显该列，不往不存在的控件上带值
            field_code = ""
        out.append({"name": name, "label": label, "field": field_code})
    return out


def label_suffix(plan: LinkInfo | None) -> str:
    """提示语里的「返回 N 列，过滤：X」，让用户核对配置。"""
    if plan is None:
        return ""
    parts = [f"返回 {len(plan.results)} 列"]
    if plan.filter is not None:
        parts.append(f"过滤：{plan.filter.source_label}")
    return "（" + "，".join(parts) + "）"
