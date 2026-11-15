"""PDF / Word 需求文档提取：只认文字，不认版式。"""
from __future__ import annotations

import io
import zipfile

from pp6kit import doc_text
from tests._harness import case

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(paragraphs: list[str], rows: list[list[str]] | None = None) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    if rows:
        trs = "".join(
            "<w:tr>" + "".join(
                f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row)
            + "</w:tr>" for row in rows)
        body += f"<w:tbl>{trs}</w:tbl>"
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
           f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def _pdf(text: str) -> bytes:
    content = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offs = []
    for i, obj in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offs:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF").encode()
    return out


@case("DOC01", "doc_text", "Word 正文和表格都能提取", "P1",
      ["段落按顺序", "表格按行输出"])
def _():
    data = _docx(["合同台账需求", "记录合同名称、金额。"],
                 [["字段", "类型"], ["合同名称", "文本"], ["合同金额", "数字"]])
    got = doc_text.extract("需求.docx", data)
    assert got["kind"] == "docx" and got["chars"] > 0, got
    text = got["text"]
    assert "合同台账需求" in text and "记录合同名称、金额。" in text, text
    assert "字段 | 类型" in text and "合同名称 | 文本" in text, text


@case("DOC02", "doc_text", "PDF 文字提取；扫描件/旧 doc 给出明确错误", "P1",
      ["文字版 PDF 能读出", "空 PDF 报扫描件", "旧 .doc 提示另存"])
def _():
    got = doc_text.extract("需求.pdf", _pdf("Budget Test"))
    assert got["kind"] == "pdf" and "Budget Test" in got["text"], got
    try:
        doc_text.extract("扫描件.pdf", _pdf(""))
    except ValueError as exc:
        assert "扫描" in str(exc) or "OCR" in str(exc), exc
    else:
        raise AssertionError("空 PDF 没报错")
    try:
        doc_text.extract("旧文档.doc", b"x")
    except ValueError as exc:
        assert ".docx" in str(exc), exc
    else:
        raise AssertionError("旧 .doc 没报错")
