"""桌面壳的文件选择器：过滤器字符串必须是 pywebview 能解析的格式。

pywebview 的 parse_file_type 只接受「描述 (*.ext;*.ext)」，且描述部分只能有
字母/数字/中文/空格——写成「Excel / PDF / Word (...」会直接抛
`is not a valid file filter`，前端表现为点了没反应或只弹一句 toast。
"""
from __future__ import annotations

from pp6kit import desktop_api
from tests._harness import case, skip


@case("DSK01", "desktop", "文件过滤器是 pywebview 合法格式（描述不能带斜杠）", "P0",
      ["Excel / zip / 多格式三组都合法", "多格式组同时含 pdf/docx"])
def _():
    try:
        from webview.util import parse_file_type
    except ImportError:
        skip("没装 pywebview：桌面文件过滤器检查跳过")
    groups = [desktop_api.EXCEL_TYPES, desktop_api.ZIP_TYPES, desktop_api.SOURCE_TYPES]
    for group in groups:
        for filt in group:
            desc, exts = parse_file_type(filt)
            assert desc, filt
            assert exts.startswith("*."), (filt, exts)
    desc, exts = parse_file_type(desktop_api.SOURCE_TYPES[0])
    assert "pdf" in exts and "docx" in exts, (desc, exts)
    assert "/" not in desc, desc
