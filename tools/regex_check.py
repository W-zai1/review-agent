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
DATE_RE = re.compile(r"(\d{4})\s*年(?:\s*(\d{1,2})\s*月)?")
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
    out = []
    for m in DATE_RE.finditer(text):
        out.append({
            "raw": m.group(0),
            "year": int(m.group(1)),
            "month": int(m.group(2)) if m.group(2) else None,
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
