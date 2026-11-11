"""校验：把 Excel 里常见的填错在生成前拦住。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import fieldtypes, naming, spec
from .model import EMPTY_OPERATORS, HIDE_OPERATORS, LABEL_BY_OP, Bo, BoField, Project

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


def _is_banned_import_field(ff) -> bool:
    """判断某个控件是否属于「导入表单里不许出现」的系统字段。"""
    name = (ff.name or "").strip()
    label = (ff.label or "").strip()
    if name in _BANNED_IMPORT_CODES:
        return True
    return any(k in label for k in _BANNED_IMPORT_LABELS)


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
        _validate_fields(bo, issues, obj_pool)
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

        for ff in form.fields:
            if ff.field_type in CONTAINER_TYPES:
                continue
            if bo is not None and bo.field_by_code(ff.name) is None and ff.name not in ("id", "version", "pinyin"):
                issues.append(Issue("warn", w, f"控件 {ff.name} 在对象字段中不存在，可能无法保存数据"))
            if ff.field_type in ("select", "linkdata") and not ff.value_source:
                issues.append(Issue("warn", w, f"控件 {ff.name} 是下拉类控件但未填取值来源"))
            # 字典控件引用的 key 必须能在对象里找到。
            # 引错了（比如没带对象前缀）会退化成「固定值 + 候选值」两套，
            # 用户看到的是选项重复两遍。
            if ff.field_type == "dictionary" and bo is not None and ff.value_source:
                want = {f.dict_key() for f in bo.fields
                        if f.type_new == "dic" and f.choices}
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

    # 父子/平行关系专项校验：父对象必须真实存在、关联字段必须属于本对象。
    # 库里 226 个 object 字段里有 2 条 objectType 是无效编码的历史脏数据，
    # 这种关系导入后就是断的，必须在出包前拦住。
    try:
        from . import relations as _relations
        for msg in _relations.validate_explicit_relations(proj):
            issues.append(Issue("error", "1_业务对象", msg))
    except Exception:
        pass

    return issues


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
                     obj_pool: list[dict] | None = None) -> None:
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
                f"导出一份含该类型字段的对象包执行 bin/pp6 learn 可自动校准",
            ))
        elif not fieldtypes.is_verified(f.type_new):
            hint = fieldtypes.note_for(f.type_new)
            issues.append(Issue(
                "warn", w,
                f"类型 {f.type_new}（{fieldtypes.labels().get(f.type_new)}）尚未用真实导出包校验，{hint}；导入后请核对",
            ))
        # 「关联对象/字典」这一列的填法（两类字段要求不同）：
        #   object → **必填**，且必须能对上系统里真实存在的对象编码，填错导入后是空下拉
        #   dic    → **可留空**，程序按字段编码自动生成字典 key（BoField.dict_key 已实现），
        #            模板说明里也是这么写的。以前和 object 一起卡成 error，
        #            导致 AI 生成的「只带候选值、没带字典 key」的选项字段全部出不了包。
        if f.type_new == "dic" and not f.choices:
            issues.append(Issue("warn", w,
                "选项类型没填「选项值」，导入后界面上是个空下拉；如需候选值请补上"))
        elif f.type_new == "object":
            _check_object_ref(f, issues, w, obj_pool)
        # 手册：编码不得出现「项目类型ID」这类命名，应直接叫「项目类型」
        if f.code.endswith("Id") or f.code.endswith("ID"):
            issues.append(Issue(
                "warn", w,
                f"命名不符合规范：{f.code} 建议去掉 Id 后缀（如 {f.code[:-2]}），提升阅读体验",
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
        # 这一条容易被漏：我们只能把 cascadeEnabled/cascadeParentCode 带进系统，
        # 而「哪些子项属于哪个父项」存在 iform_cat_dic.depend_values_ 里，
        # 业务对象导出包不带这个字段（实测 5 个真实包均无）。
        issues.append(Issue(
            "info", w,
            f"控件 {name} 配了级联父级 {parent}：导入后还要在系统的字典管理里"
            f"把《{f.label}》的子选项挂到《{pf.label}》对应选项下，否则下拉不过滤",
        ))

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
