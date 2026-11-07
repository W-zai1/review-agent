"""server 的规则 / 日志 / 完整性只读视图。

这些接口不改任务、不启动审查、不动报告文件；把投影与校验从 HTTP Handler
拆出来后，Handler 只负责路由和响应。所有存储、路径和校验器由 server 注入，
模块本身不 import server。
"""

from __future__ import annotations

import json
import re


class DiagnosticsService:
    def __init__(
        self,
        *,
        root,
        outputs,
        logs,
        trash,
        ledger,
        rules_store,
        rule_exec,
        ledger_store,
        integrity,
        read_json,
        legacy_task_view,
    ):
        self._root = root
        self._outputs = outputs
        self._logs = logs
        self._trash = trash
        self._ledger = ledger
        self._rules_store = rules_store
        self._rule_exec = rule_exec
        self._ledger_store = ledger_store
        self._integrity = integrity
        self._read_json = read_json
        self._legacy_task_view = legacy_task_view

    # ------------------------------------------------------------------
    # 规则
    # ------------------------------------------------------------------
    def rules(self):
        state = self._rules_store.load_state()
        order = {"fact": 0, "safety": 1, "compliance": 2, "style": 3}
        all_rules = sorted(
            self._rules_store.load_all_rules(),
            key=lambda x: (order.get(x.get("dimension"), 9), x.get("rule_id", "")),
        )
        items = []
        for r in all_rules:
            items.append({
                "rule_id": r["rule_id"],
                "dimension": r.get("dimension", ""),
                "desc": r.get("desc", ""),
                "severity": r.get("severity", "minor"),
                "method": r.get("method", ""),
                "source": r.get("source", ""),
                "file": r.get("file", ""),
                "enabled": state.get(r["rule_id"], True),
                # 执行方式：deterministic / declarative / prompt_only
                "capability": self._rule_exec.capability(r),
            })
        dims = {}
        for item in items:
            dims[item["dimension"]] = dims.get(item["dimension"], 0) + 1
        exec_counts = self._rule_exec.counts(all_rules)
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "enabled": sum(1 for i in items if i["enabled"]),
            "dimensions": dims,
            # 执行方式分布：确定性 / 声明式 / 仅提示
            "execution": exec_counts,
        }

    def toggle_rule(self, data):
        rule_id = str((data or {}).get("rule_id") or "").strip()
        if not rule_id:
            return {"ok": False, "error": "缺少 rule_id"}
        if "enabled" not in (data or {}):
            return {"ok": False, "error": "缺少 enabled"}
        value = data.get("enabled")
        if isinstance(value, str):
            enabled = value.strip().lower() not in ("0", "false", "no", "off")
        else:
            enabled = bool(value)
        try:
            self._rules_store.set_enabled(rule_id, enabled)
        except KeyError:
            return {"ok": False, "error": f"规则不存在：{rule_id}"}
        except Exception as e:                    # noqa: BLE001 - HTTP 边界
            return {"ok": False, "error": f"保存失败：{e}"}
        active = self._rules_store.load_active_rules()
        return {
            "ok": True,
            "rule_id": rule_id,
            "enabled": enabled,
            "enabled_count": len(active),
            "total": len(self._rules_store.load_all_rules()),
        }

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def logs(self, rid):
        dirs = sorted(
            [p for p in self._logs.glob("R-*") if p.is_dir()]
            + [p for p in (self._logs / "v1").glob("R-*") if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if rid:
            if not re.fullmatch(r"R-\d{8}-\d{3}", str(rid)):
                return {"ok": False, "error": "日志 ID 格式不合法"}
            folder = self._logs / rid
            if not folder.is_dir():
                folder = self._logs / "v1" / rid
        else:
            folder = dirs[0] if dirs else None
        if not folder or not folder.is_dir():
            # 区分「报告被删除（连同日志移入回收区）」与「日志从未存在」
            if rid and ((self._trash / f"{rid}_logs").is_dir()
                        or (self._trash / f"{rid}_v1logs").is_dir()):
                return {"ok": False, "deleted": True,
                        "error": "该报告已被删除，日志已随报告移入 outputs/.trash/（可恢复）"}
            if rid and not ((self._outputs / f"{rid}.json").exists()
                            or (self._outputs / "v1" / f"{rid}.json").exists()):
                return {"ok": False, "deleted": True,
                        "error": "该报告已不存在（可能被删除或从未生成）"}
            return {"ok": False, "error": "未找到该次审查的日志"}

        def read_json(name):
            path = folder / name
            if not path.exists():
                return {}
            try:
                return self._read_json(path)
            except Exception:                    # noqa: BLE001 - 展示层兜底
                return {}

        def read_jsonl(name):
            path = folder / name
            if not path.exists():
                return []
            rows = []
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:            # noqa: BLE001 - 跳过坏行
                        continue
            except Exception:                    # noqa: BLE001 - 展示层兜底
                return []
            return rows

        return {
            "ok": True,
            "id": folder.name,
            "task": self._legacy_task_view(read_json("task.json")),
            "decisions": read_jsonl("decisions.jsonl"),
            "tools": read_jsonl("tools.jsonl"),
            "cost": read_json("cost.json"),
        }

    # ------------------------------------------------------------------
    # 完整性
    # ------------------------------------------------------------------
    def integrity(self):
        chains = [p for p in (self._ledger / "chain.log",
                              self._ledger / "v1-chain.log")
                  if p.exists()]
        if not chains:
            return {"ok": False, "chain_ok": False, "entries": [],
                    "reports": [], "detail": "ledger 尚未生成（运行一次审查后才有）"}
        # 链的解读交给唯一实现：墓碑（独立行 / 历史原位行）不再被当作"坏行"，
        # 否则删过一份报告的用户会永远看到红叉（旧实现就是这样）。
        views = [(path, self._ledger_store.entries_for_api(path))
                 for path in chains]
        # V1 是默认链路；有 v1 链时以它作为主视图，legacy 链仍参与 chain_ok。
        primary_path, view = next(
            ((path, v) for path, v in views if path.name == "v1-chain.log"),
            views[0],
        )
        entries = view["entries"]
        chain_ok = all(v["chain_ok"] for _, v in views)
        broken_at = next((v["broken_at"] for _, v in views if not v["chain_ok"]), None)

        reports = []
        cur_rules_ver = self._integrity.rules_version()
        report_paths = [*self._outputs.glob("R-*.json"),
                        *(self._outputs / "v1").glob("R-*.json")]
        for p in sorted(report_paths):
            try:
                r = self._read_json(p)
                stored = r.get("integrity", {}).get("report_hash")
                # 复用 CLI 的校验实现：两边各写一套必然漂移——
                # 实测控制台只查了 rules_dir_hash，于是"CLI 报 87 份 stale、
                # 控制台全绿"，用户看到的和实际状态相反。
                ok, detail, stale = self._integrity.verify_report(
                    str(p), str(self._root))
                reports.append({
                    "id": r.get("review_id"),
                    "ok": ok,
                    "rules_ok": ok,     # 兼容旧前端字段：自洽通过即视为规则可接受
                    "stale": stale,
                    "rules_version": r.get("integrity", {}).get("rules_version"),
                    "verdict": r.get("verdict"),
                    "target_path": r.get("target_path"),
                    "hash_short": (stored or "")[:12],
                    "detail": "" if ok else (detail if isinstance(detail, str)
                                             else "；".join(detail)),
                })
            except Exception as e:                # noqa: BLE001 - 单份报告失败不拖垮整页
                reports.append({"id": p.name, "ok": False, "error": str(e)})
        # 报告内容被改（自洽失败）时，把对应链节点也标红
        report_state = {r.get("id"): r.get("ok") for r in reports}
        for entry in entries:
            if report_state.get(entry.get("review_id")) is False:
                entry["ok"] = False
        ok = chain_ok and all(x.get("ok") for x in reports)
        stale_count = sum(1 for x in reports if x.get("stale"))
        tampered = [x.get("id") for x in reports if not x.get("ok")]
        detail = ""
        if not ok:
            detail = ("审计链断裂（断点 seq %s）" % broken_at) if not chain_ok else \
                     ("报告校验失败：%s" % "、".join(tampered[:5]))
        return {
            "ok": ok,
            "chain_ok": chain_ok,
            "entries": entries,
            "tombstones": view["tombstones"],
            "deleted": view["deleted"],
            "reports": reports,
            "broken_at": broken_at,
            "stale_count": stale_count,
            "rules_version": cur_rules_ver,
            "detail": detail,
        }
