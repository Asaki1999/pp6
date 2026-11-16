"""patch：在一份**已有**同步包 zip 上做最小差异补丁（加字段 / 改已有字段）。

两条腿
------
1. **加字段**：编码在包里没有 → 追加 ``<attribute/>``，必要时补字典。
2. **改已有字段**：编码和包里已有的一样 + ``mode="replace"`` → 只改这个字段：
   类型、选项值、名称、必填…；物理列名 ``fieldName`` 永远不动。

用户真正在意的场景是「导入前把包改对」——导进去表结构就落库了，线上再改
字段类型只是改元数据、不会 ALTER，所以改包必须发生在导入之前。

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
from copy import copy, deepcopy
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from . import fieldtypes as _ft
from . import gen_bo
from .blueprint import control_for
from .builder import package_name
from .inspect import inspect_path
from .model import TABLE_COLUMN_TYPES, BoField, FormField, TableColumn
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
        # 表格字段（子表）：新增字段时前端可以带 columns，走和 Excel 主流程
        # 完全相同的 TableColumn → options.columns 生成路径。
        table_columns=_normalize_columns(raw.get("columns")),
        sn=sn,
    )


def _normalize_columns(raw) -> list[TableColumn]:
    """前端/Excel 传来的表格列 → TableColumn。格式与 ai_bo._clean_columns 一致。"""
    if not isinstance(raw, list):
        return []
    out: list[TableColumn] = []
    for i, item in enumerate(raw, start=1):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        typ = _ft.resolve_column_type(item.get("type") or "varchar")
        if typ not in TABLE_COLUMN_TYPES:
            typ = "varchar"
        out.append(TableColumn(
            name=name,
            input_type=typ,
            choices=[str(x).strip() for x in (item.get("choices") or item.get("values") or [])
                     if str(x).strip()],
            precision=item.get("precision") if isinstance(item.get("precision"), int) else None,
            width_percent=(float(item["widthPercent"])
                           if isinstance(item.get("widthPercent"), (int, float)) else None),
            required=bool(item.get("required")),
            multiple=bool(item.get("multiple")),
            sn=i,
        ))
    return out


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
        "columns": [
            {"name": c.name, "type": c.input_type, "choices": list(c.choices or []),
             "precision": c.precision, "widthPercent": c.width_percent,
             "required": bool(c.required), "multiple": bool(c.multiple)}
            for c in (field.table_columns or [])
        ],
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


def _unesc(value: str) -> str:
    """把 XML 属性值还原成原字符，供重新生成时再转义一次。"""
    return (value.replace("&quot;", '"').replace("&apos;", "'")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


def _attr_spans(text: str, tag: str, start: int, end: int) -> list[tuple[int, int, dict]]:
    """entity 区间内所有 <attribute/> 的 (起, 止, 属性)。"""
    pat = re.compile(r"<" + re.escape(tag) + r"\b")
    out = []
    for m in pat.finditer(text, start, end):
        stop = _self_closing_end(text, m.start())
        if stop < 0 or stop > end:
            continue
        frag = text[m.start():stop]
        attribs = {k: _unesc(v)
                   for k, v in re.findall(r'([\w:.-]+)="([^"]*)"', frag)}
        out.append((m.start(), stop, attribs))
    return out


def _attr_line_for(field: BoField, prefix: str) -> str:
    line = gen_bo._attr_line(field).strip()
    if prefix != "tns:":
        line = line.replace("<tns:attribute", f"<{prefix}attribute", 1)
    return line


def _merge_attr_line(old: dict, raw: dict, field: BoField, prefix: str,
                     align: bool = False) -> tuple[str, list[str], list[str]]:
    """改写一个**已有** attribute：新定义为准，但原有属性一个都不许丢。

    为什么不像新增字段那样直接 ``_attr_line_for``
    ---------------------------------------------
    老实现整行重生成，新定义里没填的键就跟着没了。实测（2026-11-12，项目.zip
    的 name 字段 varchar→user）：
        length  200 → 64        用户没让改长度
        format  yyyy-MM-dd      改完 varchar 还留着日期格式
    原包里还有我们没建模的属性（roleList / formula / cascade…），整行重生成
    等于拿未知换已知。所以这里以**原属性为基底**覆盖，只有三类例外：
        fieldName  物理列名，永不改（改了等于要求线上 ALTER 列名）
        sn         行序号，保持原位
        type       角色列（base / role…），不由字段类型决定

    ``align=True``（项目字段对齐）时放开「定义类」键：类型/长度/精度/必填/
    索引/默认值/options/objectType/name 一律取新值（含清空）。这是架构 F-E
    指出的必要开关——默认逻辑只在用户显式给了或物理类型变了时才覆盖，
    对齐要的是「跟项目侧完全一致」，包括把 isindex 从 N 改成 Y。

    返回 (新行文本, 改动的键, 原来的类型)。
    """
    p = field.resolved()
    old_type = str(old.get("dataTypeNew") or "")
    if align:
        merged: dict[str, object] = dict(old)
        for key in ("code", "fieldName", "sn", "type"):
            merged.pop(key, None)          # 下面单独塞回去
        merged["code"] = old.get("code") or field.code
        merged["fieldName"] = old.get("fieldName") or p["fieldName"]
        merged["sn"] = old.get("sn") or p["sn"]
        if "type" in old:
            merged["type"] = old["type"]
        merged["dataTypeNew"] = p["dataTypeNew"]
        if p.get("datatype"):
            merged["datatype"] = p["datatype"]
        if str(field.name or "").strip():
            merged["name"] = field.name
        for key in ("isnull", "isindex", "defaultvalue", "format",
                    "length", "precision", "objectType"):
            val = p.get(key)
            if val in (None, ""):
                merged.pop(key, None)
            else:
                merged[key] = val
        opts = p.get("options")
        if opts:
            merged["options"] = gen_bo.json_attr(opts)
        else:
            merged.pop("options", None)
        order = [k for k in old if k in merged]
        order += [k for k in merged if k not in old]
        astr = "".join(f' {k}="{esc_attr(merged[k])}"' for k in order)
        line = f"<{prefix}attribute{astr}/>"
        changed = [k for k in order if str(merged.get(k, "")) != str(old.get(k, ""))]
        return line, changed, old_type

    # 物理类型没变（varchar 族互改：字符串↔用户↔部门↔选项…）就不动物理列参数，
    # 这和线上「7 类可互改、不 ALTER」的行为对齐
    phys_changed = str(p.get("datatype") or "") != str(old.get("datatype") or "")
    given = {k for k, v in raw.items() if v is not None and str(v).strip() != ""}

    merged: dict[str, object] = dict(old)
    if phys_changed:
        # 换了物理类型，原类型的专属参数不能带过去
        for key in ("format", "length", "precision", "defaultvalue", "options", "objectType"):
            merged.pop(key, None)
    for key in ("code", "fieldName", "sn", "type"):
        merged.pop(key, None)          # 下面单独塞回去

    merged["code"] = old.get("code") or field.code
    merged["fieldName"] = old.get("fieldName") or p["fieldName"]
    merged["sn"] = old.get("sn") or p["sn"]
    if "type" in old:
        merged["type"] = old["type"]        # 可能本来就是空串，别当成「没有」
    merged["dataTypeNew"] = p["dataTypeNew"]
    if p.get("datatype"):
        merged["datatype"] = p["datatype"]

    # 名称/说明：只有用户真填了才动，避免前端「只改类型」时用 code 顶掉原名
    if "name" in given or "desc" in given:
        if str(raw.get("name") or "").strip():
            merged["name"] = field.name
        if str(raw.get("desc") or "").strip():
            merged["desc"] = field.desc

    # 必填/索引/默认值：前端表格永远会带 required=false / index=false，
    # 所以只在**勾上**时才认作显式声明（勾上=设为必填/索引；不勾=不动原值），
    # 否则「只改类型」会顺手把原字段的必填和索引清掉
    for key in ("isnull", "isindex", "defaultvalue"):
        if key in given or key not in merged:
            val = p.get(key)
            if val in (None, ""):
                merged.pop(key, None)
            else:
                merged[key] = val
    if _truthy(raw.get("required")):
        merged["isnull"] = "N"
    elif raw.get("isnull") is not None and str(raw.get("isnull")).strip():
        merged["isnull"] = "Y" if _truthy(raw.get("isnull")) else "N"
    if _truthy(raw.get("index")):
        merged["isindex"] = "Y"
    # 物理参数（长度/精度/格式）只在用户给了或物理类型变了时写；
    # 同族互改时原包里没有就别硬补 —— 原生 dic 字段本来就不带 format
    for key in ("format", "length", "precision"):
        if key in given or phys_changed:
            val = p.get(key)
            if val in (None, ""):
                merged.pop(key, None)
            else:
                merged[key] = val
    # options：只有显式给了、或物理类型变了才重建
    if "options" in given or phys_changed or "options" not in merged:
        opts = p.get("options")
        if opts:
            merged["options"] = gen_bo.json_attr(opts)
        elif "options" in given:
            merged.pop("options", None)
    # 关联对象/字典 key：dic 必须写成「对象编码_字段编码」，其余类型不留残留
    if field.type_new in ("dic", "object") and p.get("objectType"):
        merged["objectType"] = p["objectType"]
    elif field.type_new not in ("dic", "object") and phys_changed:
        merged.pop("objectType", None)

    for key in ("options", "format", "objectType", "length", "precision", "defaultvalue"):
        if merged.get(key) in (None, ""):
            merged.pop(key, None)

    order = [k for k in old if k in merged]
    order += [k for k in merged if k not in old]
    astr = "".join(f' {k}="{esc_attr(merged[k])}"' for k in order)
    line = f"<{prefix}attribute{astr}/>"
    changed = [k for k in order if str(merged.get(k, "")) != str(old.get(k, ""))]
    return line, changed, old_type


def _rewrite_dict_items(text: str, prefix: str, items: list[tuple[str, str, list[str]]],
                       root_close: str) -> tuple[str, int]:
    """重写已有字典的候选值：同名的复用原 key，新增的生成新短码。

    为什么要「复用 key」
    -------------------
    字典项的 key 是库里已经存着的值。用户只是加一个选项、改个显示名，
    如果把整组 key 重新生成，线上已有记录的取值就全部对不上了。
    所以按选项**名称**去匹配旧条目，命中就沿用旧 key。

    老实现只给「新增字段」补字典，已有 dic 字段改选项值时 dictionaryList
    一个字节都没动 —— 包变了、选项值还是老的（2026-11-12 实测选项.zip）。
    """
    if not items:
        return text, 0
    tag = f"{prefix}dictionary"
    tag_a = f"{prefix}attrs"
    done = 0
    for key, _dic_name, choices in items:
        pat = re.compile(
            r"<" + re.escape(tag) + r'\b[^>]*\btypeKey="' + re.escape(key)
            + r'"[^>]*(?:/>|>.*?</' + re.escape(tag) + r">)", re.S)
        old_key: dict[str, str] = {}
        for frag in pat.findall(text):
            a = dict(re.findall(r'([\w:.-]+)="([^"]*)"', frag))
            if a.get("name") and a.get("key"):
                old_key[a["name"]] = a["key"]
        text = pat.sub("", text)
        used = set(old_key.values())
        lines = []
        for i, label in enumerate(choices, start=1):
            code = old_key.get(label) or gen_bo._choice_code(label, i, used, scope=key)
            used.add(code)
            lines.append(
                f'<{tag} typeKey="{esc_attr(key)}" key="{esc_attr(code)}" '
                f'name="{esc_attr(label)}" sn="{i}" disabled="false">'
                f'<{tag_a}>{cdata(_DIC_STYLE)}</{tag_a}></{tag}>')
        sep = "\n" if "\n" in text else ""
        text = _append_to_container(text, f"{prefix}dictionaryList", lines, sep,
                                    "  " if sep else "", root_close)
        done += len(lines)
    return text, done


def _rewrite_global_items(text: str, prefix: str, ref: str,
                          items: list[dict], root_close: str) -> tuple[str, int]:
    """重写**全局字典**（typeScope=GLOBAL）的候选值：同 name 复用原 key。

    为什么单开一个函数而不复用 _rewrite_dict_items（架构 D8）
    --------------------------------------------------------
    `tools/check_patch.py` 的 61 条保真断言是硬门槛，改老函数等于动红线。
    而全局字典的语义本来就不同：
      · key 是**项目侧固定业务码**（MonthlyReportModules…），不能按 md5 重算，
        重算出来的码系统里没有 → 导入后空下拉；
      · 包里已有同名候选值时要沿用**库里原来的 key**（否则线上已存数据对不上）。
    """
    if not items:
        return text, 0
    tag = f"{prefix}dictionary"
    pat = re.compile(
        r"<" + re.escape(tag) + r'\b[^>]*\btypeKey="' + re.escape(ref)
        + r'"[^>]*(?:/>|>.*?</' + re.escape(tag) + r">)", re.S)
    old_key: dict[str, str] = {}
    for frag in pat.findall(text):
        a = dict(re.findall(r'([\w:.-]+)="([^"]*)"', frag))
        if a.get("name") and a.get("key"):
            old_key[a["name"]] = a["key"]
    text = pat.sub("", text)
    lines = []
    for i, item in enumerate(items, start=1):
        name = str(item.get("name") or "")
        code = old_key.get(name) or str(item.get("key") or "")
        if not code:
            continue
        sn = item.get("sn") or i
        # 原生 GLOBAL 字典项**不带** attrs 子元素（项目.zip 逐字节核对）
        lines.append(f'<{tag} typeKey="{esc_attr(ref)}" key="{esc_attr(code)}" '
                     f'name="{esc_attr(name)}" sn="{int(sn)}" disabled="false"/>')
    sep = "\n" if "\n" in text else ""
    text = _append_to_container(text, f"{prefix}dictionaryList", lines, sep,
                                "  " if sep else "", root_close)
    return text, len(lines)


def _global_dict_lines(text: str, prefix: str, metas: list[tuple[str, str, str, list[dict]]],
                       info: dict, root_close: str, sep: str,
                       indent: str) -> tuple[str, int, int]:
    """GLOBAL 字典：同 typeKey 不重建类型，只重写/追加候选值。

    metas 每项 = (typeKey, name, struType, items)。返回值 (新文本, 新增类型行数,
    候选值行数)。
    """
    types: list[str] = []
    type_sn = info.get("typeSn", 0)
    seen: set[str] = set()
    item_total = 0
    for ref, name, stru, items in metas:
        if ref in seen:
            continue
        seen.add(ref)
        # 包内已有该 typeKey 的类型行就不重建（重复导入同一个字典会让系统里
        # 出现两套字典，候选值互相盖）
        if not re.search(r'<[\w:]*dictionaryType\b[^>]*\btypeKey="' + re.escape(ref) + r'"', text):
            type_sn += 1
            types.append(f'<{prefix}dictionaryType typeKey="{esc_attr(ref)}" '
                         f'name="{esc_attr(name or ref)}" struType="{esc_attr(stru or "1")}" '
                         f'sn="{type_sn}" typeScope="GLOBAL"/>')
        text, n = _rewrite_global_items(text, prefix, ref, items, root_close)
        item_total += n
    if types:
        text = _append_to_container(text, f"{prefix}dictionaryTypeList", types, sep,
                                    indent, root_close)
    return text, len(types), item_total


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
        if f.dict_ref:
            # 防御：引用全局字典的字段由 _global_dict_lines 负责（typeScope=GLOBAL
            # + 项目侧固定候选码）。走到这里说明调用方漏收，绝不能按自定义字典
            # 再造一个 `对象_字段` 的 key —— 那样导入端会报「引用的字典不存在」。
            warns.append(f"{f.code}: 引用全局字典 {f.dict_ref}，字典块交给 GLOBAL 分支处理")
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


def _patch_bo_xml(text: str, info: dict, raw_fields: list[dict], mode: str,
                  align_fields: dict[str, BoField] | None = None) -> tuple[str, dict]:
    """在一份 bo XML 上追加/替换字段，返回 (新文本, 变更明细)。

    align_fields：项目对齐产生的「覆盖定义」行（code → 已算好的 BoField）。
    这些行**跳过 normalize_field**（字典块不是前端能表达的），并在
    _merge_attr_line 里走 align=True 强制覆盖定义类键。
    """
    align_fields = align_fields or {}
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
    dict_rewrites: list[tuple[str, str, list[str]]] = []
    global_rewrites: list[tuple[str, str, str, list[dict]]] = []
    warns: list[str] = []
    new_fields: list[BoField] = []
    replacements: dict[tuple[int, int], str] = {}

    for raw in raw_fields:
        # sn 先占位，真正的值在下面按「新增 or 替换」分别定
        is_align = bool(raw.get("align"))
        if is_align and raw.get("code") in align_fields:
            f = deepcopy(align_fields[raw["code"]])
        else:
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
                line, keys, old_type = _merge_attr_line(old, raw, f, prefix, align=is_align)
                replacements[(s, e)] = line
                replaced.append({"code": f.code, "name": old.get("name") or f.name, "sn": f.sn,
                                 "from": old_type, "to": f.type_new, "attrs": keys,
                                 "aligned": is_align})
                # 选项值改了就重写这个字段的字典项，否则包变了、选项还是老的
                if f.type_new == "dic" and (f.choices or f.dict_items):
                    if f.dict_ref:
                        # GLOBAL：不造新 key，只声明/复用项目侧 typeKey 与固定候选码
                        global_rewrites.append(
                            (f.dict_ref, f.dict_name, f.dict_stru or "1",
                             [dict(i) for i in (f.dict_items or [])]))
                    else:
                        dict_rewrites.append((f.dict_key(), f.name, list(f.choices)))
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
        # 新增字段也可能是「引用项目侧全局字典」的（对齐后新加的汇报类型这种）：
        # 它的 typeKey 必须声明成 GLOBAL + 固定候选码，否则 _dict_lines 会按
        # 自定义字典造一个 `对象_字段` 的 key，导入端报「引用的字典不存在」。
        if f.type_new == "dic" and f.dict_ref:
            global_rewrites.append(
                (f.dict_ref, f.dict_name, f.dict_stru or "1",
                 [dict(i) for i in (f.dict_items or [])]))

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
    # 已有 dic 字段的选项值：整组替换（同名复用 key，保住库里已存的值）
    out, rewritten = _rewrite_dict_items(out, prefix, dict_rewrites, root_close)
    # 对齐到项目侧全局字典：声明（或复用）typeScope="GLOBAL" 的 typeKey，
    # 候选值用项目侧固定业务码，不造 `对象_字段` 新 key
    g_types = g_items = 0
    if global_rewrites:
        out, g_types, g_items = _global_dict_lines(
            out, prefix, global_rewrites, info, root_close, sep, indent)
        rewritten += g_items

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
        "globalDictTypes": g_types,
        "globalDictItems": g_items,
        "dictRewritten": rewritten,
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


def _align_field(raw: dict, bo_code: str) -> BoField:
    """把 projlink 的「覆盖定义」行翻成 BoField（含 GLOBAL 字典块）。"""
    f = normalize_field(raw, bo_code, 0)
    meta = raw.get("dict")
    if meta is not None:
        f.dict_ref = meta.ref
        f.dict_name = meta.name
        f.dict_stru = meta.stru_type
        f.dict_items = [{"key": i.key, "name": i.name, "sn": i.sn} for i in meta.items]
        f.choices = [i.name for i in meta.items]
    return f


def _align_rows(data: bytes, spec: dict, chosen: dict) -> tuple[dict, dict, dict]:
    """算存量包侧的项目对齐 → (对齐行, 链接字段行, 视图/报告数据)。"""
    from . import projlink

    plans = projlink.analyze_package(data, chosen, spec)
    rows: dict[str, list[dict]] = {}
    links: dict[str, list[dict]] = {}
    for file, plan in (plans.get("objects") or {}).items():
        aligns, link_raw = projlink.plan_to_raw_rows(plan)
        rows[file] = aligns
        if link_raw:
            links[file] = [link_raw]
    plans["report"] = projlink.package_report(plans)
    return rows, links, plans


def _report_text(spec: dict, changed: dict, kept: int, dropped: int,
                 align_report: dict | None = None) -> str:
    lines = [
        "PP6 包补丁报告",
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
            span = f"{a['from']}→{a['to']}" if a.get("from") and a.get("from") != a.get("to") else a.get("to") or ""
            # 对齐行与用户手填行分开列（PRD P0-5 要求），用户一眼能看出
            # 「这条是勾了关联项目自动做的」还是「我自己要改的」
            mark = "~ 对齐" if a.get("aligned") else "~"
            lines.append(f"      {mark} {a['code']}（{a['name']}）  "
                         f"{'按新定义覆盖' if not a.get('aligned') else '按项目侧定义覆盖'}  "
                         f"{span}  sn={a['sn']}")
            if a.get("attrs"):
                lines.append(f"          改写属性：{' / '.join(a['attrs'])}")
        for a in d["skipped"]:
            lines.append(f"      = {a['code']}  已存在，跳过")
        if d["dictTypes"] or d["dictItems"]:
            lines.append(f"      字典类型 +{d['dictTypes']}，选项 +{d['dictItems']}")
        if d.get("globalDictTypes") or d.get("globalDictItems"):
            lines.append(f"      全局字典：类型 +{d['globalDictTypes']}，"
                         f"候选值 {d['globalDictItems']} 条（用项目侧固定 key）")
        if d.get("dictRewritten"):
            lines.append(f"      已有字典的选项值重写 {d['dictRewritten']} 条")
    warns = [w for d in changed.values() for w in d["warnings"]]
    if warns:
        lines += ["", "提示:"] + [f"  - {w}" for w in dict.fromkeys(warns)]
    if align_report and not align_report.get("noop", True):
        # 独立一节：勾了「关联项目」才会出现（没勾时报告与以前逐字相同）
        from . import projlink as _projlink
        lines += [""] + _projlink.report_lines(align_report)
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

    # 项目对齐：勾了「关联项目」的对象会额外产生两类 raw 行
    # （覆盖已有字段的定义 + 新增链接字段）。没勾时三步都返回空，
    # 存量包行为与以前**逐字节相同**（回归基线）。
    align_rows, link_rows, align_plans = _align_rows(data, spec, chosen)
    align_report = align_plans.get("report") or {}
    # 模式守卫只针对「对齐命中包里**已存在**的字段」：这种情况必须覆盖，
    # 否则出来的包和原包一样却报告「已对齐」。纯新增字段的对齐行在 skip 模式下
    # 照常允许（它本来就走「新增字段」分支，不涉及覆盖）。
    def _hits_existing(file: str) -> bool:
        have = {str(f.get("code") or "") for f in (chosen[file].get("fields") or [])}
        return any(r.get("code") in have for r in align_rows.get(file, []))

    if mode == "skip" and any(_hits_existing(f) for f in chosen):
        raise ValueError(
            "对齐已有字段需要覆盖模式：请把「重名字段」选成「按新定义覆盖原字段」"
            "（勾了「关联项目」时，对齐行会改写包里已有字段的类型/长度/选项）")

    if not any(_fields_for(spec, f, o) or align_rows.get(f) or link_rows.get(f)
               for f, o in chosen.items()):
        raise ValueError("还没有填写要新增的字段，也没有可对齐的字段")

    patched: dict[str, bytes] = {}
    changed: dict[str, dict] = {}
    untouched: dict[str, dict] = {}
    for file, info in chosen.items():
        existing_codes = {str(f.get("code") or "") for f in (info.get("fields") or [])}
        aligns = list(align_rows.get(file, []))
        align_codes = {r["code"] for r in aligns}
        # ① 新字段如果被对齐命中，就以**对齐行**为准（它带着项目侧的字典块，
        #    用户手填那行只剩类型/名称，用它落地会造出一个假字典）；
        #    ② 包里已存在的字段相反：用户显式填的 code 优先，对齐行让位。
        user_raws = [r for r in _fields_for(spec, file, info)
                     if not (str(r.get("code") or "").strip() in align_codes
                             and str(r.get("code") or "").strip() not in existing_codes)]
        taken = {str(r.get("code") or "").strip() for r in user_raws if str(r.get("code") or "").strip() in existing_codes}
        aligns = [r for r in aligns if r["code"] not in taken]
        align_fields = {r["code"]: _align_field(r, info["code"]) for r in aligns}
        raws = user_raws + aligns + link_rows.get(file, [])
        if not raws:
            continue
        with zipfile.ZipFile(io.BytesIO(data)) as zin:
            text = zin.read(file).decode("utf-8")
        parsed = _parse_bo_xml(text)
        if parsed is None:
            raise ValueError(f"{file}: 不是可解析的业务对象 XML")
        new_text, detail = _patch_bo_xml(text, parsed, raws, mode, align_fields)
        if new_text == text:
            untouched[file] = detail
            continue
        patched[file] = new_text.encode("utf-8")
        changed[file] = detail

    if not changed:
        reason = "、".join(
            f"{d['code']}（包里已有同名字段）"
            for det in untouched.values() for d in det.get("skipped") or []) \
            or "没有产生任何字节变化"
        raise ValueError(
            f"没有产生任何改动：{reason}。"
            "这段字段按新定义改写已有字段时，请把「重名字段」选成「按新定义覆盖原字段」；"
            "只想加字段就换一个没被占用的字段编码。")

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
        report = _report_text(spec, changed, kept, dropped, align_report)
        # zip comment 上限 64KB，超了按字节截断（末尾半个汉字无害，读时 replace）
        zout.comment = ("CONFIG_SYNC_NOTE:" + report).encode("utf-8")[:65000]

    # 同一个对象的多份副本（重复推送）算一个对象，别写成「风险等6个对象」
    names = list(dict.fromkeys(o["name"] or o["code"] for o in chosen.values()))
    labels = names[0] if len(names) == 1 else f"{names[0]}等{len(names)}个对象"
    stamp = datetime.now().strftime("%Y%m%d")
    # 只改已有字段的产物不该叫「加字段」，否则用户分不清这份包里到底做了什么
    verbs = []
    if any(d["added"] for d in changed.values()):
        verbs.append("加字段")
    if any(d["replaced"] for d in changed.values()):
        verbs.append("改字段")
    if not align_report.get("noop", True):
        verbs.append("对齐项目")
    name = package_name(labels or "业务对象", "+".join(verbs) or "补丁", stamp)
    payload = {
        "ok": True,
        "changed": changed,
        "changedFiles": sorted(changed),
        "keptEntries": kept,
        "droppedEntries": dropped,
        "report": report,
        "scope": scope,
        "mode": mode,
        # 前端「项目对齐分析」卡片直接吃这份（与 /api/parse 的 projectAlign 同构）
        "projectAlign": align_report,
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
    # 勾了「关联项目」时，导出的 Excel 也必须带对齐结果（主理人 Q2）：
    # 否则用户导出的底稿与出包产物不一致，下次走主流程又变回去。
    align_rows, link_rows, _plans = _align_rows(data, spec, chosen)
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
                        # 取值来源只有 BoField.value_source() 一处真相
                        # （全局字典要靠 dict_ref，dict_key() 会算错）
                        value_source=f.value_source(),
                        full_row=f.type_new == "clob",
                    ))
        # 对齐结果并进 Excel：已有字段只覆盖定义（code/fieldName 由反解结果带）
        for raw in align_rows.get(file, []):
            af = _align_field(raw, bo.code)
            hit = bo.field_by_code(af.code)
            if hit is None:
                continue
            af.sn = hit.sn
            af.field_name = hit.field_name or af.field_name
            bo.fields[bo.fields.index(hit)] = af
            touched.add(bo.code)
        for raw in link_rows.get(file, []):
            lf = normalize_field(raw, bo.code, 0)
            if bo.field_by_code(lf.code) is not None:
                continue
            lf.sn = alloc_sn()
            bo.fields.append(lf)
            bo.project_link = True
            touched.add(bo.code)
            for form in proj.forms:
                if form.bo_code != bo.code or any(x.name == lf.code for x in form.fields):
                    continue
                form.fields.append(FormField(
                    form_key=form.key, name=lf.code, label=lf.name,
                    field_type="select", value_source=lf.value_source(),
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
