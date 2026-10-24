#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_spec.py —— 审查 Agent 的可插拔定义（Agent Pack）

## 为什么要这一层

原来「判定标准」是硬编码在 review.py 里的：`DIM_FOR_TYPE` 写死类型→维度、
五个维度函数写死调用顺序、规则只从 `rules/` 读。结果是**整个项目只有一个 Agent**，
想加一个「安全审查 Agent」只能改主流程，改完文档审查的行为也跟着变。

这一层把 Agent 定义成**数据**（`agents/<id>/manifest.json`）：

    Agent = 规则来源 + 维度清单（含处理器）+ 类型路由 + 附加检查 + 任务书默认值

主流程退化为「按 manifest 调度」，因此：
  · 新增一个场景 = 新增一个目录，不改 review.py；
  · 同一份文件可以被不同 Agent 用不同标准审（文档审 vs 安全审）；
  · manifest 进哈希链，改判定标准这件事本身可追溯。

## manifest 字段

| 字段 | 说明 |
|---|---|
| `id` / `name` / `version` | 唯一标识、展示名、判定标准版本（改标准必须升版本） |
| `auto` | 是否参与自动路由（false = 只能显式 `--agent` 指定） |
| `match` | 自动路由条件：`ext` 扩展名 / `markers` 关键词，命中任一即可 |
| `rules` | 规则来源：文件或目录（相对项目根） |
| `dimensions` | 维度清单，**数组顺序 = 执行顺序**；`handler` 为 null 表示只由语义审负责 |
| `dims_by_type` | 对象类型 → 本次生效的维度 |
| `default_dims` | 类型未匹配时的兜底维度（**空数组 = 一个都不跑**，不是"全部跑"） |
| `checks` | 维度审之后额外执行的检查器（`builtin:*` / `pack:*`） |
| `charter` | 任务书默认值（config/charter.json 里非空字段优先） |
| `boundaries` | 本 Agent **不查什么**，直接进报告 limitations |

## 为什么 default_dims 允许为空

覆盖不到就等于没审。让「没声明」退化成「全跑」会掩盖路由错误；
退化成「空」则会让 S6 的覆盖率与 limitations 立刻显形。
"""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
DEFAULT_AGENT = "doc-review"

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_DIM_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
_HANDLER_RE = re.compile(r"^(builtin|pack):[a-z0-9_.]+$")
_SEV = ("blocker", "major", "minor", "nit")

_CACHE = {}


class AgentError(Exception):
    """manifest 不合法。带上文件位置，避免"改完没生效"却查不到原因。"""


class Agent:
    """一个已通过校验的 Agent 定义。"""

    def __init__(self, data, path: Path):
        self.data = data
        self.path = path
        self.dir = path.parent
        self.id = data["id"]
        self.name = data["name"]
        self.version = str(data["version"])
        self.description = data.get("description") or ""
        self.auto = bool(data.get("auto", False))
        self.match = data.get("match") or {}
        self.rule_paths = [ROOT / p for p in (data.get("rules") or [])]
        self.dimensions = data["dimensions"]
        self.dims_by_type = data["dims_by_type"]
        self.default_dims = data.get("default_dims") or []
        self.checks = data.get("checks") or []
        self.charter = data.get("charter") or {}
        self.boundaries = data.get("boundaries") or []
        self._dim_ids = [d["id"] for d in self.dimensions]
        self._handlers = {d["id"]: d.get("handler") for d in self.dimensions}

    # ---- 查询 ----
    @property
    def dimension_ids(self):
        """维度 id，顺序即执行顺序。"""
        return list(self._dim_ids)

    def dims_for(self, target_type):
        """该类型的生效维度；未声明 → default_dims（可能是空，见模块说明）。"""
        dims = self.dims_by_type.get(target_type)
        if dims is None:
            dims = self.default_dims
        return [d for d in self._dim_ids if d in dims]

    def handler_for(self, dim):
        return self._handlers.get(dim)

    def label(self, dim):
        for d in self.dimensions:
            if d["id"] == dim:
                return d.get("label") or dim
        return dim

    def dimension_meta(self):
        """给前端/报告用的维度元数据（含配色）。"""
        return [{"id": d["id"], "label": d.get("label") or d["id"],
                 "color": d.get("color") or "#6b7280"} for d in self.dimensions]

    def rule_ids(self):
        return sorted({r["rule_id"] for r in load_pack_rules(self)})

    def summary(self):
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "auto": self.auto,
            "dimensions": self.dimension_meta(),
            "target_types": sorted(self.dims_by_type),
            "checks": list(self.checks),
            "boundaries": list(self.boundaries),
            "rules": len(load_pack_rules(self)),
        }

    def __repr__(self):
        return f"<Agent {self.id} v{self.version}>"


# ---------------------------------------------------------------------------
# 校验：宁可在启动时报错，也不要在审查时静默少跑一项检查
# ---------------------------------------------------------------------------
def _validate(data, path: Path):
    def bad(msg):
        raise AgentError(f"{path.relative_to(ROOT).as_posix()}: {msg}")

    if not isinstance(data, dict):
        bad("manifest 必须是 JSON 对象")
    for key in ("id", "name", "version", "dimensions", "dims_by_type"):
        if key not in data:
            bad(f"缺少必填字段 {key}")
    if not _ID_RE.match(str(data["id"])):
        bad(f"id 必须是小写字母开头的 slug：{data['id']!r}")
    if not isinstance(data["dimensions"], list) or not data["dimensions"]:
        bad("dimensions 必须是非空数组")

    seen = set()
    for d in data["dimensions"]:
        if not isinstance(d, dict) or "id" not in d:
            bad("dimensions 每项必须是含 id 的对象")
        dim = d["id"]
        if not _DIM_RE.match(str(dim)):
            bad(f"维度 id 不合法：{dim!r}")
        if dim in seen:
            bad(f"维度 id 重复：{dim}（重复会让同一维度跑两遍）")
        seen.add(dim)
        h = d.get("handler")
        if h is not None and not _HANDLER_RE.match(str(h)):
            bad(f"维度 {dim} 的 handler 必须形如 builtin:xxx 或 pack:xxx，实际 {h!r}")

    for key in list(data["dims_by_type"]):
        dims = data["dims_by_type"][key]
        if not isinstance(dims, list):
            bad(f"dims_by_type[{key}] 必须是数组")
        unknown = [d for d in dims if d not in seen]
        if unknown:
            bad(f"dims_by_type[{key}] 引用了未声明的维度：{unknown}（要么声明，要么删掉）")

    for d in data.get("default_dims") or []:
        if d not in seen:
            bad(f"default_dims 引用了未声明的维度：{d}")
    for c in data.get("checks") or []:
        if not _HANDLER_RE.match(str(c)):
            bad(f"checks 项必须形如 builtin:xxx / pack:xxx，实际 {c!r}")
    if data.get("auto") and not (data.get("match") or {}).get("ext"):
        # 自动路由没有扩展名条件 = 抢所有文件，会让别的 Agent 永远不被选中
        bad("auto=true 时必须声明 match.ext，否则会吞掉其他 Agent 的路由")


# ---------------------------------------------------------------------------
# 加载 / 列表 / 路由
# ---------------------------------------------------------------------------
def _iter_manifests():
    if not AGENTS_DIR.is_dir():
        return []
    return sorted(AGENTS_DIR.glob("*/manifest.json"))


def load_agent(agent_id):
    """按 id 加载；找不到时列出可选值（而不是抛一个裸 KeyError）。"""
    if not agent_id:
        raise AgentError("缺少 agent id")
    for path in _iter_manifests():
        if path.parent.name != agent_id:
            continue
        mtime = path.stat().st_mtime
        cached = _CACHE.get(agent_id)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise AgentError(f"{path.relative_to(ROOT).as_posix()}: JSON 解析失败：{e}")
        _validate(data, path)
        if data["id"] != agent_id:
            raise AgentError(
                f"{path.relative_to(ROOT).as_posix()}: id={data['id']!r} 与目录名 "
                f"{agent_id!r} 不一致（路由靠目录名，不一致会导致加载错 Agent）")
        agent = Agent(data, path)
        _CACHE[agent_id] = (mtime, agent)
        return agent
    avail = ", ".join(a["id"] for a in list_agents())
    raise AgentError(f"未知 Agent：{agent_id}；可选：{avail or '(无)'}")


def list_agents():
    out = []
    for path in _iter_manifests():
        try:
            out.append(load_agent(path.parent.name).summary())
        except AgentError as e:
            # 单个坏 manifest 不能让整个列表挂掉，但必须显式暴露
            out.append({"id": path.parent.name, "name": "(manifest 无效)",
                        "version": "-", "error": str(e)})
    return out


def all_target_types():
    types = set()
    for path in _iter_manifests():
        try:
            types.update(load_agent(path.parent.name).dims_by_type)
        except AgentError:
            continue
    return sorted(types)


def resolve(path, text, agent_id=None, type_hint=None):
    """决定用哪个 Agent，返回 (agent, 说明)。

    显式指定 > 自动路由（按 manifest 的 match）> 默认 Agent。
    **只有 auto=true 的 Agent 参与自动路由**：安全审这类"换一套判定标准"的
    Agent 必须人工显式选择，否则一份代码会被悄悄按安全标准审成 fail。
    """
    if agent_id:
        agent = load_agent(agent_id)
        return agent, f"人工指定 Agent：{agent.name}（v{agent.version}）"

    ext = path.suffix.lower()
    for path_ in _iter_manifests():
        try:
            agent = load_agent(path_.parent.name)
        except AgentError:
            continue
        if not agent.auto:
            continue
        exts = [e.lower() for e in (agent.match.get("ext") or [])]
        if ext in exts:
            return agent, f"按扩展名 {ext} 自动路由到 {agent.name}"
        markers = agent.match.get("markers") or []
        if markers and any(m in text for m in markers):
            return agent, f"按关键词命中自动路由到 {agent.name}"

    agent = load_agent(DEFAULT_AGENT)
    return agent, f"未命中任何路由，回落到默认 Agent：{agent.name}"


# ---------------------------------------------------------------------------
# 规则加载
# ---------------------------------------------------------------------------
def _iter_rule_files(paths):
    for p in paths:
        if p.is_dir():
            for f in sorted(p.glob("*.yaml")):
                yield f
        elif p.suffix in (".yaml", ".yml") and p.exists():
            yield p
        else:
            raise AgentError(f"规则路径不存在或不可读：{p}")


def load_pack_rules(agent, state=None):
    """加载该 Agent 声明的规则，并应用启停状态（config/rules.state.json）。"""
    if state is None:
        try:
            import rules_store
            state = rules_store.load_state()
        except Exception:
            state = {}
    rules = []
    for path in _iter_rule_files(agent.rule_paths):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        if not isinstance(data, list):
            raise AgentError(
                f"{path.relative_to(ROOT).as_posix()}: 规则文件必须是 YAML 数组")
        rel = path.relative_to(ROOT).as_posix()
        for item in data:
            if not isinstance(item, dict) or not item.get("rule_id"):
                continue
            if item.get("severity") not in _SEV:
                raise AgentError(
                    f"{rel}: {item['rule_id']} 的 severity 必须是 {_SEV} 之一，"
                    f"实际 {item.get('severity')!r}")
            if not state.get(item["rule_id"], True):
                continue        # 被停用的规则不生效
            rule = dict(item)
            rule["file"] = rel
            rules.append(rule)
    return rules
