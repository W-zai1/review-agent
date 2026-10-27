#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审查情境（scenario）：按用途选择判定尺度。

**解决的问题**：同一份材料，用途不同，该报什么完全不同。
招聘初筛要的是「材料内部有没有矛盾、满不满足岗位要求」；
资质核验才要问「四六级证书是不是真的」。
把核验类规则强加给初筛，每份简历都会报一条「CET-4 无佐证」——
HR 拿着这条没法行动（只能索要原件），真正该改的矛盾反而被淹了。

**边界（很重要）**：
1. 情境只决定「哪些规则参与」，**不改规则本身的判定标准**，也不改严重度。
   换句话说：关掉的是"这一类问题不问"，不是"这类问题从轻"。
2. 关了什么必须写进报告 limitations。不写就等于偷偷把「未发现」变成「通过」——
   本项目从第一条原则起就不允许给「通过」这个结论。
3. 未知情境、配置写错、规则全被关光，一律**报错**，不静默回退到默认情境。
   静默回退最难查：用户以为用了 A 尺度，实际跑的是 B。

配置：config/scenarios.yaml（版本化；改动要升 version，报告会记录它）
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = ROOT / "config" / "scenarios.yaml"


class ScenarioError(Exception):
    """情境配置/选择有问题——一律让审查失败，不做静默兜底。"""


def _clean_list(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ScenarioError("disable_rules / disable_dimensions / extra_limitations 必须是列表")
    return [str(x).strip() for x in raw if str(x).strip()]


def load():
    """读取并校验情境配置。结构不对就抛错，不返回半成品。"""
    try:
        raw = SCENARIOS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ScenarioError(f"缺少情境配置：{SCENARIOS_PATH.name}")
    try:
        data = yaml.safe_load(raw) or {}
    except Exception as e:                     # yaml 语法错误也要说清楚是配置问题
        raise ScenarioError(f"情境配置无法解析：{e}")
    if not isinstance(data, dict):
        raise ScenarioError("情境配置顶层必须是对象")

    items = data.get("scenarios")
    if not isinstance(items, list) or not items:
        raise ScenarioError("情境配置里没有任何情境（scenarios 必须是非空列表）")

    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            raise ScenarioError("scenarios 里每一项都必须是对象")
        sid = str(it.get("id") or "").strip()
        # id 会进 URL 参数与报告字段，限制字符集，避免奇形怪状的键
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", sid):
            raise ScenarioError(f"情境 id 不合法：{sid!r}（只允许小写字母/数字/连字符）")
        if sid in seen:
            raise ScenarioError(f"情境 id 重复：{sid}")
        seen.add(sid)
        out.append({
            "id": sid,
            "name": str(it.get("name") or sid).strip(),
            "desc": str(it.get("desc") or "").strip(),
            "keep_note": str(it.get("keep_note") or "").strip(),
            "disable_rules": _clean_list(it.get("disable_rules")),
            "disable_dimensions": _clean_list(it.get("disable_dimensions")),
            "extra_limitations": _clean_list(it.get("extra_limitations")),
        })

    default = str(data.get("default") or out[0]["id"]).strip()
    if default not in seen:
        raise ScenarioError(f"default 指向不存在的情境：{default}")
    return {"version": str(data.get("version") or "").strip(), "default": default,
            "scenarios": out}


def get(sid):
    """按 id 取情境；不存在返回 None（调用方决定是报错还是用默认值）。"""
    for s in load()["scenarios"]:
        if s["id"] == sid:
            return s
    return None


def public_list():
    """给前端的清单：不含内部字段，带上"关几条"，让人一眼知道选它会少什么。"""
    data = load()
    return {
        "ok": True,
        "version": data["version"],
        "default": data["default"],
        "items": [{
            "id": s["id"],
            "name": s["name"],
            "desc": s["desc"],
            "keep_note": s["keep_note"],
            "disabled_count": len(s["disable_rules"]) + len(s["disable_dimensions"]),
            "disabled_rules": s["disable_rules"],
            "disabled_dimensions": s["disable_dimensions"],
        } for s in data["scenarios"]],
    }


def apply(sid, rules):
    """按情境过滤规则列表，返回过滤结果与"关了什么"的完整记账。

    返回：{id,name,version,rules[],skipped[],limitations[],unmatched[]}
    未知情境 / 规则被关光 → 抛 ScenarioError（不静默降级）。
    """
    data = load()
    sc = next((s for s in data["scenarios"] if s["id"] == sid), None)
    if sc is None:
        opts = "、".join(s["id"] for s in data["scenarios"])
        raise ScenarioError(f"未知审查情境：{sid}；可选：{opts}")

    disable_rules = set(sc["disable_rules"])
    disable_dims = set(sc["disable_dimensions"])
    kept, skipped, hit = [], [], set()
    for r in rules:
        rid = str(r.get("rule_id") or "")
        dim = str(r.get("dimension") or "")
        if rid in disable_rules:
            skipped.append({"rule_id": rid, "dimension": dim,
                            "desc": str(r.get("desc") or "")[:160],
                            "reason": "情境关闭（按规则）"})
            hit.add(rid)
        elif dim in disable_dims:
            skipped.append({"rule_id": rid, "dimension": dim,
                            "desc": str(r.get("desc") or "")[:160],
                            "reason": "情境关闭（按维度）"})
            hit.add(rid)
        else:
            kept.append(r)

    if not kept:
        raise ScenarioError(
            f"情境「{sc['name']}」把所有规则都关掉了，审查将无事可做——请检查配置")

    # 配置里写了、但当前规则库里找不到的 ID：可能是规则改名/删除。
    # 不报错（不影响本次审查），但必须让人看见——否则"以为关了"实际没关。
    unmatched = sorted(disable_rules - hit)

    ids = [x["rule_id"] for x in skipped]
    limitations = []
    for tpl in sc["extra_limitations"]:
        limitations.append(tpl.replace("{rules}", "、".join(ids) or "（无）"))
    if unmatched:
        limitations.append(
            f"情境「{sc['name']}」配置了 {len(unmatched)} 条当前规则库中不存在的规则："
            f"{'、'.join(unmatched)}——可能已删除或改名，请核对 config/scenarios.yaml。")

    return {
        "id": sc["id"],
        "name": sc["name"],
        "version": data["version"],
        "desc": sc["desc"],
        "rules": kept,
        "skipped": skipped,
        "unmatched": unmatched,
        "limitations": limitations,
    }
