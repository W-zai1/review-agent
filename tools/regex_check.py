#!/usr/bin/env python3
"""regex_check.py —— 数字/日期/敏感词/风格 抽取 (Demo 工具)

可被 review.py 导入，也可 CLI 运行：
    python regex_check.py < file.txt
"""
import re
import sys
import json

# 千分位 1,360 / 1,234.5 也要认：会计与财报文本的标准写法
NUM_RE = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(亿|万|%|倍)?")
DATE_RE = re.compile(
    r"(?P<year>\d{4})\s*年(?:\s*(?P<month>\d{1,2})\s*月)?"
    r"|(?P<iso_year>\d{4})-(?P<iso_month>\d{1,2})-(?P<iso_day>\d{1,2})")
# 常见 emoji / 符号表情范围
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF\uFE0F]")
CASUAL = ["吧", "呢", "啊", "嘛", "哎", "哦", "呗", "哈", "咋", "甭", "酱紫"]
# 单字口语词会命中地名/场所名（哈尔滨、酒吧、网吧…），匹配前先剔除这些词
CASUAL_EXEMPT = re.compile(
    r"(哈尔滨|哈佛|哈哈|酒吧|网吧|氧吧|吧台|书吧|哈欠|哈萨克|哈希|哈工大|宁德时代|啊里)")
EMOTION = ["我觉着", "我觉得", "太棒了", "泪目", "哭死", "绝绝子", "yyds", "栓Q", "爱了爱了"]
# 无引号、中文键名、短值都是真实仓库里的常见写法，不能只认 "key = \"value\""
SECRET_RE = re.compile(
    r"(?:password|passwd|pwd|pw|secret|api[_-]?key|apikey|access[_-]?key|token|"
    r"密钥|口令|密码|凭据)\s*[:=]\s*"
    r"(?:[\"'][^\"']{3,}[\"']"          # 带引号（≥3）
    r"|[A-Za-z0-9_\-\.]{5,}"              # 无引号裸值（≥5，覆盖 abc12 / 1234567）
    r"|sk-[A-Za-z0-9]{6,})",                # sk- 前缀
    re.I,
)
# 赋值右值是这些占位/类型名时不是密钥（避免 password = false 之类误报）
SECRET_PLACEHOLDER = re.compile(
    r"^[\"']?(null|none|true|false|undefined|nil|str|string|int|bool|bytes|"
    r"os\.environ.*|getenv.*)[\"']?$", re.I)


def _is_placeholder(raw):
    tail = raw.split("=", 1)[-1].split(":", 1)[-1].strip()
    return bool(SECRET_PLACEHOLDER.match(tail))

# 零宽/不可见字符：复制粘贴合同里极常见，会让「争议」「360万」这类关键词与数字整段失配
INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")


def strip_invisible(text):
    """剥离不可见字符，返回 (清洗后文本, 剥离数量)。

    必须在**所有匹配之前**做：否则同一份文档会同时出现
    「条款存在却报缺失」（误报）和「金额被拆断导致合计漏检」（漏报）。
    """
    cleaned, n = INVISIBLE_RE.subn("", text)
    return cleaned, n


def strip_invisible_with_map(text):
    """剥离不可见字符，并给出「清洗后位置 → 原文位置」的映射。

    为什么需要映射：`strip_invisible` 只给清洗后的文本，匹配到的位置拿去切
    原文会错位。证据必须指向**原文**的真实偏移（报告要能回查原句），
    所以这里同时返回 `index_map`：`index_map[i]` 是 `cleaned[i]` 在原文的下标。

    与 `strip_invisible` 共用同一个 `INVISIBLE_RE`：判定「什么算不可见字符」
    只有一处，两个函数的结论不会分叉。
    """
    kept, index_map = [], []
    for position, char in enumerate(text):
        if INVISIBLE_RE.match(char):
            continue
        kept.append(char)
        index_map.append(position)
    return "".join(kept), index_map


CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2, "三": 3,
             "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7,
             "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}
CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}


def cn2num(text):
    """中文数字 → int；支持 万/亿 分组。三百六十万 = 3600000。无法解析返回 None。"""
    total = section = num = 0
    seen = False
    for ch in text:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
            seen = True
        elif ch in CN_UNITS:
            section += (num or 1) * CN_UNITS[ch]
            num = 0
            seen = True
        elif ch == "万":
            section = (section + num) * 10000
            total += section
            section = num = 0
            seen = True
        elif ch == "亿":
            section = (section + num) * 100000000
            total += section
            section = num = 0
            seen = True
        else:
            return None
    return total + section + num if seen else None


def extract_numbers(text):
    out = []
    for m in NUM_RE.finditer(text):
        out.append({
            "raw": m.group(0),
            "num": float(m.group(1).replace(",", "")),
            "unit": m.group(2) or "",
            "start": m.start(),
            "end": m.end(),
        })
    return out


def extract_dates(text):
    """抽取年份/年月；2026-10-27 起支持 ISO 写法「2025-01-01」。

    参考文献与政策文件普遍用 ISO 日期（「自 2025-01-01 施行」），
    旧实现只认「YYYY 年」，导致这一整类可核验的硬事实全部漏抽。
    """
    out = []
    for m in DATE_RE.finditer(text):
        if m.group("year") is not None:
            year, month = m.group("year"), m.group("month")
        else:
            year, month = m.group("iso_year"), m.group("iso_month")
        out.append({
            "raw": m.group(0),
            "year": int(year),
            "month": int(month) if month else None,
            "start": m.start(),
            "end": m.end(),
        })
    return out


def has_emoji(text):
    return bool(EMOJI_RE.search(text))


def find_casual(text):
    """口语词匹配：先剔除地名/场所名等豁免词，避免「酒吧」「哈尔滨」误报。"""
    probe = CASUAL_EXEMPT.sub("　", text)
    return [w for w in CASUAL if w in probe]


def find_emotion(text):
    return [w for w in EMOTION if w in text]


def find_secret(text):
    return [m.group(0) for m in SECRET_RE.finditer(text) if not _is_placeholder(m.group(0))]


def find_secret_spans(text):
    """返回带位置与字段名的密钥命中，供 lint / review 生成「行号级」issue。"""
    out = []
    for m in SECRET_RE.finditer(text):
        if _is_placeholder(m.group(0)):
            continue
        key = re.match(r"([A-Za-z_\u4e00-\u9fa5]+)", m.group(0))
        out.append({
            "raw": m.group(0),
            "key": key.group(1).lower() if key else "secret",
            "start": m.start(),
            "end": m.end(),
        })
    return out


# ---------------------------------------------------------------- 隐私：银行卡 / 住宅地址
#
# 为什么不用「16-19 位数字」这么简单的正则：订单号、合同编号、流水号也是长数字，
# 一律当银行卡报就是制造噪声（第五轮已证明噪声比漏报更伤人）。用 **Luhn 校验**
# 做结构判定：真实卡号必须过 Luhn，随机长数字只有约 1/10 能过，再叠加发卡行
# 首位（3/4/5/6）与长度约束，误报率可以压到接近 0。
CARD_CANDIDATE = re.compile(
    r"(?<![\d])(\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}(?:[\s\-]?\d{1,3})?)(?![\d])")
# Luhn 不通过的「四段式数字」只在**银行卡语境**里才算疑似卡号：
# 伪造/占位的卡号本来就过不了 Luhn（实测样本 6222 0131 0736 0306 校验和为 47），
# 但真实单据里的卡号仍然要拦——所以降级为「需人工确认」，而不是直接放过。
CARD_CONTEXT = re.compile(
    r"(卡号|账户|帐号|收款|付款|退款|转入|转至|打款|汇款|银行|信用卡|储蓄卡|开户|对公)")


def luhn_ok(digits):
    """Luhn（模 10）校验：银行卡/信用卡号的必要条件。"""
    if not digits or not digits.isdigit():
        return False
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def find_bank_cards(text):
    """返回疑似银行卡号命中（含位置与 Luhn 结果）。

    两条通道：
      ① Luhn 通过 + 16~19 位 + 发卡行首位 3/4/5/6 → 高置信直接判；
      ② Luhn 不通过，但**四段式分组**（有空格/连字符）且邻近出现银行卡语境词
         → 疑似伪造/占位卡号，仍要报，但明确标注需人工确认。
    """
    out = []
    for m in CARD_CANDIDATE.finditer(text):
        raw = m.group(1)
        digits = re.sub(r"[\s\-]", "", raw)
        if not 16 <= len(digits) <= 19:
            continue
        if digits[0] not in "3456":          # 发卡行首位；1/2/7/8/9 开头的多是内部编号
            continue
        luhn = luhn_ok(digits)
        if not luhn:
            if not re.search(r"[\s\-]", raw):
                continue                     # 无分隔的裸长数字更可能是内部编号，不猜
            ctx = text[max(0, m.start() - 24): m.end() + 24]
            if not CARD_CONTEXT.search(ctx):
                continue
        out.append({"raw": raw, "digits": digits, "luhn": luhn,
                    "start": m.start(), "end": m.end()})
    return out


# 住宅地址：必须同时出现「市区县 + 路/街/巷 + 门牌号」**和**「单元/室/栋/户」
# 才判为个人住址。公司注册地址常写成「XX 路 88 号 A 座 1801」，没有室/单元，
# 而合同里披露办公地址是正常的——只认带住宅单元信息的那种。
ADDR_ROAD = re.compile(
    r"[\u4e00-\u9fa5]{2,10}(?:区|县|镇|乡)[\u4e00-\u9fa5]{0,12}(?:路|街|大道|巷|弄|胡同)"
    r"\s*\d{1,4}\s*(?:号|弄)")
ADDR_UNIT = re.compile(r"\d{1,4}\s*(?:单元|栋|幢|号楼|层|室|户)")


def find_residence_addrs(text):
    out = []
    for m in ADDR_ROAD.finditer(text):
        tail = text[m.end(): m.end() + 30]
        if not ADDR_UNIT.search(tail):
            continue
        end = m.end() + (ADDR_UNIT.search(tail).end())
        out.append({"raw": text[m.start(): end], "start": m.start(), "end": end})
    return out


def main():
    text = sys.stdin.read()
    print(json.dumps({
        "numbers": extract_numbers(text),
        "dates": extract_dates(text),
        "emoji": has_emoji(text),
        "casual": find_casual(text),
        "emotion": find_emotion(text),
        "secret": find_secret(text),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
