#!/usr/bin/env python3
"""law_ingest.py —— 把官方全文导入法条库（一次一条命令）

为什么需要它
------------
"接真实数据库"不能靠人手往 markdown 里贴条文：贴错一个字、漏掉一条，
报告引用的依据就是错的，而且没人看得出来。导入器负责三件事——
**切条、校验、登记元数据**，人只负责提供文本和来源。

用法
----
    py -3 tools/law_ingest.py --id labor-contract \\
        --name "中华人民共和国劳动合同法" --short 劳动合同法 \\
        --file raw.txt --article-count 98 \\
        --status 现行有效 --effective 2008-01-01 \\
        --source-url https://... --source-org 人力资源和社会保障部

    py -3 tools/law_ingest.py --file raw.txt --dry-run     # 只切不写，先看条数

门禁（不通过就拒绝写入，返回码 1）
----------------------------------
1. 切出的条数必须等于 `--article-count`。公网页面经常分页（实测钦州检察院
   转载页只到第五十条），少一半的库比没有库更危险：读者以为"查过了"。
2. 条号必须从 1 连续到 N。缺第 47 条意味着"查无此条"会骗人。
3. `--source-url` / `--retrieved-at` / `--status` 必填：没有来源的条文，
   和模型编的条文在报告里长得一模一样。

导入后会自动把 `config/knowledge_version.txt` 的补丁号 +1（这是**正常演进**
的唯一通道，走它就不会让历史报告被判成"篡改"）。
"""
import argparse
import datetime
import html
import json
import os
import re
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAWS_DIR = os.path.join(ROOT, "knowledge", "laws")
INDEX_FILE = os.path.join(LAWS_DIR, "index.json")
VER_FILE = os.path.join(ROOT, "config", "knowledge_version.txt")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import law_index  # noqa: E402  切条逻辑只允许有一份实现


class _TextExtractor(HTMLParser):
    """HTML → 纯文本：只保留换行语义，丢掉脚本样式。"""

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("p", "br", "div", "tr", "li"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"无法识别编码：{path}")


def to_lines(text):
    """统一成"一行一段"：去空行、去页码、压掉行内多余空白。

    **不改写一个字**——只处理空白与结构行。导入器一旦开始"润色"原文，
    它就不再是转录工具，而是第二作者。
    """
    if "<" in text and "</" in text:
        p = _TextExtractor()
        p.feed(text)
        text = html.unescape("".join(p.out))
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t\u3000\xa0]+", " ", raw).strip()
        if not line:
            continue
        # 页码/导航行：纯数字、"第 3 页 / 共 10 页"、"1/10"
        if re.fullmatch(r"\d{1,3}(/\d{1,3})?|第\s*\d+\s*页(\s*/\s*共\s*\d+\s*页)?", line):
            continue
        lines.append(line)
    return lines


def build_md(name, lines, meta_line=""):
    body = ["# " + name, ""]
    if meta_line:
        body += [meta_line, ""]
    body += lines
    return "\n\n".join(body).rstrip() + "\n"


def bump_knowledge_version():
    """知识库版本 +1（补丁位）。返回 (旧, 新)。"""
    old = "1.0.0"
    if os.path.exists(VER_FILE):
        old = open(VER_FILE, encoding="utf-8").read().strip() or old
    parts = re.findall(r"\d+", old)
    while len(parts) < 3:
        parts.append("0")
    new = ".".join(parts[:2] + [str(int(parts[2]) + 1)])
    with open(VER_FILE, "w", encoding="utf-8") as f:
        f.write(new)
    return old, new


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--file", required=True, help="官方全文（txt/html，任意常见编码）")
    ap.add_argument("--id", help="法条库内唯一 id，如 labor-contract")
    ap.add_argument("--name", help="法律全名")
    ap.add_argument("--short", default="", help="简称（报告引用用）")
    ap.add_argument("--article-count", type=int, help="官方条数，用于校验切分完整性")
    ap.add_argument("--status", default="现行有效",
                    choices=["现行有效", "已修正", "尚未生效"])
    ap.add_argument("--effective", default="", help="施行日期 YYYY-MM-DD")
    ap.add_argument("--source-url", default="", help="来源 URL（必填，除非 dry-run）")
    ap.add_argument("--source-org", default="")
    ap.add_argument("--transport", default="manual", help="web_fetch / curl / manual")
    ap.add_argument("--verify-note", default="")
    ap.add_argument("--dry-run", action="store_true", help="只切分与校验，不写盘")
    args = ap.parse_args()

    lines = to_lines(read_text(args.file))
    arts = law_index.split_articles("\n".join(lines))
    nums = [a["no"] for a in arts]
    print(f"切分结果：{len(arts)} 条"
          + (f"，条号 {min(nums)}~{max(nums)}" if nums else ""))

    errs = []
    if not arts:
        errs.append("切出 0 条：正文里没有可识别的「第X条」段首")
    else:
        gaps = [n for n in range(1, max(nums) + 1) if n not in nums]
        if gaps:
            errs.append(f"条号不连续，缺 {gaps[:10]}{'…' if len(gaps) > 10 else ''}")
        if nums and nums[0] != 1:
            errs.append(f"首条不是第一条（切出的是第 {nums[0]} 条），可能只抓到后半部分")
    if args.article_count and len(arts) != args.article_count:
        errs.append(f"条数不符：官方目录 {args.article_count} 条，实际切出 {len(arts)} 条"
                    "（公网页面常分页，只到一半就结束——这种库不能用）")
    if not args.dry_run:
        for k, v in (("--id", args.id), ("--name", args.name),
                     ("--source-url", args.source_url), ("--effective", args.effective)):
            if not v:
                errs.append(f"{k} 必填（来源与时效不能留空）")
    for e in errs:
        print(f"[FAIL] {e}")
    if errs:
        return 1
    if args.dry_run:
        print("dry-run：未写盘。前 3 条：")
        for a in arts[:3]:
            print(f"  {a['no_cn']} {a['text'][:60]}")
        return 0

    os.makedirs(LAWS_DIR, exist_ok=True)
    md_name = f"{args.id}.md"
    meta_line = f"（{args.name}，来源：{args.source_org or args.source_url}，"
    meta_line += f"抓取于 {datetime.date.today().isoformat()}）"
    with open(os.path.join(LAWS_DIR, md_name), "w", encoding="utf-8") as f:
        f.write(build_md(args.name, lines, meta_line))

    data = {"version": "法条库 v1", "updated": datetime.date.today().isoformat(),
            "note": "", "laws": []}
    if os.path.exists(INDEX_FILE):
        data = json.load(open(INDEX_FILE, encoding="utf-8"))
    entry = {
        "id": args.id, "name": args.name, "short": args.short or args.name,
        "file": md_name, "status": args.status,
        "effective_date": args.effective,
        "article_count": len(arts),
        "source_url": args.source_url, "source_org": args.source_org,
        "retrieved_at": datetime.date.today().isoformat(),
        "transport": args.transport,
        "verify_note": args.verify_note or "条号连续性与条数已由导入器校验",
    }
    laws = [l for l in data.get("laws") or [] if l.get("id") != args.id]
    laws.append(entry)
    data["laws"] = laws
    data["updated"] = datetime.date.today().isoformat()
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    ok, check_errs = law_index.check(LAWS_DIR)
    if not ok:
        for e in check_errs:
            print(f"[FAIL] {e}")
        return 1
    old, new = bump_knowledge_version()
    print(f"已写入 knowledge/laws/{md_name}，index.json 新增 1 条")
    print(f"knowledge_version：{old} → {new}")
    print("下一步：py -3 tools/law_index.py --stat && py -3 tools/law_index.py \"关键词\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
