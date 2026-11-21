"""校验：把 Excel 里常见的填错在生成前拦住。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import fieldtypes, naming, spec
from .model import (
    EMPTY_OPERATORS,
    HIDE_OPERATORS,
    LABEL_BY_OP,
    TABLE_COLUMN_TYPES,
    Bo,
    BoField,
    Project,
)

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
CODE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# 驼峰：不允许下划线（系统「添加字段」的提示：编码用驼峰命名法，如 sysData、userId）
CAMEL_RE = re.compile(r"^[a-z][a-zA-Z0-9]*$")

CONTAINER_TYPES = {"grid", "grid_layout", "tabs", "card", "fieldset", "tablelayout", "label", "attachment"}

# 生成时会自动补齐的系统字段，不在 Excel 里出现属正常
SYSTEM_CODES = {"id", "tenantId", "ip", "deleted", "createBy", "createTime",
                "updateTime", "version", "updateBy"}


@dataclass
class Issue:
    level: str      # error / warn / info
    where: str
    message: str

    def __str__(self) -> str:
        icon = {"error": "✗", "warn": "!", "info": "·"}.get(self.level, "·")
        return f"{icon} [{self.where}] {self.message}"


# 手册「创建导入专用表单」明确禁止出现在导入表单里的字段：
#   创建人、创建时间、更新人、更新时间、ID、版本、流程实例等
_BANNED_IMPORT_CODES = {
    "id", "id_", "version", "createBy", "createTime", "updateBy", "updateTime",
    "processInstanceId", "process_instance_id", "processDefId", "procInsId",
    "deleted", "tenantId", "ip", "pinyin",
}
_BANNED_IMPORT_LABELS = ("创建人", "创建时间", "更新人", "更新时间", "流程实例", "版本", "主键")


def _is_native_code(code: str, target=None) -> bool:
    """编码是不是项目对象/系统约定里本来就有的（stateId / projectId…）。

    lazy import 避免 validate ↔ projlink 的循环依赖（projlink 只在函数里
    import validate 的兄弟模块，但两边都在模块顶层互相引会成环）。
    快照读不到时 projlink 会退回「系统字段 + 链接字段」，不会因为离线失效。
    关联其它业务对象时，目标对象的字段编码与链接字段也算原生。
    """
    try:
        from . import projlink as _projlink
        if _projlink.is_project_code(code):
            return True
        return bool(target is not None and _projlink.is_target_code(code, target))
    except Exception:
        return False


def _is_banned_import_field(ff) -> bool:
    """判断某个控件是否属于「导入表单里不许出现」的系统字段。"""
    name = (ff.name or "").strip()
    label = (ff.label or "").strip()
    if name in _BANNED_IMPORT_CODES:
        return True
    return any(k in label for k in _BANNED_IMPORT_LABELS)


def _value_source_auto(bo: Bo | None, ff) -> bool:
    """这个下拉/关联控件的取值来源会不会被**自动**接上？

    接上之后再报「未填取值来源」就是噪音：object 字段的取值来源统一由
    ``blueprint._ensure_value_sources`` 建/接（目标在本包、在系统清单、有字段
    快照、离线兜底，四条路都有人兜），用户既改不掉也不需要改。何况本函数跑在
    「关联对象/字典」被 resolve 之前，编码都还没定，这时他就算想填也不知道填什么。

    只豁免 object 字段：非 object 的手写 select（比如把某个选项字段做成了下拉）
    没填取值来源就是真的漏了，那条警告要留着。
    关联对象填错/留空另有 ``_check_object_ref`` 的专门提示，不会被这条豁免吃掉。
    """
    if bo is None or ff is None:
        return False
    bf = bo.field_by_code(ff.name)
    return bf is not None and bf.type_new == "object"


def _widget_issues(form, bo, where: str, proj: Project) -> list[Issue]:
    """控件层面的检查，跟「表单是标准全套还是单张」无关，两种入口都要跑。

    「标准全套」在 Excel 里是一条 `kind=bundle` 的记录，展开成新建/详情/
    条件三张表是出包阶段的事。所以在 validate 里对 bundle 直接 continue
    会漏掉控件本身的问题——而控件填错恰恰是最常见的（金额出成文本框、
    明细表指向一个不存在的列表），且只有打开界面才发现。
    """
    out: list[Issue] = []
    list_keys = {x.key for x in proj.lists}
    for ff in form.fields:
        # ---- 表格字段（子表）：控件是 tableField，列来自对象字段
        if ff.field_type == "tableField":
            bf = bo.field_by_code(ff.name) if bo is not None else None
            if bf is None:
                out.append(Issue(
                    "error", where,
                    f"表格控件 {ff.name} 在对象字段中不存在——"
                    f"tableField 绑定的是「表格」类型的字段编码"))
            elif fieldtypes.resolve(bf.type_new) != "table":
                out.append(Issue(
                    "warn", where,
                    f"控件 {ff.name} 是表格控件，但字段类型是"
                    f"「{fieldtypes.ui_label(bf.type_new)}」，导入后可能显示异常"))
            elif not (bf.table_columns or ff.table_columns):
                out.append(Issue(
                    "error", where,
                    f"表格字段 {ff.name} 一列都没有，导入后是一张空白子表。"
                    f"请在「2.1_表格列」里按「对象编码 + 字段编码」补列"))
            continue
        # ---- 明细表：字段编码填的是列表 key，得对着 5_列表 核
        if ff.field_type == "dataTemplate":
            if not ff.value_source:
                out.append(Issue(
                    "error", where,
                    f"明细表控件 {ff.name} 没填取值来源，"
                    f"「取值来源」要填 5_列表 里的列表key"
                    f"（可选：{', '.join(sorted(list_keys)[:3]) or '还没有列表'}）"))
            elif list_keys and ff.value_source not in list_keys:
                # 不是错误：只导表单、列表早就建在系统里是常见用法
                # （反解单个表单包必然走到这条路）。但也得说一声，
                # 因为列表要是真的不存在，子表就是一片空白。
                out.append(Issue(
                    "warn", where,
                    f"明细表控件 {ff.name} 指向的列表 {ff.value_source} 不在本包内，"
                    f"导入前请确认系统里已经有这个列表，否则子表是空的"
                    f"（本包可选：{', '.join(sorted(list_keys)[:3])}）"))
            continue
        # ---- 字段类型是数字/日期/表格，控件却是文本框
        if bo is None or ff.field_type != "text":
            continue
        bf = bo.field_by_code(ff.name)
        want = {"number": "number", "datetime": "datePicker", "table": "tableField"}.get(
            bf.type_new if bf else "", "")
        if want:
            out.append(Issue(
                "warn", where,
                f"控件 {ff.name}（{bf.name}）的字段类型是 {bf.type_new}，"
                f"控件类型却填了 text，导入后是普通文本框（建议改成 {want}，"
                f"表格字段可留空控件类型，出包时会自动按 tableField 生成）"))
    return out


# 单行控件：能两列就别占整行，否则右半边整片空白
_NARROW_TYPES = {"text", "number", "datePicker", "date", "select", "dictionary",
                 "selectObject", "dropSelector", "dropSelectOrg", "autoNumber"}
# 分区（卡片）多了要滚很久，超过这个数提一句
_GROUP_SOFT_LIMIT = 7


def _layout_notes(form) -> tuple[list[str], list[str]]:
    """布局体检：返回 (整行浪费的字段, 分区过多的提示)。

    布局 V1.0 之后「一行超过 2 个控件」在生成侧已经不可能出现
    （``_plan_groups`` 硬性切成 2 列），所以这里改查两类真问题：
      - 单行控件被标成「占满整行」→ 右半边整片空白，和详情表单里
        「项目风险提示（varchar(50)）占整行」是同一类毛病；
      - 分区太多 → 详情页要滚很久。
    """
    wide: list[str] = []
    for f in form.fields:
        if f.full_row and f.field_type in _NARROW_TYPES:
            wide.append(f"{f.label or f.name}（{f.name}）")

    from .gen_form import _plan_groups
    try:
        groups = _plan_groups(form)
    except Exception:
        groups = []
    notes: list[str] = []
    if len(groups) > _GROUP_SOFT_LIMIT:
        notes.append(
            f"分了 {len(groups)} 个区（{'、'.join(t for t, _ in groups[:8])}…），"
            f"详情页要滚很久，建议合并到 {_GROUP_SOFT_LIMIT} 个以内")
    return wide, notes


def _template_sheet_of(ds) -> str:
    """一份 DataSet 是从 Excel 哪张表来的（报错要指到用户能改的那一格）。"""
    from . import modules as _mod

    return "6.1_值来源" if _mod.module_of_dataset(ds.ds_type) == "value" else "6_数据集"


def _check_cross_module_keys(proj: Project, issues: list[Issue]) -> None:
    """列表 / 数据集 / 值来源的 key 必须**跨表**唯一。

    三者最终都落成 ``data_template/{key}.xml``（列表 type=default、值来源
    type=valueSource、数据集同理），导入端按 key 建模板 —— 撞了就是**后一条
    静默覆盖前一条**：包里明明有两条 XML，导入后只剩一条，界面不报错、数据看着
    也「少了几个下拉」。表内查重（上面的 used_list / used_ds）拦不住这种撞法，
    所以这里按「5_列表 vs 6_数据集 vs 6.1_值来源」再查一遍。

    为什么是 error 不是 warn：没有任何一种「两个模块共用同一个 key」是安全的，
    用户真正想要的多半是「复制一份再改」（改个 key 就行）。
    """
    seen: dict[str, tuple[str, str]] = {}
    for lst in proj.lists:
        key = str(lst.key or "").strip()
        if key:
            seen.setdefault(key, ("5_列表", f"列表「{lst.name or key}」"))
    for ds in proj.datasets:
        key = str(ds.key or "").strip()
        if not key:
            continue
        sheet = _template_sheet_of(ds)
        kind = "选择器" if sheet == "6.1_值来源" else "数据集"
        label = f"{kind}「{ds.name or key}」"
        prev = seen.get(key)
        if prev is not None:
            issues.append(Issue(
                "error", f"{sheet} {key}",
                f"key「{key}」在 {prev[0]} 和 {sheet} 里都用了"
                f"（{prev[1]} / {label}）：两者都会写成 data_template/{key}.xml，"
                f"导入时后一条会静默覆盖前一条，请改掉其中一个"))
            continue
        seen[key] = (sheet, label)


def validate(proj: Project) -> list[Issue]:
    issues: list[Issue] = []

    # object 字段的判定和出包归一用同一份候选池，避免「check 说没问题、
    # 出包却改成了别的对象」这种只有导入后才发现的分歧
    obj_pool = _object_pool(proj)

    used_bo: dict[str, str] = {}
    for bo in proj.bos:
        w = f"业务对象 {bo.code or '<空>'}"
        if not bo.code:
            issues.append(Issue("error", "1_业务对象", "对象编码为空"))
            continue
        if not CODE_RE.match(bo.code):
            issues.append(Issue("error", w, "对象编码只能含字母数字下划线，且以字母开头"))
        if bo.code in used_bo:
            issues.append(Issue("error", w, "对象编码重复"))
        used_bo[bo.code] = bo.tbl_name
        if not bo.tbl_name:
            issues.append(Issue("error", w, "物理表名为空"))
        elif not TABLE_RE.match(bo.tbl_name):
            issues.append(Issue("error", w, "物理表名只能用小写字母数字下划线"))
        if not bo.fields:
            issues.append(Issue("error", w, "没有任何字段"))
        _validate_fields(bo, issues, obj_pool, getattr(proj, "link_target", None))
        _validate_bo_cascade(bo, issues, w)

        # 选项值的短码在同一字典内不能重复（库里没有唯一约束，重复会静默覆盖）
        try:
            from .gen_bo import choice_conflicts
            for msg in choice_conflicts(bo):
                issues.append(Issue("error", w, msg))
        except Exception:
            pass

    used_form: set[str] = set()
    for form in proj.forms:
        if form.kind == "bundle":
            # 命名/覆盖这类要求是对展开后的单张表说的，这里只管控件本身
            issues.extend(_widget_issues(
                form, proj.bo(form.bo_code), f"表单 {form.key}", proj))
            continue
        w = f"表单 {form.key or '<空>'}"
        if not KEY_RE.match(form.key or ""):
            issues.append(Issue("error", w, "表单key 必须小写字母开头，仅含小写字母/数字/下划线"))
        if form.key in used_form:
            issues.append(Issue("error", w, "表单key 重复"))
        used_form.add(form.key)
        bo = proj.bo(form.bo_code)
        if bo is None:
            issues.append(Issue("error", w, f"绑定的对象 {form.bo_code} 不存在"))
        elif not form.fields:
            issues.append(Issue("error", w, "表单没有任何控件"))
        # 手册：条件专用表单命名固定为「条件专用_{对象名称}」，且须覆盖对象全部字段
        if form.kind == "condition" and bo is not None:
            if form.name != f"条件专用_{bo.name}":
                issues.append(Issue(
                    "warn", w, f"条件专用表单命名建议为「条件专用_{bo.name}」（当前：{form.name}）",
                ))
            have = {ff.name for ff in form.fields}
            lacking = [x.code for x in bo.fields if x.code not in have]
            if lacking:
                issues.append(Issue(
                    "warn", w,
                    f"条件专用表单未覆盖全部字段，自动化场景会取不到：{', '.join(lacking[:6])}",
                ))
        if form.kind == "import" and bo is not None and form.name != f"导入专用_{bo.name}":
            issues.append(Issue("warn", w, f"导入专用表单命名建议为「导入专用_{bo.name}」"))

        # 手册「创建导入专用表单」的硬性约束（这两条以前完全没查）：
        # 1. 创建人/创建时间/更新人/更新时间/ID/版本/流程实例 不得出现在导入表单里
        # 2. 必填字段必须与新建表单保持一致，否则导入校验与页面上不一致
        if form.kind == "import":
            banned = [ff for ff in form.fields if _is_banned_import_field(ff)]
            if banned:
                issues.append(Issue(
                    "error", w,
                    "导入专用表单不得包含系统字段："
                    + "、".join(f"{x.label or x.name}" for x in banned[:6]),
                ))
            cread = next((f for f in proj.forms
                          if f.kind == "create" and f.bo_code == form.bo_code), None)
            if cread is not None:
                want = {ff.name for ff in cread.fields if ff.required}
                have = {ff.name for ff in form.fields if ff.required}
                only_in_import = have - want
                missing = want - have
                if missing:
                    issues.append(Issue(
                        "warn", w,
                        "导入表单缺少新建表单里的必填："
                        + "、".join(sorted(missing)[:6])))
                if only_in_import:
                    issues.append(Issue(
                        "warn", w,
                        "导入表单比新建表单多标了必填："
                        + "、".join(sorted(only_in_import)[:6])))

        issues.extend(_widget_issues(form, bo, w, proj))

        for ff in form.fields:
            # 明细表控件的 name 是「5_列表」的列表key，不是对象字段编码，
            # 按字段存在性去核对必然误报一条「可能无法保存数据」——
            # 子表本来就是嵌另一个列表，取数由 dataTemplate 自己负责。
            if ff.field_type in CONTAINER_TYPES or ff.field_type == "dataTemplate":
                continue
            if bo is not None and bo.field_by_code(ff.name) is None and ff.name not in ("id", "version", "pinyin"):
                issues.append(Issue("warn", w, f"控件 {ff.name} 在对象字段中不存在，可能无法保存数据"))
            if ff.field_type in ("select", "linkdata") and not ff.value_source \
                    and not _value_source_auto(bo, ff):
                issues.append(Issue("warn", w, f"控件 {ff.name} 是下拉类控件但未填取值来源"))
            # 字典控件引用的 key 必须能在对象里找到。
            # 引错了（比如没带对象前缀）会退化成「固定值 + 候选值」两套，
            # 用户看到的是选项重复两遍。
            # 注意「有没有候选值」不影响「字典存在与否」：dic 字段就算这轮
            # 没取到候选值（AI 模型波动，2026-11-12 尹姣定表实测 4 个字段整轮
            # 缺 values），它的字典 key 也照样在出包时声明——漏了它表单引用
            # 就全部判死，用户被卡在导出。空下拉另有字段级 warn 提醒补值。
            if ff.field_type == "dictionary" and bo is not None and ff.value_source:
                # 用 value_source() 而不是 dict_key()：全局字典（项目对齐/反解带入）
                # 的 key 是项目侧 typeKey（report_type），dict_key() 永远算不出来，
                # 用后者会对着合法配置一路报「引用的字典不存在」。
                want = {f.value_source() for f in bo.fields if f.type_new == "dic"}
                if ff.value_source not in want:
                    issues.append(Issue(
                        "error", w,
                        f"控件 {ff.name} 引用的字典 {ff.value_source} 在对象里不存在，"
                        f"预览时选项会重复显示（应为 {', '.join(sorted(want)[:3]) or '对象里没有可选字典'}）"))
            if ff.field_type == "objectRole":
                issues.append(Issue("warn", w, f"控件 {ff.name} 依赖系统内的角色ID，建议在界面里手工添加"))

        # 布局（布局 V1.0：分区 -> 卡片 -> 表格布局V1.0）
        # 每行最多 2 个控件已由生成器保证，这里查的是「单行控件被标成整行」
        # 和「分区过多」这两类会真的让页面变难看的配置。
        wide_fields, layout_notes = _layout_notes(form)
        if wide_fields:
            issues.append(Issue(
                "warn", w,
                "这些控件占了整行但都是单行控件（" + "、".join(wide_fields[:4])
                + "），右半边会整片空白；长内容请改用 textarea/editor，"
                  "否则把「占满整行」清空",
            ))
        for note in layout_notes:
            issues.append(Issue("info", w, note))

        _validate_cascade(form, issues, w)
        _validate_hide_rules(form, issues, w, bo)

    used_list: set[str] = set()
    for lst in proj.lists:
        w = f"列表 {lst.key or '<空>'}"
        if not KEY_RE.match(lst.key or ""):
            issues.append(Issue("error", w, "列表key 必须小写字母开头，仅含小写字母/数字/下划线"))
        if lst.key in used_list:
            issues.append(Issue("error", w, "列表key 重复"))
        used_list.add(lst.key)
        bo = proj.bo(lst.bo_code)
        if bo is None:
            issues.append(Issue("error", w, f"对象 {lst.bo_code} 不存在"))
        else:
            for code in lst.columns:
                if bo.field_by_code(code) is None:
                    issues.append(Issue("error", w, f"显示字段 {code} 在对象 {bo.code} 中不存在"))
            if lst.tree and not lst.parent_field:
                issues.append(Issue("warn", w, "勾了树形展示但未填父字段编码"))
        for v in lst.views:
            if v not in ("grid", "board", "gantt", "chart"):
                issues.append(Issue("error", w, f"未知视图类型 {v}"))

    used_ds: set[str] = set()
    for ds in proj.datasets:
        w = f"数据集 {ds.key or '<空>'}"
        if not KEY_RE.match(ds.key or ""):
            issues.append(Issue("error", w, "数据集key 必须小写字母开头，仅含小写字母/数字/下划线"))
        if ds.key in used_ds:
            issues.append(Issue("error", w, "数据集key 重复"))
        used_ds.add(ds.key)
        if not ds.from_table:
            issues.append(Issue("error", w, "主表名为空"))

    _check_cross_module_keys(proj, issues)

    # 父子/平行关系专项校验：父对象必须真实存在、关联字段必须属于本对象。
    # 库里 226 个 object 字段里有 2 条 objectType 是无效编码的历史脏数据，
    # 这种关系导入后就是断的，必须在出包前拦住。
    try:
        from . import relations as _relations
        for msg in _relations.validate_explicit_relations(proj):
            issues.append(Issue("error", "1_业务对象", msg))
    except Exception:
        pass

    # 关联项目专项校验（自关联 / project_id_ 被占 / 多个链接字段 / 快照不可用）。
    # 规则主体在 projlink.validate 里，这里只负责把它的级别映射成本模块的 Issue：
    # 同一条规则若两边各写一遍，早晚会出现「check 说没事、出包却拦住」。
    try:
        from . import projlink as _projlink
        for item in _projlink.validate(proj):
            issues.append(Issue(item.get("level", "info"),
                                item.get("where", "1_业务对象"),
                                item.get("message", "")))
    except Exception:
        pass

    # 同名对象提醒：2026-11-11 起 bind() 不再静默把同名对象绑到系统 id 上
    # （防新建覆盖线上对象），所以这里提前告诉用户「同名了，走的是新建，
    # 要更新就填已有对象ID」——不然用户以为在改，其实建了个新的。
    try:
        from . import objects as _objects
        if _objects.catalog():
            for bo in proj.bos:
                if bo.exist_id:
                    continue
                hit = _objects.lookup(bo.name) or {}
                if not hit.get("exists"):
                    hit = _objects.lookup(bo.code) or {}
                if hit.get("exists"):
                    issues.append(Issue(
                        "warn", f"业务对象 {bo.code}",
                        f"与系统已有对象「{hit['code']}」同编码（id {hit.get('id', '')}），"
                        f"导入时系统会按编码匹配、按【更新】处理；"
                        f"若确实想新建，请改对象编码"))
    except Exception:
        pass

    # 级联：分组父值必须真实存在于父字段候选值里，否则导入后下拉是空的。
    # 这类包「导入成功」但级联不联动，最难查，必须在出包前说清楚。
    for bo in proj.bos:
        for f in bo.fields:
            if f.type_new != "dic" or not f.cascade_parent:
                continue
            parent = bo.field_by_code(f.cascade_parent)
            where = f"业务对象 {bo.code}"
            if parent is None:
                issues.append(Issue(
                    "error", where,
                    f"字段 {f.code}: 级联父字段 {f.cascade_parent} 在本对象里不存在"))
                continue
            if not f.choice_groups:
                issues.append(Issue(
                    "warn", where,
                    f"字段 {f.code}: 配了级联父级 {parent.code}，但候选值没有分组；"
                    f"请按「{parent.choices[0] if parent.choices else '父值'}：子1、子2」"
                    f"写进「选项值」，否则导入后级联不会生效"))
                continue
            missing = [p for p in f.choice_groups if p not in parent.choices]
            if missing:
                issues.append(Issue(
                    "warn", where,
                    f"字段 {f.code}: 级联分组里的父值 {'、'.join(missing[:3])} "
                    f"不在父字段 {parent.code} 的候选值里，这部分选项不会联动"))

    # 流程（workflowDefinition）：结构错会让导入端直接拒绝，
    # 所以在出包前拦住，而不是让用户拿着 XML 去试
    _validate_scripts(proj, issues)
    _validate_workflows(proj, issues)

    return issues


# 表单脚本（7_表单脚本）的钩子白名单。
# 来源：2026-11-14 在 8112 真实库 iform_form_def.extend_attr_.script 上统计 85 个表单得出
#   onLoad 80 / beforeSubmit 45 / beforeLoad 33 / saveBefore 20 / afterSubmit 16 /
#   onLoadActions 3 / onValidate 2
# 写错的钩子名不会报错、也不会执行——只能靠导入后人工点页面才发现，所以这里挡一道。
KNOWN_JFORM_HOOKS = {
    "onLoad", "beforeLoad", "beforeSubmit", "saveBefore", "afterSubmit",
    "onLoadActions", "onValidate",
}


def _validate_scripts(proj: Project, issues: list[Issue]) -> None:
    """表单脚本的静态体检：挂载方式、钩子名、括号配平。

    只做「写错却静默失效」这一类检查，不判断业务逻辑对不对——
    脚本内容本身由实施同事负责。
    """
    for fkey, script in proj.form_scripts.items():
        w = f"表单脚本 {fkey}"
        if "Object.assign(JForm" not in script.replace(" ", ""):
            if "Object.assign(" in script.replace(" ", ""):
                issues.append(Issue(
                    "warn", w,
                    "脚本用了 Object.assign 但没挂到 JForm 上；平台只认 "
                    "Object.assign(JForm, {...}) 这一种写法，其它挂载方式导入后不生效",
                ))
            else:
                issues.append(Issue(
                    "error", w,
                    "脚本缺少 Object.assign(JForm, {...})，平台不会执行；"
                    "整段脚本要包在 Object.assign(JForm, { onLoad: function(form){...} }) 里",
                ))
        # 钩子名：只认真实库里出现过的那些。拼成 onload / onl oad 都不会报错，只会静默不跑
        for hook in re.findall(r"([A-Za-z_]\w*)\s*:\s*(?:async\s+)?function\s*\(\s*form", script):
            if hook not in KNOWN_JFORM_HOOKS:
                issues.append(Issue(
                    "warn", w,
                    f"钩子 {hook} 不在已知集合里（{'/'.join(sorted(KNOWN_JFORM_HOOKS))}）；"
                    f"名字拼错不会报错、也不会执行，核对一下平台文档",
                ))
        if script.count("{") != script.count("}"):
            issues.append(Issue(
                "error", w,
                f"花括号不配平（{script.count('{')} 个 {{ / {script.count('}')} 个 }}），"
                f"脚本语法错误会让整张表单打不开",
            ))


def _validate_workflows(proj: Project, issues: list[Issue]) -> None:
    """流程三张表的校验。

    只查「导入后一定出问题」的硬结构，不查审美（配色/坐标）。
    样本依据：PP6/流程/项目立项_20261113145550.xml 的 v30 图（13 节点 / 15 流转）。
    """
    if not proj.workflows:
        return

    from .model import (WF_BRANCH, WF_ONGOING, WF_PASSED, WF_REJECTED,
                        WF_START, WF_STATE_OPTIONS)

    used_names: dict[str, int] = {}
    for wf in proj.workflows:
        w = f"流程 {wf.name or '<空>'}"
        if not (wf.name or "").strip():
            issues.append(Issue("error", "9_流程", "流程名称为空"))
            continue
        used_names[wf.name] = used_names.get(wf.name, 0) + 1
        if used_names[wf.name] > 1:
            # 节点与流转都按流程名关联，重名会把两条流程的节点串到一起
            issues.append(Issue("error", w, "流程名称重复，节点/流转会串到同一条流程上"))
        bo = proj.bo(wf.bo_code)
        if bo is None:
            issues.append(Issue(
                "error", w, f"对象编码 {wf.bo_code} 在 1_业务对象 中不存在，"
                            f"流程导入后会挂不到任何对象上"))
        if not (wf.category_id or proj.wf_category_id):
            issues.append(Issue(
                "warn", w, "没有流程分类ID，导入后流程会落在「未分类」（不影响流程能跑）"))

        # ---- 节点
        if not wf.nodes:
            issues.append(Issue("error", w, "没有任何节点，先在 10_流程节点 里排节点"))
            continue
        seen: dict[str, int] = {}
        starts = 0
        has_end = False
        for node in wf.nodes:
            nw = f"流程节点 {node.name}"
            if not (node.name or "").strip():
                issues.append(Issue("error", "10_流程节点", "节点名称为空"))
                continue
            seen[node.name] = seen.get(node.name, 0) + 1
            if node.kind == WF_START:
                starts += 1
            if node.kind in (WF_PASSED, WF_REJECTED):
                has_end = True
            if node.kind == WF_BRANCH and (node.form_key or "").strip():
                # 判断节点没有 Properties/FormKey 可写（样本 v30 的 3 个判断节点
                # 都没有表单），填了也无处落，反而是填错人的信号
                issues.append(Issue(
                    "error", nw, "判断节点不能填表单（判断节点没有待办、也没有表单），请清空「表单」列"))
            if node.kind == WF_ONGOING:
                ids = [x for x in node.handler_ids if x]
                names = [x for x in node.handler_names if x]
                if not ids and not names:
                    # 自动生成的兜底流程（开始→审批→通过）允许留空：
                    # 这个 app 不逼人配流程，审批人由项目经理到系统里指定。
                    # 出包时 Handler 会写成「提交时人工选择下一处理人」，不报错。
                    issues.append(Issue(
                        "warn" if getattr(wf, "auto", False) else "error", nw,
                        "默认流程的审批人留空了，导入后到 管理 › 流程设计 里指定处理人"
                        if getattr(wf, "auto", False) else
                        "审批节点没有处理人，导入后待办没人收到；"
                        "请在「处理人ID」列填角色/用户 id"))
                elif ids and names and len(ids) != len(names):
                    # Handler 是「texts 与 ids 一一对应」的紧凑 JSON，
                    # 长度不一致会错位：显示的是一批人，实际派给另一批
                    issues.append(Issue(
                        "error", nw, f"「处理人」{len(names)} 个、「处理人ID」{len(ids)} 个，"
                                     f"数量必须一一对应（否则待办会派给错的人）"))
            if node.business_state and node.business_state not in WF_STATE_OPTIONS:
                issues.append(Issue(
                    "warn", nw, f"业务状态「{node.business_state}」不在 "
                                f"{'/'.join(WF_STATE_OPTIONS)} 里，BusinessState 会留空"))
        for name, count in seen.items():
            if count > 1:
                issues.append(Issue(
                    "error", f"流程节点 {name}", f"节点名称在流程 {wf.name} 里重复 {count} 次，"
                                                 f"流转按名字找节点会连错"))
        if starts == 0:
            issues.append(Issue("error", w, "没有「开始」节点，流程无法发起"))
        elif starts > 1:
            issues.append(Issue("error", w, f"有 {starts} 个「开始」节点，每个流程只能有一个"))
        if not has_end:
            issues.append(Issue("error", w, "没有「通过」或「驳回」节点，流程跑不到终点"))
        if not any(n.kind == WF_REJECTED for n in wf.nodes):
            issues.append(Issue(
                "warn", w, "没有「驳回」节点：审批人只能退回给上游，不能直接终止"))

        # ---- 流转
        names = {n.name for n in wf.nodes}
        for edge in wf.edges:
            if edge.src not in names or edge.dst not in names:
                bad = edge.src if edge.src not in names else edge.dst
                issues.append(Issue(
                    "error", w, f"流转 {edge.src or '?'} → {edge.dst or '?'} 引用了不存在的节点「{bad}」"
                                f"（填节点名或「节点序号」都可以）"))
                continue
            if edge.condition_field and bo is not None:
                if bo.field_by_code(edge.condition_field) is None:
                    issues.append(Issue(
                        "error", w, f"流转 {edge.src} → {edge.dst} 的条件字段"
                                    f"「{edge.condition_field}」在对象 {bo.code} 里不存在"))
        for node in wf.nodes:
            if node.kind != WF_BRANCH:
                continue
            outs = [e for e in wf.edges if e.src == node.name]
            if len(outs) < 2:
                issues.append(Issue(
                    "error", f"流程节点 {node.name}",
                    f"判断节点只有 {len(outs)} 条出边，至少要 2 条（否则流程会走进死路）"))
            else:
                orders = [e.order for e in outs if e.order]
                if len(orders) != len(set(orders)):
                    # 顺序重复会让界面上两个按钮抢同一个位置
                    issues.append(Issue(
                        "warn", f"流程节点 {node.name}",
                        "判断节点有多条出边的「顺序」重复，请改成 1、2…"))

        # ---- 回流成环：审批流里有回边是正常的（退回），但会打乱画布分层，
        # 提示一句免得用户以为是生成器排错了位置
        if _has_cycle(wf):
            issues.append(Issue(
                "warn", w, "流转里存在回到上游的环（退回/返工），流程画布的分层可能不紧凑，"
                           "导入后可在流程设计器里微调位置"))


def _has_cycle(wf) -> bool:
    """流转里是否有环（有向图 DFS 三色标记）。"""
    graph: dict[str, list[str]] = {}
    for e in wf.edges:
        graph.setdefault(e.src, []).append(e.dst)
    white, gray, black = 0, 1, 2
    color = {n.name: white for n in wf.nodes}
    for start in list(color):
        if color.get(start, black) != white:
            continue
        stack = [(start, iter(graph.get(start, [])))]
        color[start] = gray
        while stack:
            node, kids = stack[-1]
            moved = False
            for kid in kids:
                if color.get(kid, white) == gray:
                    return True
                if color.get(kid, white) == white:
                    color[kid] = gray
                    stack.append((kid, iter(graph.get(kid, []))))
                    moved = True
                    break
            if not moved:
                color[node] = black
                stack.pop()
    return False


def _to_camel(snake: str) -> str:
    """sys_data_ → sysData，用于提示里给个改好的样子。"""
    parts = [p for p in (snake or "").strip("_").split("_") if p]
    if not parts:
        return ""
    return parts[0].lower() + "".join(p[:1].upper() + p[1:].lower() for p in parts[1:])


def _object_pool(proj: Project) -> list[dict]:
    """object 字段判定用的候选池：本包对象 + 系统对象清单，和出包归一用同一套。

    不自己拼一套：validate 说「能对上」而 builder 归一成别的对象，
    就会出现「check 通过、导入后下拉框空着」这种最难查的问题。
    """
    try:
        from . import objects as _objects
        return _objects.field_pool(proj)
    except Exception:
        return []


def _check_table_field(f: BoField, issues: list[Issue], w: str) -> None:
    """表格字段（子表）的列定义体检。

    真实约束来自生产包（project_create 的 planExcel / budgetExcel / riskExcel）：
      · 有列才有意义——没有 columns 的表单导入后是一张空白子表；
      · 列名在同一张表里不能重复（系统按列名找列，重名会串）；
      · 选项列要么给候选值，要么引用已有共享字典，否则单元格是个空下拉。
    这些都是「不看不知道、导入后才发现」的问题，所以放在 check 阶段拦。
    """
    cols = list(f.table_columns or [])
    if not cols:
        # 逃生口：「扩展参数」里手写的 columns（老工作簿、或从原生包直接抄的整块 JSON）
        raw = (f.options or {}).get("columns")
        if isinstance(raw, list) and raw:
            return          # 有列就放行，结构对不对由导入端兜底
        issues.append(Issue(
            "error", w,
            "「表格」字段没有任何列，导入后是一张空白子表。"
            "请到「2.1_表格列」按「对象编码 + 字段编码」补列（一列一行）"))
        return

    seen: dict[str, int] = {}
    for i, c in enumerate(cols, 1):
        name = (c.name or "").strip()
        if not name:
            issues.append(Issue("error", w, f"第 {i} 列的列名为空"))
            continue
        if name in seen:
            issues.append(Issue(
                "error", w,
                f"列名「{name}」重复（第 {seen[name]} 列和第 {i} 列），系统按列名定位会串列"))
        else:
            seen[name] = i
        if c.input_type not in TABLE_COLUMN_TYPES:
            issues.append(Issue(
                "warn", w,
                f"第 {i} 列「{name}」的类型 {c.input_type} 不认识，按字符串处理"))
        if c.input_type == "dic" and not c.choices and not c.dict_ref:
            issues.append(Issue(
                "warn", w,
                f"第 {i} 列「{name}」是选项列，既没填「选项值」也没填「关联字典」，"
                f"单元格是个空下拉"))
        if c.input_type != "number" and c.precision:
            issues.append(Issue(
                "warn", w,
                f"第 {i} 列「{name}」不是数字列，「精度」不会生效"))


def _check_object_ref(f: BoField, issues: list[Issue], w: str,
                      pool: list[dict] | None = None) -> None:
    """核对 object 字段的「关联对象/字典」。

    这一列填错最隐蔽：填「项目」「关联客户」「pp_project」字面看着都合理，
    导入后却是一个**空下拉框**，原来 check 只查非空所以查不出来。
    出包前 builder 会统一归一（objects.resolve_object_fields），
    所以这里只要**能归一**就放行，只有归不了的才提示。

    `pool` 为空（没连库、没有随包快照、也没有本包对象）时保持原有行为：
    已填值不判（判不了），留空仍然报 error。
    """
    try:
        from . import objects as _objects
    except Exception:
        return
    pool = list(pool or [])
    # 「系统里有没有这个对象」这个判断只有拿到真实清单才成立；
    # 只有本包对象时不能据此报「不是系统已有对象」，否则离线环境会满屏误报
    sys_known = bool(_objects.catalog())

    raw = (f.object_type or "").strip()
    if raw:
        if sys_known and _objects.resolve(raw, pool) is None:
            issues.append(Issue(
                "warn", w,
                f"关联对象 {raw} 既不在本包内，也不是系统已有对象，"
                f"导入后会是空下拉框。应填已有对象编码（如 project / customer），"
                f"或先把「{raw}」建成业务对象",
            ))
        return

    # 留空：只认「字段名/字段编码正好是对象名」，和出包时的推断规则完全一致
    # （不用包含匹配，否则「项目进度」会被判成 project，改错比报错更难发现）
    code = ((_objects.match_exact(f.name or "", pool) if pool else None)
            or (_objects.match_exact(f.code or "", pool) if pool else None)
            or "")
    if code:
        label = _objects.label(code, pool) or code
        issues.append(Issue(
            "info", w,
            f"「关联对象/字典」留空，出包时会按字段名推断为 {code}（{label}）；"
            f"要关联别的对象请直接填对象编码",
        ))
    else:
        # 没有系统清单时「推断不出来」是必然的，得说清原因，否则用户会以为
        # 系统里真没有 project 这个对象
        hint = "" if sys_known else "（本机还没有系统对象清单，连库刷新后再 check 可自动核对）"
        issues.append(Issue(
            "error", w,
            f"对象类型留空且无法从字段名推断出关联对象，请填对象编码（如 project）{hint}",
        ))


def _validate_fields(bo: Bo, issues: list[Issue],
                     obj_pool: list[dict] | None = None,
                     target=None) -> None:
    seen: dict[str, str] = {}
    for f in bo.fields:
        w = f"字段 {bo.code}.{f.code or '<空>'}"
        if not f.code:
            issues.append(Issue("error", "2_字段", "字段编码为空"))
            continue
        if not CODE_RE.match(f.code):
            issues.append(Issue("error", w, "字段编码只能含字母数字下划线，且以字母开头（推荐小驼峰）"))
        elif not CAMEL_RE.match(f.code):
            # 系统「添加字段」提示：编码用驼峰命名法（sysData、userId）
            issues.append(Issue(
                "warn", w,
                f"编码 {f.code} 含下划线，系统要求驼峰命名法（如 sysData、userId）"
                f"{'，建议改为 ' + _to_camel(f.code) if _to_camel(f.code) else ''}",
            ))
        if naming.is_reserved(f.code):
            issues.append(Issue(
                "warn", w,
                f"编码 {f.code} 是数据库关键字。列名会自动加尾下划线（{f.code}_）所以不影响导入，"
                f"但系统文档要求避免，建议改名如 {f.code}Value",
            ))
        # 拼音长串：系统原生对象用 name / stateId / handlerIds 这类短语义名，
        # shiFouGongYiJiShuLeiXiangMu 这种 20+ 字符的拼音串既不好读也不好维护
        if f.code.islower() and len(f.code) > 18:
            issues.append(Issue(
                "warn", w,
                f"编码 {f.code} 长 {len(f.code)} 字符，疑似拼音直译。"
                f"系统原生字段用简短英文（项目名称=name、项目状态=stateId），建议改成语义化英文",
            ))
        if f.code in seen:
            issues.append(Issue("error", w, "字段编码重复"))
        seen[f.code] = f.name
        if not fieldtypes.is_known(f.type_new):
            issues.append(Issue(
                "warn", w,
                f"未知数据类型 {f.type_new}：将按字符串生成。"
                f"如需校准类型映射，导出一份含该类型字段的对象包后执行 pp6 learn",
            ))
        elif not fieldtypes.is_verified(f.type_new):
            hint = fieldtypes.note_for(f.type_new)
            issues.append(Issue(
                "warn", w,
                f"类型 {f.type_new}（{fieldtypes.labels().get(f.type_new)}）尚未经真实导出包验证，{hint}；导入后请核对",
            ))
        # 「关联对象/字典」这一列的填法（两类字段要求不同）：
        #   object → **必填**，且必须能对上系统里真实存在的对象编码，填错导入后是空下拉
        #   dic    → **可留空**，程序按字段编码自动生成字典 key（BoField.dict_key 已实现），
        #            模板说明里也是这么写的。以前和 object 一起卡成 error，
        #            导致 AI 生成的「只带候选值、没带字典 key」的选项字段全部出不了包。
        if f.type_new == "dic" and not f.choices:
            if f.cascade_parent:
                # 级联字段没选项 = 这个包一定不联动，不是「可能空下拉」。
                # 级联的过滤逻辑是：选中父值 → 在子字典里找 depend_values_ 含该
                # 父项 id 的项。字典是空的，任何父值都过滤不出东西。
                # 2026-11-12 用户实测：包里字典项 0 个，导入后品种分类永远空，
                # 用户看到的现象就是「级联被吃了」。这种包没有导入价值，直接拦。
                issues.append(Issue(
                    "error", w,
                    f"级联字段「{f.name}」的选项值为空。级联靠字典项过滤，"
                    f"字典没有候选值就永远联动不出来（导入后父级选完子级仍是空）。"
                    f"请在「选项值」里按「父选项：子选项1、子选项2」的格式填写分组"))
            else:
                issues.append(Issue("warn", w,
                    "选项类型没填「选项值」，导入后界面上是个空下拉；如需候选值请补上"))
        elif f.type_new == "dic" and f.cascade_parent and not f.choice_groups:
            # 有选项但没分组：级联值算不出来，导入后父级选完子级仍全量显示。
            # 这不是致命错误（可能真的是「所有父级共享全部子项」），但要提醒。
            issues.append(Issue(
                "warn", w,
                f"级联字段「{f.name}」的选项值是平铺写法，没有分组信息，"
                f"导入后父级选完子级仍会全量显示。按「父选项：子1、子2」逐行填写即可"))
        elif f.type_new == "object":
            _check_object_ref(f, issues, w, obj_pool)
        elif f.type_new == "table":
            _check_table_field(f, issues, w)
        # 手册：编码不得出现「项目类型ID」这类命名，应直接叫「项目类型」
        # 例外：项目对象与系统原生的编码本来就带 Id（stateId / classificationId /
        # gradeId / projectId…）。对齐就是为了「跟项目完全一致」，再报「建议去掉
        # Id 后缀」等于让用户跟系统对着干，也会把这份警告刷满整页。
        if (f.code.endswith("Id") or f.code.endswith("ID")) and not _is_native_code(f.code, target):
            issues.append(Issue(
                "warn", w,
                f"命名不符合规范：{f.code} 建议去掉 Id 后缀（如 {f.code[:-2]}）",
            ))
        col = f.resolved_field_name()
        if not col.endswith("_"):
            issues.append(Issue("warn", w, f"物理列名 {col} 应以 _ 结尾（手册要求）"))
        # 系统明确禁止：不允许用数据库关键字命名字段
        # 注意判的是「列名本身」，order_ 不算（带下划线已避开），只有手填成 order 才拦
        if naming.is_reserved(col):
            issues.append(Issue(
                "error", w,
                f"物理列名 {col} 是数据库关键字，建表会失败。改成 {col}_ 或换个名字",
            ))
        if f.type_new == "number" and f.length and f.length > 38:
            issues.append(Issue("warn", w, "number 长度过大"))
        if f.field_name and not TABLE_RE.match(f.field_name.rstrip("_")):
            issues.append(Issue("warn", w, f"物理列名 {f.field_name} 不符合下划线风格"))

    names = set(seen)
    if bo.pk and bo.pk not in names and bo.pk not in SYSTEM_CODES:
        issues.append(Issue("warn", f"业务对象 {bo.code}", f"主键 {bo.pk} 不在字段列表中"))
    # 物理列名冲突
    col_seen: dict[str, str] = {}
    for f in bo.fields:
        col = f.resolved_field_name()
        if col in col_seen:
            issues.append(Issue("error", f"字段 {bo.code}.{f.code}", f"物理列名 {col} 与 {col_seen[col]} 冲突"))
        col_seen[col] = f.code


# ---------------------------------------------------------------- 级联 / 条件隐藏

# 能当级联父级的控件：选项来自字典的下拉。别的类型选完没有「值」的概念可继承，
# 写了 cascadeParentCode 系统也不会过滤，等于白配。
CASCADE_TYPES = {"select", "dictionary"}


def _cascade_parents(form) -> dict[str, str]:
    return {f.name: f.cascade_parent for f in form.fields if f.cascade_parent}


def _validate_cascade(form, issues: list[Issue], w: str) -> None:
    """级联链的合法性：父级必须存在、类型对得上、且不能绕成环。"""
    by_name = {f.name: f for f in form.fields}
    parents = _cascade_parents(form)

    for name, parent in parents.items():
        f = by_name[name]
        if parent == name:
            issues.append(Issue("error", w, f"控件 {name} 的级联父级指向自己，会形成死循环"))
            continue
        pf = by_name.get(parent)
        if pf is None:
            issues.append(Issue(
                "error", w,
                f"控件 {name} 的级联父级 {parent} 不在本表单里（级联只能引用同表单的控件）",
            ))
            continue
        if pf.field_type not in CASCADE_TYPES:
            issues.append(Issue(
                "warn", w,
                f"控件 {name} 的级联父级 {parent} 是 {pf.field_type}，只有选项类控件（select/字典）能作为父级",
            ))
        if f.field_type not in CASCADE_TYPES:
            issues.append(Issue(
                "warn", w,
                f"控件 {name} 是 {f.field_type}，级联只对 select/字典类控件生效",
            ))
        # 值来源不是字典的下拉，选项是固定列表，系统没法按父级过滤
        if f.field_type == "select" and (f.options or {}).get("datasource") != "dictionary":
            issues.append(Issue(
                "warn", w,
                f"控件 {name} 的选项来自值来源而不是层级字典，级联可能不生效"
                f"（字典类控件才带父子关系）",
            ))
        # 级联值（哪些子项属于哪个父项）现在会写进字段 options 的
        # selectOptions[].dependValues（2026-11-12 查线上前端源码确认的落点），
        # 所以这里不再要求用户去字典管理手工挂。真正的前提是「选项值要按分组写」，
        # 那条在字段校验里已经按 error 拦住/按 warn 提示了。

    # 环检测：沿父链走，走回自己就报错
    for start in parents:
        seen: list[str] = []
        cur = start
        while cur in parents:
            if cur in seen:
                issues.append(Issue(
                    "error", w,
                    "级联关系成环：" + " → ".join(seen[seen.index(cur):] + [cur]),
                ))
                break
            seen.append(cur)
            cur = parents[cur]


def _validate_hide_rules(form, issues: list[Issue], w: str, bo=None) -> None:
    """条件隐藏：引用的控件必须同表单存在，算子/取值要能拼成一条规则。"""
    by_name = {f.name: f for f in form.fields}
    seen: dict[tuple, int] = {}
    for rule in form.hide_rules:
        tag = f"隐藏规则 {rule.target or '<空>'}"
        if not rule.target:
            issues.append(Issue("error", w, "隐藏规则没有填写「隐藏控件」"))
            continue
        if rule.target not in by_name:
            issues.append(Issue("error", w, f"{tag}：目标控件 {rule.target} 不在本表单里"))
        if not rule.source:
            issues.append(Issue("error", w, f"{tag}：没有填写「依赖控件」"))
            continue
        if rule.source not in by_name:
            issues.append(Issue("error", w, f"{tag}：依赖控件 {rule.source} 不在本表单里"))
            continue
        if rule.source == rule.target:
            issues.append(Issue("error", w, f"{tag}：依赖控件不能是它自己"))
        if rule.operator not in HIDE_OPERATORS:
            issues.append(Issue(
                "error", w,
                f"{tag}：条件算子 {rule.operator} 不支持（可用：{'、'.join(HIDE_OPERATORS)}）",
            ))
        elif rule.operator not in EMPTY_OPERATORS and not rule.value:
            issues.append(Issue(
                "error", w,
                f"{tag}：条件「{LABEL_BY_OP.get(rule.operator, rule.operator)}」必须填条件值",
            ))
        # 条件值能不能对上「依赖控件」的选项：对不上，这条条件永远不会成立，
        # 而且界面上看不出来（Excel 里填的是中文，系统里比的是选项值）
        if bo is not None and rule.value:
            from .gen_bo import dict_choice_codes
            codes = dict_choice_codes(bo, rule.source)
            if codes and rule.value not in codes and rule.value not in codes.values():
                issues.append(Issue(
                    "warn", w,
                    f"{tag}：条件值 {rule.value} 不是 {rule.source} 的选项名也不是选项值，"
                    f"条件可能永远不成立（可填：{'、'.join(list(codes)[:6])}）",
                ))
        key = (rule.target, rule.source, rule.operator, rule.value)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            issues.append(Issue("warn", w, f"{tag}：同一条条件重复添加，结果一样，可以删掉一条"))


# ---------------------------------------------------------------- 业务对象层的级联

# 业务对象属性上能用的级联组合（出处：PP6/业务对象/项目立项业务对象.zip）：
#   子级一律是「选项」dic；
#   父级可以是「选项」dic（品种分类 ← 品种类别），
#   也可以是「关联对象」object（品种类别 ← 隶属的项目类型 classificationId）。
BO_CASCADE_CHILD = {"dic"}
BO_CASCADE_PARENT = {"dic", "object"}


def _validate_bo_cascade(bo: Bo, issues: list[Issue], w: str) -> None:
    """字段级的级联（attribute options 里的 cascadeEnabled / cascadeParentCode）。

    和表单层是两件事：业务对象定义「这个字段本身跟着谁过滤」，表单控件再声明一遍。
    真实样例两层都写，只写一层的话，属性面板和表单设计器会有一个看不到配置。
    """
    by_code = {f.code: f for f in bo.fields}
    parents = {f.code: f.cascade_parent for f in bo.fields if f.cascade_parent}

    for code, parent in parents.items():
        f = by_code[code]
        tag = f"字段 {code}（{f.name}）"
        if parent == code:
            issues.append(Issue("error", w, f"{tag} 的级联父字段指向自己，会形成死循环"))
            continue
        pf = by_code.get(parent)
        if pf is None:
            issues.append(Issue(
                "error", w,
                f"{tag} 的级联父字段 {parent} 在本对象里不存在（级联只能引用同一个对象的字段）",
            ))
            continue
        if f.type_new not in BO_CASCADE_CHILD:
            issues.append(Issue(
                "warn", w,
                f"{tag} 的类型是 {f.type_new}，样本里级联只用「选项」类型，可能不生效",
            ))
        if pf.type_new not in BO_CASCADE_PARENT:
            issues.append(Issue(
                "warn", w,
                f"{tag} 的父字段 {parent} 类型是 {pf.type_new}，只支持「选项」或「关联对象」",
            ))

    for start in parents:
        seen: list[str] = []
        cur = start
        while cur in parents:
            if cur in seen:
                issues.append(Issue(
                    "error", w,
                    "字段级联成环：" + " → ".join(seen[seen.index(cur):] + [cur]),
                ))
                break
            seen.append(cur)
            cur = parents[cur]
