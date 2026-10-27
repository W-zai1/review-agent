#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docx_extract.py —— 从 .docx 提取「可审查文本 + 版式结构」（零第三方依赖）

职责边界：
  - 只负责「取文本与版式属性」，**不负责审查**；审查由 review.py 的格式维完成
  - 版式属性按段落粒度提取（每段一个 dict），并解析主题字体与文档默认字号，
    供 FORMAT 规则判定「实际生效值 vs 要求值」
  - 提取失败一律抛异常（调用方决定如何处理），绝不静默返回空结构冒充「已解析」

版式属性（OOXML，单位说明）：
  - 字体：w:rPr/w:rFonts 的 w:ascii（西文）、w:eastAsia（中文）；未显式指定
    时看 w:eastAsiaTheme / w:asciiTheme，最终回退到主题 minorFont（Hans=宋体）
  - 字号：w:sz 单位为半磅（24 = 12pt = 小四）
  - 首行缩进：w:ind 的 w:firstLineChars（1/100 字符，200 = 2 字符）
    与 w:firstLine（缇，1/20 磅，480 ≈ 小四 2 字符）
  - 行距：w:spacing 的 w:line（1/20 磅，440 = 22 磅）+ w:lineRule（exact=固定值）
  - 标题：段落样式 w:pStyle（Heading1-9 / 标题 1-9）或 w:outlineLvl
  - 表格：段落在 <w:tbl> 内（单元格段落不做正文格式判定）
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
MAX_XML_BYTES = 8 * 1024 * 1024

# 正文段最小文本长度（过短的段落多为标签/目录残留，不做正文格式判定）
BODY_MIN_TEXT = 10
# 表/图题注行：行首「表 1-1」「图 2-1」
CAPTION_RE = re.compile(r"^(图|表)\s*\d")

_HEADING_STYLE = {
    "Heading1", "Heading2", "Heading3", "Heading4", "Heading5",
    "Heading6", "Heading7", "Heading8", "Heading9",
    "标题1", "标题2", "标题3", "标题4", "标题5",
    "标题6", "标题7", "标题8", "标题9",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _sub(el, name):
    """取第一个子元素（局部名匹配），无则 None。"""
    if el is None:
        return None
    return next((c for c in el if _local(c.tag) == name), None)


def _all(el, name):
    if el is None:
        return []
    return [c for c in el if _local(c.tag) == name]


def _attr(el, name, default=None):
    if el is None:
        return default
    v = el.get(W + name)
    return default if v is None else v


def _read_capped(z: zipfile.ZipFile, name: str) -> str:
    info = z.getinfo(name)
    with z.open(name) as fh:
        data = fh.read(MAX_XML_BYTES if info.file_size > MAX_XML_BYTES else info.file_size)
    return data.decode("utf-8", "replace")


def _font_typeface(theme_xml: str, kind: str, script: str = "Hans") -> str | None:
    """从 theme1.xml 的 majorFont/minorFont 里取指定 script 的字面字体名。"""
    m = re.search(r"<a:%s>.*?</a:%s>" % (kind, kind), theme_xml, re.S)
    if not m:
        return None
    for f in re.finditer(
            r'<a:font\b[^>]*script="([^"]+)"[^>]*typeface="([^"]+)"', m.group(0)):
        if f.group(1) == script:
            return f.group(2)
    return None


def _doc_default_sz(styles_xml: str) -> int | None:
    m = re.search(r"<w:docDefaults>.*?</w:docDefaults>", styles_xml, re.S)
    if not m:
        return None
    s = re.search(r'<w:sz\s+w:val="(\d+)"', m.group(0))
    return int(s.group(1)) if s else None


def _para_props(p_el):
    """收集段落属性：字体集合/字号集合/缩进/行距/样式/大纲级别/加粗。"""
    east_set = set()
    ascii_set = set()
    sz_set = set()
    bold = False
    first_sz = None
    first_east = None
    first_ascii = None

    def _consume_rpr(rpr):
        nonlocal first_sz, first_east, first_ascii
        rf = _sub(rpr, "rFonts")
        if rf is not None:
            east = _attr(rf, "eastAsia") or (W + "eastAsiaTheme" if rf.get(W + "eastAsiaTheme") else None)
            asc = _attr(rf, "ascii") or (W + "asciiTheme" if rf.get(W + "asciiTheme") else None)
            if east:
                east_set.add(east)
            if asc:
                ascii_set.add(asc)
            if first_east is None and east and not east.startswith(W):
                first_east = east
            if first_ascii is None and asc and not asc.startswith(W):
                first_ascii = asc
        sz = _sub(rpr, "sz")
        if sz is not None:
            v = _attr(sz, "val")
            if v:
                sz_set.add(int(v))
                if first_sz is None:
                    first_sz = int(v)
        b = _sub(rpr, "b")
        if b is not None and b.get(W + "val") not in ("0", "false"):
            bold = True

    ppr = _sub(p_el, "pPr")
    pstyle = None
    outline_lvl = None
    first_line_chars = None
    first_line = None
    spacing_line = None
    spacing_rule = None
    if ppr is not None:
        _consume_rpr(_sub(ppr, "rPr"))          # 段落标记属性
        st = _sub(ppr, "pStyle")
        if st is not None:
            pstyle = _attr(st, "val")
        ol = _sub(ppr, "outlineLvl")
        if ol is not None:
            try:
                outline_lvl = int(_attr(ol, "val") or -1)
            except ValueError:
                outline_lvl = None
        ind = _sub(ppr, "ind")
        if ind is not None:
            flc = _attr(ind, "firstLineChars")
            fl = _attr(ind, "firstLine")
            if flc is not None:
                try:
                    first_line_chars = int(flc)
                except ValueError:
                    pass
            if fl is not None:
                try:
                    first_line = int(fl)
                except ValueError:
                    pass
        sp = _sub(ppr, "spacing")
        if sp is not None:
            ln = _attr(sp, "line")
            if ln is not None:
                try:
                    spacing_line = int(ln)
                except ValueError:
                    pass
            spacing_rule = _attr(sp, "lineRule")

    for r in _all(p_el, "r"):
        _consume_rpr(_sub(r, "rPr"))

    return {
        "east_set": east_set, "ascii_set": ascii_set, "sz_set": sz_set,
        "first_sz": first_sz, "first_east": first_east, "first_ascii": first_ascii,
        "bold": bold, "pstyle": pstyle, "outline_lvl": outline_lvl,
        "first_line_chars": first_line_chars, "first_line": first_line,
        "spacing_line": spacing_line, "spacing_rule": spacing_rule,
    }


def _para_text(p_el) -> str:
    out = []
    for r in _all(p_el, "r"):
        for c in r:
            n = _local(c.tag)
            if n == "t":
                out.append(c.text or "")
            elif n == "tab":
                out.append(" ")
            elif n == "br":
                out.append("\n")
    return "".join(out)


def extract(path) -> dict:
    """提取 docx 正文文本与版式结构。返回 dict；失败抛 ValueError。"""
    path = Path(path)
    try:
        with zipfile.ZipFile(str(path)) as z:
            names = z.namelist()
            if "word/document.xml" not in names:
                raise ValueError("不是有效的 Word 文档（缺少 word/document.xml）")
            doc_xml = _read_capped(z, "word/document.xml")
            styles_xml = _read_capped(z, "word/styles.xml") if "word/styles.xml" in names else ""
            theme_xml = ""
            for n in sorted(names):
                if n.startswith("word/theme") and n.endswith(".xml"):
                    theme_xml = _read_capped(z, n)
                    break
    except zipfile.BadZipFile:
        raise ValueError("Word 文件已损坏或不是 .docx 格式")

    root = ET.fromstring(doc_xml)
    theme_minor = _font_typeface(theme_xml, "minorFont", "Hans") if theme_xml else None
    theme_major = _font_typeface(theme_xml, "majorFont", "Hans") if theme_xml else None
    doc_default_sz = _doc_default_sz(styles_xml) if styles_xml else None

    paragraphs = []
    text_lines = []

    def _walk(parent, in_table):
        for el in parent:
            tag = _local(el.tag)
            if tag == "tbl":
                for c in el:
                    if _local(c.tag) == "tr":
                        for tc in c:
                            if _local(tc.tag) == "tc":
                                _walk(tc, True)
                continue
            if tag == "p":
                t = _para_text(el)
                props = _para_props(el)
                paragraphs.append({
                    "text": t, "in_table": in_table, **props,
                })
                if t.strip():
                    text_lines.append(t)
            else:
                # 容器（body/sdt/ins/del…）继续下钻
                _walk(el, in_table)

    _walk(root, False)
    return {
        "ok": True,
        "text": "\n".join(text_lines),
        "paragraphs": paragraphs,
        "theme_minor_hans": theme_minor,
        "theme_major_hans": theme_major,
        "doc_default_sz": doc_default_sz,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python tools/docx_extract.py <file.docx>")
        sys.exit(2)
    try:
        info = extract(sys.argv[1])
    except ValueError as e:
        print(f"✗ {e}")
        sys.exit(1)
    print(f"paragraphs={len(info['paragraphs'])} chars={len(info['text'])} "
          f"theme_minor(Hans)={info['theme_minor_hans']} doc_default_sz={info['doc_default_sz']}")
    for p in info["paragraphs"][:5]:
        print(p)
