# -*- coding: utf-8 -*-
"""批次 1 的确定性工具：`BaseTool` 的真实实现（阶段 3）。

为什么不能直接把 `tools/*.py` 搬进来：
    那 42 个模块是「被 `review.py` import 的函数集合」，签名是
    `fn(session, text, rules, ctx)`——它们**知道自己在审查流程里**，会把副作用
    写进 Session。工具的定义正相反：给定入参就返回结果，不知道谁在调它
    （见 docs/TOOL_SPECIFICATION.md §1）。

    更要命的是依赖方向：`capabilities` 层不许 import `tools/`（ARCHITECTURE §3
    第 3 条，`checks/test_architecture_guard.py` 会扫出来）。所以这里的做法是
    **注入算法本体**：`MathCheckTool(arith_check.run_all)`。

    这样拆的好处是两条都不动：
        · 算法只有一份（仍是 `tools/arith_check.py`，行为不变）
        · 工具层不认识 `tools/`，能单独构造、单独测试、将来换实现只换注入的那个函数

本文件只收「纯函数、无 IO、无 Session」的工具。有 IO 的（lint 要读文件、
docx 要解压、检索要读索引）留到批次 2，理由与迁移批次表一致。
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .tools import BaseTool, Permission, ToolContext, ToolResult

# `run_all(text, active_rule_ids) -> list[dict]`，与 tools/arith_check.py、
# tools/logic_checks.py 的现有签名一致。不在这里定义 Protocol：
# 一个 Callable 别名就够，多一层抽象没人维护。
RunAll = Callable[[str, "Sequence[str] | None"], list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class TextCheckArgs:
    """所有「对一段文本跑确定性检查」的工具共用入参。"""

    text: str
    rule_ids: tuple[str, ...] | None = None


class _TextCheckTool(BaseTool):
    """把 `run_all` 形状的检查包成工具。子类只声明名字与描述。"""

    permission = Permission.ANALYZE
    arguments_type = TextCheckArgs

    def __init__(self, *, name: str, description: str, run_all: RunAll) -> None:
        self.name = name
        self.description = description
        self._run_all = run_all

    async def execute(self, arguments: TextCheckArgs,
                      context: ToolContext) -> ToolResult:
        text = arguments.text or ""
        if not text.strip():
            # 空输入不是「没问题」：把空当干净会让「审了个空文件」显示成 pass
            return ToolResult.failure("text 为空：没有可检查的内容（空输入不等于无问题）")
        # None = 不按规则过滤（跑全部）；空元组 = 一条都不跑。
        # 两者不能混：把 [] 归一成 None 会让「本次只启用 0 条规则」变成「跑全量」。
        rule_ids = (None if arguments.rule_ids is None
                    else tuple(arguments.rule_ids))
        findings = tuple(self._run_all(text, rule_ids))
        return ToolResult.success(
            data={"findings": findings, "count": len(findings)},
            # 命中的 quote 逐字来自待审文档：内容不可信，调用方只能当数据看
            untrusted=True,
        )


class MathCheckTool(_TextCheckTool):
    """数值一致性：合计、百分比、星期、时长、份额合计、单位换算。"""

    def __init__(self, run_all: RunAll) -> None:
        super().__init__(
            name="math.check",
            description=("对文本里的数字做确定性核算：合计是否等于分项之和、"
                         "百分比/份额是否自洽、星期与日期是否对得上、"
                         "时长与年限是否矛盾。纯规则，同输入同输出。"),
            run_all=run_all,
        )


class LogicCheckTool(_TextCheckTool):
    """逻辑矛盾：年龄与工龄、抽样与结论、绝对化断言、金额大小写。"""

    def __init__(self, run_all: RunAll) -> None:
        super().__init__(
            name="logic.check",
            description=("检测同一段文本内部的逻辑矛盾：年龄与工龄冲突、"
                         "抽样量不足以支撑结论、绝对化表述与限定条件并存、"
                         "金额大小写不一致。纯规则，同输入同输出。"),
            run_all=run_all,
        )


@dataclass(frozen=True, slots=True)
class RuleClassifyArgs:
    """规则执行方式分类的入参：把规则定义（YAML 读出来的 dict）传进来。"""

    rules: tuple[dict[str, Any], ...]


class RuleClassifyTool(BaseTool):
    """回答「这条规则是代码算的、还是声明式跑的、还是只在提示词里出现」。

    这个问题的答案决定了结论的可信度：`prompt_only` 的规则跨次可能漂移，
    报告里必须能让人看出差别（见 tools/rule_exec.py 的模块说明）。
    """

    name = "rules.classify"
    description = ("按规则的执行方式分类：deterministic（有专用执行器）/ "
                   "declarative（自带 pattern，由通用引擎跑）/ prompt_only（只在提示词里）。"
                   "纯查表，不读文件、不跑审查。")
    permission = Permission.READ
    arguments_type = RuleClassifyArgs

    def __init__(self, *, summarize: Callable[[Sequence[Mapping[str, Any]]], dict],
                 counts: Callable[[Sequence[Mapping[str, Any]]], dict]) -> None:
        self._summarize = summarize
        self._counts = counts

    async def execute(self, arguments: RuleClassifyArgs,
                      context: ToolContext) -> ToolResult:
        rules = tuple(arguments.rules or ())
        if not rules:
            return ToolResult.failure("rules 为空：没有可分类的规则")
        bad = [i for i, rule in enumerate(rules)
               if not isinstance(rule, Mapping) or not rule.get("rule_id")]
        if bad:
            return ToolResult.failure(
                f"第 {bad[:5]} 条规则缺少 rule_id：无法按执行方式归类")
        summary = self._summarize(list(rules))
        return ToolResult.success(data={
            "summary": summary,
            "counts": self._counts(list(rules)),
            # 未登记执行器的规则最需要被看见：它们是最不可复现的那批
            "prompt_only": tuple(summary.get("prompt_only") or ()),
        })


@dataclass(frozen=True, slots=True)
class IntegrityHashArgs:
    text: str


class IntegrityHashTool(BaseTool):
    """对一段内容算 SHA-256：引用去重、证据指纹、跨报告比对「同一段原文」。"""

    name = "integrity.hash"
    description = ("计算一段文本的 SHA-256（UTF-8 字节）。用于证据指纹、"
                   "引用去重、跨报告比对同一段原文。不读文件、不写盘。")
    permission = Permission.READ
    arguments_type = IntegrityHashArgs

    def __init__(self, *, sha256_bytes: Callable[[bytes], str]) -> None:
        self._sha256_bytes = sha256_bytes

    async def execute(self, arguments: IntegrityHashArgs,
                      context: ToolContext) -> ToolResult:
        payload = (arguments.text or "").encode("utf-8")
        return ToolResult.success(data={
            "sha256": self._sha256_bytes(payload),
            "size": len(payload),
        })


@dataclass(frozen=True, slots=True)
class PatternScanArgs:
    """声明式规则扫描：判定条件全在规则里，工具只做通用匹配。"""

    text: str
    rules: tuple[dict[str, Any], ...]
    dimension: str | None = None
    skip_comments: bool = True
    target_suffix: str = ""


class PatternScanTool(BaseTool):
    """执行带 `pattern` 的声明式规则（`require` / `guard` / `unless` 组合判断）。

    与旧 `pattern_rules.scan()` 共用同一个纯核心 `scan_hits()`：工具不造假 Session，
    但匹配语义必须完全一致——否则「流水线说有命中、工具说没有」无法解释。
    """

    name = "rules.pattern"
    description = ("按规则自带的 pattern 扫描文本，支持 require（同行必须同时出现）、"
                   "guard / unless（出现即放弃）与注释行去噪。"
                   "判定条件来自规则定义，工具本身不含业务规则。")
    permission = Permission.ANALYZE
    arguments_type = PatternScanArgs

    def __init__(self, *, scan_hits: Callable[..., tuple[int, list]]) -> None:
        self._scan_hits = scan_hits

    async def execute(self, arguments: PatternScanArgs,
                      context: ToolContext) -> ToolResult:
        text = arguments.text or ""
        if not text.strip():
            return ToolResult.failure("text 为空：没有可扫描的内容（空输入不等于无问题）")
        rules = tuple(arguments.rules or ())
        if not rules:
            return ToolResult.failure("rules 为空：没有可执行的规则")
        with_pattern = [r for r in rules if isinstance(r, Mapping) and r.get("pattern")]
        if not with_pattern:
            return ToolResult.failure(
                "rules 里没有任何带 pattern 的规则：本工具只执行声明式规则")
        bad = [r for r in with_pattern
               if not r.get("rule_id") or not r.get("dimension")]
        if bad:
            return ToolResult.failure(
                "带 pattern 的规则必须同时声明 rule_id 与 dimension，"
                f"缺失 {len(bad)} 条（否则结论无法归属到规则/维度）")

        total, hits = self._scan_hits(
            text, rules, dimension=arguments.dimension,
            skip_comments=bool(arguments.skip_comments),
            target_suffix=arguments.target_suffix or "")
        findings = tuple({
            "rule_id": hit["rule"]["rule_id"],
            "dimension": hit["rule"]["dimension"],
            "quote": hit["quote"],
            "location": f"第 {hit['lineno']} 行",
            "evidence": f"{hit['rule'].get('desc', '')}｜命中「{hit['matched']}」",
            "suggestion": hit["suggestion"],
            "confidence": hit["confidence"],
            "tool": "pattern_rule",
        } for hit in hits)
        return ToolResult.success(
            data={"findings": findings, "count": len(findings),
                  "rules_examined": total},
            untrusted=True,
        )


@dataclass(frozen=True, slots=True)
class TextRegexArgs:
    """隐形字符剥离 + 正则匹配。坐标一律给**原文**偏移。"""

    text: str
    pattern: str
    ignore_case: bool = False
    multiline: bool = False
    max_matches: int = 50


class TextRegexTool(BaseTool):
    """在文本里按正则找匹配，匹配前先剥掉零宽/不可见字符。

    为什么必须先剥离：复制粘贴来的合同里常混零宽字符，会让「市场占有率」
    这类关键词整段失配——「明明写了却没查出来」比报错更难发现。

    为什么坐标要映射回原文：证据要能回查原句。清洗后的偏移直接拿去切原文会错位
    （少掉几个零宽字符），所以命中的 `start` / `end` 是**原文**下标。
    """

    name = "text.regex"
    description = ("按正则扫描文本；扫描前剥离零宽/不可见字符（\\u200b 等），"
                   "返回原文坐标下的命中片段。用于关键词、数字格式、敏感结构等确定性匹配。")
    permission = Permission.ANALYZE
    arguments_type = TextRegexArgs

    def __init__(self, *, normalize_with_map:
                 Callable[[str], tuple[str, list[int]]]) -> None:
        self._normalize = normalize_with_map

    async def execute(self, arguments: TextRegexArgs,
                      context: ToolContext) -> ToolResult:
        text = arguments.text or ""
        if not text.strip():
            return ToolResult.failure("text 为空：没有可匹配的内容（空输入不等于无问题）")
        pattern = arguments.pattern or ""
        if not pattern:
            return ToolResult.failure("pattern 为空：没有可执行的匹配条件")

        flags = 0
        if arguments.ignore_case:
            flags |= re.IGNORECASE
        if arguments.multiline:
            flags |= re.MULTILINE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult.failure(f"pattern 不是合法正则：{exc}")

        cleaned, index_map = self._normalize(text)
        limit = max(1, int(arguments.max_matches or 50))
        matches: list[dict[str, Any]] = []
        truncated = False
        for found in compiled.finditer(cleaned):
            if len(matches) >= limit:
                truncated = True
                break
            start, end = found.start(), found.end()
            if end <= start:                 # 零宽匹配：没有可引用的原文片段
                continue
            original_start = index_map[start]
            original_end = index_map[end - 1] + 1
            matches.append({
                "match": found.group(0),
                "quote": text[original_start:original_end],
                "start": original_start,
                "end": original_end,
                "line": cleaned.count("\n", 0, start) + 1,
            })
        warnings = (f"命中超过上限 {limit} 条，已截断",) if truncated else ()
        return ToolResult.success(
            data={"matches": tuple(matches), "count": len(matches),
                  "matched_on_cleaned_text": cleaned != text},
            warnings=warnings,
            untrusted=True,
        )


@dataclass(frozen=True, slots=True)
class MatrixMatchArgs:
    """要求侧 vs 候选侧的逐项对照。

    `config` 给了就直接用（纯函数式）；不给就由注入的加载器去读配置——
    读文件这件事留在装配层，工具本身不做 IO。
    """

    purpose_text: str
    target_text: str
    config: dict[str, Any] | None = None


class MatrixMatchTool(BaseTool):
    """把「JD / 检查清单」与「候选材料」按维度逐项对照，给出覆盖与缺口。

    为什么不把「读配置」放进工具：`match_matrix.load_config()` 是文件 IO，
    而工具应当是「给定入参就出结果」。配置由装配层注入（`review.py` 负责），
    所以测试可以塞一份 `config`，完全不碰磁盘。
    """

    name = "matrix.match"
    description = ("把要求侧文本（JD / 招标要求 / 检查清单）与候选侧文本按维度"
                   "逐项对照，输出每项是满足 / 部分 / 缺失 / 超范围，以及覆盖率与缺口。"
                   "不是「打分」：只回答「要求有没有被覆盖」。")
    permission = Permission.ANALYZE
    arguments_type = MatrixMatchArgs

    def __init__(self, *, build: Callable[..., dict[str, Any]],
                 load_config: Callable[[], dict[str, Any]]) -> None:
        self._build = build
        self._load_config = load_config

    async def execute(self, arguments: MatrixMatchArgs,
                      context: ToolContext) -> ToolResult:
        purpose = arguments.purpose_text or ""
        target = arguments.target_text or ""
        if not purpose.strip():
            return ToolResult.failure(
                "purpose_text 为空：没有要求侧文本，逐项对照无从谈起")
        if not target.strip():
            return ToolResult.failure("target_text 为空：没有可对照的候选文本")

        config = arguments.config
        if config is None:
            config = self._load_config()
        matrix = self._build(purpose, target, config)
        warnings: tuple[str, ...] = ()
        if not matrix.get("available"):
            # 「没有矩阵」不是错误（往往只是没上传要求文件），但必须说出来，
            # 否则调用方会以为「对照过且全满足」。
            warnings = (f"对照矩阵未生成：{matrix.get('reason') or '原因未知'}",)
        return ToolResult.success(
            data={"matrix": matrix,
                  "coverage": matrix.get("coverage") or {},
                  "available": bool(matrix.get("available"))},
            warnings=warnings,
            untrusted=True,
        )


@dataclass(frozen=True, slots=True)
class LawCiteArgs:
    """法条比对：法条库可注入（`articles`），不注入才走加载器。"""

    text: str
    min_confidence: float = 0.6
    # 键写成 "law_id:条号"（如 "labor-contract:19"）：JSON 友好，也看得懂
    articles: dict[str, Any] | None = None


def _normalize_articles(raw: Mapping[str, Any]) -> dict[tuple[str, Any], Any]:
    """把 `{"labor-contract:19": {...}}` 还原成 `{("labor-contract", 19): {...}}`。"""
    out: dict[tuple[str, Any], Any] = {}
    for key, article in raw.items():
        law_id, _, number = str(key).partition(":")
        try:
            number_value: Any = int(number)
        except ValueError:
            number_value = number
        out[(law_id, number_value)] = article
    return out


class LawCiteTool(BaseTool):
    """把合同条款与法条原文比对（试用期上限 / 竞业限制期限 / 试用期工资）。

    缺条文时不是「没问题」：`skipped` 会说明「该项未执行」，这里把它提成 warning。
    """

    name = "law.cite"
    description = ("把文本里的试用期时长、竞业限制期限、试用期工资比例与法条原文比对，"
                   "给出结论与依据。法条库可由入参注入；缺条文时明确报「未执行」，"
                   "不当作「已核验」。")
    permission = Permission.ANALYZE
    arguments_type = LawCiteArgs

    def __init__(self, *, check: Callable[..., tuple[list, list, list]],
                 load_articles: Callable[[], dict[tuple[str, Any], Any]]) -> None:
        self._check = check
        self._load_articles = load_articles

    async def execute(self, arguments: LawCiteArgs,
                      context: ToolContext) -> ToolResult:
        text = arguments.text or ""
        if not text.strip():
            return ToolResult.failure("text 为空：没有可比对的条款内容")
        articles = (None if arguments.articles is None
                    else _normalize_articles(arguments.articles))
        if articles is None:
            articles = self._load_articles()
        issues, verified, skipped = self._check(
            text, arguments.min_confidence, articles=articles)
        return ToolResult.success(
            data={"issues": tuple(issues), "verified": tuple(verified),
                  "skipped": tuple(skipped)},
            warnings=tuple(skipped),        # 「没查」必须说出来，不能混进结论
            untrusted=True,
        )


@dataclass(frozen=True, slots=True)
class DocxFormatArgs:
    """Word 版式判定。给 `info` 就直接判（纯函数式），只给 `path` 才去解析。"""

    rule_ids: tuple[str, ...]
    path: str | None = None
    spec: dict[str, Any] | None = None
    info: dict[str, Any] | None = None


class DocxFormatTool(BaseTool):
    """按启用的 FORMAT-* 规则判版式（字体 / 字号 / 缩进 / 行距 / 标题 / 图表编号）。

    `rule_ids` 必须显式给：判哪几项**取决于本次启用的规则**，工具不替调用方猜。
    （`docx_format_checks.run_all(info, ridset, spec)` 里 `ridset` 就是这份清单。）

    阈值全部来自 `spec`（格式规格）；规格没写的那一项不判——没有委托方标准时
    「字号对不对」没有答案。
    """

    name = "docx.format"
    description = ("按启用的 FORMAT-* 规则检查 Word 版式：正文字体/字号、首行缩进、"
                   "行距、标题层级、图表编号一致性。阈值来自格式规格；"
                   "规格没规定的项不判。需 docx 解析产物或文档路径。")
    permission = Permission.ANALYZE
    arguments_type = DocxFormatArgs

    def __init__(self, *, run_all: Callable[..., list[dict[str, Any]]],
                 extract: Callable[[str], dict[str, Any]]) -> None:
        self._run_all = run_all
        self._extract = extract

    async def execute(self, arguments: DocxFormatArgs,
                      context: ToolContext) -> ToolResult:
        rule_ids = tuple(arguments.rule_ids or ())
        if not rule_ids:
            return ToolResult.failure(
                "rule_ids 为空：不指定要判哪几项，版式检查没有意义")

        info = arguments.info
        if info is None:
            path = arguments.path or ""
            if not path.strip():
                return ToolResult.failure(
                    "info 与 path 都没有：需要 docx 解析产物或文档路径")
            try:
                info = self._extract(path)
            except Exception as exc:            # noqa: BLE001 - 解析失败要给出明确错误
                return ToolResult.failure(
                    f"docx 解析失败（{path}）：{type(exc).__name__}: {exc}")
        if not isinstance(info, Mapping):
            return ToolResult.failure(
                f"info 必须是解析产物字典，实际 {type(info).__name__}")

        findings = tuple(self._run_all(info, set(rule_ids), arguments.spec))
        return ToolResult.success(
            data={"findings": findings, "count": len(findings),
                  "rules_examined": len(rule_ids)},
            untrusted=True,
        )


@dataclass(frozen=True, slots=True)
class TextLintArgs:
    """代码/文本 lint：入参是路径（外部 linter 按路径工作）。"""

    path: str


class TextLintTool(BaseTool):
    """对代码文件跑 ruff / eslint 并做密钥扫描。

    为什么入参是路径而不是文本：ruff / eslint 是**按文件**工作的外部命令，
    传一段字符串给它等于在临时目录里造文件——那层副作用不该藏在工具里。
    所以 `run_lint` 由装配层注入，工具只负责「校验入参 → 调 → 归一结果」。

    状态语义（沿用 `tools/lint.py`）：`ok` / `issues` / `skipped` / `failed`。
    `skipped` 不是成功（例如文件后缀不支持），要以 warning 讲出来；
    `failed` 是工具坏了，按失败返回。
    """

    name = "text.lint"
    description = ("对代码文件跑 ruff（Python）/ eslint（JS/TS）并做密钥扫描，"
                   "返回 lint 与密钥类问题。需要文件路径；外部工具缺失时如实报 skipped。")
    permission = Permission.ANALYZE
    arguments_type = TextLintArgs

    def __init__(self, *, run_lint: Callable[[str], dict[str, Any]]) -> None:
        self._run_lint = run_lint

    async def execute(self, arguments: TextLintArgs,
                      context: ToolContext) -> ToolResult:
        path = (arguments.path or "").strip()
        if not path:
            return ToolResult.failure("path 为空：lint 需要文件路径")
        try:
            result = self._run_lint(path)
        except Exception as exc:                # noqa: BLE001 - 外部工具异常要转成明确错误
            return ToolResult.failure(
                f"lint 执行失败（{path}）：{type(exc).__name__}: {exc}")
        if not isinstance(result, Mapping):
            return ToolResult.failure(
                f"run_lint 必须返回 dict，实际 {type(result).__name__}")

        status = str(result.get("status") or "")
        details = result.get("details")
        findings = tuple(result.get("findings") or ())
        if status == "failed":
            return ToolResult.failure(
                f"lint 执行失败（{path}）：{details or '外部工具返回失败'}")
        warnings = ()
        if status == "skipped":
            warnings = (f"lint 未执行：{details or '该类型文件不跑 lint'}",)
        return ToolResult.success(
            data={"file": result.get("file") or path, "status": status,
                  "details": details, "findings": findings,
                  "count": len(findings)},
            warnings=warnings,
            untrusted=True,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeRetrieveArgs:
    """知识库检索：法条与事实库分开取，`scope` 决定取哪一边。"""

    query: str
    top_n: int = 5
    scope: str = "laws"          # laws / facts / all


class KnowledgeRetrieveTool(BaseTool):
    """按关键词检索法条与事实库。

    与「审材料」的工具有一个关键区别：**知识库是系统自己的资产，不是待审文档**，
    所以结果 `untrusted=False`。把两者混为一谈，会让「知识库里的某句话」
    被当成「材料作者试图影响审查」的证据。

    检索不到时不能说「没有问题」：知识库里没有命中，与「法律上没有规定」
    是两件事，所以给一条 warning。
    """

    name = "knowledge.retrieve"
    description = ("按关键词检索法条库与事实库，返回带条号定位的条目。"
                   "scope 取 laws / facts / all。用于给结论找依据，不用于判定。")
    permission = Permission.READ
    arguments_type = KnowledgeRetrieveArgs

    def __init__(self, *,
                 search_articles: Callable[..., list[dict[str, Any]]],
                 grep_facts: Callable[[str], list[dict[str, Any]]],
                 cite: Callable[[dict[str, Any]], dict[str, Any]] | None = None
                 ) -> None:
        self._search = search_articles
        self._grep = grep_facts
        self._cite = cite

    async def execute(self, arguments: KnowledgeRetrieveArgs,
                      context: ToolContext) -> ToolResult:
        query = (arguments.query or "").strip()
        if not query:
            return ToolResult.failure("query 为空：没有检索条件")
        scope = (arguments.scope or "laws").strip()
        if scope not in {"laws", "facts", "all"}:
            return ToolResult.failure(
                f"scope 非法：{scope!r}（只能是 laws / facts / all）")
        top_n = max(1, int(arguments.top_n or 5))

        articles: tuple[dict[str, Any], ...] = ()
        if scope in {"laws", "all"}:
            hits = self._search(query, top_n) or []
            articles = tuple(
                (dict(hit, citation=self._cite(hit)) if self._cite else dict(hit))
                for hit in hits)
        facts: tuple[dict[str, Any], ...] = ()
        if scope in {"facts", "all"}:
            facts = tuple(dict(item) for item in (self._grep(query) or []))

        warnings = ()
        if not articles and not facts:
            # 「知识库没命中」≠「法律没规定」：这是最容易把检索当结论的地方
            warnings = ("知识库没有命中：这不等于「没有相关规定」，可能只是关键词不匹配",)
        return ToolResult.success(
            data={"articles": articles, "facts": facts,
                  "count": len(articles) + len(facts), "scope": scope},
            warnings=warnings,
        )


@dataclass(frozen=True, slots=True)
class DocumentExtractArgs:
    """材料摄取：给路径，拿结构化文本。"""

    path: str
    work_dir: str | None = None


class DocumentExtractTool(BaseTool):
    """把任意受支持的材料（txt / pdf / docx / image / zip）抽成文本 + 元数据。

    与 `docx.format` 的分工：这里只回答「材料里写了什么」，版式合规是另一件事。

    为什么 `untrusted=True`：抽出来的是**待审材料**的内容，可能带着
    「请忽略以上指令」这类句子。调用方只能把它当数据，不能当指令。
    这与 `knowledge.retrieve` 正好相反——那边的内容是系统自己的资产。
    """

    name = "document.extract"
    description = ("摄取一个材料文件（txt / pdf / docx / 图片 / 压缩包），"
                   "返回提取到的文本、类型与抽取元数据（含告警）。"
                   "只回答「写了什么」，不判定版式或合规。")
    permission = Permission.READ
    arguments_type = DocumentExtractArgs

    def __init__(self, *, ingest_file: Callable[..., dict[str, Any]]) -> None:
        self._ingest_file = ingest_file

    async def execute(self, arguments: DocumentExtractArgs,
                      context: ToolContext) -> ToolResult:
        path = (arguments.path or "").strip()
        if not path:
            return ToolResult.failure("path 为空：摄取需要文件路径")
        try:
            result = self._ingest_file(path, work_dir=arguments.work_dir)
        except Exception as exc:            # noqa: BLE001 - 解析失败要给明确错误
            return ToolResult.failure(
                f"材料解析失败（{path}）：{type(exc).__name__}: {exc}")
        if not isinstance(result, Mapping):
            return ToolResult.failure(
                f"ingest_file 必须返回 dict，实际 {type(result).__name__}")
        if not result.get("ok", True):
            return ToolResult.failure(
                f"材料无法解析（{path}）：{result.get('reason') or '解析器返回失败'}")

        meta = dict(result.get("meta") or {})
        warnings = tuple(str(item) for item in (meta.get("warnings") or ()))
        outputs = tuple(dict(item) for item in (result.get("outputs") or ()))
        return ToolResult.success(
            data={"kind": result.get("kind") or "",
                  "text": result.get("text") or "",
                  "meta": meta,
                  "outputs": outputs,
                  "chars": len(result.get("text") or "")},
            warnings=warnings,
            untrusted=True,
        )


def batch1_tools(*, math_run_all: RunAll, logic_run_all: RunAll,
                 rule_summarize, rule_counts, sha256_bytes,
                 pattern_scan_hits: Callable[..., tuple[int, list]],
                 strip_invisible_with_map:
                 Callable[[str], tuple[str, list[int]]],
                 matrix_build: Callable[..., dict[str, Any]],
                 matrix_load_config: Callable[[], dict[str, Any]],
                 law_check: Callable[..., tuple[list, list, list]],
                 law_load_articles: Callable[[], dict[tuple[str, Any], Any]],
                 docx_format_run_all: Callable[..., list[dict[str, Any]]],
                 docx_extract: Callable[[str], dict[str, Any]],
                 lint_run: Callable[[str], dict[str, Any]],
                 search_articles: Callable[..., list[dict[str, Any]]],
                 grep_facts: Callable[[str], list[dict[str, Any]]],
                 cite_article: Callable[[dict[str, Any]], dict[str, Any]],
                 ingest_file: Callable[..., dict[str, Any]],
                 ) -> tuple[BaseTool, ...]:
    """批次 1 已实现的工具集合（算法本体由调用方注入）。

    为什么集中成一个函数：注册表怎么装配是入口的事，工具清单是能力层的事。
    补新工具时只在这个元组里加一项，注释里写清卡点的不往里塞。
    """
    return (
        MathCheckTool(math_run_all),
        LogicCheckTool(logic_run_all),
        RuleClassifyTool(summarize=rule_summarize, counts=rule_counts),
        IntegrityHashTool(sha256_bytes=sha256_bytes),
        PatternScanTool(scan_hits=pattern_scan_hits),
        TextRegexTool(normalize_with_map=strip_invisible_with_map),
        MatrixMatchTool(build=matrix_build, load_config=matrix_load_config),
        LawCiteTool(check=law_check, load_articles=law_load_articles),
        DocxFormatTool(run_all=docx_format_run_all, extract=docx_extract),
        TextLintTool(run_lint=lint_run),
        KnowledgeRetrieveTool(search_articles=search_articles,
                              grep_facts=grep_facts, cite=cite_article),
        DocumentExtractTool(ingest_file=ingest_file),
    )
