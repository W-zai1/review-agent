#!/usr/bin/env python3
"""law_index.py —— 法条级索引与检索（零第三方依赖）

为什么要有这个文件
------------------
`tools/retrieve.py` 的 `grep_knowledge()` 只是一行 `if query in line`：
它能告诉你"某一行出现了某词"，但说不出"这是《劳动合同法》第十九条"。
审查报告要引用的不是"某行"，而是**条号 + 原文**——否则读者无法复核，
系统也只是把模型记忆换了个地方存放。

数据分层
--------
knowledge/laws/
├── index.json          元数据：法名/效力状态/施行日/来源 URL/抓取时间/条数
├── <id>.md             法条正文（一条一段，条头用「第X条　」）
└── synonyms.json       日常语 → 法律术语（"不想干了" → "解除劳动合同"）

正文与元数据分开的理由：法条是**慢变量**，但它的**效力状态**会变
（修正、废止）。改状态不该重写正文，改正文也不该丢来源记录。

关键约束（踩过才知道）
----------------------
1. **条号必须连续**：只灌了半部法（公网页面常常分页到第 50 条就没了）
   却当成全书用，会给出"查无此条"的错误结论——比不查更糟。
   `check()` 把条号连续性和 `article_count` 一致性当硬校验。
2. **正文里引用的条号不是条头**：「依照本法第四十七条规定…」在段首出现时
   会被朴素正则当成第 47 条。用"条号必须等于期望的下一条"来排除。
3. **检索到条文 ≠ 适用对条文**：本模块只负责"找到相关条文"，
   是否适用、有无例外仍要人/模型判断——所以返回结果里带章与节，
   让读者能自己看上下文。

CLI
---
    py -3 tools/law_index.py "试用期 六个月"        # 检索
    py -3 tools/law_index.py --article 19           # 看单条
    py -3 tools/law_index.py --check                # 校验库完整性
    py -3 tools/law_index.py --stat                 # 库概况
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAWS_DIR = os.path.join(ROOT, "knowledge", "laws")
INDEX_FILE = os.path.join(LAWS_DIR, "index.json")
SYNONYM_FILE = os.path.join(LAWS_DIR, "synonyms.json")

# 条头：段首「第X条」+ 分隔（全角空格 / 半角空格 / 制表）。允许无分隔但需后面跟正文。
_ART_HEAD = re.compile(r"^第([一二三四五六七八九十百零两]+)条(?:[　\s]+|(?=[^　\s]))")
_CHAPTER = re.compile(r"^#{2,3}\s*第([一二三四五六七八九十]+)([章节])[　\s]*(.*)$")
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


class LawLibraryError(Exception):
    """法条库自身有问题（缺元数据 / 条号不连续）。**必须报错，不能静默跳过**：
    静默的后果是报告照常生成，但引用的条文少了一半，没人发现。"""


def cn2int(s):
    """中文数字 → 整数（覆盖 1~999，法条够用）。也接受「19」这样的阿拉伯数字：
    CLI 上人习惯写 `--article 19`，只认中文会让这个参数永远失效。"""
    if not s:
        return None
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    # 允许直接粘「第十九条」：人从报告里复制条号时带「第」「条」两个字
    s = s.removeprefix("第").removesuffix("条").strip()
    if s.isdigit():
        return int(s)
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
        else:
            return None
    return total + section + num


def int2cn(n):
    """整数 → 中文数字（1~999）。用于把条号回写成「第十九条」。"""
    if not isinstance(n, int) or n <= 0:
        return ""
    d = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
    if n < 10:
        return d[n]
    if n < 20:
        return "十" + (d[n % 10] if n % 10 else "")
    if n < 100:
        return d[n // 10] + "十" + (d[n % 10] if n % 10 else "")
    hundred, rest = divmod(n, 100)
    out = d[hundred] + "百"
    if rest == 0:
        return out
    if rest < 10:
        return out + "零" + d[rest]
    return out + int2cn(rest)


def load_index(path=INDEX_FILE):
    if not os.path.exists(path):
        raise LawLibraryError(f"缺少法条库索引：{path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if "laws" not in data or not isinstance(data["laws"], list):
        raise LawLibraryError("index.json 缺少 laws 列表")
    return data


def _required_meta(law):
    """一部法必须交代清楚的五件事，少一件就不许入库。

    `status` 尤其不能省：引用已废止的条文，比不给依据更糟——
    读者会以为它有法律效力。
    """
    missing = [k for k in ("id", "name", "file", "status", "effective_date",
                           "source_url", "retrieved_at", "article_count")
               if not law.get(k)]
    if missing:
        raise LawLibraryError(
            f"法条元数据缺字段 {missing}（{law.get('id') or law.get('file')}）；"
            "来源/时效/条数缺一不可，否则无法复核")
    if law["status"] not in ("现行有效", "已修正", "已废止", "尚未生效"):
        raise LawLibraryError(
            f"{law['id']} 的 status={law['status']!r} 不在允许值内"
            "（现行有效/已修正/已废止/尚未生效）")


def split_articles(text):
    """把正文切成条：返回 [{no, no_cn, chapter, section, title, text}]。

    只认**顺序正确**的条头：正文里「依照本法第四十七条」出现在段首时，
    因为 47 ≠ 期望的下一条，会被当成普通段落并入当前条。
    """
    arts, chapter, section = [], "", ""
    expected = 1
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m_ch = _CHAPTER.match(line)
        if m_ch:
            kind, title = m_ch.group(2), m_ch.group(3).strip()
            if kind == "章":
                chapter, section = f"第{m_ch.group(1)}章　{title}", ""
            else:
                section = f"第{m_ch.group(1)}节　{title}"
            continue
        if line.startswith("#"):
            continue
        m = _ART_HEAD.match(line)
        if m:
            no = cn2int(m.group(1))
            if no == expected:
                body = line[m.end():].strip()
                cur = {"no": no, "no_cn": f"第{int2cn(no)}条",
                       "chapter": chapter, "section": section,
                       "paras": [body] if body else []}
                arts.append(cur)
                expected += 1
                continue
        if cur is not None:
            cur["paras"].append(line)
    for a in arts:
        a["text"] = "\n".join(a.pop("paras"))
        # 条目标题：取首句第一分句，供检索结果摘要用（不当作法律含义）
        head = re.split(r"[。；]", a["text"], maxsplit=1)[0]
        a["title"] = head[:60]
    return arts


def load_law(law):
    path = os.path.join(LAWS_DIR, law["file"])
    if not os.path.exists(path):
        raise LawLibraryError(f"{law['id']} 的正文文件不存在：{law['file']}")
    with open(path, encoding="utf-8") as f:
        arts = split_articles(f.read())
    return arts


def load_all(laws_dir=None):
    """加载全部法条 → (laws, articles)。articles 每条带 law_id/law_name。"""
    global LAWS_DIR
    if laws_dir:
        LAWS_DIR = laws_dir
    data = load_index(os.path.join(LAWS_DIR, "index.json"))
    laws, articles = [], []
    for law in data["laws"]:
        _required_meta(law)
        arts = load_law(law)
        if not arts:
            raise LawLibraryError(f"{law['id']} 切分后 0 条，正文格式不对")
        for a in arts:
            a["law_id"] = law["id"]
            a["law_name"] = law["name"]
            a["law_short"] = law.get("short") or law["name"]
            a["law_status"] = law["status"]
        articles.extend(arts)
        laws.append(dict(law, _loaded=len(arts)))
    return laws, articles


def check(laws_dir=None):
    """库完整性校验。返回 (ok, [错误])。**这是入库前的门禁。**"""
    errs = []
    try:
        laws, articles = load_all(laws_dir)
    except LawLibraryError as e:
        return False, [str(e)]
    for law in laws:
        got = law["_loaded"]
        want = int(law["article_count"])
        if got != want:
            errs.append(f"{law['id']}：声明 {want} 条，实际切出 {got} 条"
                        "（多半是正文不完整，例如页面分页只到第 50 条）")
        nums = [a["no"] for a in articles if a["law_id"] == law["id"]]
        gaps = [n for n in range(1, (max(nums) if nums else 0) + 1) if n not in nums]
        if gaps:
            errs.append(f"{law['id']}：条号缺 {gaps[:10]}"
                        f"{'…' if len(gaps) > 10 else ''}（共缺 {len(gaps)} 条）")
        if law["status"] == "已废止":
            errs.append(f"{law['id']}：状态为已废止，不应留在当前库中"
                        "（引用已废止条文比不给依据更糟——读者会以为它有效）；"
                        "如需留档请移出 knowledge/laws/ 另存")
    return (not errs), errs


def load_synonyms(path=SYNONYM_FILE):
    if not os.path.exists(path):
        return {"version": "none", "groups": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _expand_terms(query, syn):
    """query → (terms, expanded_from)。同义词命中时把法律术语一起加进来。

    纯关键词的最大漏洞是「术语 ≠ 日常语」：用户写"我不想干了"，
    法条写"劳动者解除劳动合同"，substring 一条都命中不了。
    """
    base = [t for t in re.split(r"[\s，,、;；。]+", (query or "").strip()) if t]
    terms, why = list(base), {}
    for g in syn.get("groups") or []:
        hit = [t for t in g.get("terms") or [] if any(t in b or b in t for b in base)]
        if hit:
            for e in g.get("expand") or []:
                if e not in terms:
                    terms.append(e)
                    why[e] = hit[0]
    return terms, why


def _score(art, terms, weights=None):
    """打分：命中词数 + 长词加权 + 章/节标题命中加成。

    只做可解释的打分，不用向量：命中哪几个词要能一条条列出来，
    否则"为什么选了这一条"没法向读者交代。

    `weights` 用来区分**用户自己写的词**和**同义词扩展出来的词**。
    不区分的话，长扩展词（「按月给予劳动者经济补偿」12 个字）会压过
    用户真正想搜的词——实测「竞业限制 不得超过二年」被排到第二十三条，
    而规定期限上限的是第二十四条。
    """
    weights = weights or {}
    hay = art["chapter"] + art["section"] + art["text"]
    hit, total = [], 0.0
    for t in terms:
        if not t:
            continue
        c = hay.count(t)
        if c:
            w = weights.get(t, 1.0)
            hit.append(t)
            total += len(t) * (1.0 + min(c, 4) * 0.15) * w
            if t in art["chapter"] or t in art["section"]:
                total += 1.5 * w
            if t in art["title"]:
                total += 1.0 * w
    return total, hit


def get_article(law_id, no, laws_dir=None):
    """按「法 + 条号」精确取条文。

    为什么需要它：检查器心里**知道自己要哪一条**（试用期上限就是第十九条）。
    用检索去"找回"自己要的那条，会在用词重叠时飘到隔壁条文——实测
    「竞业限制三年」被检索带到第二十三条（讲约定竞业限制），而不是
    真正规定期限上限的第二十四条。**确定的事不要交给检索猜。**
    """
    global LAWS_DIR
    if laws_dir:
        LAWS_DIR = laws_dir
    _laws, articles = load_all()
    for a in articles:
        if a["law_id"] == law_id and a["no"] == int(no):
            return a
    return None


def search(query, top_n=5, laws_dir=None, min_score=0.0):
    """关键词（+同义词扩展）检索，返回 top_n 条，带章条定位。

    多词查询做「全词命中加成」：搜「竞业限制 两年」时，同时出现两个词的
    第二十四条才是读者要的，只命中一个词的第二十三条不该排前面。
    """
    global LAWS_DIR
    if laws_dir:
        LAWS_DIR = laws_dir
    _laws, articles = load_all()
    syn = load_synonyms(os.path.join(LAWS_DIR, "synonyms.json"))
    terms, why = _expand_terms(query, syn)
    base = [t for t in re.split(r"[\s，,、;；。]+", (query or "").strip()) if t]
    # 用户写的词权重 1.0，同义词扩展出来的词 0.5：扩展是为了补召回，不是为了改排序
    weights = {t: (1.0 if t in base else 0.5) for t in terms}
    scored = []
    for a in articles:
        sc, hit = _score(a, terms, weights)
        if sc > min_score and hit:
            hay = a["chapter"] + a["section"] + a["text"]
            if len(base) > 1 and all(b in hay for b in base):
                sc += 3.0                      # 全词命中：多词查询的强信号
            scored.append(dict(a, score=round(sc, 2), matched=hit))
    scored.sort(key=lambda x: (-x["score"], x["law_id"], x["no"]))
    out = scored[:max(1, int(top_n))]
    for o in out:
        o["expanded_from"] = {k: v for k, v in why.items() if k in o["matched"]}
    return out


def citation(item, max_chars=400):
    """把命中条目渲染成可放进报告的引用串：「《劳动合同法》第十九条（第二章 …）：原文」。

    法名用简称、条号在前：读者要的是"第几条"，章只是帮助定位上下文。
    旧的写法把章名和条号粘成「第二章 劳动合同的订立第十九条」，条号被埋掉。
    """
    name = item.get("law_short") or item["law_name"]
    loc = f"《{name}》{item['no_cn']}"
    ctx = [x.replace("　", " ") for x in (item.get("chapter"), item.get("section")) if x]
    if ctx:
        loc += "（" + " / ".join(ctx) + "）"
    body = item["text"].replace("\n", " ")
    if len(body) > max_chars:
        body = body[:max_chars] + "…"
    return f"{loc}：{body}"


def stat(laws_dir=None):
    laws, articles = load_all(laws_dir)
    return {
        "laws": len(laws),
        "articles": len(articles),
        "detail": [{"id": l["id"], "name": l["name"], "status": l["status"],
                    "articles": l["_loaded"], "effective_date": l["effective_date"]}
                   for l in laws],
    }


def main():
    args = sys.argv[1:]
    if "--check" in args:
        ok, errs = check()
        for e in errs:
            print(f"[FAIL] {e}")
        print("法条库校验：" + ("通过" if ok else f"{len(errs)} 项不通过"))
        return 0 if ok else 1
    if "--stat" in args:
        print(json.dumps(stat(), ensure_ascii=False, indent=2))
        return 0
    if "--article" in args:
        i = args.index("--article")
        want = cn2int(args[i + 1]) if i + 1 < len(args) else None
        law_id = None
        if "--law" in args:
            j = args.index("--law")
            law_id = args[j + 1] if j + 1 < len(args) else None
        _laws, articles = load_all()
        hits = [a for a in articles
                if a["no"] == want and (not law_id or a["law_id"] == law_id)]
        if not hits:
            print(f"未收录：第 {want} 条" + (f"（{law_id}）" if law_id else ""))
            return 2
        for a in hits:
            print(citation(a, max_chars=100000))
        return 0
    q = " ".join(a for a in args if not a.startswith("--"))
    if not q:
        print(__doc__)
        return 0
    res = search(q, top_n=5)
    if not res:
        print(f"未命中任何条文：{q!r}")
        print("（命中 0 条 ≠ 没有相关规定；只说明本库未收录或措辞不匹配）")
        return 2
    for r in res:
        print(f"[{r['score']:>5}] {citation(r)}")
        print(f"        命中词：{'、'.join(r['matched'])}"
              + (f"；同义词扩展：{r['expanded_from']}" if r["expanded_from"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
