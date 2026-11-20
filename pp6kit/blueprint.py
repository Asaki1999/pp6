"""搭建蓝图：按《搭建规范手册》把一个业务对象展开成标准表单组。

手册定义的搭建顺序：
    业务对象 → 新建表单 → 详情表单 → 条件专用表单 → 导入专用表单（按需）→ 多维表格

其中「条件专用表单」有明确的生成规则：
    该表单预先配置好对象所有字段的显示类型，供自动化等场景直接取用。
      · 日期字段      → 日期控件
      · 人员字段      → 选择器-人员
      · 组织字段      → 选择器-部门
      · 关联/字典字段 → 下拉框（父子关系的关联对象用关联数据）
      · 其余          → 单行文本
    命名固定为「条件专用_{对象名称}」。
"""
from __future__ import annotations

from dataclasses import replace

from .model import (
    FORM_BUNDLE,
    FORM_CONDITION,
    FORM_CREATE,
    FORM_DETAIL,
    FORM_IMPORT,
    Bo,
    DataSet,
    Form,
    FormField,
    Project,
    value_source_key,
    value_source_key_notes,
    value_source_name,
)
from .modules import DS_VALUE

# 详情态的关联对象用系统原本的字段类型（selectObject）。依据 native 样本
# PP6/表单/项目详情表单 1.zip → form/001-project_summary.xml：详情/基本信息里的
# object 字段全是 selectObject（boCode + store=id），**没有** value_source。
_SELECT_OBJECT_KINDS = {FORM_DETAIL, "detail_expand", FORM_CONDITION}

# 条件专用表单的控件映射（对照线上系统 Version 6.0 的真实控件名）
# 之前 datetime→date、dic→select 是错的：线上真实控件名是 datePicker / dictionary。
# 2026-10 对照线上系统 iform_form_field 表 68 种控件校正。
_CONTROL_BY_TYPE = {
    "datetime": "datePicker",       # 线上是 datePicker，不是 date
    "user": "dropSelector",
    "department": "dropSelectOrg",
    "object": "select",             # 关联对象用下拉（树形的在 control_for 里改 linkdata）
    "dic": "dictionary",            # 选项/字典用 dictionary，不是 select
    "number": "number",
    "attachment": "attachment",
    "autoNumber": "autoNumber",
    "clob": "textarea",             # 大文本用多行文本，不是单行 text
    # 以下类型没有专用控件，退化为单行文本
    "i18n": "text",
    "formula": "text",
    # 表格 = 子表（明细行）。真实控件名是 **tableField**，不是 dataTemplate。
    # 依据：project_create 的 3 个表格字段（planExcel / budgetExcel / riskExcel）
    # 在 form/002-project_create_detail_.xml 里全是 fieldType="tableField"，
    # 控件自己带 columns/configurableColumns 两套列定义。
    # dataTemplate 是「把 5_列表 的列表模板嵌进表单」，和表格字段无关——
    # 以前把 table 映射成 dataTemplate，结果子表挂到一个不相干的列表上，整块是空的。
    "table": "tableField",
}

# 流程冗余字段 / 流程状态是系统反写用的，不在表单上给用户填。
PROCESS_FIELD_CODES = {"processDefId", "processInstanceId", "processNode",
                       "processHandler", "processName", "processCode",
                       "processState"}

_NAME_TEMPLATE = {
    FORM_CREATE: "新建{obj}",
    FORM_DETAIL: "{obj}详情",
    FORM_CONDITION: "条件专用_{obj}",
    FORM_IMPORT: "导入专用_{obj}",
}

_KEY_TEMPLATE = {
    FORM_CREATE: "create_{code}",
    FORM_DETAIL: "detail_{code}",
    # 平台惯例：业务对象 options.formKey 指向这张表，样例里是 fetch_basic_project_information
    FORM_CONDITION: "fetch_basic_{code}_information",
    FORM_IMPORT: "import_{code}",
}


def control_for(field, kind: str = "") -> str:
    """按字段类型挑控件；关联对象还要看**表单是哪种**。

    2026-11-19 对齐线上导出包 config-sync-20261119184322（详见
    docs/参考-关联对象值来源-20261119.md）：

      · 新建 / 导入这类「编辑态」→ ``linkdata``（关联数据：挂值来源 + 联动 + 过滤）
      · 详情 / 展开详情 / 条件专用这类「查看/条件态」→ ``selectObject``
        （系统里关联对象的**原本字段类型**，只带 boCode + store=id，没有值来源）

    样本里同一个字段在新建表单是 linkdata、在详情表单是 selectObject——这是
    平台的设计，不是两种写法随便挑一种。

    不传 kind 的调用（projlink 的字段对齐链路）保持旧口径：树形关联 → linkdata，
    其余 → select。那条链路改的是人手写好的控件，按「最小改动」处理。
    """
    if field.type_new == "object":
        if kind in _SELECT_OBJECT_KINDS:
            return "selectObject"
        if kind in (FORM_CREATE, FORM_IMPORT) and _linkable(field):
            return "linkdata"
        if field.options.get("tree") is True:
            return "linkdata"
    return _CONTROL_BY_TYPE.get(field.type_new, "text")


def _linkable(field) -> bool:
    """这个关联字段能不能建/接值来源：目标对象明确，且不是自关联。

    自关联（上级项目这种）的下拉走对象自身，不另建值来源；给它出 linkdata
    会指向一个不存在的数据集，导入后是个点不开的空控件。
    """
    return bool(field.object_type) and field.object_type != field.bo_code


def fields_from_bo(bo: Bo, kind: str, form_key: str = "") -> list[FormField]:
    """从对象字段直接生成控件。

    控件类型一律走 control_for（按字段类型映射）：
    以前写成 `control_for(f) if kind == FORM_CONDITION else "text"`，
    只有条件专用表单是对的，新建/详情/导入表单全被硬编码成 text——
    后果就是选项字段在界面里是个**文本框**（用户截图里「选项值」显示中文、
    下拉不出来、也没法选），日期字段也是.text，整张表单等于废的。

    2026-10-29 修正：所有 kind 都用 control_for。
    2026-11-19：kind 要传进去——关联对象在新建表单是 linkdata、详情表单是
    selectObject（见 control_for）。以前这里只按字段类型映射，两种表单拿到
    同一个控件，详情页的关联字段就成了「带值来源的下拉」，与线上不一致。

    排序：``is_link`` 字段（「项目选择 / 合同选择」）排第一位，其余按 sn。
    只有排序变了、``sn`` 本身不动——存量包靠 sn 认列顺序，改 sn 等于要求
    线上 ALTER（见 projlink 模块头的铁律 1）。
    """
    out: list[FormField] = []
    for f in sorted(bo.fields, key=lambda x: (0 if x.is_link else 1, x.sn)):
        if f.code in PROCESS_FIELD_CODES:
            continue
        out.append(
            FormField(
                form_key=form_key,
                name=f.code,
                label=f.name,
                field_type=control_for(f, kind),
                # 子表横向铺满：真实表单里表格控件独占一行（连 grid_layout 都不进）
                full_row=f.type_new in ("clob", "table"),
                # 取值来源只有 BoField.value_source() 一处真相：
                # 全局字典（dict_ref）与自定义字典（dict_key）在这里分流，
                # 三处各写一遍「dic → dict_key()」时全局字典必然指向空下拉。
                value_source=f.value_source(),
                # 表格字段的列随控件走：Excel/界面里改的列定义要在表单上体现
                table_columns=list(f.table_columns or []),
            )
        )
    return out


def link_field_names(bo: Bo) -> set[str]:
    """「XX选择」链接字段的编码 + 显示名。

    控件上的 ``name`` 可能是字段编码，也可能是用户照着 Excel 抄的显示名
    （4_表单控件 里两列都能填），所以两种都认。
    """
    out: set[str] = set()
    for f in bo.fields:
        if not f.is_link:
            continue
        out.add(f.code)
        if f.name:
            out.add(f.name)
    return out


def pin_link_controls(bo: Bo, form: Form) -> bool:
    """把「XX选择」控件挪到该表单的第一位（用户口径：它必须置顶）。

    为什么是「挪控件」而不是改 ``sn``：sn 是物理列顺序的一部分（存量包靠它
    对列），改 sn 等于要求线上 ALTER；表单上的先后只由 form.fields 的顺序决定，
    所以这里只重排列表。

    为什么还要单独做一次（``fields_from_bo`` 已经排过一次）：``_make`` 末尾会按
    「分区分组」再 sort 一遍，链接字段通常没有分区（用户不会在 4_表单控件 里
    写它），排序键是空串 → 会被挤到最后一行。
    """
    names = link_field_names(bo)
    if not names:
        return False
    hit = [f for f in form.fields if f.name in names]
    if not hit or (len(hit) == 1 and form.fields[0] is hit[0]):
        return False
    rest = [f for f in form.fields if f.name not in names]
    form.fields = hit + rest
    return True


def _link_codes(project: Project, link_target, link_target_code: str) -> dict[str, str]:
    """每个对象里「哪一个是 XX选择 字段」：{对象编码: 字段编码}。

    为什么不只看 ``BoField.is_link``：``blueprint.expand`` 排在
    ``builder.ensure_project_links`` 之前，expand 里那次 ``_ensure_value_sources``
    跑的时候链接字段还没被打上标记（新增的还没建、复用的刚从 Excel 读回来）。
    只看 is_link 的话，那一轮会把「项目类型」这种同名关联字段的过滤条件推到
    链接字段那份值来源上（实测：复用老字段时值来源凭空多出一条
    ``filterConditions``，而本意是「XX选择默认不过滤」）。

    判定就用 ``projlink.plan_link_field`` 自己的结论（它不管 pool 参数）：
    新增 → 目标对象的 link_code；复用 → 用户已有那个字段的编码。两边只有一份
    真相，不会出现「builder 补的是 A、值来源按 B 算」。
    """
    from . import projlink as _pl

    out: dict[str, str] = {}
    if not link_target_code:
        return out
    for bo in project.bos:
        if not getattr(bo, "project_link", False):
            continue
        try:
            got = _pl.plan_link_field(bo, [], link_target)
        except Exception:            # noqa: BLE001 —— 判定失败就当没有链接字段
            continue
        if got.action in ("add", "reuse"):
            out[bo.code] = got.code
    return out


def _is_link_field(bo: Bo, f, link_code_of: dict[str, str]) -> bool:
    """这个 object 字段是不是本包的「XX选择」字段（见 ``_link_codes``）。"""
    if getattr(f, "is_link", False):
        return True
    return bool(f.code) and link_code_of.get(bo.code, "") == f.code


def _make(bo: Bo, kind: str, src: Form | None, group_src: Form | None = None) -> Form:
    key = _KEY_TEMPLATE[kind].format(code=bo.code)
    form = Form(
        key=key,
        name=_NAME_TEMPLATE[kind].format(obj=bo.name),
        bo_code=bo.code,
        form_type="form",
        layout=src.layout if src else 2,
        layout_style=src.layout_style if src else "grouped",
        type_id=src.type_id if src else "",
        status=src.status if src else "deploy",
        width=src.width if src else 90,
        kind=kind,
        auto_hidden=src.auto_hidden if src else False,
    )
    if kind == FORM_CONDITION:
        form.fields = fields_from_bo(bo, FORM_CONDITION, key)
    elif src is not None and src.fields:
        # 用户手填了控件就用他的；但「标准全套」空壳（src.fields 为空）不能
        # 落到这里——那是要按 kind 现推的（新建→linkdata、详情→selectObject）。
        form.fields = [replace(f, form_key=key) for f in src.fields]
    else:
        # 必须传真实 kind：以前写死 FORM_DETAIL，导致新建表单里的关联对象
        # 也按详情口径出成 selectObject，值来源/联动整条链都没接上
        # （2026-11-19 用户样本 config-sync-20261119184322 的形状）。
        form.fields = fields_from_bo(bo, kind, key)

    # 新建表单里填的「分区」按字段编码带到详情/条件专用表单，
    # 这样一套表单的分组名字是一致的，不用在三个地方各填一遍。
    # 顺带按分区分组排序：详情表单的字段是按对象 sn 出的，分组会交错，
    # 排一下才和新建表单看起来是一套东西。
    if group_src is not None and group_src.fields:
        by_name = {f.name: f.group for f in group_src.fields if f.group}
        for f in form.fields:
            if not f.group:
                f.group = by_name.get(f.name, "")
        order: dict[str, int] = {}
        for f in group_src.fields:
            order.setdefault(f.group or "", len(order))
        form.fields.sort(key=lambda f: order.get(f.group, len(order)))
    # 分组排完再把「XX选择」拎回第一行（见 pin_link_controls 的说明）
    pin_link_controls(bo, form)
    return form


def expand(project: Project, announce_value_sources: bool = True) -> list[str]:
    """把「标准全套」展开成新建/详情/条件专用三张表单。

    ``announce_value_sources``：要不要把「已自动创建值来源数据集…」这类**模型层**
    提示放进返回列表。出包时若没勾「选择器」模块，这些值来源不会进包（控件还会
    被降级），此时报「已自动创建」是自相矛盾的，由 builder 传 False 关掉；
    预览 / 确认版 Excel 仍然要报（那里没有「模块勾选」这回事）。
    """
    notices: list[str] = []
    existing = {(f.bo_code, f.kind) for f in project.forms if f.kind != FORM_BUNDLE}
    added: list[Form] = []

    for form in list(project.forms):
        if form.kind != FORM_BUNDLE:
            continue
        bo = project.bo(form.bo_code)
        if bo is None:
            notices.append(f"表单 {form.key}: 标准全套找不到对象 {form.bo_code}，已跳过")
            continue
        # 详情/条件专用表单也要继承新建表单的 typeId / 布局样式 / 状态；
        # 传 fields=[] 的空壳，避免把新建表单的字段（required/只读）原样复制过去。
        base = replace(form, fields=[])
        for kind in (FORM_CREATE, FORM_DETAIL, FORM_CONDITION):
            if (bo.code, kind) in existing:
                notices.append(f"对象 {bo.code}: 已存在 {kind} 表单，跳过自动生成")
                continue
            added.append(_make(bo, kind, form if kind == FORM_CREATE else base,
                               group_src=form))
            existing.add((bo.code, kind))
        notices.append(f"对象 {bo.code}: 已按规范展开为标准全套（新建 / 详情 / 条件专用）")

    project.forms = [f for f in project.forms if f.kind != FORM_BUNDLE] + added
    notices.extend(_ensure_value_sources(project,
                                         announce=announce_value_sources))
    return notices


def _ensure_value_sources(project: Project, announce: bool = True) -> list[str]:
    """为 object 字段自动建「值来源」数据集，并把控件接到它上面。

    为什么必须有这一步（2026-11-11 查 crsanjiu 库得出的结论）：
    真实系统 select 控件的取数 97% 来自 valueSource 数据集——控件的
    field_options.value_source 存的是数据集 key（如 project_grade_value），
    而不是对象编码；每个被引用的对象都有配套的值来源数据集（库里 154 个）。
    app 之前把控件 value_source 填成对象编码（fields_from_bo），却没有任何
    数据集支撑，导入后下拉引用悬空、取不到数。

    规则：
    - 同一个目标对象 + **同一套过滤/返回配置**才共用一个数据集；真实库
      `project` 被 39 个控件共用，但那些控件都没有过滤条件。两个控件的过滤
      条件不同却共用一份值来源，导入后必有一个是错的，而界面上看着都对，
      所以这里按配置分组，多余的配置另建 ``<obj>_value_2``。
    - 控件的 value_source 只有「恰好等于目标对象编码」的才替换（那是我们自己的
      默认填法）；用户手填或反解来的数据集 key 不动
    - 目标对象不存在（既不在本包也不在系统清单）不建，避免悬空数据集；
      **例外**是本次出包的链接目标（见下），它走离线兜底照样要建

    2026-11-19 追加（样本 config-sync-20261119184322）：值来源要带
    filterConditions 与 resultColumns，过滤条件默认按「本对象有个同名关联字段
    指向同类对象」推导（项目类型筛项目），返回列默认主键 + 名称。

    「关联其他业务对象」的目标（builder.ensure_project_links 刚补/复用的那个
    链接字段）不受上面那条「目标必须在包内或清单里」的限制：它的目标本来就可能
    既不在本包、离线时也不在 ``objects.catalog()`` 里（用户手输的对象名要连库
    才核得出来），而 ``links.LinkPlan.target`` 自带离线兜底（``pp_<编码>`` 表 +
    主键/名称两列）。拦掉它，新建表单的 linkdata 就没有值来源可指——下拉能点，
    但一个数据也取不到。
    """
    from . import links as _links
    from . import objects as _objects

    notices: list[str] = []
    ds_by_key = {d.key: d for d in project.datasets}
    pkg_codes = {b.code for b in project.bos}
    names = {it["code"]: (it.get("name") or it["code"]) for it in _objects.catalog()}
    for b in project.bos:
        names[b.code] = b.name
    plan = _links.LinkPlan(project)
    # 本次出包的链接目标（默认 project，「关联其他业务对象」时是用户选的那个）。
    # 只放行**这一个编码**，不放行全部：AI 编出来的错误关联编码还是要拦住，
    # 否则包里会多出一堆没人用、也打不开的悬空值来源。
    link_target = getattr(project, "link_target", None)
    if link_target is None:
        # 与 builder.ensure_project_links 同一兜底：不传 link_target 的调用
        # （测试、增量补丁里直接调）默认就是「关联项目」，否则同一个 Project
        # 在两条调用路径上会长得不一样。
        from . import projlink as _pl
        link_target = _pl.project_target()
    link_target_code = str(getattr(link_target, "code", "") or "")
    if link_target_code and link_target_code not in names:
        # 用户自选的目标对象可能不在清单里（离线 / 手输），但它有名字：
        # 值来源数据集要叫「合同值来源」，不能叫「hetong值来源」。
        names[link_target_code] = str(getattr(link_target, "name", "") or link_target_code)

    ordered: list[tuple[Bo, object]] = []
    for bo in project.bos:
        for f in bo.fields:
            if f.type_new != "object" or not f.object_type or f.object_type == bo.code:
                continue              # 自关联（上级项目）下拉走对象自身，不另建
            if (f.object_type != link_target_code
                    and f.object_type not in pkg_codes
                    and f.object_type not in names):
                continue              # 归一失败的目标，建了也是悬空
            ordered.append((bo, f))
    if not ordered:
        return notices
    link_code_of = _link_codes(project, link_target, link_target_code)

    # 系统库里已有的数据集 key（refresh 时拉的 iform_data_dataset.key_）。
    # 引用这些对象时可以少建一份**数据集**，导入也不必覆盖系统那份。
    # ⚠ 但它只是 dataSet 的 key——同名 dataTemplate（值来源）不一定存在：
    #   样本 005-zlyglxmlx.xml 里 dataTemplateDef 的 key 是 zlyglxmlx，
    #   它引用的 dataSet 才叫 project，两者根本不同名；而控件的 linkdata 要的
    #   是**前者**。所以「本次出包的链接目标」不参与这套复用（见 force_own），
    #   否则控件会指向一个值来源里不存在的 key。离线/无清单时为空。
    try:
        sys_ds_keys = set(_objects.extra().get("datasets") or [])
    except Exception:            # noqa: BLE001
        sys_ds_keys = set()

    # 已经指向某个已存在数据集的控件：这些字段不再动（反解来的包、用户手配的）。
    # 本包与系统库的 key 都算「已存在」——反解真实包的控件指向的是系统数据集。
    wired: set[tuple[str, str]] = set()
    all_known = set(ds_by_key) | sys_ds_keys
    for form in project.forms:
        bo = project.bo(form.bo_code)
        if bo is None:
            continue
        for ff in form.fields:
            if not ff.value_source:
                continue
            bf = next((x for x in bo.fields if x.code == ff.name or x.name == ff.name), None)
            if bf is None or bf.type_new != "object" or not bf.object_type:
                continue
            if ff.value_source in all_known and ff.value_source != bf.object_type:
                # 已指向真实数据集 key（≠对象编码默认填法）→ 用户配置/反解来的，不动。
                # value_source 恰好等于对象编码的仍走下面的流程：
                # 那是 blueprint 的默认填法，要按「本包/系统是否已有数据集」决定
                # 是引用还是替换。
                wired.add((bo.code, bf.code))

    key_by_field: dict[tuple[str, str], str] = {}
    created: list[str] = []
    reused: list[str] = []
    # key 的命名痕迹（词典缺词退拼音）：与 created 一样属于「模型层事实」，
    # 没勾「选择器」模块时 key 不进包，提示也就没意义 → 同样按 announce 出。
    key_notes: list[str] = []

    for dst in sorted({f.object_type for _bo, f in ordered}):
        pending = [(bo, f) for bo, f in ordered
                   if f.object_type == dst and (bo.code, f.code) not in wired]
        if not pending:
            continue
        target = plan.target(dst)
        if target is None:
            continue
        # 按自动推导出的「过滤 + 返回」分组，同组共用一份值来源
        groups: list[tuple[tuple, list[tuple[Bo, object]]]] = []
        for bo, f in pending:
            # 「XX选择」链接字段：用户口径是**返回全部业务字段 + 主键、不默认过滤**，
            # 所以它既不走同名推导（不猜过滤列），也不与其他字段共用一份值来源
            # ——sig 末位带上 is_link，普通关联字段（项目类型 → classification）
            # 的老口径（主键+名称、按同名推导）一个字都不变。
            is_link = _is_link_field(bo, f, link_code_of)
            pair = None if is_link else _links.auto_filter(bo, f, target)
            sig: tuple = (pair[0], pair[1], ()) if pair else ("", "", ())
            sig = sig + (is_link,)
            for got, users in groups:
                if got == sig:
                    users.append((bo, f))
                    break
            else:
                groups.append((sig, [(bo, f)]))

        # key 一律经 value_source_key 生成（<对象名翻英文>_value，冲突才 _2/_3）。
        # 名字优先、编码兜底：``caiGou`` 这种拼音编码直接拼就是 caigou_value，
        # 而「采购申请」翻英文是 purchase_apply —— 用户要的是后者（见
        # model.value_source_key_base 的说明）。注意 datasetKey / boCode 仍写
        # **对象编码**，两者是两回事，别一起改。
        obj_name = names.get(dst, "")
        if obj_name == dst:
            # 系统清单里只有编码、没有名字（``it["name"]`` 为空）：当没名字处理，
            # 走编码兜底——与 reader 的判据一致，别让同一份对象在两条路径上
            # 生成两个不同族的 key。
            obj_name = ""
        primary_key = value_source_key(dst, bo_name=obj_name)
        # 本包已有的值来源：先认本次口径的 key，再认「指向这个对象的、不带过滤的
        # 那份」——用户完全可能自己给它起了名字（``classification_value``），
        # 只按 key 字符串猜会把那份当不存在，于是重复建一份、控件反而被降级。
        local_primary = _local_value_dataset(project, dst, primary_key)
        sys_primary = next((k for k in (primary_key, dst) if k in sys_ds_keys), "")
        used_keys: set[str] = set()

        for idx, (sig, users) in enumerate(groups):
            key = ""
            # 本次出包的「链接目标」字段（关联项目 / 关联其他业务对象）必须由本包
            # 自带一份 dataTemplate（值来源），不能只引用 ``sys_ds_keys`` 里的同名
            # key：
            #   ① 那份清单是 **dataSet**（数据集）的 key，不是 **dataTemplate**
            #      （值来源）的 key。样本 005-zlyglxmlx.xml 就是证据——
            #      dataTemplateDef 的 key 是 zlyglxmlx，它引用的 dataSet 才叫
            #      project。控件的 linkdata 要的是**前一**种，按对象编码复用等于
            #      指向一个值来源里不存在的 key，导入后下拉是空的；
            #   ② 目标可能压根不在系统里（用户手输的对象名，离线时核不出来），
            #      复用无从谈起；
            #   ③ 表单侧写了 link_linkage / link_condition，只认**本包这份**数据集
            #      的返回列与 rule_id，复用别人的数据集等于把联动/过滤静默降级。
            # 只对「关联XX 链接字段」这一族强制；其他普通关联字段（项目类型这种
            # 指向 classification 的）保持原有复用行为，避免一刀切。
            force_own = bool(link_target_code) and any(
                f.object_type == link_target_code for _b, f in users)
            # 这一组里有没有「XX选择」链接字段：决定了这份值来源是「返回全部业务
            # 字段、不默认过滤」还是老的「主键+名称、按同名推导过滤」。
            # 分组时已经把 is_link 放进 sig 末位（同组的必然一致），直接读 sig。
            group_is_link = bool(sig[3])
            if idx == 0 and local_primary:
                # 本包已有该目标的值来源 → 用户在 6_数据集 里写的配置是权威，
                # 只把没写的部分（过滤/返回列）按推导补上，绝不覆盖已填的
                key = local_primary
                ds = ds_by_key[key]
                if not ds.filter_field and not ds.filter_target:
                    ds.filter_field, ds.filter_target = sig[0], sig[1]
                if group_is_link:
                    # 两个开关只是「默认值」：filter_of 里显式填过的 filter_field
                    # 与 result_list 里显式填过的 result_fields 都优先于它们。
                    ds.result_all = True
                    ds.auto_filter = False
            elif idx == 0 and sys_primary and not sig[0] and not sig[2] \
                    and not force_own and not group_is_link:
                # 系统自带 <obj>_value 且我们不打算加过滤/自定义返回列 → 直接引用
                if sys_primary not in ds_by_key:
                    key = sys_primary
                    reused.append(key)
            if not key:
                for cand, purpose in _ds_candidates(dst, len(groups) + 1,
                                                    bo_name=obj_name):
                    if cand in used_keys or cand in sys_ds_keys or cand in ds_by_key:
                        # 系统数据集改不了、本包同名数据集配置又不同 → 换个候选
                        continue
                    ds = DataSet(key=cand,
                                 name=value_source_name(names.get(dst, dst), cand,
                                                        purpose=purpose),
                                 from_table=dst, ds_type=DS_VALUE,
                                 filter_field=sig[0], filter_target=sig[1],
                                 result_fields=list(sig[2]),
                                 # 见 model.DataSet 上两个开关的说明：链接字段的
                                 # 值来源要「全部业务字段 + 主键、默认不过滤」。
                                 result_all=group_is_link,
                                 auto_filter=not group_is_link)
                    project.datasets.append(ds)
                    ds_by_key[cand] = ds
                    created.append(f"{ds.name}({cand})")
                    key_notes.extend(value_source_key_notes(cand, obj_name))
                    key = cand
                    break
            if not key:
                continue
            used_keys.add(key)
            for bo, f in users:
                key_by_field[(bo.code, f.code)] = key

    # 这两条是**模型层**的事实（数据集建了/复用了），不代表「包里会有」：
    # 没勾「选择器」模块时它们不会进包、控件也会降级，那时由调用方传
    # announce=False 关掉，改由 builder 出一条「本次包里不含任何值来源」。
    # 用参数而不是事后按字符串过滤 —— 提示文案一改，字符串过滤就静默失效了。
    if created and announce:
        notices.append(
            "已自动创建值来源数据集 " + "、".join(created)
            + "：关联对象的下拉框要靠它取数（真实系统的做法）")
        notices.extend(key_notes)
    if reused and announce:
        notices.append(
            "引用系统已有数据集 " + "、".join(sorted(set(reused)))
            + "（系统自带值来源，不重复创建）")

    # 接线：控件 value_source 恰好等于目标对象编码（blueprint 的默认填法）→ 指向数据集。
    # 用户手填的数据集 key、反解来的 key 都不等于对象编码，天然不受影响。
    for form in project.forms:
        bo = project.bo(form.bo_code)
        if bo is None:
            continue
        for ff in form.fields:
            # 空 value_source 也要接：用户在「4_表单控件」里手写了控件、却没填
            # 「取值来源」时，以前会漏接——下拉指向空，导入后点开什么都没有。
            if ff.field_type not in ("select", "linkdata"):
                continue
            bf = next((x for x in bo.fields if x.code == ff.name or x.name == ff.name), None)
            if bf is None or bf.type_new != "object" or not bf.object_type:
                continue
            key = key_by_field.get((bo.code, bf.code))
            if key and ff.value_source in ("", bf.object_type):
                ff.value_source = key

    # 值来源口径的两句（返回列 / 联动列数）同样只在真会进包时才报；
    # 「控件改用对象选择器」那条是表单事实，任何情况都要报。
    if announce:
        notices.extend(_link_dataset_defaults(project, ds_by_key))
    notices.extend(_demote_unwired_links(project, ds_by_key, wired))
    if announce:
        notices.extend(_value_source_notes(project, plan, key_by_field))
    return notices


def _link_dataset_defaults(project: Project, ds_by_key: dict) -> list[str]:
    """给「XX选择」字段那份值来源打上两个默认口径：全字段返回 + 不默认过滤。

    为什么必须在最后**再扫一遍**、而不是建数据集时置位一次就完：
    ``blueprint.expand``（建标准全套表单）排在 ``builder.ensure_project_links``
    之前，那时链接字段要么还不存在、要么刚从「确认版 Excel」读回来、``is_link``
    还是 False。于是它的值来源会先按老口径建出来（主键+名称、按同名推导过滤），
    之后控件又已经指向这份数据集（进了 ``wired``），再也不会被重建——用户升级后
    会看到「自动补的字段是全字段，复用的老字段只有主键和名称」，同一个对象里
    两种口径并存，且没有任何报错。

    两个开关都只是**默认值**：``ds.filter_field`` / ``ds.result_fields`` 显式
    填过的仍然优先（filter_of 的显式分支、result_list 的 picked 分支），
    用户在 6_数据集 / app 面板里的配置不会被这里覆盖。
    """
    touched: list[str] = []
    for form in project.forms:
        bo = project.bo(form.bo_code)
        if bo is None:
            continue
        for ff in form.fields:
            if ff.field_type != "linkdata" or not ff.value_source:
                continue
            bf = next((x for x in bo.fields
                       if x.code == ff.name or x.name == ff.name), None)
            if bf is None or not getattr(bf, "is_link", False):
                continue
            ds = ds_by_key.get(ff.value_source)
            if ds is None or (ds.result_all and not ds.auto_filter):
                continue
            ds.result_all = True
            ds.auto_filter = False
            if ds.key not in touched:
                touched.append(ds.key)
    if not touched:
        return []
    return ["值来源 " + "、".join(touched)
            + "：按「XX选择」口径返回全部业务字段并清空默认过滤"
              "（6_数据集 / 面板里显式配过的过滤与返回列仍然优先）"]


def _demote_unwired_links(project: Project, ds_by_key: dict, wired: set) -> list[str]:
    """编辑态里**没有值来源**的关联控件，从 linkdata 降级成 selectObject。

    样本 config-sync-20261119184322 里同一个新建表单有两种关联控件：

        suoShuXiangMu    linkdata       ← 实施人员专门配了值来源 + 联动 + 过滤
        xiangMuLeiXing   selectObject   ← 「项目类型」这种普通关联字段

    也就是说 linkdata 的含义是「我挂了一份值来源」，不是一个更好看的下拉。
    没有值来源却写成 linkdata，`linkdata` 指向的就是一个包里根本没有的 key
    （系统清单里那些同名 key 只是 dataSet 的 key），导入后下拉点开是空的——
    比出成系统原本的对象选择器（selectObject）更糟。

    判定「有没有值来源」只看**本包**：值来源要随包走才取得到数，所以
    ``value_source`` 必须落在 ``ds_by_key``（本包 6_数据集）里。用户显式配过
    数据集 key 的（``wired``，如实名指向系统数据集、反解来的包）保持不动——
    那是他明确要的，不该被我们改掉。树形关联也保持 linkdata：树选择器只有
    linkdata 有，降级成 selectObject 会把层级拍平（PL30 那条口径）。
    """
    demoted: list[str] = []
    for form in project.forms:
        if form.kind not in (FORM_CREATE, FORM_IMPORT):
            continue
        bo = project.bo(form.bo_code)
        if bo is None:
            continue
        for ff in form.fields:
            if ff.field_type != "linkdata":
                continue
            if ff.value_source and ff.value_source in ds_by_key:
                continue                      # 包内有值来源 → 名副其实
            bf = next((x for x in bo.fields if x.code == ff.name or x.name == ff.name), None)
            if bf is None or bf.type_new != "object":
                continue
            if (bo.code, bf.code) in wired:
                continue                      # 用户显式配的数据集 key，不动
            if (bf.options or {}).get("tree") is True:
                continue                      # 树形关联只能走 linkdata
            ff.field_type = "selectObject"
            ff.value_source = ""
            demoted.append(f"控件「{ff.label or ff.name}」改用对象选择器"
                           f"（该字段没有值来源；需要联动/过滤请在 6.1_值来源 或 6_数据集 里建值来源）")
    return demoted


def self_value_key(code: str, taken=None, name: str = "") -> str:
    """自身选择器的 key = ``<对象名翻英文>_value``（第二份起 ``_2``/``_3``）。

    key 由工具生成、用户不用算（用户 2026-11-20 口径）。词干取**对象名称**的
    英文（采购申请 → purchase_apply），因为对象编码本身可能是拼音（``caiGou``）
    ——用编码拼就是 ``caigou_value``，用户看到第一句就是「怎么是拼音」。
    规则本体在 ``model.value_source_key``（reader / webui 也用它，只此一处）。
    """
    return value_source_key(code, taken, bo_name=name)


def _ds_targets_code(project: Project, ds: DataSet, code: str) -> bool:
    """这份值来源取的是不是 ``code`` 这个对象的数据。

    ``from_table`` 有三种写法：对象编码（``classification``，链接目标那样建的）、
    物理表名（``pp_classification``，自身值来源那样建的）、反解包里原样留下的
    表名。所以按「编码 / 本包对象的表名 / 补上 pp_ 前缀」三者一起比。

    为什么要按**目标对象**而不是 key 字符串找：key 现在是「对象名翻英文」
    （项目类型 → ``project_type_value``），与对象编码（``classification``）不再
    同族；继续拿 key 拼字符串去猜，用户在 6_数据集 / 6.1_值来源 里显式命名的
    那份（``classification_value``）就找不到了——结果是重复建一份、控件被降级。
    """
    text = str(code or "").strip()
    src = str(ds.from_table or "").strip()
    if not text or not src:
        return False
    candidates = {text, text.lower()}
    bo = project.bo(text) or next((b for b in project.bos if b.tbl_name == text), None)
    if bo is not None:
        candidates |= {bo.code, bo.tbl_name}
    candidates |= {"pp_" + c for c in list(candidates) if c}
    return src in candidates


def _local_value_dataset(project: Project, dst: str, primary_key: str) -> str:
    """本包已有的、指向 ``dst`` 的值来源 → 复用它，返回它的 key（没有就空串）。

    优先 ``primary_key``（本次口径的默认那份）；否则挑**没有过滤条件**的那份：
    同一个对象有多份选择器时，带过滤的是给特定控件用的，不带过滤的才是
    「这个对象的全部数据」，也就是这一组字段要的默认那份。
    """
    if any(d.key == primary_key for d in project.datasets):
        return primary_key
    plain = next((d for d in project.datasets
                  if _ds_targets_code(project, d, dst)
                  and not d.filter_field and not d.filter_target), None)
    return plain.key if plain is not None else ""


def ensure_self_value_sources(project: Project) -> list[str]:
    """每个业务对象默认有一份**指向自己**的值来源（用户 2026-11-19 口径）。

    用户原话：「值来源默认可以配置多个，除了默认的关联对象的全部值来源之外，
    默认要支持配置单个或多个自身的值来源；值来源的返回结果默认包含主键和生成
    业务对象的全部自建字段」。所以这里给每个出包对象补一份：

        key          = ``<对象名翻英文>_value``（见 self_value_key）
        名称          = ``<对象名>值来源``
        datasetKey   = 本对象编码
        from_table   = 本对象的物理表名（``pp_<编码>``）
        返回列        = ``result_all=True`` → 主键 → 名称 → 其余非系统字段
        过滤          = ``auto_filter=False`` + 无过滤字段 → 不出 filterConditions

    **返回列不另算一套**：渲染侧（gen_tpl.render_dataset / links.result_list）
    已经有「all_fields=True = 主键 → 名称 → 其余全部非系统业务字段」这条口径，
    这里只是把开关打开。两份代码各推一遍清单，迟早会出现「Excel 里看到 6 列、
    包里 5 列」这种只在线上才暴露的分叉。

    幂等 / 优先级（三条都不能少）：
      1. 已有同 key（典型是「项目」对象自己那份 project_value，链接字段先建的）
         → **复用，不改配置、不加第二份**；
      2. 用户在 6.1_值来源（或老 6_数据集）里显式写过的同 key → 用户配置优先；
      3. 重复出包（确认版 Excel 读回来再出包）不重复创建 → 也不再刷同一条提示。
         判定就看读回来的 ``auto_generated``（Excel「自动」列=是）。

    「同一个对象多份」不在这里生成：多份是用户在 6.1_值来源 里加行表达出来的
    （key 留空 → reader 按 ``_2``/``_3`` 自动编号，名称带用途后缀），这里只保证
    **默认那一份**存在，且不会和用户的那几份撞 key。
    """
    from . import links as _links

    notices: list[str] = []
    taken = {d.key for d in project.datasets}
    for bo in project.bos:
        key = self_value_key(bo.code, name=bo.name)
        # 「本包已有这个对象的值来源」按**目标对象**判定，不按 key 字符串：
        # key 现在是「对象名翻英文」，与对象编码不再同族（见 _ds_targets_code）。
        targets = [d for d in project.datasets
                   if _ds_targets_code(project, d, bo.code)]
        if any(d.key == key for d in targets):
            # ① key 正是本次口径生成的（``purchase_apply_value``）——
            #    典型是「项目」对象自己那份（链接字段先建的）或用户手写的同 key。
            continue
        if any(d.auto_generated for d in targets):
            # ② 上一版按**对象编码**生成的自动那份（key 是拼音，如 caigou_value）：
            #    **沿用旧 key**，不翻档也不再建第二份。存量包铁律——老确认版 Excel
            #    读回来再出包，产物要跟老的一致，不能凭空多出一条同名的选择器。
            #    想换成英文 key：在 6.1_值来源 里把那一行的「值来源key」清空
            #    （留空 = 按新口径重新生成）。
            continue
        if targets:
            # ③ 只有用户手配的其它值来源（常带过滤）：那份不是「全部数据」的默认份，
            #    不复用；但 key 万一撞上就往后排 _2（同一个对象可以有多份）。
            key = self_value_key(bo.code, taken, name=bo.name)
        ds = DataSet(
            key=key,
            name=value_source_name(bo.name, key),
            # 物理表名（pp_cai_gou）：render_dataset 里 from_table 不以 pp_ 开头
            # 才会去目标对象上取表名，直接给表名等于把「取哪张表」钉死在本对象上。
            from_table=bo.tbl_name or bo.code,
            ds_type=DS_VALUE,
            id_field="id_",
            name_field="name_",
            unique_field="id_",
            # 见 docstring：两个开关就是「默认口径」，用户显式配置仍然优先。
            result_all=True,
            auto_filter=False,
            auto_generated=True,
        )
        project.datasets.append(ds)
        taken.add(key)
        # 列数与渲染侧同一份清单里数出来（business_fields 已经排掉系统字段）
        n = len(_links.target_from_bo(bo).business_fields())
        notices.append(
            f"对象 {bo.code}：已生成自身选择器 {ds.name}"
            f"（{key}，返回主键 + {n} 个自建字段）")
        notices.extend(value_source_key_notes(key, bo.name))
    return notices


def demote_links_without_value_package(project: Project) -> list[str]:
    """没勾「选择器」模块时，编辑态的 linkdata 控件降级成 selectObject。

    为什么必须降级（这是「取消勾选也不留悬空」的那一半）：linkdata 的
    ``fieldOptions.linkdata`` 存的是一份**值来源的 key**，值来源必须随包走
    才取得到数。这个模块不勾，包里就没有任何 ``data-template/*.xml``，
    控件指向的 key 在导入端根本不存在——下拉点开是空的，而且界面看着
    「配好了」，属于线上极难查的一类问题。系统原本的对象选择器
    （selectObject）至少能选到对象、存下 id，只是没有联动/过滤。

    与 ``_demote_unwired_links`` 的分工：
      · 那个：值来源**在包内但这份 key 没有**（用户删了配置）时降级；
      · 这个：值来源**整个模块都不在包里**时降级，判据只能是 plan，与
        模型里有没有这份数据集无关（模型里一定有，见 ensure_self_value_sources）。

    树形关联（options.tree）这里也降级，与 ``_demote_unwired_links`` 的例外
    不同：树选择器只有 linkdata 有，但**没有值来源的 linkdata 连数据都取不到**，
    留着等于给用户一个打不开的空控件。提示里说明「要保留树形请勾上值来源」。
    """
    demoted: list[str] = []
    for form in project.forms:
        if form.kind not in (FORM_CREATE, FORM_IMPORT):
            continue
        bo = project.bo(form.bo_code)
        if bo is None:
            continue
        for ff in form.fields:
            if ff.field_type != "linkdata":
                continue
            bf = next((x for x in bo.fields if x.code == ff.name or x.name == ff.name), None)
            if bf is None or bf.type_new != "object":
                continue
            tree = (bf.options or {}).get("tree") is True
            ff.field_type = "selectObject"
            ff.value_source = ""
            demoted.append(
                f"控件「{ff.label or ff.name}」改用对象选择器"
                f"（本次没有勾选「选择器」模块，包里没有 {ff.name} 指向的值来源"
                + ("；树形关联本来要用关联数据控件，要保留请勾上「选择器」" if tree else "")
                + "）")
    return demoted


def _ds_candidates(dst: str, count: int, bo_name: str = "") -> list[tuple[str, str]]:
    """值来源数据集的候选 ``(key, 用途后缀)``：主名字优先，往后依次 ``_2`` / ``_3`` …

    ``<对象名翻英文>_value`` 是真实系统的命名惯例（线上 203 条选择器里绝大多数
    是这个形态，如 ``classification_value`` / ``customer_value``），而对象名翻
    英文走 ``naming.object_code``（英文词典优先、查不到才退拼音）——对象编码本身
    可能是拼音（``caiGou``），不能拿它拼 key（见 ``model.value_source_key_base``）。
    同一对象的多份配置（过滤条件不同才需要各建一份）往后排 ``_2`` / ``_3``，
    名称带用途后缀（``_按项目类型过滤`` / ``_2``），设计器列表里能一眼分开。
    """
    base = value_source_key(dst, bo_name=bo_name)
    out = [(base, "")]
    out.extend((f"{base}_{n}", "") for n in range(2, max(3, count + 2)))
    return out


def _still_linked(project: Project, bo: Bo, bf, key: str) -> bool:
    """这个字段的控件还挂着 ``key`` 这份值来源吗（linkdata 形态）。

    ``_demote_unwired_links`` 会把本包没有值来源的 linkdata 改成对象选择器，
    改完就不存在「引用」这回事了；提示语不能再按「引用系统值来源」出一行。
    """
    names = {bf.code, bf.name}
    for form in project.forms:
        if form.bo_code != bo.code:
            continue
        for ff in form.fields:
            if (ff.field_type == "linkdata" and ff.name in names
                    and ff.value_source == key):
                return True
    return False


def _value_source_notes(project: Project, plan, key_by_field: dict) -> list[str]:
    """「已为 X 建值来源并配置联动（返回 N 列，过滤：Y）」——让人能核对。"""
    from . import links as _links

    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for bo in project.bos:
        for bf in sorted(bo.fields, key=lambda x: x.sn):
            key = key_by_field.get((bo.code, bf.code))
            if not key or (bo.code, bf.code) in seen:
                continue
            seen.add((bo.code, bf.code))
            target = plan.target(bf.object_type)
            if target is None:
                continue
            if not _still_linked(project, bo, bf, key):
                # 控件已经在 _demote_unwired_links 里改成对象选择器（本包没有这份
                # dataTemplate）：那就不再存在「引用」关系，别报一条自相矛盾的
                # 「引用系统已有值来源 X」。
                continue
            ds = next((d for d in project.datasets if d.key == key), None)
            if ds is None:
                # 本包**没有**这份 dataTemplate（复用了系统清单里的 key——典型是
                # 树形关联那类必须走 linkdata 的字段）。不能说「返回 N 列」：
                # 用户会去包里翻这份值来源，翻不到；得明确「不在本包内」。
                lines.append(
                    f"关联对象 {bo.code}.{bf.code}（{bf.name}）→ 引用系统已有值来源 "
                    f"`{key}`（不在本包内，导入后按系统那份取数）")
                continue
            results, _explicit = target.result_list(ds.result_fields,
                                                    all_fields=ds.result_all)
            # 与生成侧同一份返回列（含自动联动到的列），提示里的列数才对得上
            results = _links.with_linked_columns(target, bo, bf, results, _explicit)
            filt = _links.filter_of(ds, target, bo, bf)
            extra = f"，过滤：{filt.source_label}" if filt is not None else ""
            linked = [x for x in results
                      if any(f.resolved_field_name() == x.column
                             and f.code != bf.code for f in bo.fields)]
            tail = f"，联动 {len(linked)} 列" if linked else ""
            lines.append(
                f"关联对象 {bo.code}.{bf.code}（{bf.name}）→ 值来源 {key}："
                f"返回 {len(results)} 列{extra}{tail}")
    return lines
