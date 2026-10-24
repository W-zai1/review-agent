#!/usr/bin/env python3
"""integrity.py —— 哈希计算 + Ledger 校验 (Demo 工具)

完整性三件套：target_hash / ruleset_hash / knowledge_hash / report_hash / logs_hash / prev_report_hash
CLI:
    python integrity.py verify          # 全量校验 ledger 链 + 所有报告自洽
    python integrity.py verify R-xxx    # 只校验某一份报告（仍需 ledger 链）
"""
import os
import sys
import json
import hashlib
import glob


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# 目录哈希必须排除的东西：
# · __pycache__ / *.pyc —— Python 一 import 就会生成，内容随解释器版本变化，
#   算进去会让「同一份判定标准」在不同机器/不同次运行得到不同 hash（实测
#   agent_dir_hash 因此把一份正常报告判成「疑似判定标准被改」）；
# · .DS_Store 等系统垃圾文件。
_HASH_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
_HASH_SKIP_FILES = {".DS_Store", "Thumbs.db"}


def sha256_dir(d):
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(d, "**", "*"), recursive=True)):
        if not os.path.isfile(p):
            continue
        parts = set(os.path.relpath(p, d).split(os.sep))
        if parts & _HASH_SKIP_DIRS:
            continue
        if os.path.basename(p) in _HASH_SKIP_FILES or p.endswith(".pyc"):
            continue
        h.update(sha256_file(p).encode("utf-8"))
        h.update(os.path.relpath(p, d).encode("utf-8"))
    return h.hexdigest()


def knowledge_version(root=None):
    """当前知识库版本号（config/knowledge_version.txt）。

    和 rules_version 同理：事实库/政策库更新属于**正常演进**，
    报告里记录的 knowledge_hash 必然失配，但那时结论并没有被篡改，
    只是「当时的依据已经不在了」。不区分这两者，一次正常补数据就会
    让全部历史报告变红——实测改 facts.json 后 87 份报告全 FAIL。
    版本文件必须放在 knowledge/ 之外，否则它自己会改变 knowledge/ 的 hash。
    """
    root = root or _root()
    path = os.path.join(root, "config", "knowledge_version.txt")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or "unversioned"
    except OSError:
        return "unversioned"


def agent_version(root, agent_id):
    """当前 Agent 判定标准的版本号（agents/<id>/manifest.json 的 version 字段）。

    和 rules_version 同一个用途：区分「判定标准升级过」与「判定标准被偷改」。
    """
    path = os.path.join(root or _root(), "agents", agent_id, "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            return str(json.load(f).get("version") or "")
    except Exception:
        return ""


def serialize_active_rules(rules):
    arr = sorted(rules, key=lambda r: r.get("rule_id", ""))
    return json.dumps(arr, ensure_ascii=False, sort_keys=True)


def compute_report_hash(report):
    """报告 hash：必须把自己（嵌套在 integrity 内的 report_hash）字段置空，否则永远对不上（自指问题）。"""
    r = json.loads(json.dumps(report, ensure_ascii=False))  # 深拷贝，避免改动原对象
    r.get("integrity", {}).pop("report_hash", None)
    return sha256_bytes(json.dumps(r, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def verify_ledger(ledger_path):
    if not os.path.exists(ledger_path):
        return (False, "ledger 不存在")
    prev = "0" * 64
    seq = 0
    with open(ledger_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            rec = json.loads(raw)
            seq += 1
            if rec.get("seq") != seq:
                return (False, f"序号不连续 @line{seq}")
            if rec.get("prev_hash") != prev:
                return (False, f"链条断裂 @seq{rec.get('seq')}: prev_hash 不匹配")
            prev = rec.get("report_hash", "")
    return (True, f"ledger 校验通过，共 {seq} 条记录，链完整")


def rules_version(root=None):
    """当前规则集版本号（config/rules_version.txt）。

    规则集升级（增删改 rules/*.yaml）会让历史报告记录的 rules_dir_hash 失配——
    那是**规则演进**，不是篡改。报告里记录生成时的版本号，校验时据此区分两者。
    版本文件必须放在 rules/ 之外，否则它自己就会改变 rules/ 的 hash。
    """
    root = root or _root()
    path = os.path.join(root, "config", "rules_version.txt")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or "unversioned"
    except OSError:
        return "unversioned"


def verify_report(report_path, root):
    """校验单份报告自洽性，返回 (ok, 详情或错误列表, stale)。

    stale=True 表示「这份报告基于旧版判定依据（规则集 / 知识库 / Agent），之后升级过」——
    属于正常演进，不算失败；只有版本号未变却 hash 变了才判为篡改。
    """
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)
    inc = report.get("integrity", {})
    errs = []
    stale = False
    legacy = "rules_dir_hash" not in inc

    # 0/1) target
    # 「原件被替换」与「报告被篡改」是两件事：前者说明这份报告的原文定位
    # 已经不适用于当前文件（该重审），后者说明有人改了结论（该报警）。
    # 归到同一档会让正常换版本的文件把历史报告全打成红。
    soft = []
    tp = os.path.join(root, report.get("target_path", ""))
    if os.path.exists(tp) and sha256_file(tp) != inc.get("target_hash"):
        soft.append("target_hash 不匹配（原件已被替换：报告中的原文定位可能失效，建议重新审查）")
    # 2) ruleset（报告内嵌 + 磁盘 rules/ 快照）
    if "active_rules" in report:
        if sha256_bytes(serialize_active_rules(report["active_rules"]).encode("utf-8")) != inc.get("ruleset_hash"):
            errs.append("ruleset_hash 不匹配")
    rules_dir = os.path.join(root, "rules")
    if not legacy and os.path.isdir(rules_dir):
        if sha256_dir(rules_dir) != inc.get("rules_dir_hash"):
            rep_ver = inc.get("rules_version")
            cur_ver = rules_version(root)
            if rep_ver and rep_ver == cur_ver:
                # 版本号没变，hash 却变了 → 绕过版本机制偷改规则库
                errs.append("rules_dir_hash 不匹配且规则集版本未变（疑似规则库被篡改）")
            else:
                # 版本变了（或旧报告没有版本号）→ 规则集演进过，不是篡改
                stale = True
    # 2.5) Agent 判定标准（manifest + 该 Agent 自带规则/检查器）
    # 只看本 Agent 自己的目录：别的 Agent 改了，不该让这份报告变成可疑。
    agent_meta = report.get("agent") or {}
    inc_agent_hash = inc.get("agent_dir_hash")
    if agent_meta.get("id") and inc_agent_hash:
        adir = os.path.join(root, "agents", agent_meta["id"])
        if os.path.isdir(adir) and sha256_dir(adir) != inc_agent_hash:
            if agent_meta.get("version") and agent_meta["version"] == agent_version(
                    root, agent_meta["id"]):
                errs.append("agent_dir_hash 不匹配且 Agent 版本未变（疑似判定标准被改）")
            else:
                stale = True

    # 3) knowledge
    kd = os.path.join(root, "knowledge")
    if os.path.isdir(kd) and sha256_dir(kd) != inc.get("knowledge_hash"):
        rep_ver = inc.get("knowledge_version")
        cur_ver = knowledge_version(root)
        if rep_ver and rep_ver == cur_ver:
            # 版本没变，内容却变了 → 绕过版本机制改了事实库
            errs.append("knowledge_hash 不匹配且知识库版本未变（疑似知识库被篡改）")
        else:
            # 版本变了，或旧报告没有版本号 → 知识库演进过，结论依据已变
            stale = True
    # 4) logs
    ld = os.path.join(root, "logs", report.get("review_id", ""))
    if os.path.isdir(ld) and sha256_dir(ld) != inc.get("logs_hash"):
        errs.append("logs_hash 不匹配")
    # 5) report 自洽
    if compute_report_hash(report) != inc.get("report_hash"):
        errs.append("report_hash 自洽失败")
    note = "report 自洽通过" + ("（旧报告：无 rules_dir_hash）" if legacy else "")
    if stale:
        note = "report 自洽通过（基于旧版判定依据，规则集/知识库/Agent 之后升级过）"
    # ok 只反映「报告自身有没有被改」；依据变化（原件被替换/规则或知识库升级）
    # 统一走 stale（黄），由调用方决定是否提示重审。
    return (len(errs) == 0, errs or soft or note, stale or bool(soft))


def _root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    root = _root()
    ledger = os.path.join(root, "ledger", "chain.log")
    if len(sys.argv) >= 2 and sys.argv[1] == "verify":
        rid = sys.argv[2] if len(sys.argv) >= 3 else None
        ok, msg = verify_ledger(ledger)
        print(f"[ledger] {'OK' if ok else 'FAIL'} - {msg}")
        out_dir = os.path.join(root, "outputs")
        if rid:
            reports = [os.path.join(out_dir, f"{rid}.json")]
        else:
            reports = sorted(glob.glob(os.path.join(out_dir, "R-*.json")))
        for rp in reports:
            if not os.path.exists(rp):
                print(f"[report {os.path.basename(rp)}] SKIP - 文件不存在")
                continue
            ok2, msg2, stale = verify_report(rp, root)
            tag = "STALE" if stale else ("OK" if ok2 else "FAIL")
            print(f"[report {os.path.basename(rp)}] {tag} - {msg2}")
    else:
        print("usage: integrity.py verify [R-id]")


if __name__ == "__main__":
    main()
