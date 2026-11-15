"""从 PDF / Word 需求文档里提取纯文字，交给 AI 设计业务对象。

为什么不让模型直接读二进制：PDF / docx 的排版信息对「有哪些字段」帮助不大，
先把文字抽出来，用户还能在输入框里删改一遍再生成——比黑盒上传稳。

边界：
  · 只支持文字版 PDF（pypdf）；扫描件没有文字层，必须 OCR 或换 Word。
  · 只支持 .docx；旧版 .doc 请另存为 .docx / PDF。
  · 不解析版式/图片；Word 表格按「单元格 | 单元格」输出，保留表头。
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# 太长会把 AI 上下文撑爆；20k 中文字符已经够一份完整需求文档
MAX_CHARS = 20000
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("不是有效的 .docx 文件（旧版 .doc 请另存为 .docx / PDF）") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("Word 文档结构损坏，读不出正文") from exc

    body = root.find(f"{_W}body")
    if body is None:
        return ""

    def para_text(p) -> str:
        return "".join(t.text or "" for t in p.iter(f"{_W}t")).strip()

    def walk(node) -> list[str]:
        out: list[str] = []
        for child in node:
            tag = child.tag
            if tag == f"{_W}p":
                text = para_text(child)
                if text:
                    out.append(text)
            elif tag == f"{_W}tbl":
                for tr in child.findall(f"{_W}tr"):
                    cells = []
                    for tc in tr.findall(f"{_W}tc"):
                        text = " ".join(
                            x for x in (para_text(p) for p in tc.findall(f"{_W}p")) if x
                        )
                        cells.append(text)
                    if any(cells):
                        out.append(" | ".join(cells))
            else:
                out.extend(walk(child))
        return out

    return "\n".join(walk(body))


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:              # 打包漏了依赖时给出能看懂的错
        raise ValueError("服务端缺少 PDF 解析组件（pypdf），请重新打包") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ValueError("PDF 有密码，读不了；请去掉密码后重试") from exc
        pages = [(page.extract_text() or "") for page in reader.pages]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"PDF 读取失败：{exc}") from exc
    return "\n".join(pages)


def extract(filename: str, data: bytes) -> dict:
    """按扩展名提取正文；返回 {text, kind, chars, truncated, name}。"""
    name = str(filename or "").strip()
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        text, kind = _pdf_text(data), "pdf"
    elif suffix == ".docx":
        text, kind = _docx_text(data), "docx"
    elif suffix == ".doc":
        raise ValueError("旧版 .doc 不支持，请另存为 .docx 或 PDF 后再传")
    else:
        raise ValueError("只支持 PDF 或 Word（.docx）文档")
    if not data:
        raise ValueError("文件是空的")
    # 统一空白：PDF 抽出来经常一行一个字符/一堆空行
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("没有提取到文字：可能是扫描版 PDF（需要 OCR）或空文档")
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS] + "\n…（文档过长，已截断）"
    return {"text": text, "kind": kind, "chars": len(text),
            "truncated": truncated, "name": name}
