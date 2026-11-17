"""回归测试的公共设施：用例注册、样本构造、zip/XML 断言工具。

为什么不用 pytest：
  1. 打包机上的 venv 只装了 openpyxl，装 pytest 需要联网；
  2. 这个项目的「回归」要能一条命令跑完并输出可直接贴进验收单的清单，
     自带 runner 更容易控制输出格式与用例编号。

用例编号约定：`模块前缀 + 两位序号`（NAM01 / BO07 / HTTP12 …），
编号一旦发布不再复用——删掉的用例保留空号，避免验收记录对不上。
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent

# 回归一律离线：不查目标库、不按「同编码对象」自动改成更新导入。
# 否则同一份用例在「连着库」和「没连库」的机器上产物不一样。
os.environ.setdefault("PP6_OFFLINE", "1")


# ---------------------------------------------------------------- 用例注册

@dataclass
class Case:
    cid: str
    module: str
    title: str
    level: str = "P1"          # P0=核心链路 / P1=常规 / P2=边界
    tags: list[str] = dc_field(default_factory=list)
    fn: Callable | None = None


REGISTRY: list[Case] = []


def case(cid: str, module: str, title: str, level: str = "P1",
         tags: list[str] | None = None):
    """把一个函数登记成用例。函数体里直接 assert。"""
    def deco(fn: Callable) -> Callable:
        REGISTRY.append(Case(cid, module, title, level, tags or [], fn))
        return fn
    return deco


class Skip(Exception):
    """环境不满足（例如需要连库/需要大模型）时抛出，不算失败。"""


def skip(reason: str):
    raise Skip(reason)


# ---------------------------------------------------------------- Excel 构造

def xlsx(sheets: dict[str, list[dict]]) -> bytes:
    """按 spec 的列定义造一个填写模板。

    sheets = {"1_业务对象": [{"对象名称": "项目", "流程驱动": "否"}, ...]}
    用**中文表头**传值，helper 负责对齐列序——手写 list 迟早会错位，
    之前就踩过「允许空=否」被当成 defaultvalue 写进 XML。
    """
    from openpyxl import Workbook

    from pp6kit import spec

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        if name not in spec.COLUMNS:
            raise KeyError(f"未知 sheet：{name}")
        cols = spec.COLUMNS[name]
        index = {c.header: i for i, c in enumerate(cols)}
        ws = wb.create_sheet(name)
        ws.append([c.header for c in cols])
        # 预铺空串，避免 openpyxl 写出 None 导致「尾部空列」影响判断
        blank = ["" for _ in cols]
        for item in rows:
            line = list(blank)
            for header, value in item.items():
                if header not in index:
                    raise KeyError(f"[{name}] 没有这一列：{header}")
                line[index[header]] = value
            ws.append(line)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def read_xlsx_rows(data: bytes, sheet: str) -> list[dict]:
    """把产出的 xlsx 读回成 [{表头: 值}]，用于断言反解结果。"""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True)
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    out = []
    for row in rows[1:]:
        if not any(v not in (None, "") for v in row):
            continue
        out.append({h: row[i] for i, h in enumerate(header) if i < len(row)})
    return out


# ---------------------------------------------------------------- 样本构造

def _bo(**kw) -> dict:
    base = {"对象名称": "", "对象类型": "object", "记录变更日志": "是",
            "流程驱动": "否", "关系类型": "PARENT"}
    base.update(kw)
    return base


def field(bo_code: str, name: str, type_: str, **kw) -> dict:
    base = {"对象编码": bo_code, "字段名称": name, "数据类型": type_,
            "允许空": "是", "建索引": "否"}
    base.update(kw)
    return base


def issues_of(proj) -> tuple[list[str], list[str], list[str]]:
    """跑一遍校验，返回 (errors, warns, infos) 的文案。"""
    from pp6kit.validate import validate

    issues = validate(proj)
    return (tuple(i.message for i in issues if i.level == "error"),
            tuple(i.message for i in issues if i.level == "warn"),
            tuple(i.message for i in issues if i.level == "info"))


def project_issues(sheets: dict):
    proj, read_errors, _ = project_of(xlsx(sheets))
    errs, warns, infos = issues_of(proj)
    return tuple(read_errors) + errs, warns, infos


def simple_package() -> bytes:
    """一份最小但完整的填写表：1 个对象 4 个字段 + 1 张新建表单 + 1 个列表。"""
    from pp6kit import spec

    return xlsx({
        spec.S_BO: [
            _bo(**{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project",
                   "对象说明": "回归样本"}),
        ],
        spec.S_FIELD: [
            field("project", "项目名称", "字符串", 长度=200),
            field("project", "申请金额", "数字", 精度=2),
            field("project", "状态", "选项", 选项值="进行中,已结束,已归档"),
            field("project", "开始日期", "日期"),
            field("project", "负责人", "用户"),
            field("project", "备注", "多行文本"),
        ],
        spec.S_FORM: [
            {"表单key": "create_project", "表单名称": "新建项目", "对象编码": "project",
             "表单类型": "新建表单", "布局": "一行两列", "状态": "deploy",
             "补隐藏字段": "是"},
        ],
        spec.S_FORMFIELD: [
            {"表单key": "create_project", "字段编码": "xiangMuMingCheng",
             "显示标签": "项目名称", "控件类型": "text", "必填": "是"},
            {"表单key": "create_project", "字段编码": "shenQingJinE",
             "显示标签": "申请金额", "控件类型": "number"},
            {"表单key": "create_project", "字段编码": "zhuangTai",
             "显示标签": "状态", "控件类型": "dictionary",
             "取值来源": "project_zhuangTai"},
            {"表单key": "create_project", "字段编码": "kaiShiRiQi",
             "显示标签": "开始日期", "控件类型": "date"},
        ],
        spec.S_LIST: [
            {"列表key": "project_list", "列表名称": "项目列表", "对象编码": "project",
             "视图类型": "grid", "排序字段": "createTime", "排序方向": "desc"},
        ],
    })


_SAMPLE_CACHE: dict[str, bytes] = {}


def project_of(data: bytes):
    """把填写表读成 Project 对象（走 reader.read_workbook，和出包同一条路）。"""
    import tempfile

    from pp6kit import reader

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as fh:
        fh.write(data)
        path = Path(fh.name)
    try:
        proj, errors, stats = reader.read_workbook(path)
    finally:
        path.unlink(missing_ok=True)
    return proj, errors, stats


def sample_project():
    """标准样本的 Project（缓存）。"""
    if "proj" not in _SAMPLE_CACHE:
        proj, errors, stats = project_of(simple_package())
        _SAMPLE_CACHE["proj"] = (proj, errors, stats)
    return _SAMPLE_CACHE["proj"]



def sample_zip() -> bytes:
    """标准样本出包结果（缓存，避免每条用例重跑一次完整生成）。"""
    if "zip" not in _SAMPLE_CACHE:
        from pp6kit import webui
        body, _ = webui.build_zip(simple_package())
        _SAMPLE_CACHE["zip"] = body
    return _SAMPLE_CACHE["zip"]


def native_zip(name: str = "项目.zip") -> bytes:
    """原生导出包（线上系统导出的真实样本），用于反解与改包用例。"""
    path = ROOT / "PP6" / "业务对象" / name
    if not path.is_file():
        skip(f"缺少原生样本 {path.relative_to(ROOT)}")
    return path.read_bytes()


# ---------------------------------------------------------------- 解析断言工具

def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist()}


def texts(data: bytes) -> dict[str, str]:
    return {n: b.decode("utf-8") for n, b in unzip(data).items() if n.endswith(".xml")}


def pick(data: bytes, prefix: str) -> str:
    """取第一个 path 以 prefix 开头的 XML 文本。"""
    hits = {n: t for n, t in texts(data).items() if n.startswith(prefix)}
    assert hits, f"包里没有 {prefix}* 的 XML，实际：{sorted(texts(data))}"
    return hits[sorted(hits)[0]]


def attrs(fragment: str) -> dict[str, str]:
    return dict(re.findall(r'([\w:.-]+)="([^"]*)"', fragment))


def tag(text: str, name: str) -> str:
    """取第一个 name 标签的整段（自闭合也行）。"""
    m = re.search(r"<[\w:]*" + re.escape(name) + r"\b[^>]*?/?>", text)
    assert m, f"没找到 <{name}>"
    return m.group(0)


def all_tags(text: str, name: str) -> list[str]:
    return re.findall(r"<[\w:]*" + re.escape(name) + r"\b[^>]*?/?>", text)


def attr_of(text: str, tag_name: str, code: str, key: str) -> str:
    """在某类标签里按 code 找到那一条，取它的属性。"""
    for frag in all_tags(text, tag_name):
        a = attrs(frag)
        if a.get("code") == code:
            return a.get(key, "")
    raise AssertionError(f"<{tag_name}> 里没有 code={code} 的条目")


def xml_ok(text: str) -> None:
    """用标准库解析一遍，确认 XML 合法（不依赖 xmllint）。"""
    import xml.etree.ElementTree as ET

    try:
        ET.fromstring(text)
    except ET.ParseError as exc:      # pragma: no cover - 失败时才有用
        head = text[:400].replace("\n", " ")
        raise AssertionError(f"XML 不合法：{exc}\n开头：{head}") from None


def has(text: str, needle: str) -> None:
    assert needle in text, f"期望包含 {needle!r}，实际没有"


def lacks(text: str, needle: str) -> None:
    assert needle not in text, f"不应该包含 {needle!r}"
