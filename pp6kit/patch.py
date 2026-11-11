"""patch：在一份**已有**同步包 zip 上做「业务对象加字段」的最小差异补丁。

为什么不是「反解成 Excel 再重新出包」
------------------------------------
主流程（inspect → Excel → builder）适合从零搭配置；但用户手上这份 zip
常常是系统「推送工作区」导出来的原生包，里面有对象角色、操作日志模板、
字典样式、表单权限、关系等特有内容。反解只捞回我们能表达的那部分，
重新生成等于把没建模的细节全部抹掉。

所以这里走另一条路：**字符串级补丁**。
  - 只改目标 bo XML：在 bodefentity 最后一个 attribute 后插入新的
    <attribute/>，必要时往 dictionaryTypeList / dictionaryList 补选项；
  - 其余 XML 条目（以及 bo XML 里没碰到的字节）原封不动复制；
  - zip 条目顺序、时间戳、压缩方式、ZipInfo 元数据一并保留；
  - comment 沿用系统原生格式 CONFIG_SYNC_NOTE:。

这样导入端拿到的仍然是原来那份包，只是多了几个字段，风险最小。
字段行的生成规则直接复用 gen_bo._attr_line（BoField.resolved），
保证「加字段」和「从 Excel 新建」出来的 XML 属性集合完全一致。

另附两条配套能力：
  - patched_workbook：把原包反解成 Excel 并带上新字段，供想继续改表单/列表
    的人走完整主流程重新出包；
  - fields_from_excel / fields_template_bytes：批量录入新增字段。
"""
from __future__ import annotations

import io
import json
import re
import tempfile
import zipfile
from collections import Counter
from copy import copy
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from . import fieldtypes as _ft
from . import gen_bo
from .blueprint import control_for
from .builder import package_name
from .inspect import inspect_path
from .model import BoField, FormField
from .template import build_workbook
from .xmlw import cdata, esc_attr

# 系统内置字段：不允许以「新增字段」的方式再塞一份
_RESERVED_CODES = {
    "id", "tenantId", "ip", "deleted", "createBy", "createTime",
    "updateTime", "version", "updateBy", "pinyin",
}

# 增量字段表的列（前端表格 / 粘贴 / 模板 Excel 共用一套口径）
FIELD_COLUMNS = [
    ("字段编码", "code", 20, "英文小驼峰，如 riskLevel；必填，不能与包里已有字段重名"),
    ("字段名称", "name", 18, "界面显示名，留空用字段编码"),
    ("数据类型", "type", 18, "字符串 / 数字 / 日期时间 / 大文本 / 用户 / 部门 / 选项 / 对象 / 自动编号 …"),
    ("选项值", "choices", 30, "仅「选项」类型：逗号或换行分隔，顺序即界面顺序"),
    ("关联对象/字典", "objectType", 22, "类型=对象时填对象编码；类型=选项时留空自动生成字典"),
    ("长度", "length", 10, "留空按类型默认"),
    ("精度", "precision", 10, "小数位，仅数字有效"),
    ("默认值", "default", 14, "填了会同时打开 isDefault 开关"),
    ("必填", "required", 10, "是 / 否，默认否"),
    ("建索引", "index", 10, "是 / 否，默认否"),
    ("物理列名", "fieldName", 20, "留空自动 = 下划线_，如 risk_level_"),
    ("字段说明", "desc", 28, "选填"),
]

# 增量 Excel 表头 -> 内部字段键
_HEADER_MAP = {
    "字段编码": "code", "字段名称": "name", "数据类型": "type",
    "选项值": "choices", "关联对象/字典": "objectType", "关联对象": "objectType",
    "长度": "length", "精度": "precision", "默认值": "default",
    "必填": "required", "允许空": "is_null",
    "建索引": "index", "物理列名": "fieldName", "字段说明": "desc",
    "说明": "desc", "级联父字段": "cascadeParent", "扩展参数": "options",
    "对象编码": "bo_code", "序号": "sn",
}

# 与 gen_bo._dict_blocks 完全一致的选项展示样式（2026-11-11 原生包逐字节核对）
_DIC_STYLE = ('{"backgroundColor":"","shape":"tag","effect":"light",'
              '"type":"custom","color":"#000000","boardColor":"","icon":""}')


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "y", "yes", "true", "是", "√")


def _as_int(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _split_choices(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
    else:
        items = re.split(r"[,，;；\r\n]+", str(value or ""))
    return [x for x in (s.strip() for s in items) if x]


def _as_options(value) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        got = json.loads(str(value))
        return got if isinstance(got, dict) else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"扩展参数不是合法 JSON：{exc}") from exc


# ---------------------------------------------------------------- 字段规格


def normalize_field(raw: dict, bo_code: str, sn: int = 0) -> BoField:
    """把前端/Excel 的一行字段定义收拢成 BoField（生成规则与 Excel 主流程同源）。"""
    code = str(raw.get("code") or "").strip()
    if not code:
        raise ValueError("字段编码不能为空")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", code):
        raise ValueError(f"字段编码 {code!r} 不合法：需英文小驼峰，如 riskLevel")
    if code in _RESERVED_CODES:
        raise ValueError(f"字段编码 {code!r} 是系统内置字段，不能新增")

    type_new = _ft.resolve(raw.get("type") or "varchar")
    if not _ft.is_known(type_new):
        raise ValueError(f"字段 {code} 的数据类型 {type_new!r} 不在类型字典里")

    raw_null = raw.get("is_null")
    if raw_null is not None and str(raw_null).strip():
        is_null = _truthy(raw_null)
    else:
        is_null = not _truthy(raw.get("required"))

    return BoField(
        bo_code=bo_code,
        code=code,
        name=str(raw.get("name") or "").strip() or code,
        type_new=type_new,
        length=_as_int(raw.get("length")),
        precision=_as_int(raw.get("precision")),
        is_index=_truthy(raw.get("index")),
        is_null=is_null,
        default_value=str(raw.get("default", raw.get("default_value") or "") or ""),
        options=_as_options(raw.get("options")),
        desc=str(raw.get("desc") or "").strip(),
        field_name=str(raw.get("fieldName") or "").strip(),
        object_type=str(raw.get("objectType") or "").strip(),
        cascade_parent=str(raw.get("cascadeParent") or "").strip(),
        choices=_split_choices(raw.get("choices")),
        sn=sn,
    )


def field_payload(field: BoField) -> dict:
    return {
        "code": field.code,
        "name": field.name,
        "type": field.type_new,
        "choices": "，".join(field.choices),
        "objectType": field.object_type,
        "length": field.length,
        "precision": field.precision,
        "default": field.default_value,
        "required": not field.is_null,
        "index": field.is_index,
        "fieldName": field.field_name,
        "desc": field.desc,
    }


# ---------------------------------------------------------------- 读取原包


def _parse_bo_xml(text: str) -> dict | None:
    """读 bo XML 的实体信息与已有字段；解析失败返回 None。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    ent = next((c for c in root if _local(c.tag) == "bodefentity"), None)
    if ent is None:
        return None
    attrs = [c for c in ent if _local(c.tag) == "attribute"]
    fields = [{
        "code": a.attrib.get("code", ""),
        "name": a.attrib.get("name", ""),
        "type": a.attrib.get("dataTypeNew", ""),
        "fieldName": a.attrib.get("fieldName", ""),
        "sn": int(a.attrib["sn"]) if a.attrib.get("sn", "").isdigit() else i,
    } for i, a in enumerate(attrs, 1)]

    type_keys: list[str] = []
    type_sn = 0
    item_sn: dict[str, int] = {}
    for child in root:
        if _local(child.tag) == "dictionaryTypeList":
            for d in child:
                if _local(d.tag) != "dictionaryType":
                    continue
                key = d.attrib.get("typeKey", "")
                type_keys.append(key)
                if d.attrib.get("sn", "").isdigit():
                    type_sn = max(type_sn, int(d.attrib["sn"]))
        elif _local(child.tag) == "dictionaryList":
            for d in child:
                if _local(d.tag) != "dictionary":
                    continue
                key = d.attrib.get("typeKey", "")
                if d.attrib.get("sn", "").isdigit():
                    item_sn[key] = max(item_sn.get(key, 0), int(d.attrib["sn"]))

    return {
        "id": ent.attrib.get("id", ""),
        "code": ent.attrib.get("code", ""),
        "name": ent.attrib.get("name", ""),
        "tbl": ent.attrib.get("tblName", ""),
        "fields": fields,
        "maxSn": max((f["sn"] for f in fields), default=0),
        # 线上真实包里 sn 有重复（cs 对象 30 个字段、sn 只到 23），
        # 不能简单 max+1，否则新字段会和已有字段撞号
        "usedSn": {f["sn"] for f in fields},
        "typeKeys": type_keys,
        "typeSn": type_sn,
        "itemSn": item_sn,
    }


def _xml_prefix(text: str, local: str) -> str:
    m = re.search(r"<(?:([A-Za-z_][\w.-]*):)?" + re.escape(local) + r"\b", text)
    if not m:
        return ""
    return (m.group(1) or "") + (":" if m.group(1) else "")


def inspect_package(data: bytes) -> dict:
    """列出包里有哪些业务对象、各自已有字段，供前端选择加工目标。"""
    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"这不是一个有效的 zip 包：{exc}") from exc

    with zin:
        entries = [{
            "name": i.filename,
            "size": i.file_size,
            "kind": i.filename.split("/")[0] if "/" in i.filename else "",
        } for i in zin.infolist()]
        comment = zin.comment.decode("utf-8", "replace")
        objects: list[dict] = []
        bad: list[str] = []
        for i in zin.infolist():
            name = i.filename
            if not (name.startswith("bo/") and name.endswith(".xml")):
                continue
            if name.startswith("__MACOSX") or "/." in name:
                continue
            try:
                text = zin.read(name).decode("utf-8")
            except UnicodeDecodeError:
                bad.append(f"{name}: 不是 UTF-8 XML，已跳过")
                continue
            info = _parse_bo_xml(text)
            if info is None:
                bad.append(f"{name}: 不是业务对象 XML（缺 bodefentity），已跳过")
                continue
            objects.append({
                "file": name,
                "id": info["id"],
                "code": info["code"],
                "name": info["name"],
                "tbl": info["tbl"],
                "fieldCount": len(info["fields"]),
                "maxSn": info["maxSn"],
                "fields": info["fields"],
                "hasId": bool(info["id"]),
            })

    counts = Counter(o["code"] for o in objects)
    for o in objects:
        o["copies"] = counts[o["code"]]

    notes = list(bad)
    if not objects:
        notes.append("包里没有可用的 bo/*.xml，无法加字段（请确认上传的是导出/推送包）")
    dup = {c: n for c, n in counts.items() if n > 1}
    if dup:
        notes.append("同一对象在包里有多份 XML（重复推送所致）：" +
                     "、".join(f"{c} ×{n}" for c, n in dup.items()) +
                     "；默认只改选中的那一份，可勾选「同步改所有副本」")
    if comment and not comment.startswith("CONFIG_SYNC_NOTE:"):
        notes.append("原包 comment 不是 CONFIG_SYNC_NOTE: 格式，出包时会按系统格式重写")

    return {
        "ok": True,
        "objects": objects,
        "entries": entries,
        "entryCount": len(entries),
        "boCount": len(objects),
        "comment": comment,
        "notes": notes,
        "types": [{"code": c, "label": _ft.ui_label(c),
                   "verified": _ft.is_verified(c)} for c in _ft.all_types()],
        "columns": [{"label": a, "key": b, "width": c, "note": d}
                    for a, b, c, d in FIELD_COLUMNS],
    }


# ---------------------------------------------------------------- XML 补丁


def _self_closing_end(text: str, start: int) -> int:
    """从元素起点找到自闭合 `/>` 的结束位置；跳过引号内的 `>`。"""
    quote = ""
    i = start
    while i < len(text) - 1:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "/" and text[i + 1] == ">":
            return i + 2
        elif ch == ">":
            return i + 1
        i += 1
    return -1


def _attr_spans(text: str, tag: str, start: int, end: int) -> list[tuple[int, int, dict]]:
    """entity 区间内所有 <attribute/> 的 (起, 止, 属性)。"""
    pat = re.compile(r"<" + re.escape(tag) + r"\b")
    out = []
    for m in pat.finditer(text, start, end):
        stop = _self_closing_end(text, m.start())
        if stop < 0 or stop > end:
            continue
        frag = text[m.start():stop]
        attribs = dict(re.findall(r'([\w:.-]+)="([^"]*)"', frag))
        out.append((m.start(), stop, attribs))
    return out


def _attr_line_for(field: BoField, prefix: str) -> str:
    line = gen_bo._attr_line(field).strip()
    if prefix != "tns:":
        line = line.replace("<tns:attribute", f"<{prefix}attribute", 1)
    return line


def _dict_lines(fields: list[BoField], prefix: str, info: dict) -> tuple[list[str], list[str], list[str]]:
    """新增 dic 字段需要的字典类型行 / 选项行，规则与 gen_bo._dict_blocks 一致。"""
    type_lines: list[str] = []
    item_lines: list[str] = []
    warns: list[str] = []
    type_sn = info["typeSn"]
    item_sn = dict(info["itemSn"])
    existing_types = set(info["typeKeys"])
    tag_t = f"{prefix}dictionaryType"
    tag_d = f"{prefix}dictionary"
    tag_a = f"{prefix}attrs"

    for f in fields:
        if f.type_new != "dic" or not f.choices:
            continue
        key = f.dict_key()
        if key in existing_types:
            warns.append(f"字典 {key} 原包里已存在，只追加选项，不重建字典类型")
        else:
            type_sn += 1
            existing_types.add(key)
            type_lines.append(
                f'<{tag_t} typeKey="{esc_attr(key)}" name="{esc_attr(f.name + "选项")}" '
                f'struType="0" sn="{type_sn}" typeScope="BO_ATTR"/>')
        used: set[str] = set()
        base = item_sn.get(key, 0)
        for i, label in enumerate(f.choices, start=1):
            code = gen_bo._choice_code(label, i, used, scope=key)
            sn = base + i
            item_lines.append(
                f'<{tag_d} typeKey="{esc_attr(key)}" key="{esc_attr(code)}" '
                f'name="{esc_attr(label)}" sn="{sn}" disabled="false">'
                f'<{tag_a}>{cdata(_DIC_STYLE)}</{tag_a}></{tag_d}>')
        item_sn[key] = base + len(f.choices)
    return type_lines, item_lines, warns


def _append_to_container(text: str, tag: str, lines: list[str], sep: str,
                         indent: str, root_close: str) -> str:
    """往 <tag>…</tag> / <tag/> 里追加行；容器不存在就在根节点闭合前建一个。"""
    if not lines:
        return text
    body = sep.join(indent + ln.strip() for ln in lines)

    self_pat = re.compile(r"<" + re.escape(tag) + r"\b[^>]*/>")
    m = self_pat.search(text)
    if m:
        head = m.group(0)[:-2].rstrip() + ">"
        block = head + sep + body + sep + f"</{tag}>"
        return text[:m.start()] + block + text[m.end():]

    close = f"</{tag}>"
    idx = text.find(close)
    if idx >= 0:
        # sep 为空（原生单行包）时就是纯拼接，原 XML 的其余字节完全不动
        return text[:idx] + sep + body + sep + text[idx:]

    pos = text.rfind(root_close)
    if pos < 0:
        raise ValueError(f"XML 里找不到根节点闭合标记 {root_close}")
    block = f"<{tag}>" + sep + body + sep + f"</{tag}>"
    return text[:pos] + (sep if sep else "") + block + text[pos:]


def _sn_allocator(info: dict):
    """从 max(最大 sn, 字段数)+1 起，跳过包里已经用过的 sn。"""
    used = set(info["usedSn"])
    cur = max(info["maxSn"], len(info["fields"]))

    def nxt() -> int:
        nonlocal cur
        cur += 1
        while cur in used:
            cur += 1
        used.add(cur)
        return cur

    return nxt


def _patch_bo_xml(text: str, info: dict, raw_fields: list[dict], mode: str) -> tuple[str, dict]:
    """在一份 bo XML 上追加/替换字段，返回 (新文本, 变更明细)。"""
    prefix = _xml_prefix(text, "bodefentity")
    tag_ent = f"{prefix}bodefentity"
    tag_attr = f"{prefix}attribute"

    m = re.search(r"<" + re.escape(tag_ent) + r"\b[^>]*>", text)
    if not m:
        raise ValueError("找不到 bodefentity 开标签")
    close = text.find(f"</{tag_ent}>", m.end())
    if close < 0:
        raise ValueError("找不到 bodefentity 闭合标签")

    spans = _attr_spans(text, tag_attr, m.end(), close)
    used_cols = {a.get("fieldName", "") for _, _, a in spans if a.get("fieldName")}
    sep = "\n" if "\n" in text[m.end():close] else ""
    indent = "  " if sep else ""

    existing = {f["code"] for f in info["fields"]}
    max_sn = info["maxSn"]
    next_sn = _sn_allocator(info)
    seen: set[str] = set()
    added: list[dict] = []
    replaced: list[dict] = []
    skipped: list[dict] = []
    warns: list[str] = []
    new_fields: list[BoField] = []
    replacements: dict[tuple[int, int], str] = {}

    for raw in raw_fields:
        # sn 先占位，真正的值在下面按「新增 or 替换」分别定
        f = normalize_field(raw, info["code"], 0)
        if f.code in seen:
            raise ValueError(f"新增字段里 {f.code!r} 重复了")
        seen.add(f.code)
        if f.type_new == "dic" and not f.choices:
            warns.append(f"{f.code}: 选项类型没填候选值，导入后下拉会是空的")
        if f.type_new == "object" and not f.object_type:
            warns.append(f"{f.code}: 对象类型没填关联对象，导入后可能取不到数据")

        if f.code in existing:
            if mode == "replace":
                s, e, old = next(x for x in spans if x[2].get("code") == f.code)
                f.sn = int(old.get("sn", max_sn + 1)) if str(old.get("sn", "")).isdigit() else max_sn + 1
                # 替换时保留原物理列名，避免线上表被要求改列
                if old.get("fieldName") and not f.field_name:
                    f.field_name = old["fieldName"]
                replacements[(s, e)] = _attr_line_for(f, prefix)
                replaced.append({"code": f.code, "name": f.name, "sn": f.sn})
            else:
                skipped.append({"code": f.code, "name": f.name,
                                "reason": "包里已有同名字段"})
            continue

        col = f.resolved_field_name()
        if col in used_cols:
            raise ValueError(
                f"字段 {f.code} 的物理列名 {col} 与包里已有字段冲突，请改「物理列名」")
        used_cols.add(col)
        f.sn = next_sn()
        line = _attr_line_for(f, prefix)
        new_fields.append(f)
        added.append({"code": f.code, "name": f.name, "type": f.type_new, "sn": f.sn, "line": line})

    # 先做替换（从后往前，避免位移），再在最后一个 attribute 之后追加。
    # 替换时只换 <.../> 本身，原有缩进留在 out[:s] 里，不要重复补缩进。
    out = text
    for (s, e), line in sorted(replacements.items(), reverse=True):
        out = out[:s] + line + out[e:]

    # 替换后长度变了，重新定位实体区间再算插入点
    m2 = re.search(r"<" + re.escape(tag_ent) + r"\b[^>]*>", out)
    close2 = out.find(f"</{tag_ent}>", m2.end()) if m2 else -1
    if m2 is None or close2 < 0:
        raise ValueError("替换后 XML 结构异常")
    spans2 = _attr_spans(out, tag_attr, m2.end(), close2)
    insert_at = spans2[-1][1] if spans2 else m2.end()
    if added:
        if sep:
            # 原包是多行：最后一个 attribute 后面本来就有换行 + 缩进闭合标签，
            # 这里补「换行 + 新行」，原有换行继续留给闭合标签，不会多出空行。
            add = "\n" + "\n".join(indent + a["line"].strip() for a in added)
        else:
            # 系统推送包是单行紧凑格式：直接拼，不加任何空白
            add = "".join(a["line"] for a in added)
        out = out[:insert_at] + add + out[insert_at:]

    # 字典（选项值）
    type_lines, item_lines, dwarns = _dict_lines(new_fields, prefix, info)
    warns.extend(dwarns)
    root_close = f"</{_xml_prefix(text, 'bodefs')}bodefs>"
    if type_lines:
        out = _append_to_container(out, f"{prefix}dictionaryTypeList", type_lines, sep, indent, root_close)
    if item_lines:
        out = _append_to_container(out, f"{prefix}dictionaryList", item_lines, sep, indent, root_close)

    # 自检：补丁后的 XML 必须仍能解析，且 attribute 数量符合预期
    try:
        check = ET.fromstring(out)
        ent2 = next(c for c in check if _local(c.tag) == "bodefentity")
        n_after = sum(1 for c in ent2 if _local(c.tag) == "attribute")
    except (ET.ParseError, StopIteration) as exc:
        raise ValueError(f"补丁后的 XML 无法解析，已中止：{exc}") from exc
    expect = len(spans) + len(added)
    if n_after != expect:
        raise ValueError(f"补丁后字段数不对（{n_after} != {expect}），已中止")

    return out, {
        "added": [{k: v for k, v in a.items() if k != "line"} for a in added],
        "replaced": replaced,
        "skipped": skipped,
        "warnings": warns,
        "dictTypes": len(type_lines),
        "dictItems": len(item_lines),
        "fieldCountBefore": len(spans),
        "fieldCountAfter": n_after,
    }


# ---------------------------------------------------------------- 出包


def _resolve_targets(data: bytes, spec: dict) -> dict[str, dict]:
    """把 targets 展开成 文件 -> 对象信息；all_copies 时带上同 code 的所有副本。"""
    snap = inspect_package(data)
    by_file = {o["file"]: o for o in snap["objects"]}
    targets = [str(x) for x in (spec.get("targets") or [])]
    if not targets:
        raise ValueError("请先选择要加字段的业务对象")
    missing = [t for t in targets if t not in by_file]
    if missing:
        raise ValueError("包里找不到这些 XML：" + "、".join(missing))
    chosen: dict[str, dict] = {t: by_file[t] for t in targets}
    if _truthy(spec.get("allCopies")):
        codes = {o["code"] for o in chosen.values()}
        for o in snap["objects"]:
            if o["code"] in codes:
                chosen[o["file"]] = o
    return chosen


def _fields_for(spec: dict, file: str, info: dict) -> list[dict]:
    by_target = spec.get("byTarget") or {}
    if isinstance(by_target, dict) and file in by_target:
        return list(by_target[file] or [])
    return list(spec.get("fields") or [])


def _report_text(spec: dict, changed: dict, kept: int, dropped: int) -> str:
    lines = [
        "PP6 加字段报告",
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"原始条目 {kept + dropped} 个：保留 {kept}，按 bo-only 丢弃 {dropped}",
        f"范围: {spec.get('scope') or 'full'}   重名处理: {spec.get('mode') or 'skip'}",
        "",
        "【改动的文件】",
    ]
    for file, d in changed.items():
        lines.append(f"  {file}  字段 {d['fieldCountBefore']} → {d['fieldCountAfter']}")
        for a in d["added"]:
            lines.append(f"      + {a['code']}（{a['name']}）  {a['type']}  sn={a['sn']}")
        for a in d["replaced"]:
            lines.append(f"      ~ {a['code']}（{a['name']}）  按新定义覆盖  sn={a['sn']}")
        for a in d["skipped"]:
            lines.append(f"      = {a['code']}  已存在，跳过")
        if d["dictTypes"] or d["dictItems"]:
            lines.append(f"      字典类型 +{d['dictTypes']}，选项 +{d['dictItems']}")
    warns = [w for d in changed.values() for w in d["warnings"]]
    if warns:
        lines += ["", "提示:"] + [f"  - {w}" for w in dict.fromkeys(warns)]
    lines += [
        "",
        "未改动的条目均为原包字节原样复制（含 ZipInfo 元数据）。",
        "新字段只进业务对象；表单/列表控件请用「导出带新字段的 Excel」走主流程重新出包。",
    ]
    return "\n".join(lines)


def build_patch(data: bytes, spec: dict) -> tuple[bytes, str, dict]:
    """在原 zip 上打补丁，返回 (zip 字节, 下载文件名, 报告)。"""
    spec = spec or {}
    mode = str(spec.get("mode") or "skip").lower()
    if mode not in ("skip", "replace"):
        raise ValueError("mode 只能是 skip 或 replace")
    scope = str(spec.get("scope") or "full").lower()
    if scope not in ("full", "bo-only"):
        raise ValueError("scope 只能是 full 或 bo-only")
    chosen = _resolve_targets(data, spec)
    if not any(_fields_for(spec, f, o) for f, o in chosen.items()):
        raise ValueError("还没有填写要新增的字段")

    patched: dict[str, bytes] = {}
    changed: dict[str, dict] = {}
    for file, info in chosen.items():
        raws = _fields_for(spec, file, info)
        if not raws:
            continue
        with zipfile.ZipFile(io.BytesIO(data)) as zin:
            text = zin.read(file).decode("utf-8")
        parsed = _parse_bo_xml(text)
        if parsed is None:
            raise ValueError(f"{file}: 不是可解析的业务对象 XML")
        new_text, detail = _patch_bo_xml(text, parsed, raws, mode)
        patched[file] = new_text.encode("utf-8")
        changed[file] = detail

    kept = dropped = 0
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, \
            zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            is_bo_xml = (item.filename.startswith("bo/") and
                         item.filename.endswith(".xml"))
            if scope == "bo-only" and not is_bo_xml:
                dropped += 1
                continue
            # 复用 ZipInfo 能保住时间戳、压缩方式、Unix 权限位；但 zipfile 的
            # 写入路径会把 external_attr=0 改成默认 0o600<<16，写完要还原。
            # 用副本，别把只读的原 ZipInfo 就地改脏。
            info = copy(item)
            original_attr = item.external_attr
            zout.writestr(info, patched.get(item.filename) or zin.read(item.filename))
            info.external_attr = original_attr
            kept += 1
        report = _report_text(spec, changed, kept, dropped)
        # zip comment 上限 64KB，超了按字节截断（末尾半个汉字无害，读时 replace）
        zout.comment = ("CONFIG_SYNC_NOTE:" + report).encode("utf-8")[:65000]

    # 同一个对象的多份副本（重复推送）算一个对象，别写成「风险等6个对象」
    names = list(dict.fromkeys(o["name"] or o["code"] for o in chosen.values()))
    labels = names[0] if len(names) == 1 else f"{names[0]}等{len(names)}个对象"
    stamp = datetime.now().strftime("%Y%m%d")
    name = package_name(labels or "业务对象", "加字段", stamp)
    payload = {
        "ok": True,
        "changed": changed,
        "changedFiles": sorted(changed),
        "keptEntries": kept,
        "droppedEntries": dropped,
        "report": report,
        "scope": scope,
        "mode": mode,
    }
    return buf.getvalue(), name, payload


def dry_run(data: bytes, spec: dict) -> dict:
    """只算报告不下载：直接复用出包逻辑，保证预览与实际产物一字不差。

    故意不写第二套「预判」算法——以前吃过亏：预览说加 3 个、
    出包时因为物理列名冲突只加了 2 个，两边口径一漂移就没人信预览了。
    """
    _blob, _name, payload = build_patch(data, spec)
    chosen = _resolve_targets(data, spec)
    payload["targets"] = {
        f: {"object": f"{o['name']}（{o['code']}）", **payload["changed"].get(f, {})}
        for f, o in chosen.items()
    }
    return payload


# ---------------------------------------------------------------- 配套 Excel


def _project_with_fields(data: bytes, spec: dict):
    """反解原包 → 把新字段并进 Project（供 build_workbook 用）。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "source.zip"
        src.write_bytes(data)
        proj, notes = inspect_path(src)
    chosen = _resolve_targets(data, spec)
    mode = str(spec.get("mode") or "skip").lower()
    touched: set[str] = set()
    for file, info in chosen.items():
        bo = proj.bo(info["code"])
        if bo is None:
            notes.append(f"{file}: 反解结果里找不到对象 {info['code']}，Excel 中未加入该字段")
            continue
        raws = _fields_for(spec, file, info)
        used_sn = {f.sn for f in bo.fields}
        max_sn = max((f.sn for f in bo.fields), default=0)

        def alloc_sn() -> int:
            nonlocal max_sn
            max_sn = max(max_sn, len(bo.fields)) + 1
            while max_sn in used_sn:
                max_sn += 1
            used_sn.add(max_sn)
            return max_sn

        for raw in raws:
            f = normalize_field(raw, bo.code, 0)
            hit = bo.field_by_code(f.code)
            if hit is not None:
                if mode != "replace":
                    continue
                f.sn = hit.sn
                f.field_name = f.field_name or hit.field_name
                bo.fields[bo.fields.index(hit)] = f
            else:
                f.sn = alloc_sn()
                bo.fields.append(f)
            touched.add(bo.code)
            if _truthy(spec.get("syncForm")):
                for form in proj.forms:
                    if form.bo_code != bo.code or any(x.name == f.code for x in form.fields):
                        continue
                    form.fields.append(FormField(
                        form_key=form.key,
                        name=f.code,
                        label=f.name,
                        field_type=control_for(f),
                        value_source=(f.dict_key() if f.type_new == "dic"
                                      else (f.object_type if f.type_new == "object" else "")),
                        full_row=f.type_new == "clob",
                    ))
    return proj, notes, touched


def patched_workbook(data: bytes, spec: dict) -> tuple[bytes, str]:
    """原包反解 + 新字段 → 一份完整规格 Excel（可继续走主流程重新出包）。"""
    proj, notes, touched = _project_with_fields(data, spec)
    wb = build_workbook(proj)
    buf = io.BytesIO()
    wb.save(buf)
    label = "、".join(sorted(touched)) or "业务对象"
    stamp = datetime.now().strftime("%Y%m%d")
    return buf.getvalue(), package_name(label, "加字段-规格表", stamp).replace(".zip", ".xlsx")


def fields_template_bytes() -> bytes:
    """增量字段表模板：一个页签、表头 + 说明 + 一行示例。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "新增字段"
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="2563EB")
    for col, (label, _key, width, note) in enumerate(FIELD_COLUMNS, 1):
        cell = ws.cell(row=1, column=col, value=label)
        cell.font = head
        cell.fill = fill
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[cell.column_letter].width = width
        ws.cell(row=2, column=col, value=note).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[2].height = 34
    sample = ["riskLevel", "风险等级", "选项", "高,中,低", "", "", "", "", "否", "否", "", "示例行，填完请删掉"]
    for col, value in enumerate(sample, 1):
        ws.cell(row=3, column=col, value=value)
    ws.freeze_panes = "A3"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def fields_from_excel(data: bytes) -> dict:
    """读增量字段 Excel（模板页签「新增字段」，也认主模板的「2_字段」）。"""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise ValueError(f"Excel 打不开：{exc}") from exc

    ws = wb["新增字段"] if "新增字段" in wb.sheetnames else (
        wb["2_字段"] if "2_字段" in wb.sheetnames else None)
    if ws is None:
        for cand in wb.worksheets:
            heads = [str(c.value or "").strip() for c in cand[1]]
            if "字段编码" in heads:
                ws = cand
                break
    if ws is None:
        raise ValueError("找不到「新增字段」或「2_字段」页签（表头需含「字段编码」）")

    heads = [str(c.value or "").strip() for c in ws[1]]
    keys = [_HEADER_MAP.get(h, "") for h in heads]
    if "code" not in keys:
        raise ValueError("表头里没有「字段编码」这一列")

    fields: list[dict] = []
    skipped = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not any(v not in (None, "") for v in row):
            continue
        raw = {}
        for key, value in zip(keys, row):
            if key and value not in (None, ""):
                raw[key] = value
        code = str(raw.get("code") or "").strip()
        # 模板第 2 行是说明文字，按「没有字段编码」自然跳过
        if not code:
            skipped += 1
            continue
        if code.startswith("字段编码"):
            continue
        # 模板第 2 行是每列的说明文字（中文长句），不是字段；
        # 真正填错的编码（如 1abc）仍交给 normalize_field 明确报错。
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", code) and (
                len(code) > 12 or re.search(r"[\u4e00-\u9fff\s，。；]", code)):
            skipped += 1
            continue
        raw["code"] = code
        raw["bo_code"] = str(raw.get("bo_code") or "")
        fields.append(field_payload(normalize_field(raw, raw["bo_code"], 0)))
    return {"ok": True, "fields": fields, "skippedRows": skipped}
