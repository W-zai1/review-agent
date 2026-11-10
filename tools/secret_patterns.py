#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""secret_patterns.py —— 凭据正则的唯一来源。

为什么单独成模块：
    「什么算密钥」此前有两个定义处——`tools/regex_check.py` 的 `SECRET_RE`
    （文档/代码里的 `key = value` 赋值）与 `tools/secret_scan.py` 的
    `PATTERNS`（交付物里的 token 格式）。两边各自演进，新增一种 token
    时很容易只改一处，于是「扫描器说干净、审查却报密钥」或反之。

这里把两族正则放在同一张表里，但**不合并检测集合**：
    评审族（ASSIGNMENT_RE）：任意 `password: 123456` 都算，宁多勿漏，
        服务 `lint` / 安全审查 Agent 的逐文档检查；
    交付族（DELIVERY_PATTERNS）：只认确定的凭据格式，宁少勿滥，
        服务 CI 的交付包扫描（`tools/secret_scan.py`）。
    实测把评审族套到仓库受跟踪文件上会命中 50 处测试夹具与文档示例；
    合并会让 CI 常红，也会改变审查报告的 findings。两族的**用途不同**，
    共享的是「正则文本只有一份」，不是「一条规则两个场景通用」。

改名或调整顺序要同步：
    `tools/regex_check.py`（SECRET_RE / SECRET_PLACEHOLDER 的别名）
    `tools/secret_scan.py`（PATTERNS 的别名）
    `checks/test_secret_patterns.py`（身份与顺序断言）
"""
import re

# ---------------------------------------------------------------------------
# 评审族：文档与代码里的硬编码凭据赋值
# ---------------------------------------------------------------------------
# 无引号、中文键名、短值都是真实仓库里的常见写法，不能只认 "key = \"value\""
ASSIGNMENT_RE = re.compile(
    r"(?:password|passwd|pwd|pw|secret|api[_-]?key|apikey|access[_-]?key|token|"
    r"密钥|口令|密码|凭据)\s*[:=]\s*"
    r"(?:[\"'][^\"']{3,}[\"']"          # 带引号（≥3）
    r"|[A-Za-z0-9_\-\.]{5,}"              # 无引号裸值（≥5，覆盖 abc12 / 1234567）
    r"|sk-[A-Za-z0-9]{6,})",                # sk- 前缀
    re.I,
)
# 赋值右值是这些占位/类型名时不是密钥（避免 password = false 之类误报）
PLACEHOLDER_RE = re.compile(
    r"^[\"']?(null|none|true|false|undefined|nil|str|string|int|bool|bytes|"
    r"os\.environ.*|getenv.*)[\"']?$", re.I)

# ---------------------------------------------------------------------------
# 交付族：会进提交/发布包的 token 格式（顺序即扫描顺序，勿随意调整）
# ---------------------------------------------------------------------------
DELIVERY_PATTERNS = (
    ("OpenAI/DeepSeek 风格 API Key",
     re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("AWS Access Key",
     re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("GitHub Token",
     re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("Slack Token",
     re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API Key",
     re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Stripe Secret Key",
     re.compile(r"sk_live_[A-Za-z0-9]{20,}")),
    ("私钥文件",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("疑似硬编码凭据",
     re.compile(
         r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*"
         r"[\"'][^\"'\s]{20,}[\"']")),
)
