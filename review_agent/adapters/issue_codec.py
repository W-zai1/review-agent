# -*- coding: utf-8 -*-
"""issue dict ⇄ Finding 的可逆编解码（阶段 1 收口用）。

为什么需要它
------------
阶段 1 的验收标准是「`review.py` 内部不再用裸 dict 表达结论」。但旧管线里
issue 是**边跑边改**的：S3 追加、S5 折叠、整改层补 `fix`/`consequence`、
长文聚合补 `aggregated_count`。指望一次性把二十多个 `make_issue` 调用点全改成
对象，等于先停掉主流程再重构——违反 REFACTOR_PLAN 的「旧流程始终可运行」。

所以收口做法是**在 S6 出口装一个闸门**：

    最终 issue 列表 ──encode──▶ Finding ──decode──▶ issue 列表（必须逐键相等）

它保证三件事（都是可执行的，不是文档承诺）：

    1. **表达力**：领域模型能表达旧报告的每一条结论。表达不了的键进 `extras`，
       不会静默丢失；真丢了，decode 还原不出原 dict，`CodecMismatch` 当场报出来。
    2. **单一写路径**：报告里的 `issues` / `suppressed` 由领域对象回写，
       而不是「一边是对象、一边是 dict」两套结论。
    3. **格式冻结**：decode 产出的键名、键序、标量类型与旧版一致，
       报告 JSON 不会因为这次迁移而漂移（历史报告要能并排比对）。

`extras` 不是「设计漏了」
--------------------------
`fix` / `consequence` / `aggregated_count` / `folded_into` 这些是**报告层的排版
与归口信息**，不是「发现了什么」。领域层 v1 只建模结论本身（见 DOMAIN_MODEL.md
§2「六个对象」）。把它们塞进 Finding 会让领域模型变成报告的镜像——那才是真正的
过度设计。这里显式登记为 extras，阶段 3 再判断是否升为领域字段。

类型保真
--------
旧报告的标量类型不统一：`confidence` 可能是 `1` 而不是 `1.0`，早期版本用 `null`
而不是 `false` 表示 `open_discovery`，缺 `confidence_basis` 键而不是空串。
如果回写时「按领域模型的类型」强转，报告 JSON 字节会变，`report_hash` 跟着变，
历史报告就无法并排比对。所以 encode 会把「回写会变」的键原样记进 extras，
decode 优先用它——判据是 `type(a) is type(b) and a == b`，不做隐式类型放宽。

已知边界
--------
* 不合成 Evidence：旧 issue 只有 quote（原文片段）和 evidence（判定依据的文字），
  没有「校验过没有」。凭空造一条 `NOT_CHECKED` 证据等于伪造证据链；
  证据对象由 S4（Evidence Analyzer，阶段 2）产出。
* 结构性装不下（未知严重度、非法 dimension、非法 rule_id）**不兜底**，
  直接抛 `CodecMismatch`：这种输入本来就该在进入报告前被发现。
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..domain import (
    ConfidenceBasis,
    Finding,
    FindingOrigin,
    FindingStatus,
    InvariantViolation,
    Location,
    RuleRef,
    Severity,
    ids,
)

# 被领域对象消费的键。其余键一律原样进 extras。
KNOWN_KEYS: tuple[str, ...] = (
    "issue_id", "dimension", "severity", "quote", "location",
    "rule_id", "rule_text", "evidence", "tool", "confidence",
    "confidence_basis", "suggestion", "open_discovery",
)

_SENTENCE_RE = re.compile(r"第\s*(\d+)\s*句")

# confidence_basis → 结论来源。与 legacy_report._origin 同一张表：
# 那张表服务「报告→对象」，这张服务「对象→报告」，口径必须一致。
_ORIGIN_BY_BASIS: Mapping[ConfidenceBasis, FindingOrigin] = {
    ConfidenceBasis.MODEL: FindingOrigin.MODEL,
    ConfidenceBasis.HEURISTIC: FindingOrigin.HEURISTIC,
    ConfidenceBasis.RESEARCH: FindingOrigin.RESEARCH,
    ConfidenceBasis.RULE: FindingOrigin.RULE,
}


class CodecMismatch(InvariantViolation):
    """旧格式 issue 无法被领域模型完整表达（或回写后与原值不一致）。"""

    def __init__(self, message: str, *, issue_id: str = "") -> None:
        super().__init__(message)
        self.issue_id = issue_id


@dataclass(frozen=True, slots=True)
class EncodedIssue:
    """一条 issue 的领域视图 + 领域不建模的原样字段。"""

    finding: Finding
    extras: Mapping[str, Any] = field(default_factory=dict)
    drop: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "extras", MappingProxyType(dict(self.extras)))


def _same(a: Any, b: Any) -> bool:
    """严格的「回写不变」判据：类型相同、值相等、NaN 视作相等。"""
    if type(a) is not type(b):
        return False
    if isinstance(a, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def _basis(raw: Any) -> ConfidenceBasis | None:
    if raw in (None, ""):
        return None
    try:
        return ConfidenceBasis(str(raw))
    except ValueError:
        return None


def _location(raw: Any) -> Location:
    label = str(raw or "").strip() or "未定位"
    m = _SENTENCE_RE.search(label)
    return Location(label=label, sentence=int(m.group(1)) if m else None)


def _base_dict(finding: Finding) -> dict[str, Any]:
    """不含 extras 的回写结果：领域模型能表达的那部分。

    encode 用它判断「哪些键需要原样保住」，decode 在它上面叠加 extras。
    """
    out: dict[str, Any] = {
        "issue_id": finding.id,
        "dimension": finding.dimension,
        "severity": finding.severity.value,
        "quote": finding.quote,
        "location": finding.location.label,
        # 旧报告里 rule_id/rule_text 永远存在（可能是空串），不因 rule=None 省键
        "rule_id": finding.rule.rule_id if finding.rule else "",
        "rule_text": finding.rule.text if finding.rule else "",
        "evidence": finding.statement,
        "tool": finding.tool,
        "confidence": finding.confidence,
    }
    # 键序与 Session.make_issue 一致：confidence → confidence_basis → suggestion → open_discovery
    if finding.confidence_basis is not None:
        out["confidence_basis"] = finding.confidence_basis.value
    out["suggestion"] = finding.suggestion
    if finding.origin is FindingOrigin.OPEN_DISCOVERY:
        out["open_discovery"] = True
    return out


def encode_issue(raw: Mapping[str, Any], *, run_id: str) -> EncodedIssue:
    """旧格式 issue dict → 领域对象。

    结构性装不下（未知严重度、非法 dimension）抛 CodecMismatch；
    只是「类型/取值格式不一致」的键，原样记进 extras 保住报告字节。
    """
    if not isinstance(raw, Mapping):
        raise CodecMismatch(f"issue 必须是 Mapping，实际 {type(raw).__name__}")

    raw_id = str(raw.get("issue_id") or "")
    try:
        ids.parse_finding_id(raw_id)
    except Exception as e:                              # noqa: BLE001 - 统一转 CodecMismatch
        raise CodecMismatch(f"issue_id 不合法：{raw_id!r}", issue_id=raw_id) from e

    dimension = str(raw.get("dimension") or "").strip().lower()
    severity_raw = str(raw.get("severity") or "").strip()
    try:
        severity = Severity(severity_raw)
    except ValueError as e:
        raise CodecMismatch(
            f"{raw_id} 的严重度 {severity_raw!r} 不在领域枚举内", issue_id=raw_id) from e

    rule_id = str(raw.get("rule_id") or "").strip()
    rule: RuleRef | None = None
    if rule_id:
        try:
            rule = RuleRef(rule_id=rule_id, dimension=dimension,
                           text=str(raw.get("rule_text") or ""),
                           declared_severity=severity)
        except InvariantViolation as e:
            raise CodecMismatch(f"{raw_id} 无法构成 RuleRef：{e}", issue_id=raw_id) from e

    confidence = float(raw.get("confidence") or 0.0)
    basis = _basis(raw.get("confidence_basis"))
    origin = (FindingOrigin.OPEN_DISCOVERY if raw.get("open_discovery")
              else _ORIGIN_BY_BASIS.get(basis, FindingOrigin.RULE))

    # 结论级发现但置信度不足：状态标记出来，让裁定层知道「这条要人工确认」。
    status = (FindingStatus.NEEDS_CONFIRMATION if confidence < 0.7
              else FindingStatus.PROPOSED)

    try:
        finding = Finding(
            id=raw_id, run_id=run_id, dimension=dimension, severity=severity,
            quote=str(raw.get("quote") or ""),
            location=_location(raw.get("location")),
            confidence=confidence, origin=origin, rule=rule,
            statement=str(raw.get("evidence") or ""),
            confidence_basis=basis, status=status,
            tool=str(raw.get("tool") or ""),
            suggestion=str(raw.get("suggestion") or ""),
        )
    except InvariantViolation as e:
        raise CodecMismatch(f"{raw_id} 无法构成 Finding：{e}", issue_id=raw_id) from e

    base = _base_dict(finding)
    extras: dict[str, Any] = {k: v for k, v in raw.items() if k not in KNOWN_KEYS}
    drop: list[str] = []
    for key in KNOWN_KEYS:
        if key in raw:
            if key not in base or not _same(base[key], raw[key]):
                extras[key] = raw[key]
        elif key in base:
            # 早期报告没有这个键（如 confidence_basis、open_discovery）：
            # 回写时也不补，否则键集合变了。
            drop.append(key)
    return EncodedIssue(finding=finding, extras=extras, drop=tuple(drop))


def decode_issue(encoded: EncodedIssue) -> dict[str, Any]:
    """领域对象 → 旧格式 issue dict（键序固定，标量类型保真）。"""
    finding = encoded.finding
    out = _base_dict(finding)
    for key in encoded.drop:
        out.pop(key, None)
    for key, value in encoded.extras.items():
        if key in KNOWN_KEYS:
            out[key] = value
        else:
            out[key] = value
    return out


def encode_issues(issues: Sequence[Mapping[str, Any]], *,
                  run_id: str) -> tuple[EncodedIssue, ...]:
    return tuple(encode_issue(raw, run_id=run_id) for raw in issues)


def export_issues(issues: Sequence[Mapping[str, Any]], *,
                  run_id: str) -> list[dict[str, Any]]:
    """encode → decode → 逐键校验，返回「由领域对象回写」的 issue 列表。

    校验失败抛 CodecMismatch（附第一条不一致的键），而不是返回一份悄悄变形的报告。
    """
    out: list[dict[str, Any]] = []
    for raw in issues:
        encoded = encode_issue(raw, run_id=run_id)
        rebuilt = decode_issue(encoded)
        if set(rebuilt) != set(raw) or not all(_same(rebuilt[k], raw[k]) for k in raw):
            raise CodecMismatch(
                f"{encoded.finding.id} 回写后与原值不一致：{_diff(raw, rebuilt)}",
                issue_id=encoded.finding.id)
        out.append(rebuilt)
    return out


def _diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    missing = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    changed = sorted(k for k in set(before) & set(after) if not _same(before[k], after[k]))
    bits = []
    if missing:
        bits.append(f"丢失键 {missing}")
    if added:
        bits.append(f"多出键 {added}")
    bits += [f"{k}: {before[k]!r} → {after[k]!r}" for k in changed]
    return "；".join(bits) or "（无差异）"
