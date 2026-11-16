"""字段体检：把「可能填重了 / 填岔了」的字段挑出来给人看。

来自实际使用中的三类坑：

1. **同一份需求 Excel 多个 sheet 里，同一个东西被写成两个名字**
   （立项申请单位 / 立项申报单位）→ 到系统里就是两个字段，数据各填一半。
2. **多个对象或表单都有同一个字段，但类型不一样**
   （一个文本一个下拉）→ 列表取数、条件筛选时对不上。
3. **同一个字段在不同 sheet 里候选值不一样**
   （一边 5 项一边 7 项）→ 选得到 / 选不到同一个值，报表口径就乱了。

这里只负责**找出来**，不自动合并：两个名字是不是同一件东西，只有业务能判断，
自动改名会把「确实需要区分」的字段搞坏。所以输出是「疑似清单 + 每处在哪」，
由人决定改哪个。

判据尽量保守，宁可少报：中文短字段（≤3 字）基本不参与近似匹配，
否则「金额 / 总金额」「日期 / 时间」会刷一屏。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from difflib import SequenceMatcher

from .model import Project

# 近似名：这两个阈值是拿真实字段名试出来的
#   「立项申请单位 / 立项申报单位」相似度 0.833 → 命中 warn
#   「项目名称 / 项目简称」0.5、「金额 / 总金额」0.6 → 不报（确实不是一回事）
NEAR_HIT = 0.80
NEAR_WEAK = 0.72
NEAR_MIN_LEN = 5      # 只差一个字也要够长才算，短名字误报太多


@dataclass
class Place:
    """字段的一处出现。"""

    where: str                    # 「对象 立项 / 立项申请单位（applicantUnit）」
    owner: str                    # 所属对象或表单的名称
    code: str
    name: str
    type_text: str                # 已转成中文（文本 / 选项 / 关联对象 …）
    choices: tuple[str, ...] = () # dic 字段的候选值，别的类型为空


@dataclass
class Finding:
    kind: str                     # near-name / name-type / code-type / option / form-type
    level: str                    # warn / info
    title: str
    detail: str = ""
    places: list[str] = dc_field(default_factory=list)


def _type_text(type_new: str) -> str:
    """业务对象字段类型的中文叫法。"""
    try:
        from . import fieldtypes
        return fieldtypes.labels().get(type_new, type_new)
    except Exception:
        return type_new


# 表单控件类型是另一套取值（dictionary / select / linkdata …），
# 业务对象那边的标签里没有，单独列一份，免得界面上裸出英文码。
_FORM_TYPE_CN = {
    "text": "文本",
    "textarea": "多行文本",
    "number": "数字",
    "date": "日期",
    "select": "下拉",
    "dictionary": "数据字典",
    "dropSelector": "下拉选择",
    "dropSelectOrg": "组织选择",
    "linkdata": "关联数据",
    "editor": "富文本",
    "autoNumber": "自动编号",
    "attachment": "附件",
    "hidden": "隐藏字段",
    "tableField": "表格（子表）",
    # dataTemplate 是「把 5_列表 的列表模板嵌进表单」，和表格字段（子表）是两码事，
    # 以前这两个在界面上都叫「明细表」，出问题时看不出是哪种。
    "dataTemplate": "明细表（嵌列表）",
    "user": "用户",
    "department": "部门",
    "cascader": "级联选择",
}


def _form_type_text(t: str) -> str:
    return _FORM_TYPE_CN.get(t, t)


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _is_subsequence(short: str, long_: str) -> bool:
    """short 的字是否按顺序都能在 long_ 里找到（允许中间跳过字）。

    「项目来源」→「项目资金来源」为真（项目 + 来源，跳过了「资金」）；
    「立项申请单位」→「立项申报单位」为假（「请」在后者里没有）。
    """
    it = iter(long_)
    return all(ch in it for ch in short)


def near_level(a: str, b: str) -> tuple[str, float] | None:
    """两个字段名是不是疑似同一个。命中返回 (级别, 相似度)，否则 None。

    这条判据被两处共用：本文件的对象内/跨对象比对，以及读表阶段跨 sheet 比对
    （ai_sheet.cross_sheet_dupes）。放在这里是为了只有一套阈值，
    不会出现「体检说不像、上传提示说像」。
    """
    if a == b or len(a) < 3 or len(b) < 3:
        return None
    # 短名是长名的子序列、且长度差 2 字以上 → 限定词差异，不是写重了
    if abs(len(a) - len(b)) >= 2:
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        if _is_subsequence(short, long_):
            return None
    sim = _sim(a, b)
    if sim >= NEAR_HIT:
        return ("warn", sim)
    if min(len(a), len(b)) >= NEAR_MIN_LEN and sim >= NEAR_WEAK:
        return ("info", sim)
    return None


def _bo_places(proj: Project) -> list[Place]:
    out: list[Place] = []
    for bo in proj.bos:
        for f in bo.fields:
            if f.is_system:
                continue
            out.append(Place(
                where=f"对象 {bo.name or bo.code} / {f.name or f.code}（{f.code}）",
                owner=bo.name or bo.code,
                code=f.code,
                name=(f.name or f.code).strip(),
                type_text=_type_text(f.type_new),
                choices=tuple(f.choices or ()),
            ))
    return out


def _form_places(proj: Project) -> list[Place]:
    out: list[Place] = []
    for form in proj.forms:
        if form.kind == "bundle":
            continue
        for ff in form.fields:
            out.append(Place(
                where=f"表单 {form.name or form.key} / {ff.label or ff.name}（{ff.name}）",
                owner=form.name or form.key,
                code=ff.name,
                name=(ff.label or ff.name).strip(),
                type_text=_form_type_text(ff.field_type),
            ))
    return out


def _near_names(places: list[Place]) -> list[Finding]:
    """名字很像但不是同一个 —— 多半是同一件东西写了两遍。"""
    by_name: dict[str, list[Place]] = {}
    for p in places:
        by_name.setdefault(p.name, []).append(p)

    names = sorted(by_name)
    out: list[Finding] = []
    for i, a in enumerate(names):
        # 三个字的短名不参与：中文短词差一个字，往往是不同概念
        if len(a) < 3:
            continue
        for b in names[i + 1:]:
            if len(b) < 3:
                continue
            hit = near_level(a, b)
            if hit is None:
                continue
            level, sim = hit
            pa, pb = by_name[a], by_name[b]
            owners = {p.owner for p in pa} | {p.owner for p in pb}

            # 名字近似的同时候选值也不一样，是最典型的「同一件东西写了两遍」，
            # 单看名字可能还犹豫，把候选值差异一起摆出来就好判断了
            set_a = {p.choices for p in pa if p.choices}
            set_b = {p.choices for p in pb if p.choices}
            choice_note = ""
            if set_a and set_b and set_a != set_b:
                va = {v for s in set_a for v in s}
                vb = {v for s in set_b for v in s}
                only_a = [v for v in va if v not in vb]
                only_b = [v for v in vb if v not in va]
                parts = []
                if only_a:
                    parts.append(f"{a} 多 {'、'.join(only_a[:4])}")
                if only_b:
                    parts.append(f"{b} 多 {'、'.join(only_b[:4])}")
                if parts:
                    choice_note = "；候选值也不一样：" + "，".join(parts)

            out.append(Finding(
                kind="near-name",
                level=level,
                title=f"「{a}」和「{b}」名字很像",
                detail=(
                    f"相似度 {sim:.0%}，出现在 {len(owners)} 个位置。"
                    "如果指的是同一个东西，建议统一成一个字段名；"
                    "如果确实是两个东西，可以忽略"
                    + choice_note
                ),
                places=[p.where for p in pa + pb],
            ))
    return out


def _group_conflicts(places: list[Place], key, kind: str, what: str) -> list[Finding]:
    """按某个键分组，组内 type / 候选值不一致就报。"""
    groups: dict[str, list[Place]] = {}
    for p in places:
        groups.setdefault(key(p), []).append(p)

    out: list[Finding] = []
    for k, ps in groups.items():
        if len({p.owner for p in ps}) < 2:
            continue          # 只有一个对象里出现，谈不上「不一致」
        types = {p.type_text for p in ps}
        if kind == "type" and len(types) > 1:
            out.append(Finding(
                kind="name-type" if what == "name" else "code-type",
                level="warn",
                title=f"{'「' + k + '」' if what == 'name' else '字段 ' + k}在不同地方类型不一样",
                detail="、".join(sorted(types))
                       + " —— 类型不一致时，列表取数、条件筛选会出现对不上的情况",
                places=[f"{p.where} → {p.type_text}" for p in ps],
            ))
        elif kind == "choices":
            sets = [set(p.choices) for p in ps if p.choices]
            if len({frozenset(s) for s in sets}) < 2:
                continue
            all_vals = set().union(*sets)
            common = set.intersection(*sets)
            only = {}
            for p in ps:
                if not p.choices:
                    continue
                extra = [v for v in p.choices if v not in common]
                if extra:
                    only[p.owner] = extra
            if not only:
                continue
            detail = f"共有 {len(common)} 项，各处不一样的地方：" + "；".join(
                f"{owner} 多 {len(vals)} 项（{'、'.join(vals[:5])}）"
                for owner, vals in list(only.items())[:4]
            )
            if len(all_vals) > len(common):
                detail += f"。合计出现 {len(all_vals)} 个值"
            out.append(Finding(
                kind="option",
                level="warn",
                title=f"{'「' + k + '」' if what == 'name' else '字段 ' + k}的候选值不一致",
                detail=detail,
                places=[f"{p.where} → {len(p.choices)} 项" if p.choices else p.where
                        for p in ps],
            ))
    return out


def _form_type_clashes(proj: Project) -> list[Finding]:
    """同一个字段在新建表单和详情表单里控件类型不一样。"""
    groups: dict[str, list[Place]] = {}
    for p in _form_places(proj):
        groups.setdefault(p.code, []).append(p)

    out: list[Finding] = []
    for code, ps in groups.items():
        types = {p.type_text for p in ps}
        if len(types) < 2 or len({p.owner for p in ps}) < 2:
            continue
        out.append(Finding(
            kind="form-type",
            level="warn",
            title=f"字段 {code} 在不同表单里控件类型不一样",
            detail="、".join(sorted(types))
                   + " —— 同一字段在新建和详情里表现不一致，填进去的值可能展示不出来",
            places=[f"{p.where} → {p.type_text}" for p in ps],
        ))
    return out


def check(proj: Project) -> list[Finding]:
    """跑全部检查。返回按「越像同一件东西」排前面的清单。"""
    bo_places = _bo_places(proj)
    out: list[Finding] = []

    out.extend(_near_names(bo_places))

    # 类型冲突：同一个字段如果 code 和名字都撞，只报一条（不然一件事说两遍）。
    # 优先按名字报——用户看的是中文名，按 code 报还得先翻译一次。
    t_name = _group_conflicts(bo_places, lambda p: p.name, "type", "name")
    t_code = _group_conflicts(bo_places, lambda p: p.code, "type", "code")
    seen_places = {tuple(sorted(f.places)) for f in t_name}
    out.extend(t_name)
    out.extend(f for f in t_code if tuple(sorted(f.places)) not in seen_places)

    # 候选值同理
    o_name = _group_conflicts(bo_places, lambda p: p.name, "choices", "name")
    o_code = _group_conflicts(bo_places, lambda p: p.code, "choices", "code")
    seen_places = {tuple(sorted(f.places)) for f in o_name}
    out.extend(o_name)
    out.extend(f for f in o_code if tuple(sorted(f.places)) not in seen_places)

    out.extend(_form_type_clashes(proj))

    seen: set[tuple] = set()
    uniq: list[Finding] = []
    for f in out:
        key = (f.kind, f.title)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)

    out = uniq
    order = {"near-name": 0, "name-type": 1, "code-type": 2,
             "option": 3, "form-type": 4}
    level_order = {"warn": 0, "info": 1}
    return sorted(out, key=lambda f: (
        level_order.get(f.level, 2), order.get(f.kind, 9), f.title,
    ))


def to_payload(findings: list[Finding]) -> list[dict]:
    return [
        {
            "kind": f.kind,
            "level": f.level,
            "title": f.title,
            "detail": f.detail,
            "places": f.places,
        }
        for f in findings
    ]
