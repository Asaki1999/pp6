"""对象所属关系识别：读系统历史关系 → 给建议 → 用户确认后才生效。

为什么单独一个模块而不是塞进 objects.py：
objects.py 已经承担「对象清单 / 字典 / 归一 / 出包对表」四件事，
关系识别是第五条独立链路（历史数据 + 规则 + 用户确认），放进来会让它更难维护。

设计红线（产品拍板，勿改）：
  1. 自动识别只给**建议**，默认一律「不建立」（enabled=false）——
     「有 projectId 字段」不等于「想建父子关系」，误建 PARENT 会影响系统的
     数据归属与列表行为，这个成本比用户多点一下高得多。
  2. 每个对象最多一条关系；确认后才写 Bo.parent_obj/parent_field/rel_type。
  3. 识别/历史数据不可用时，出包流程必须照常走，识别只是增强、不是门槛。

关系方向（已与真实导出包核对，架构稿曾写反）：
    src = 子对象（rel_field 属于它），dst = 父对象。
    例：project_create --project_id_--> project（项目立项挂在项目下）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .resources import resource_root

REL_TYPES = ("PARENT", "PARALLEL")
DEFAULT_REL_TYPE = "PARENT"

# 置信度只分三档，不给百分比——伪精确反而误导。
HIGH, MEDIUM, LOW = "high", "medium", "low"

_SOURCE_LABEL = {
    "target_db": "目标库",
    "package_snapshot": "随包快照",
    "unavailable": "无",
}


# ---------------------------------------------------------------- 历史关系目录


@dataclass
class RelationCatalog:
    """obj_relationship 的本地视图。

    source 只有两档有效数据：
      target_db        用户缓存 ~/.pp6kit/relationships.json（refresh 从库里刷的，
                       与 objects.json 同级，视为目标库数据，全功能识别）
      package_snapshot 随包快照 assets/relationships.json（打包时冻结的副本，
                       可能很旧 → R1 降级、R3 停用，宁缺毋滥）
    """

    rows: list[dict] = field(default_factory=list)
    source: str = "unavailable"
    fetched_at: str = ""
    error: str = ""

    @property
    def degraded(self) -> bool:
        """随包快照算降级：R1 降中、R3 停用。"""
        return self.source == "package_snapshot"

    def parent_count(self, code: str) -> int:
        """某对象历史上被多少个子对象当父对象（dst == code）。"""
        return sum(1 for r in self.rows if r.get("dst") == code)

    def history_fields(self, parent_code: str) -> set[str]:
        """某父对象的历史关联列名集合（其子对象们用过的 rel_field）。"""
        return {r.get("field") for r in self.rows
                if r.get("dst") == parent_code and r.get("field")}

    def parents_of_field(self, field_name: str) -> list[str]:
        """某物理列名在历史上指向过哪些父对象（按出现次数排序）。"""
        from collections import Counter
        hits = Counter(r.get("dst") for r in self.rows
                       if r.get("field") == field_name and r.get("dst"))
        return [code for code, _ in hits.most_common()]


_cache: RelationCatalog | None = None


def _cache_path() -> Path:
    return Path.home() / ".pp6kit" / "relationships.json"


def _read_rows(path: Path) -> tuple[list[dict], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], ""
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return [], ""
    return rows, str(data.get("fetched_at") or "") if isinstance(data, dict) else ""


def load_catalog(force: bool = False) -> RelationCatalog:
    """读关系历史：用户缓存优先，其次随包快照，都没有就 unavailable。"""
    global _cache
    if _cache is not None and not force:
        return _cache
    rows, fetched = _read_rows(_cache_path())
    if rows:
        _cache = RelationCatalog(rows=rows, source="target_db", fetched_at=fetched)
        return _cache
    rows, fetched = _read_rows(resource_root() / "assets" / "relationships.json")
    if rows:
        _cache = RelationCatalog(rows=rows, source="package_snapshot", fetched_at=fetched)
        return _cache
    _cache = RelationCatalog()
    return _cache


# ---------------------------------------------------------------- 识别


def _package_parent_options(project) -> list[dict]:
    return [{"code": b.code, "name": b.name, "source": "package", "packageNew": True}
            for b in project.bos]


def _system_parent_options(project) -> list[dict]:
    from . import objects
    pkg_codes = {b.code for b in project.bos}
    return [{"code": it["code"], "name": it.get("name") or it["code"],
             "source": "system", "packageNew": it["code"] in pkg_codes}
            for it in objects.catalog()]


def _confidence_of(rule: str, catalog: RelationCatalog) -> str:
    base = {"R1": HIGH, "R2": MEDIUM, "R3": MEDIUM, "R4": LOW}[rule]
    # 随包快照可能很旧：R1 的「历史字段名印证」可信度跟着降一档
    if rule == "R1" and catalog.degraded:
        return MEDIUM
    return base


def analyze(project, catalog: RelationCatalog | None = None) -> dict:
    """对本包对象生成关系建议。**只读**，绝不修改 Bo。

    返回 view model（直接进 parse/AI 的响应 JSON）。
    """
    from . import objects

    catalog = catalog or load_catalog()
    pool = objects.catalog()
    system_codes = {it["code"] for it in pool}
    name_of = {it["code"]: (it.get("name") or it["code"]) for it in pool}
    for b in project.bos:
        name_of.setdefault(b.code, b.name)

    parent_options = _package_parent_options(project) + _system_parent_options(project)

    # 清单为空：不做自动建议，但保留 Excel 显式关系的展示与校验
    no_catalog = not pool and not project.bos
    status = "ok"
    notice = None
    if not pool:
        status = "empty_catalog"
        notice = "本地没有系统对象清单，未执行关系识别；Excel 里已填写的父子关系仍按原样出包"
    elif catalog.source == "unavailable":
        status = "unavailable"
        notice = "没有对象关系历史数据（未连库刷新，也没有随包快照），只能识别字段指向，无法给出历史印证"

    items: list[dict] = []
    for bo in project.bos:
        item = _analyze_bo(bo, project, catalog, system_codes, name_of,
                           allow_rules=bool(pool))
        items.append(item)

    return {
        "status": status,
        "objectSource": objects.status().get("source") or "",
        "historySource": catalog.source,
        "historySourceLabel": _SOURCE_LABEL.get(catalog.source, catalog.source),
        "historyFetchedAt": catalog.fetched_at,
        "notice": notice,
        "parentOptions": parent_options,
        "items": items,
    }


def _analyze_bo(bo, project, catalog, system_codes, name_of, allow_rules=True) -> dict:
    from . import objects

    pkg_codes = {b.code for b in project.bos}

    def _parent_src(code: str) -> str:
        return "package" if code in pkg_codes else "system"

    # R0：Excel 已指定的显式关系（用户明确输入，优先级最高，不作为建议）
    excel_rel = _excel_relation(bo, project, system_codes, name_of)

    candidates: list[dict] = []
    if allow_rules:
        seen: set[tuple[str, str]] = set()      # (field_code, parent_obj) 去重
        for f in bo.fields:
            physical = f.resolved_field_name()
            if f.type_new == "object" and f.object_type:
                parent = f.object_type
                if parent == bo.code:
                    continue                  # 自关联（如「上级项目」）不建议为父子
                if parent not in system_codes and parent not in pkg_codes:
                    continue                  # 归一失败/目标不存在，不给建议
                hist_fields = catalog.history_fields(parent)
                if physical in hist_fields:
                    rule = "R1"
                    evidence = [
                        f"object 字段已归一到 {parent}",
                        f"物理列 {physical} 与历史关联字段一致",
                    ]
                else:
                    rule = "R2"
                    evidence = [f"object 字段已归一到 {parent}"]
                hit_count = catalog.parent_count(parent)
                if hit_count:
                    evidence.append(f"系统中已有 {hit_count} 个对象以「{name_of.get(parent, parent)}」为父对象")
                candidates.append(_candidate(bo, f, parent, rule, catalog,
                                             name_of, _parent_src(parent),
                                             evidence, hit_count,
                                             len(hist_fields)))
                seen.add((f.code, parent))
            elif catalog.source != "unavailable" and not catalog.degraded:
                # R3：非 object 字段的物理列名命中历史关联列 → 列出全部历史父对象。
                # 随包快照降级时停用（旧数据做这种弱匹配噪音太大）。
                for parent in catalog.parents_of_field(physical):
                    if parent == bo.code or (f.code, parent) in seen:
                        continue
                    hit_count = catalog.parent_count(parent)
                    candidates.append(_candidate(
                        bo, f, parent, "R3", catalog, name_of, _parent_src(parent),
                        [f"物理列 {physical} 在历史关系中被用作关联字段"],
                        hit_count, 1))
                    seen.add((f.code, parent))

    candidates = _dedup_sort(candidates)

    # 显式关系与建议不一致 → 放进 conflicts，仅作解释，不覆盖 Excel 值
    conflicts: list[dict] = []
    if excel_rel["specified"] and excel_rel["valid"]:
        for c in candidates:
            if c["parentObj"] != excel_rel["parentObj"] or \
               c["parentField"] != excel_rel["parentField"]:
                conflicts.append({
                    "candidateId": c["id"], "parentObj": c["parentObj"],
                    "parentField": c["parentField"],
                    "message": f"Excel 已指定「{excel_rel['parentObj']}」，"
                               f"与建议「{c['parentObj']}」不一致，默认沿用 Excel 值",
                })

    return {
        "objectCode": bo.code,
        "objectName": bo.name,
        "fieldOptions": [
            {"code": f.code, "name": f.name,
             "physicalName": f.resolved_field_name(),
             "type": f.type_new, "objectType": f.object_type or ""}
            for f in bo.fields
        ],
        "excelRelation": excel_rel,
        "candidates": candidates,
        "conflicts": conflicts,
    }


def _excel_relation(bo, project, system_codes, name_of) -> dict:
    """Excel 里用户手填的父子关系（R0）。"""
    if not bo.parent_obj:
        return {"specified": False, "valid": True, "parentObj": None,
                "parentField": None, "relType": bo.rel_type or DEFAULT_REL_TYPE,
                "errors": []}
    pkg_codes = {b.code for b in project.bos}
    errors: list[str] = []
    if bo.parent_obj not in system_codes and bo.parent_obj not in pkg_codes:
        errors.append(f"父对象 {bo.parent_obj} 既不在本包内，也不在系统对象清单里")
    field_obj = None
    if bo.parent_field:
        field_obj = next((f for f in bo.fields
                          if f.resolved_field_name() == bo.parent_field
                          or f.code == bo.parent_field), None)
        if field_obj is None:
            errors.append(f"关联字段 {bo.parent_field} 不在本对象的字段列表里")
    rel_type = (bo.rel_type or DEFAULT_REL_TYPE).upper()
    if rel_type not in REL_TYPES:
        errors.append(f"关系类型 {bo.rel_type} 不合法（只能是 PARENT / PARALLEL）")
    return {
        "specified": True, "valid": not errors,
        "parentObj": bo.parent_obj, "parentField": bo.parent_field,
        "relType": rel_type, "errors": errors,
    }


def _candidate(bo, f, parent, rule, catalog, name_of, parent_source,
               evidence, parent_count, field_count) -> dict:
    return {
        "id": f"{bo.code}:{f.code}:{parent}:{rule}",
        "rule": rule,
        "parentObj": parent,
        "parentName": name_of.get(parent, parent),
        "parentSource": parent_source,
        "packageNew": parent_source == "package",
        "fieldCode": f.code,
        "parentField": f.resolved_field_name(),
        "confidence": _confidence_of(rule, catalog),
        "relType": DEFAULT_REL_TYPE,
        "enabled": False,                    # 红线：默认一律不建立
        "historyParentCount": parent_count,
        "historyFieldCount": field_count,
        "evidence": evidence,
    }


def _dedup_sort(candidates: list[dict]) -> list[dict]:
    rank = {"R1": 0, "R2": 1, "R3": 2, "R4": 3}
    best: dict[tuple[str, str], dict] = {}
    for c in candidates:
        key = (c["fieldCode"], c["parentObj"])
        if key not in best or rank[c["rule"]] < rank[best[key]["rule"]]:
            best[key] = c
    out = sorted(best.values(),
                 key=lambda c: (rank[c["rule"]], -c["historyParentCount"], c["parentObj"]))
    return out


# ---------------------------------------------------------------- 用户确认


def parse_decisions(raw: str | None) -> dict | None:
    """解析 X-Relations 请求头。None = 没带；非法 → ValueError（API 层转 400）。"""
    if raw is None or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"X-Relations 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("X-Relations 需要 {\"version\":1,\"decisions\":{...}}")
    decisions = data.get("decisions")
    if not isinstance(decisions, dict):
        raise ValueError("X-Relations.decisions 必须是对象")
    for bo_code, d in decisions.items():
        if not isinstance(d, dict) or "enabled" not in d:
            raise ValueError(f"决策 {bo_code} 缺少 enabled")
        if d.get("enabled"):
            if not d.get("parentObj"):
                raise ValueError(f"决策 {bo_code} 要建立关系但缺少 parentObj")
            if not d.get("fieldCode"):
                raise ValueError(f"决策 {bo_code} 要建立关系但缺少 fieldCode")
            rt = str(d.get("relType") or DEFAULT_REL_TYPE).upper()
            if rt not in REL_TYPES:
                raise ValueError(f"决策 {bo_code} 的 relType 不合法：{d.get('relType')}")
    return decisions


def apply_confirmations(project, decisions: dict, catalog=None) -> list[str]:
    """把用户确认的关系写进 Bo。服务端不信任前端候选，全部重新校验。"""
    from . import objects

    notices: list[str] = []
    pool = objects.catalog()
    system_codes = {it["code"] for it in pool}
    pkg = {b.code: b for b in project.bos}

    for bo_code, d in decisions.items():
        bo = pkg.get(bo_code)
        if bo is None:
            raise ValueError(f"决策里的对象 {bo_code} 不在本包内")
        if not d.get("enabled"):
            if bo.parent_obj:
                notices.append(f"对象「{bo.name}」已取消与 {bo.parent_obj} 的父子关系")
            bo.parent_obj = ""
            bo.parent_field = ""
            bo.rel_type = DEFAULT_REL_TYPE
            continue
        parent = str(d["parentObj"]).strip()
        if parent not in system_codes and parent not in pkg:
            raise ValueError(f"父对象 {parent} 既不在本包内，也不在系统对象清单里")
        if parent == bo.code:
            raise ValueError(f"对象 {bo_code} 不能把父子关系指向自己")
        field_code = str(d["fieldCode"]).strip()
        target_field = next((f for f in bo.fields if f.code == field_code), None)
        if target_field is None:
            raise ValueError(f"字段 {field_code} 不在对象 {bo_code} 的字段列表里")
        rel_type = str(d.get("relType") or DEFAULT_REL_TYPE).upper()
        bo.parent_obj = parent
        bo.parent_field = target_field.resolved_field_name()
        bo.rel_type = rel_type
        parent_name = (objects.label(parent) or parent) if parent in system_codes \
            else pkg[parent].name
        notices.append(
            f"对象「{bo.name}」已确认隶属于「{parent_name}」"
            f"（{rel_type}，关联字段 {target_field.name}）")
    return notices


def validate_explicit_relations(project) -> list[str]:
    """出包前的关系专项校验，返回错误列表（空 = 通过）。"""
    from . import objects

    errors: list[str] = []
    pool = objects.catalog()
    system_codes = {it["code"] for it in pool}
    pkg_codes = {b.code for b in project.bos}
    for bo in project.bos:
        if not bo.parent_obj:
            continue
        if bo.parent_obj not in system_codes and bo.parent_obj not in pkg_codes:
            errors.append(
                f"业务对象 {bo.code}: 父对象 {bo.parent_obj} 既不在本包内，"
                f"也不在系统对象清单里，导入后关系会失效")
        if bo.parent_field and not any(
                f.resolved_field_name() == bo.parent_field or f.code == bo.parent_field
                for f in bo.fields):
            errors.append(
                f"业务对象 {bo.code}: 关联字段 {bo.parent_field} 不在本对象的字段列表里")
        rel_type = (bo.rel_type or DEFAULT_REL_TYPE).upper()
        if rel_type not in REL_TYPES:
            errors.append(f"业务对象 {bo.code}: 关系类型 {bo.rel_type} 不合法")
    return errors
