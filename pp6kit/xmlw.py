"""极简 XML 写出工具。

不用 ElementTree 的原因：需要精确控制属性顺序、CDATA、命名空间前缀，
以及 `&quot;` 转义风格——这些是保证目标系统能原样识别的前提。
"""
from __future__ import annotations

import json
from typing import Iterable


def esc_attr(value) -> str:
    if value is None:
        return ""
    s = str(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\r\n", "&#10;")
        .replace("\n", "&#10;")
    )


def cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def json_attr(obj) -> str:
    """压缩 JSON（与系统导出格式一致：无空格）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def attrs(d: dict, only: Iterable[str] | None = None) -> str:
    keys = only if only is not None else d.keys()
    out = []
    for k in keys:
        if k not in d:
            continue
        v = d[k]
        if v is None:
            continue
        out.append(f' {k}="{esc_attr(v)}"')
    return "".join(out)


def el(name: str, a: dict | str = "", inner: str | None = None, indent: int = 0) -> str:
    """生成一个元素。a 可为属性 dict 或已拼好的属性串；inner 为 None 时自闭合。"""
    pad = "  " * indent
    astr = a if isinstance(a, str) else attrs(a)
    if inner is None:
        return f"{pad}<{name}{astr}/>"
    if not inner.strip().startswith("<![CDATA") and "\n" not in inner:
        return f"{pad}<{name}{astr}>{inner}</{name}>"
    children = inner if inner.startswith("<![CDATA") else inner
    return f"{pad}<{name}{astr}>{children}</{name}>"


def wrap(name: str, body: list[str], a: dict | str = "", indent: int = 0) -> str:
    pad = "  " * indent
    astr = a if isinstance(a, str) else attrs(a)
    if not body:
        return f"{pad}<{name}{astr}/>"
    inner = "\n".join(body)
    return f"{pad}<{name}{astr}>\n{inner}\n{pad}</{name}>"


DECL_PLAIN = '<?xml version="1.0" encoding="UTF-8"?>'
DECL_STANDALONE = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
