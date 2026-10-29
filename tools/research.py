#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""research.py —— 对「库外事实主张」真去查，而不是只标注「无出处」。

旧链路的问题（用户原话）：
  「一句话有几个事实观点，却无法通过调用工具搜索告知是否属实，
    只会标注在此毫无意义。」

本模块把事实审从「只比对 facts.json 的 9 条断言」改成三步：

    抽主张  →  检索（本地来源库 / 公开网页）  →  判定（支持 / 反驳 / 未找到）

三个刻意的设计：

1. **先本地、后网页**。`knowledge/sources/*.json` 是可积累的来源库
   （`--add-source <url>` 抓一页存一页）；本地能判定就不发网络请求。
2. **结论必须带来源**。support/refute 都带 URL、片段、抓取时间；
   没有来源的「未找到」不生成 issue，只进报告的「已检索但未找到」清单。
   这正好解决「无出处」噪音：不知道就说不知道，并列出查过哪里。
3. **缓存即学习**。同一条主张第二次出现直接命中 `state/research_cache/`，
   不再联网；缓存里存的是「主张 → 结论 + 来源」，不是被审文档的内容。

外部检索默认用 Bing 的 HTML 结果页（无需 API Key）。企业内网/离线环境
可用 `--no-web`，或只往 `knowledge/sources/` 里加来源。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import html as html_mod
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fact_gap          # noqa: E402
import regex_check       # noqa: E402

STATE = ROOT / "state"
CACHE_DIR = STATE / "research_cache"
SOURCES_DIR = ROOT / "knowledge" / "sources"
LEARNED = STATE / "learned_facts.jsonl"
CONFIG = ROOT / "config" / "research.yaml"

DEFAULTS = {
    "enabled": True,
    "max_claims_per_doc": 3,
    "max_results": 8,
    "timeout_seconds": 8,
    "max_seconds": 60,
    "cache_ttl_days": 30,
    "fetch_pages": 2,
    "providers": ["local", "so360_m", "sogou_m", "bing"],
    "official_domains": [
        "gov.cn", "stats.gov.cn", "caict.ac.cn", "cnnic.cn",
        "mohrss.gov.cn", "samr.gov.cn", "court.gov.cn", "npc.gov.cn",
    ],
    # 这些词单独出现时不是实体，抽主张时跳过
    "entity_stopwords": [
        "同比", "环比", "增长", "下降", "数据", "报告", "统计", "显示",
        "目前", "今年", "去年", "本月", "上月", "其中", "以上", "以下",
        "公司", "企业", "用户", "市场", "行业", "全国", "全球",
    ],
}
METRIC_WORDS = (
    "月活", "日活", "活跃用户", "用户数", "用户规模", "出货量", "销量",
    "营收", "收入", "净利润", "利润", "市场份额", "市占率", "占比",
    "增长率", "同比", "环比", "客单价", "价格", "订单量", "门店数",
    "员工数", "产能", "产量", "装机量", "覆盖率", "渗透率", "转化率",
    "满意度", "下载量", "播放量", "粉丝数", "点赞量",
)
FUTURE_RE = re.compile(r"(预计|预估|计划|目标为|目标达到|目标是|拟于|将于|争取|有望)")
DATE_VERB_RE = re.compile(r"(成立|推出|发布|上市|施行|生效|获得|完成|收购|合并|注册)")
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


# ---------------------------------------------------------------------------
# 配置 / 缓存
# ---------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        try:
            data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if not str(k).startswith("_")})
        except Exception as e:                              # noqa: BLE001
            cfg["_config_error"] = f"{type(e).__name__}: {e}"
    return cfg


def _cache_key(claim):
    raw = "|".join(str(claim.get(k) or "") for k in
                   ("entity", "metric", "kind", "value", "unit", "year"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cache_path(claim):
    return CACHE_DIR / f"{_cache_key(claim)}.json"


def cache_get(claim):
    p = _cache_path(claim)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ts = datetime.datetime.fromisoformat(data["checked_at"])
        ttl = int(data.get("ttl_days") or 30)
        if datetime.datetime.now().astimezone() - ts > datetime.timedelta(days=ttl):
            return None
        return data
    except Exception:                                       # noqa: BLE001
        return None


def cache_put(claim, verdict, ttl_days):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(verdict)
    payload["claim"] = {k: claim.get(k) for k in
                        ("entity", "metric", "kind", "value", "unit", "year", "quote")}
    payload["checked_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    payload["ttl_days"] = int(ttl_days)
    try:
        _cache_path(claim).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def cache_clear():
    n = 0
    if CACHE_DIR.exists():
        for p in CACHE_DIR.glob("*.json"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n


# ---------------------------------------------------------------------------
# 学到的断言：把「这次查实的结论」沉淀成下一条可复用的知识。
#
# 与 facts.json 的区别：
#   facts.json  = 人工审核过的判定库，审查只按它下结论；
#   learned_facts = 自动检索得到的「可复用结论」，带来源与置信度，
#                  只用于检索层，不冒充人工事实库。
# 只有「权威来源」或「两个独立来源一致」才写入，避免把一次搜索噪声学成知识。
# ---------------------------------------------------------------------------
def _learned_key(row):
    return "|".join(str(row.get(k) or "") for k in
                    ("entity", "metric", "value", "unit", "year"))


def load_learned():
    if not LEARNED.exists():
        return []
    out = []
    for line in LEARNED.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def learned_lookup(claim):
    """下一条同类主张先查「上次已查实的结论」，不再联网。"""
    ent = str(claim.get("entity") or "")
    metric = str(claim.get("metric") or "")
    unit = str(claim.get("unit") or "")
    for row in load_learned():
        if str(row.get("entity") or "") != ent:
            continue
        if str(row.get("metric") or "") != metric:
            continue
        if claim.get("year") and row.get("year") and str(claim["year"]) != str(row["year"]):
            continue
        if not _unit_eq(row.get("unit"), unit):
            continue
        try:
            want = _base(claim.get("value"), unit)
            got = (row.get("value") if row.get("base")
                   else _base(row.get("value"), row.get("unit")))
        except (TypeError, ValueError):
            continue
        ratio = got / want if want else 0
        status = "supported" if 0.5 <= ratio <= 2 else "refuted"
        return {
            "status": status, "method": "learned", "confidence": row.get("confidence"),
            "value": row.get("value"), "display": row.get("display"),
            "sources": row.get("sources") or [],
            "reason": ("命中已学断言（上次检索结论，带来源）" if status == "supported"
                       else "与已学断言不一致（上次检索结论，带来源）"),
            "query": f"{ent} {metric} {claim.get('year') or ''}".strip(),
            "providers": ["learned"],
        }
    return None


def learn_verdict(claim, verdict):
    """支持/反驳且置信度 >= 0.8 时沉淀；同一结论只写一次。"""
    if verdict.get("status") not in ("supported", "refuted"):
        return False
    if float(verdict.get("confidence") or 0) < 0.8:
        return False
    row = {
        "entity": claim.get("entity"), "metric": claim.get("metric"),
        "value": verdict.get("value"), "unit": claim.get("unit"),
        "base": True,
        "year": claim.get("year"), "display": verdict.get("display"),
        "status": verdict.get("status"), "confidence": verdict.get("confidence"),
        "sources": (verdict.get("sources") or [])[:2],
        "learned_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    exists = {_learned_key(x) for x in load_learned()}
    if _learned_key(row) in exists:
        return False
    STATE.mkdir(exist_ok=True)
    try:
        with open(LEARNED, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 来源库（knowledge/sources/*.json，可自己长）
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1
        elif tag in ("p", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(raw):
    p = _TextExtractor()
    try:
        p.feed(raw)
    except Exception:                                       # noqa: BLE001
        pass
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def fetch_url(url, timeout=12, max_bytes=2_000_000):
    req = urllib.request.Request(
        url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        data = r.read(max_bytes)
    if "html" in ctype or data[:200].lstrip().startswith(b"<"):
        return html_to_text(data.decode("utf-8", "replace"))
    return data.decode("utf-8", "replace")


def _is_official(url, cfg):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in cfg.get("official_domains") or [])


def load_sources():
    out = []
    if not SOURCES_DIR.exists():
        return out
    for p in sorted(SOURCES_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:                                   # noqa: BLE001
            continue
        if isinstance(d, dict) and d.get("text"):
            d["_file"] = p.name
            out.append(d)
    return out


def add_source(url, title=None, official=None, cfg=None):
    """抓一页存进来源库；下次同一实体的主张就能先查本地。"""
    cfg = cfg or load_config()
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("只接受 http(s) URL")
    text = fetch_url(url, timeout=int(cfg.get("timeout_seconds") or 12))
    if len(text) < 80:
        raise ValueError(f"抓到的正文太短（{len(text)} 字符），可能被反爬或不是文章页")
    text = text[:200_000]
    title = str(title or "").strip()
    if not title:
        first = next((l.strip() for l in text.splitlines() if l.strip()), "")
        title = first[:80] or url
    item = {
        "title": title,
        "url": url,
        "text": text,
        "official": _is_official(url, cfg) if official is None else bool(official),
        "retrieved_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    fp = SOURCES_DIR / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:12] + ".json")
    fp.write_text(json.dumps(item, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"file": str(fp), "title": title, "chars": len(text),
            "official": item["official"]}


# ---------------------------------------------------------------------------
# 主张抽取：只抽「实体 + 指标 + 数值」这种能核的硬主张
# ---------------------------------------------------------------------------
def _metric_before(sentence, start):
    window = sentence[max(0, start - 24):start]
    for w in sorted(METRIC_WORDS, key=len, reverse=True):
        if w in window:
            return w
    # 退回「数字前最后一个中文词组」，去掉动词尾巴
    runs = re.findall(r"[\u4e00-\u9fff]{2,}", window)
    if not runs:
        return ""
    cand = runs[-1]
    cand = re.sub(r"(达到|达|约为|约|超过|突破|只有|仅为|为|共|合计|增长到|增至|下降到|降至)$", "", cand)
    return cand[-8:] if len(cand) >= 2 else ""


def _clean_entity(entity):
    """`fact_gap.guess_entity` 对长句会返回「公司产品在目标市场」这种短语。

    搜索时只留主语部分；切不干净也比整段当实体强——至少能命中「公司产品」相关页。
    """
    e = str(entity or "").strip()
    for sep in ("在", "的", "将", "已经", "达到", "为", "是"):
        if sep in e and len(e.split(sep)[0]) >= 2:
            e = e.split(sep)[0]
            break
    return e[:12]


def extract_claims(sentences, assertions=None, max_claims=5):
    """sentences: [(句子, 起始字符位置)]，由 review.iter_sentences 提供。"""
    assertions = assertions or []
    claims = []
    seen = set()
    for idx, (sent, pos) in enumerate(sentences):
        s = str(sent or "").strip()
        if not s or len(s) > 400 or FUTURE_RE.search(s):
            continue
        entity = _clean_entity(fact_gap.guess_entity(s))
        if len(entity) < 2 or entity in (DEFAULTS["entity_stopwords"] or []):
            continue
        # 已知断言覆盖的句子不再研究：facts.json 已经判过
        if any(a.get("entity") and a["entity"] in s and
               (a.get("attr") in s or a.get("type") == "date")
               for a in assertions):
            continue
        # 数值主张
        for n in regex_check.extract_numbers(s):
            if not n.get("unit"):
                continue
            if 1900 <= n["num"] <= 2100 and s[n["end"]:n["end"] + 1] == "年":
                continue
            metric = _metric_before(s, n["start"])
            if not metric:
                continue
            key = (entity, metric, n["num"], n["unit"])
            if key in seen:
                continue
            seen.add(key)
            claims.append({
                "kind": "number", "quote": s, "pos": pos, "sentence_index": idx + 1,
                "entity": entity, "metric": metric,
                "value": n["num"], "unit": n["unit"], "raw": n["raw"],
                "year": int(YEAR_RE.search(s).group(1)) if YEAR_RE.search(s) else None,
            })
        # 日期主张（成立/推出/发布/施行……）
        if DATE_VERB_RE.search(s):
            dates = regex_check.extract_dates(s)
            if dates:
                d = dates[0]
                key = (entity, "date", d["year"], "")
                if key not in seen:
                    seen.add(key)
                    claims.append({
                        "kind": "date", "quote": s, "pos": pos,
                        "sentence_index": idx + 1, "entity": entity,
                        "metric": (DATE_VERB_RE.search(s).group(1)
                                   if DATE_VERB_RE.search(s) else "日期"),
                        "value": d["year"], "unit": "年", "raw": d["raw"],
                        "year": d["year"],
                    })
    # 优先有「数据显示/达到/占比」这类明确断言的句子
    claims.sort(key=lambda c: (0 if re.search(r"(据|显示|达到|占比|同比|环比|为|共)", c["quote"]) else 1,
                               c["sentence_index"]))
    return claims[:max_claims]


def build_query(claim):
    """查询只放实体 + 指标 + 年份，**不放被核验的数值**。

    把「10 亿」拼进查询，搜索引擎会把带「10 亿」的页面排前面，
    等于用答案找答案；核验必须独立于被核验值。
    """
    parts = [str(claim.get("entity") or ""), str(claim.get("metric") or "")]
    if claim.get("year"):
        parts.append(str(claim["year"]))
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# 检索：本地来源库 + Bing HTML
# ---------------------------------------------------------------------------
def local_search(query, claim, cfg):
    ent = str(claim.get("entity") or "")
    metric = str(claim.get("metric") or "")
    out = []
    for src in load_sources():
        text = str(src.get("text") or "")
        if ent not in text and metric not in text:
            continue
        idx = text.find(ent) if ent in text else text.find(metric)
        snippet = text[max(0, idx - 120):idx + 220].replace("\n", " ")
        score = text.count(ent) + text.count(metric)
        out.append({
            "title": src.get("title") or src.get("url") or "",
            "url": src.get("url") or "",
            "snippet": snippet.strip(),
            "official": bool(src.get("official")),
            "source": "local",
            "score": score,
        })
    out.sort(key=lambda r: (r["official"], r["score"]), reverse=True)
    return out[:3]


def _strip_tags(s):
    return html_mod.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def bing_search(query, cfg):
    url = ("https://www.bing.com/search?" +
           urllib.parse.urlencode({"q": query, "setlang": "zh-CN",
                                   "count": int(cfg.get("max_results") or 8)}))
    req = urllib.request.Request(
        url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
    with urllib.request.urlopen(req, timeout=int(cfg.get("timeout_seconds") or 12)) as r:
        page = r.read(1_500_000).decode("utf-8", "replace")
    chunks = re.split(r'<li class="b_algo"', page)[1:]
    out = []
    for ch in chunks:
        m = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', ch, re.S)
        if not m:
            continue
        href = html_mod.unescape(m.group(1))
        title = _strip_tags(m.group(2))
        sm = re.search(r"<p[^>]*>(.*?)</p>", ch, re.S)
        snippet = _strip_tags(sm.group(1)) if sm else _strip_tags(ch)[:300]
        if not href.startswith("http"):
            continue
        out.append({
            "title": title, "url": href, "snippet": snippet,
            "official": _is_official(href, cfg),
            "source": "bing",
        })
        if len(out) >= int(cfg.get("max_results") or 8):
            break
    return out


def page_evidence(results, claim, cfg):
    """抓结果页正文，只保留「实体 + 指标 + 数字」同句的句子。

    搜索摘要经常把不同主体的数字拼在一段里（「全球出货量 3.28 亿」
    和「小米出货量 1.69 亿」）。摘要层面很容易张冠李戴；正文句子层面
    才能看清主语。抓不到就跳过，不影响摘要层的结论。
    """
    ent = str(claim.get("entity") or "")
    metric = str(claim.get("metric") or "")
    out = []
    for r in (results or [])[:int(cfg.get("fetch_pages") or 0)]:
        url = r.get("url") or ""
        if not url.startswith("http"):
            continue
        try:
            text = fetch_url(url, timeout=min(10, int(cfg.get("timeout_seconds") or 12)),
                             max_bytes=800_000)
        except Exception:                                   # noqa: BLE001
            continue
        keep = []
        for sent in re.split(r"(?<=[。！？；])|\n", text):
            s = sent.strip()
            if len(s) < 8 or len(s) > 300:
                continue
            if ent and ent not in s:
                continue
            if not _metric_in(s, metric):
                continue
            if not regex_check.extract_numbers(s):
                continue
            keep.append(s)
            if len(keep) >= 4:
                break
        if keep:
            out.append({
                "title": r.get("title"), "url": url,
                "snippet": "。".join(keep)[:3000],
                "official": bool(r.get("official")),
                "source": "page",
            })
    return out
    """360 移动版结果页：桌面版容易触发「访问异常」，移动版目前稳定且摘要更全。"""
    url = "https://m.so.com/s?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
    with urllib.request.urlopen(req, timeout=int(cfg.get("timeout_seconds") or 12)) as r:
        page = r.read(1_800_000).decode("utf-8", "replace")
    out, seen = [], set()
    for ch in re.split(r'class="g-card res-list', page)[1:]:
        m = re.search(r'<h3[^>]*class="res-title"[^>]*>(.*?)</h3>', ch, re.S)
        if not m:
            continue
        direct = re.search(r'data-pcurl="([^"]+)"', ch)
        href = html_mod.unescape(direct.group(1)) if direct else ""
        if not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        title = _strip_tags(m.group(1))
        sm = (re.search(r'<div class="summary[^"]*"[^>]*>(.*?)</div>', ch, re.S)
              or re.search(r'<p class="g-main summary"[^>]*>(.*?)</p>', ch, re.S))
        snippet = _strip_tags(sm.group(1)) if sm else ""
        out.append({
            "title": title, "url": href, "snippet": snippet,
            "official": _is_official(href, cfg),
            "source": "so360",
        })
        if len(out) >= int(cfg.get("max_results") or 8):
            break
    return out


def so360_mobile_search(query, cfg):
    """360 移动版结果页：桌面版容易触发「访问异常」，移动版目前稳定且摘要更全。"""
    url = "https://m.so.com/s?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
    with urllib.request.urlopen(req, timeout=int(cfg.get("timeout_seconds") or 12)) as r:
        page = r.read(1_800_000).decode("utf-8", "replace")
    if "访问异常" in page:
        return []
    out, seen = [], set()
    for ch in re.split(r'class="g-card res-list', page)[1:]:
        m = re.search(r'<h3[^>]*class="res-title"[^>]*>(.*?)</h3>', ch, re.S)
        if not m:
            continue
        direct = re.search(r'data-pcurl="([^"]+)"', ch)
        href = html_mod.unescape(direct.group(1)) if direct else ""
        if not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        title = _strip_tags(m.group(1))
        sm = (re.search(r'<div class="summary[^"]*"[^>]*>(.*?)</div>', ch, re.S)
              or re.search(r'<p class="g-main summary"[^>]*>(.*?)</p>', ch, re.S))
        snippet = _strip_tags(sm.group(1)) if sm else ""
        out.append({
            "title": title, "url": href, "snippet": snippet,
            "official": _is_official(href, cfg),
            "source": "so360",
        })
        if len(out) >= int(cfg.get("max_results") or 8):
            break
    return out


def so360_search(query, cfg):
    """360 搜索的 HTML 结果页。

    选它而不是 Bing 的原因很实际：同一 query 下 Bing 对中文垂类数据
    （月活/出货量）经常只返回品牌官网导航页，360 的摘要里能直接看到
    「豆包月活达 1.72 亿」这类可比数值。搜索质量优先于「用大厂」。
    """
    url = "https://www.so.com/s?" + urllib.parse.urlencode(
        {"q": query, "src": "srp"})
    req = urllib.request.Request(
        url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
    with urllib.request.urlopen(req, timeout=int(cfg.get("timeout_seconds") or 12)) as r:
        page = r.read(1_800_000).decode("utf-8", "replace")
    out = []
    for ch in re.split(r'<li class="res-list"', page)[1:]:
        m = re.search(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', ch, re.S)
        if not m:
            continue
        direct = re.search(r'data-mdurl="([^"]+)"', ch)
        href = html_mod.unescape(direct.group(1) if direct else m.group(1))
        if not href.startswith("http") or "so.com/link" in href:
            continue
        # AI 聚合页不是原文，不能当来源
        if "ai.so.com" in href:
            continue
        title = _strip_tags(m.group(2))
        sm = (re.search(r'data-result-summary[^>]*>(.*?)</span>', ch, re.S)
              or re.search(r'<p class="res-desc"[^>]*>(.*?)</p>', ch, re.S)
              or re.search(r'class="res-list-summary"[^>]*>(.*?)</span>', ch, re.S))
        snippet = _strip_tags(sm.group(1)) if sm else ""
        out.append({
            "title": title, "url": href, "snippet": snippet,
            "official": _is_official(href, cfg),
            "source": "so360",
        })
        if len(out) >= int(cfg.get("max_results") or 8):
            break
    return out


def sogou_mobile_search(query, cfg):
    """搜狗移动版：不逐条解析卡片，直接把结果页正文当一个大摘要。

    搜索结果页本身就是「多篇摘要的集合」；judge 会按分句要求
    「实体 + 指标 + 数字」同句，因此不需要精确的卡片边界。
    引用指向搜索结果页，读者可以点开复核。
    """
    url = "https://m.sogou.com/web/searchList.jsp?" + urllib.parse.urlencode(
        {"keyword": query})
    req = urllib.request.Request(
        url, headers={
            "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                           "Mobile/15E148 Safari/604.1"),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
    with urllib.request.urlopen(req, timeout=int(cfg.get("timeout_seconds") or 12)) as r:
        page = r.read(1_800_000).decode("utf-8", "replace")
    text = html_to_text(page)
    if not text or "SourceVerifyCode" in text or "访问异常" in text:
        return []
    return [{
        "title": f"搜狗搜索结果：{query}",
        "url": url,
        "snippet": text[:20000],
        "official": False,
        "source": "sogou",
    }]


def web_search(query, cfg):
    """按顺序尝试多个引擎；任一有结果即返回。"""
    errors = []
    for name, fn in (("so360_m", so360_mobile_search), ("so360", so360_search),
                     ("sogou_m", sogou_mobile_search), ("bing", bing_search)):
        if name not in (cfg.get("providers") or ["so360_m"]):
            continue
        try:
            hits = fn(query, cfg)
            if hits:
                return hits
            errors.append(f"{name}: 0 条")
        except Exception as e:                              # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {e}")
    if errors:
        cfg.setdefault("_errors", []).extend(errors)
    return []


# ---------------------------------------------------------------------------
# 判定：支持 / 反驳 / 未找到
# ---------------------------------------------------------------------------
def _base(num, unit):
    if unit == "亿":
        return float(num) * 1e8
    if unit == "万":
        return float(num) * 1e4
    return float(num)


def _disp(num, unit):
    return f"{num:g}{unit or ''}"


def _unit_eq(a, b):
    a, b = (a or ""), (b or "")
    if a == b or {a, b} <= {"%", "％"}:
        return True
    # 亿/万只是书写单位，换算成基数后可以比
    return {a, b} <= {"亿", "万"}


def _near(text, start, end, needles, span=36):
    ctx = text[max(0, start - span):end + span]
    return any(n and n in ctx for n in needles)


METRIC_ALIASES = {
    "月活": ("月活", "月活跃用户", "MAU"),
    "日活": ("日活", "日活跃用户", "DAU"),
    "出货量": ("出货量", "出货", "发货量"),
    "销量": ("销量", "销售量"),
    "营收": ("营收", "营业收入", "收入"),
    "收入": ("收入", "营收"),
    "净利润": ("净利润", "净利"),
    "市场份额": ("市场份额", "市占率", "份额"),
    "占比": ("占比", "比例", "份额"),
    "价格": ("价格", "售价", "均价", "定价"),
    "用户数": ("用户数", "用户规模", "用户量"),
}


def _metric_in(text, metric):
    return any(a in text for a in METRIC_ALIASES.get(metric, (metric,)))


def _clause(text, start, end):
    """数字所在的单个分句。

    这是「同一指标」判断的关键：摘要里「2024 年第四季度全球智能手机
    出货量达到 3.28 亿部」和「小米 2024 年出货量 1.69 亿部」是两个分句，
    不能因为标题里出现「小米」就把 3.28 亿当成小米的出货量。
    逗号也要切：搜索结果页里「全年营收 3659 亿，手机出货量全球前三」
    是两个指标，不能把营收当出货量。
    """
    left = 0
    for m in re.finditer(r"[。！？；;，,、\n]", text[:start]):
        left = m.end()
    right = len(text)
    m = re.search(r"[。！？；;，,、\n]", text[end:])
    if m:
        right = end + m.start()
    return text[left:right]


def judge(claim, results, cfg):
    """只看「同一实体 + 同一指标 + 同一单位」的数字；宁可不判，也不硬判。"""
    # 同一 URL 在搜索结果里可能出现多次（聚合页/重复块），去重后再判
    dedup, seen_url = [], set()
    for r in results or []:
        u = r.get("url") or ""
        if u and u in seen_url:
            continue
        seen_url.add(u)
        dedup.append(r)
    results = dedup
    ent = str(claim.get("entity") or "")
    metric = str(claim.get("metric") or "")
    unit = str(claim.get("unit") or "")
    value = claim.get("value")
    citations = []
    supports, contra = [], []

    for r in results or []:
        text = f"{r.get('title') or ''}。{r.get('snippet') or ''}"
        if ent and ent not in text:
            continue
        if claim.get("kind") == "date":
            target = int(value)
            for ym in YEAR_RE.finditer(text):
                clause = _clause(text, ym.start(), ym.end())
                if ent not in clause or not _metric_in(clause, metric):
                    continue
                y = int(ym.group(1))
                if y == target:
                    supports.append((dict(r, snippet=clause), y, f"{y}年"))
                elif r.get("official"):
                    contra.append((dict(r, snippet=clause), y, f"{y}年"))
            continue
        for n in regex_check.extract_numbers(text):
            if not _unit_eq(n.get("unit"), unit):
                continue
            clause = _clause(text, n["start"], n["end"])
            # 实体和指标必须与数字在**同一个分句**里；只在标题/前一句出现不算。
            if ent not in clause:
                continue
            if not _metric_in(clause, metric):
                continue
            # 年份不符的句子直接跳过：2023 年的 1.46 亿不能拿来核 2024 年的数。
            # 用数字前 80 字的窗口找年份——「小米手机 2024 年出货量…其中出货量
            # 达到 1.69 亿」这种写法，年份在主句前半段，不在数字所在的分句里。
            if claim.get("year"):
                ywin = text[max(0, n["start"] - 80):n["end"] + 20]
                years = YEAR_RE.findall(ywin)
                if years and str(claim["year"]) not in years:
                    continue
            # 「预计/目标」是预测值，不拿来反驳事实陈述
            if re.search(r"(预计|预估|目标|有望|计划)", clause):
                continue
            # 摘要不是正文：实体必须紧贴数字（30 字内），指标必须紧贴数字
            # 前面（14 字内）。否则「2024 年全年营收 3659 亿」会被当成
            # 「出货量」的证据——搜索结果页里这种不同指标的句子挨在一起。
            if r.get("source") != "page":
                window = text[max(0, n["start"] - 30):n["end"] + 5]
                if ent not in window:
                    continue
                if not _metric_in(text[max(0, n["start"] - 14):n["start"]], metric):
                    continue
            got = _base(n["num"], n.get("unit"))
            want = _base(value, unit)
            ratio = got / want if want else 0
            if 0.5 <= ratio <= 2:
                supports.append((dict(r, snippet=clause), got,
                                 _disp(n["num"], n.get("unit"))))
            elif ratio > 2.5 or ratio < 0.4:
                contra.append((dict(r, snippet=clause), got,
                               _disp(n["num"], n.get("unit"))))

    def _cite(r, got, display=None):
        return {"title": r.get("title"), "url": r.get("url"),
                "snippet": (r.get("snippet") or "")[:240],
                "official": bool(r.get("official")),
                "source": r.get("source"), "value": got,
                "display": display}

    def _cites(pairs, limit=2):
        out, seen = [], set()
        for r, got, disp in pairs:
            u = r.get("url") or r.get("title")
            if u in seen:
                continue
            seen.add(u)
            out.append(_cite(r, got, disp))
            if len(out) >= limit:
                break
        return out

    if supports:
        r, got, disp = supports[0]
        return {
            "status": "supported",
            "method": r.get("source") or "search",
            "confidence": 0.9 if r.get("official") else 0.75,
            "value": got, "display": disp, "sources": _cites(supports),
            "reason": "检索到的来源与原文数值一致（容差 2 倍内）",
            "query": build_query(claim),
        }
    if contra:
        official = [x for x in contra if x[0].get("official")]
        domains = {urllib.parse.urlparse(x[0].get("url") or "").hostname for x in contra}
        # 一条来源也允许反驳，但必须满足：数字与实体/指标在同一段上下文里
        # （judge 已做 _near 校验），且置信度降到 0.7 并明确提示「建议复核」。
        # 两条独立来源或一条权威来源，才给 0.8~0.9。
        if official or len(domains) >= 2 or contra:
            r, got, disp = (official or contra)[0]
            conf = 0.9 if official else (0.8 if len(domains) >= 2 else 0.7)
            return {
                "status": "refuted",
                "method": r.get("source") or "search",
                "confidence": conf,
                "value": got,
                "display": disp,
                "sources": _cites(official or contra),
                "reason": ("权威来源与原文数值不一致" if official else
                           ("两个独立来源与原文数值不一致" if len(domains) >= 2 else
                            "检索来源与原文数值不一致（单一来源，建议复核）")),
                "query": build_query(claim),
            }
    return {
        "status": "unverified", "method": "search", "confidence": 0.0,
        "value": None, "sources": _cites([(r, None, None) for r in (results or [])], limit=2),
        "reason": ("检索到页面，但没有找到「同一实体 + 同一指标」的可比数值"
                   if results else "没有检索到相关来源"),
        "query": build_query(claim),
    }


def verify(claim, cfg=None, web=True, provider=None):
    cfg = cfg or load_config()
    results = []
    used = []
    if "local" in (cfg.get("providers") or ["local"]):
        local = local_search(build_query(claim), claim, cfg)
        if local:
            results += local
            used.append("local")
            # 本地来源库是人工/已审核沉淀的，优先级高于公开搜索：
            # 本地能判就直接判，别让一条搜索摘要覆盖掉确定的来源。
            lv = judge(claim, local, cfg)
            if lv.get("status") in ("supported", "refuted"):
                lv["providers"] = ["local"]
                return lv
    need_web = web and any(p in (cfg.get("providers") or [])
                           for p in ("so360_m", "so360", "sogou_m", "bing"))
    if need_web and not any(x.get("official") for x in results):
        try:
            fn = provider or web_search
            webhits = fn(build_query(claim), cfg)
            if webhits:
                results += webhits
                used.append("web")
                # 摘要层容易把「全球出货量」和「小米出货量」拼在一起；
                # 抓正文，只保留实体+指标+数字同句的句子再判一次。
                pages = page_evidence(webhits, claim, cfg)
                if pages:
                    pv = judge(claim, pages, cfg)
                    if pv.get("status") in ("supported", "refuted"):
                        pv["providers"] = used + ["page"]
                        return pv
                    # 正文没判出来（比如首条是官网介绍页），继续用摘要判；
                    # 摘要层的实体必须紧贴数字，3.28 亿那种张冠李戴会被过滤。
        except Exception as e:                              # noqa: BLE001
            cfg.setdefault("_errors", []).append(f"{type(e).__name__}: {e}")
    verdict = judge(claim, results, cfg)
    verdict["providers"] = used
    return verdict


# ---------------------------------------------------------------------------
# 接入审查：新增事实 issue / 已核验事实 / 未找到清单
# ---------------------------------------------------------------------------
def local_verdict(claim, cfg):
    """人工来源库能判就直接判；优先级高于已学结论与缓存。

    本地来源库是人工/已审核沉淀的，新加一页就应该覆盖旧的检索结论；
    否则「上次学到的值」会挡住「这次刚补的来源」。
    """
    if "local" not in (cfg.get("providers") or []):
        return None
    hits = local_search(build_query(claim), claim, cfg)
    if not hits:
        return None
    v = judge(claim, hits, cfg)
    if v.get("status") in ("supported", "refuted"):
        v["providers"] = ["local"]
        return v
    return None


def review_claims(s, text, sentences, assertions, enabled=True, web=True,
                  provider=None, max_claims=None):
    """在报告定稿前跑一遍；返回 report["research"] 块。"""
    cfg = load_config()
    if not enabled:
        return {"enabled": False, "reason": "本次以 --no-research 运行"}
    limit = int(max_claims if max_claims is not None
                else cfg.get("max_claims_per_doc") or 3)
    claims = extract_claims(sentences, assertions, max_claims=limit)
    block = {
        "enabled": True, "claims_found": len(claims), "checked": 0,
        "supported": [], "refuted": [], "unverified": [],
        "cache_hits": 0, "learned_hits": 0, "local_hits": 0,
        "needs_confirm": [],
        "providers": cfg.get("providers") or [],
        "errors": [], "items": [],
    }
    t0 = time.perf_counter()
    for c in claims:
        if time.perf_counter() - t0 > float(cfg.get("max_seconds") or 60):
            block["stopped_early"] = True
            break
        local = local_verdict(c, cfg)
        learned = None if local else learned_lookup(c)
        cached = None if (local or learned) else cache_get(c)
        if local:
            verdict = local
            block["local_hits"] += 1
            method = "local"
        elif learned:
            verdict = learned
            block["learned_hits"] += 1
            method = "learned"
        elif cached:
            verdict = cached
            block["cache_hits"] += 1
            method = "cache"
        else:
            verdict = verify(c, cfg, web=web, provider=provider)
            method = "search"
            ttl = (90 if verdict.get("status") in ("supported", "refuted")
                   else 1)
            cache_put(c, verdict, ttl)
            learn_verdict(c, verdict)
        block["checked"] += 1
        srcs = verdict.get("sources") or []
        item = {
            "quote": c["quote"], "entity": c["entity"], "metric": c["metric"],
            "kind": c["kind"], "value": c["value"], "unit": c["unit"],
            "status": verdict.get("status"), "reason": verdict.get("reason"),
            "display": verdict.get("display"),
            "query": verdict.get("query"), "method": method,
            "providers": verdict.get("providers") or [],
            "sources": srcs[:2],
        }
        block["items"].append(item)
        if verdict.get("status") == "refuted":
            block["refuted"].append(item)
            got = verdict.get("value")
            unit = c.get("unit") or ""
            label = (verdict.get("display")
                     or (f"{got:.6g}{unit}" if isinstance(got, (int, float)) else str(got)))
            authoritative = float(verdict.get("confidence") or 0) >= 0.8
            if not authoritative:
                # 单一来源：只进「待人工确认」，不进整改单。
                # 搜索摘要里的口径（累计/单季、全球/中国）经常和原文不同，
                # 直接写成 major 会把「可能错」冒充成「确定错」。
                item["needs_confirm"] = True
                block["needs_confirm"].append(item)
            if authoritative:
                fix = {
                    "action": "replace", "where": f"第 {c['sentence_index']} 句",
                    "from": c["quote"],
                    "to": (c["quote"].replace(c.get("raw") or "", label, 1)
                           if c.get("raw") else None),
                    "topic": f"{c['entity']}{c['metric']}".strip(),
                    "consequence": "对外发布构成事实性错误；被引用、被追责时无法用「笔误」解释。",
                    "how": f"改为「{label}」并按来源标注统计口径与时间。",
                    "has_rewrite": bool(c.get("raw")),
                    "summary": f"把「{c.get('raw') or c['quote'][:30]}」改为「{label}」",
                }
            else:
                # 单一非权威来源：可以提示「检索到不同数值」，但不能替用户拍板
                # 改成哪个——口径（累计/单季、全球/中国）可能不同，硬改写就是造新错。
                fix = {
                    "action": "manual", "where": f"第 {c['sentence_index']} 句",
                    "from": c["quote"], "to": None,
                    "topic": f"{c['entity']}{c['metric']}".strip(),
                    "consequence": "对外发布构成事实性错误；被引用、被追责时无法用「笔误」解释。",
                    "how": f"检索到 {label}（{srcs[0].get('url') if srcs else '来源见报告'}），"
                           f"与原文 {c.get('raw')} 不一致；先确认统计口径与年份，"
                           f"确认后改为 {label} 并注明来源。",
                    "has_rewrite": False,
                    "summary": f"先确认口径：检索到 {label}，与原文 {c.get('raw')} 不一致",
                }
            ev = (f"外部检索核验：{verdict.get('reason')}；"
                  f"来源 {srcs[0].get('title') if srcs else '—'}"
                  f"（{srcs[0].get('url') if srcs else '无链接'}）显示 "
                  f"{label}，原文为 {c.get('raw')}"
                  + ("。已按来源改写。" if authoritative
                     else "。口径可能不同，未自动改写。"))
            suggestion = (f"改为「{label}」并注明来源：{srcs[0].get('url') if srcs else '（检索来源见报告）'}"
                          if authoritative else
                          f"检索到不同数值（{label}），请先确认口径后再改写；"
                          f"来源：{srcs[0].get('url') if srcs else '见报告'}")
            issue = s.make_issue(
                "fact", "FACT-001", c["quote"], f"第 {c['sentence_index']} 句",
                ev, f"research:{method}",
                (float(verdict.get("confidence") or 0.8)
                 if authoritative else 0.65),
                suggestion, basis="research",
                severity=("major" if authoritative else "minor"))
            issue["fix"] = fix
            issue["citations"] = [{"citation": f"外部检索：{x.get('title')}",
                                   "url": x.get("url")} for x in srcs[:2]]
            stored = s.add_issue(issue)
            if stored is not issue:
                # 规则层已经报过同一句：合并证据，但把检索到的改写补上
                if not stored.get("fix"):
                    stored["fix"] = fix
                stored.setdefault("citations", issue["citations"])
            s.decision(f"外部检索反驳 {c['entity']}{c['metric']}：{c.get('raw')} → {label}",
                       "FACT-001", verdict.get("reason") or "",
                       ["按原文保留", "标为待核验"],
                       ("检索来源给出不同数值；不直接改用户原文，只给改写建议"
                        if authoritative else
                        "单一来源反驳，已转入待人工确认，不进整改单"))
        elif verdict.get("status") == "supported":
            block["supported"].append(item)
            s.verified_facts.append(
                f"{c['entity']}{c['metric']} {c.get('raw')}"
                f"（外部核验：{(srcs[0].get('title') if srcs else '来源')[:40]}"
                f"，{srcs[0].get('url') if srcs else ''}）")
        else:
            block["unverified"].append(item)
    block["errors"] = list(cfg.get("_errors") or [])
    block["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    # 已核验的句子不再重复报「无出处/无口径」：用户要的是「是否属实」，
    # 不是「这句话没有引用」。数据不丢，进 suppressed 并写明由哪条核验吸收。
    if block["checked"]:
        covered = [str(c.get("quote") or "") for c in claims]
        kept = []
        for it in s.issues:
            rid = str(it.get("rule_id") or "")
            q = re.sub(r"\s+", "", str(it.get("quote") or ""))
            if rid in ("FACT-002", "FACT-004", "COMP-004") and any(
                    q and (q in re.sub(r"\s+", "", c) or
                           re.sub(r"\s+", "", c) in q) for c in covered):
                s.folded.append(dict(
                    it, folded_into="FACT-001",
                    fold_reason="该句已做外部检索核验，不再重复报「无出处/无口径」"))
                continue
            kept.append(it)
        s.issues = kept
    s.tool("research", f"库外事实主张 {len(claims)} 条",
           f"支持 {len(block['supported'])} / 反驳 {len(block['refuted'])} / "
           f"未找到 {len(block['unverified'])} / 缓存 {block['cache_hits']} / "
           f"已学 {block['learned_hits']} / 本地 {block['local_hits']}",
           block["latency_ms"],
           status="success" if claims else "skipped")
    return block


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="事实主张检索核验")
    ap.add_argument("claim", nargs="?", help="一句带数字/日期的事实主张")
    ap.add_argument("--no-web", action="store_true", help="只查本地来源库")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--add-source", metavar="URL", help="抓取一页存进 knowledge/sources/")
    ap.add_argument("--title", default="", help="配合 --add-source 的来源标题")
    ap.add_argument("--official", action="store_true", help="标记为权威来源")
    ap.add_argument("--clear-cache", action="store_true", help="清空检索缓存")
    ap.add_argument("--learned", action="store_true", help="列出已沉淀的可复用结论")
    args = ap.parse_args()

    if args.learned:
        rows = load_learned()
        print(f"已学结论 {len(rows)} 条（state/learned_facts.jsonl）：")
        for r in rows:
            print(f"  {r.get('entity')}{r.get('metric')} {r.get('display')}"
                  f"（{r.get('status')}，{r.get('learned_at')}）")
            for s in r.get("sources") or []:
                print(f"    - {s.get('title')} {s.get('url')}")
        return
    if args.clear_cache:
        print(f"已清空 {cache_clear()} 条检索缓存")
        return
    if args.add_source:
        info = add_source(args.add_source, title=args.title,
                          official=True if args.official else None)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return
    if not args.claim:
        ap.print_help()
        return

    cfg = load_config()
    sentences = [(args.claim, 0)]
    claims = extract_claims(sentences, assertions=[], max_claims=3)
    if not claims:
        print("没有抽到可核验的「实体 + 指标 + 数值」主张。")
        return
    claim = claims[0]
    verdict = verify(claim, cfg, web=not args.no_web)
    out = {"claim": claim, "verdict": verdict}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    print(f"主张：{claim['quote']}")
    print(f"实体：{claim['entity']}｜指标：{claim['metric']}｜"
          f"原文：{claim.get('raw')}")
    print(f"结论：{verdict['status']} —— {verdict.get('reason')}")
    for s in (verdict.get("sources") or [])[:3]:
        print(f"  - {s.get('title')} {s.get('url')}")
    print(f"查询：{verdict.get('query')}")


if __name__ == "__main__":
    main()
