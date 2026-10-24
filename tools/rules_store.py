#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rules_store.py —— 规则加载 + 启停状态

规则正文仍只存在 rules/*.yaml（单一事实源），启停状态单独存
config/rules.state.json（只记录被禁用的 rule_id，默认全部启用）。
"""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES_DIR = ROOT / "rules"
STATE_PATH = ROOT / "config" / "rules.state.json"


def load_all_rules():
    """按文件名顺序加载全部 YAML 规则，附加 file 字段。"""
    rules = []
    for path in sorted(RULES_DIR.glob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict) or not item.get("rule_id"):
                continue
            rule = dict(item)
            rule["file"] = path.name
            rules.append(rule)
    return rules


def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name("rules.state.json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def is_enabled(rule_id, state=None):
    state = load_state() if state is None else state
    return bool(state.get(rule_id, True))


def load_active_rules():
    state = load_state()
    return [r for r in load_all_rules() if state.get(r["rule_id"], True)]


def set_enabled(rule_id, enabled):
    """启用时从 state 中删除记录（默认即启用），停用时写 false。"""
    all_ids = {r["rule_id"] for r in load_all_rules()}
    if rule_id not in all_ids:
        raise KeyError(rule_id)
    state = load_state()
    if enabled:
        state.pop(rule_id, None)
    else:
        state[rule_id] = False
    save_state(state)
    return bool(enabled)


def reset_state():
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass
