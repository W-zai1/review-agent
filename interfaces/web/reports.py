#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告管理：列表、读取、收藏、移入回收站、恢复、彻底删除。

HTTP 层（server.py::Handler）只负责解析参数 / 包装错误；这里的返回值与
`server.py` 原有字段一一对应，不新增、不改名。所有路径由构造函数注入，
测试可以在临时目录里构造一个 service，不碰真实 outputs/ledger。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from review_agent.infrastructure import ledger as ledger_store

_REPORT_ID = re.compile(r"R-\d{8}-\d{3}")


class ReportService:
    def __init__(
        self,
        *,
        root: Path,
        outputs: Path,
        log_dir: Path,
        state_dir: Path,
        trash: Path,
        ledger_dir: Path,
        favorites_path: Path,
        llm_cache_dir: Path,
        favorites_getter=None,
        favorites_setter=None,
        purge_gap=None,
    ):
        self.root = Path(root)
        self.outputs = Path(outputs)
        self.log_dir = Path(log_dir)
        self.state_dir = Path(state_dir)
        self.trash = Path(trash)
        self.ledger_dir = Path(ledger_dir)
        self.favorites_path = Path(favorites_path)
        self.llm_cache_dir = Path(llm_cache_dir)
        self._favorites_getter = favorites_getter
        self._favorites_setter = favorites_setter
        self._purge_gap = purge_gap

    # ----- 收藏 -----
    def favorites(self):
        if self._favorites_getter is not None:
            return list(self._favorites_getter())
        try:
            data = json.loads(self.favorites_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        ids = data.get("favorites") if isinstance(data, dict) else data
        if not isinstance(ids, list):
            return []
        out = []
        for x in ids:
            rid = str(x or "")
            if _REPORT_ID.fullmatch(rid) and rid not in out:
                out.append(rid)
        return out

    def save_favorites(self, ids):
        if self._favorites_setter is not None:
            self._favorites_setter(list(ids))
            return
        self.favorites_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "favorites": list(ids),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        tmp = self.favorites_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(self.favorites_path))

    def set_favorite(self, data):
        rid = str((data or {}).get("id") or "")
        if not _REPORT_ID.fullmatch(rid):
            return {"ok": False, "error": "报告 ID 格式不合法"}
        if not ((self.outputs / f"{rid}.json").exists()
                or (self.outputs / "v1" / f"{rid}.json").exists()):
            return {"ok": False, "error": f"未找到报告 {rid}"}
        want = (data or {}).get("favorite")
        if not isinstance(want, bool):
            return {"ok": False, "error": "favorite 必须是 true / false"}
        ids = self.favorites()
        if want and rid not in ids:
            ids.insert(0, rid)
        elif not want and rid in ids:
            ids.remove(rid)
        self.save_favorites(ids)
        return {"ok": True, "id": rid, "favorite": want, "favorites": ids}

    def set_favorites(self, data):
        raw_ids = (data or {}).get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return {"ok": False, "error": "ids 必须是非空数组"}
        if len(raw_ids) > 200:
            return {"ok": False, "error": "一次最多收藏 200 份报告"}
        ids = list(dict.fromkeys(str(item or "") for item in raw_ids))
        bad = [rid for rid in ids if not _REPORT_ID.fullmatch(rid)]
        if bad:
            return {"ok": False, "error": f"报告 ID 格式不合法：{bad[0]}"}
        missing = [rid for rid in ids
                   if not ((self.outputs / f"{rid}.json").exists()
                           or (self.outputs / "v1" / f"{rid}.json").exists())]
        if missing:
            return {"ok": False, "error": f"未找到报告 {missing[0]}"}
        want = (data or {}).get("favorite")
        if not isinstance(want, bool):
            return {"ok": False, "error": "favorite 必须是 true / false"}
        current = self.favorites()
        chosen = set(ids)
        current = [rid for rid in current if rid not in chosen]
        if want:
            current = ids + current
        self.save_favorites(current)
        return {"ok": True, "ids": ids, "favorite": want,
                "updated": len(ids), "favorites": current}

    # ----- 列表 / 读取 -----
    def reports(self):
        fav = set(self.favorites())
        items = []
        paths = [*self.outputs.glob("R-*.json"),
                 *(self.outputs / "v1").glob("R-*.json")]
        by_id = {}
        for p in paths:
            try:
                r = self._read_json(p)
            except Exception:
                continue
            rid = str(r.get("review_id") or p.stem)
            by_id[rid] = (p, r)

        for rid, (p, r) in by_id.items():
            inc = r.get("integrity", {})
            mtime = p.stat().st_mtime
            target = r.get("target") if isinstance(r.get("target"), dict) else {}
            ts = str(r.get("timestamp") or r.get("generated_at") or "")
            if not ts:
                ts = time.strftime("%Y-%m-%dT%H:%M:%S",
                                   time.localtime(mtime))
            target_path = r.get("target_path") or target.get("path")
            items.append({
                "id": rid,
                "verdict": r.get("verdict"),
                "target_type": r.get("target_type") or target.get("type"),
                "target_path": target_path,
                "target_name": Path(target_path or "").name,
                "review_mode": r.get("review_mode"),
                "timestamp": ts,
                "issues": len(r.get("issues", [])),
                "stats": r.get("stats", {}),
                "engine": r.get("engine") or (
                    "architecture-v1" if p.parent.name == "v1" else "legacy"),
                "report_hash": (inc.get("report_hash") or "")[:16],
                "favorite": rid in fav,
                "mtime": mtime,
            })
        items.sort(key=lambda x: (x["timestamp"], x["id"]), reverse=True)
        return {"items": items, "favorites": sorted(fav)}

    def report(self, rid):
        if not rid:
            return {"ok": False, "error": "缺少 id"}
        if not _REPORT_ID.fullmatch(str(rid)):
            return {"ok": False, "error": "报告 ID 格式不合法"}
        hit = next(self.outputs.glob(f"{rid}.json"), None)
        if hit is None:
            hit = next((self.outputs / "v1").glob(f"{rid}.json"), None)
        if not hit:
            return {"ok": False, "error": f"未找到报告 {rid}"}
        return {"ok": True, "report": self._read_json(hit)}

    # ----- 删除 -----
    def delete_report(self, rid, data):
        if not rid:
            return {"ok": False, "error": "缺少 id"}
        if not _REPORT_ID.fullmatch(str(rid)):
            return {"ok": False, "error": "报告 ID 格式不合法"}
        hit = self.outputs / f"{rid}.json"
        sidecar = self.outputs / f"{rid}.md"
        v1_hit = self.outputs / "v1" / f"{rid}.json"
        v1_sidecar = self.outputs / "v1" / f"{rid}.md"
        if not any(p.exists() for p in (hit, sidecar, v1_hit, v1_sidecar)):
            return {"ok": False, "error": f"未找到报告 {rid}"}

        confirm = str((data or {}).get("confirm") or "")
        if confirm != rid:
            return {"ok": False, "need_confirm": True,
                    "error": "删除需要二次确认：请提交 confirm=<报告ID>"}

        report = None
        for candidate in (v1_hit, hit):
            if candidate.is_file():
                try:
                    report = self._read_json(candidate)
                    break
                except Exception:
                    continue

        self.trash.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema": 1,
            "review_id": rid,
            "deleted_at": ledger_store.now_iso(),
            "files": [],
        }
        moved = []
        for src in (self.outputs / f"{rid}.json",
                    self.outputs / f"{rid}.md"):
            if src.exists():
                dst = self._trash_destination(src.name)
                shutil.move(str(src), str(dst))
                self._record_trash_move(manifest, src, dst)
                moved.append(src.name)
        log_src = self.log_dir / rid
        if log_src.is_dir():
            dst = self._trash_destination(f"{rid}_logs")
            shutil.move(str(log_src), str(dst))
            self._record_trash_move(manifest, log_src, dst)
            moved.append(f"logs/{rid}/")
        moved.extend(self._purge_derived(rid, report=report,
                                         manifest=manifest))
        self._save_trash_manifest(manifest)

        tombstoned = self._mark_ledger_deleted(rid)
        favs = self.favorites()
        if rid in favs:
            favs.remove(rid)
            self.save_favorites(favs)
        return {
            "ok": True,
            "id": rid,
            "moved": moved,
            "trash": "outputs/.trash/",
            "ledger_tombstone": tombstoned,
        }

    def delete_reports(self, data):
        if str((data or {}).get("confirm") or "") != "DELETE_SELECTED":
            return {"ok": False, "need_confirm": True,
                    "error": "批量删除需要确认：confirm=DELETE_SELECTED"}
        raw_ids = (data or {}).get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return {"ok": False, "error": "ids 必须是非空数组"}
        if len(raw_ids) > 200:
            return {"ok": False, "error": "一次最多删除 200 份报告"}
        ids = list(dict.fromkeys(str(item or "") for item in raw_ids))
        deleted = []
        errors = []
        for rid in ids:
            try:
                result = self.delete_report(rid, {"confirm": rid})
            except Exception as exc:  # noqa: BLE001 - 单份失败不拖垮批次
                result = {"ok": False,
                          "error": f"{type(exc).__name__}: {exc}"}
            if result.get("ok"):
                deleted.append(rid)
            else:
                errors.append({"id": rid,
                               "error": result.get("error") or "删除失败"})
        return {
            "ok": not errors,
            "deleted": deleted,
            "failed": errors,
            "total": len(ids),
        }

    def trash_reports(self):
        ids = set()
        manifest_dir = self.trash / ".manifests"
        if manifest_dir.is_dir():
            for path in manifest_dir.glob("R-*.json"):
                rid = path.stem
                if _REPORT_ID.fullmatch(rid):
                    ids.add(rid)
        if self.trash.is_dir():
            for path in self.trash.iterdir():
                match = re.match(
                    r"(?:v1_)?(R-\d{8}-\d{3})(?:-\d+)?\.json$", path.name)
                if path.is_file() and match:
                    ids.add(match.group(1))

        items = []
        for rid in ids:
            if ((self.outputs / f"{rid}.json").exists()
                    or (self.outputs / "v1" / f"{rid}.json").exists()):
                continue
            manifest = self._load_trash_manifest(rid)
            report = self._find_report(rid)
            paths = self._trash_items(rid, report=report)
            if not paths:
                continue
            mtimes = []
            size = 0
            for src, _original in paths:
                try:
                    if src.is_dir():
                        files = [p for p in src.rglob("*") if p.is_file()]
                        size += sum(p.stat().st_size for p in files)
                        mtimes.extend(p.stat().st_mtime for p in files)
                    else:
                        st = src.stat()
                        size += st.st_size
                        mtimes.append(st.st_mtime)
                except OSError:
                    continue
            deleted_at = (manifest or {}).get("deleted_at")
            if not deleted_at and mtimes:
                deleted_at = datetime.fromtimestamp(
                    max(mtimes)).astimezone().isoformat(timespec="seconds")
            target = report.get("target") if isinstance(report, dict) else {}
            target_path = (
                (report or {}).get("target_path")
                or (target or {}).get("path")
                or ""
            )
            items.append({
                "id": rid,
                "target_name": (
                    (report or {}).get("target_name")
                    or Path(str(target_path)).name
                ),
                "target_path": str(target_path),
                "verdict": (report or {}).get("verdict", ""),
                "review_mode": (report or {}).get("review_mode", ""),
                "issues": len((report or {}).get("issues") or []),
                "deleted_at": deleted_at or "",
                "files": len(paths),
                "size": size,
                "restorable": True,
            })
        items.sort(key=lambda x: (x["deleted_at"], x["id"]), reverse=True)
        return {"ok": True, "items": items, "total": len(items)}

    def restore_report(self, data):
        rid = str((data or {}).get("id") or "")
        if not _REPORT_ID.fullmatch(rid):
            return {"ok": False, "error": "报告 ID 格式不合法"}
        if ((self.outputs / f"{rid}.json").exists()
                or (self.outputs / "v1" / f"{rid}.json").exists()):
            return {"ok": False, "error": f"报告 {rid} 已存在，拒绝覆盖"}
        report = self._find_report(rid)
        items = self._trash_items(rid, report=report)
        if not items:
            return {"ok": False, "error": f"回收站中未找到报告 {rid}"}
        conflicts = [str(original) for _src, original in items
                     if original.exists()]
        if conflicts:
            return {"ok": False,
                    "error": f"恢复目标已存在，拒绝覆盖：{conflicts[0]}"}

        moved = []
        try:
            for src, original in items:
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(original))
                moved.append((original, src))
        except Exception as exc:
            for original, src in reversed(moved):
                try:
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(original), str(src))
                except OSError:
                    pass
            return {"ok": False,
                    "error": f"恢复中断，已回滚：{type(exc).__name__}: {exc}"}

        manifest = self._trash_manifest_path(rid)
        try:
            manifest.unlink()
        except OSError:
            pass
        ledger_restored = self._mark_ledger_restored(rid)
        return {
            "ok": True,
            "id": rid,
            "restored": [str(original) for original, _src in moved],
            "ledger_restore": ledger_restored,
        }

    def purge_trash_report(self, rid, data):
        if not _REPORT_ID.fullmatch(str(rid or "")):
            return {"ok": False, "error": "报告 ID 格式不合法"}
        if str((data or {}).get("confirm") or "") != rid:
            return {"ok": False, "need_confirm": True,
                    "error": "彻底删除需要二次确认：请提交 confirm=<报告ID>"}
        report = self._find_report(rid)
        items = self._trash_items(rid, report=report, dedupe=False)
        if not items:
            return {"ok": False, "error": f"回收站中未找到报告 {rid}"}
        removed = []
        for src, _original in items:
            try:
                if src.is_dir():
                    shutil.rmtree(str(src))
                elif src.exists():
                    src.unlink()
                removed.append(str(src))
            except OSError as exc:
                return {"ok": False,
                        "error": f"彻底删除失败：{type(exc).__name__}: {exc}",
                        "removed": removed}
        try:
            self._trash_manifest_path(rid).unlink()
        except OSError:
            pass
        return {"ok": True, "id": rid, "removed": removed}

    def empty_trash(self, data):
        if str((data or {}).get("confirm") or "") != "EMPTY":
            return {"ok": False, "need_confirm": True,
                    "error": '清空回收站需要二次确认：请提交 confirm="EMPTY"'}
        if not self.trash.is_dir():
            return {"ok": True, "removed": 0}
        removed = 0
        for path in list(self.trash.iterdir()):
            if path.is_dir():
                shutil.rmtree(str(path))
            else:
                path.unlink()
            removed += 1
        return {"ok": True, "removed": removed}

    # ----- 文件/台账辅助 -----
    @staticmethod
    def _read_json(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def _trash_destination(self, name):
        self.trash.mkdir(parents=True, exist_ok=True)
        dst = self.trash / name
        if dst.exists():
            stem, ext = os.path.splitext(name)
            dst = self.trash / f"{stem}-{int(time.time() * 1000)}{ext}"
        return dst

    def _root_relative(self, path):
        try:
            return Path(path).resolve().relative_to(
                self.root.resolve()).as_posix()
        except (OSError, ValueError):
            return ""

    def _trash_manifest_path(self, rid):
        return self.trash / ".manifests" / f"{rid}.json"

    def _record_trash_move(self, manifest, src, dst):
        if manifest is None:
            return
        rel = self._root_relative(src)
        if rel:
            manifest.setdefault("files", []).append({
                "from": rel,
                "to": Path(dst).name,
            })

    def _save_trash_manifest(self, manifest):
        if not manifest or not manifest.get("review_id"):
            return
        path = self._trash_manifest_path(manifest["review_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)

    def _load_trash_manifest(self, rid):
        path = self._trash_manifest_path(rid)
        if not path.is_file():
            return None
        try:
            data = self._read_json(path)
        except Exception:
            return None
        if not isinstance(data, dict) or data.get("review_id") != rid:
            return None
        files = data.get("files")
        if not isinstance(files, list):
            return None
        return data

    def _trash_suffix_match(self, name, prefix, suffix):
        if not name.startswith(prefix) or not name.endswith(suffix):
            return False
        middle = name[len(prefix):len(name) - len(suffix)]
        return not middle or (middle.startswith("-") and middle[1:].isdigit())

    def _legacy_trash_items(self, rid, report=None, dedupe=True):
        if not self.trash.is_dir():
            return []
        snapshot_name = (Path(self._snapshot_ref(report)).name
                         if self._snapshot_ref(report) else "")
        items = []
        for src in sorted(self.trash.iterdir(), key=lambda p: p.name):
            if not (src.is_file() or src.is_dir()):
                continue
            name = src.name
            original = None
            if self._trash_suffix_match(name, rid, ".json"):
                original = self.outputs / f"{rid}.json"
            elif self._trash_suffix_match(name, rid, ".md"):
                original = self.outputs / f"{rid}.md"
            elif self._trash_suffix_match(name, f"v1_{rid}", ".json"):
                original = self.outputs / "v1" / f"{rid}.json"
            elif self._trash_suffix_match(name, f"v1_{rid}", ".md"):
                original = self.outputs / "v1" / f"{rid}.md"
            elif src.is_dir() and self._trash_suffix_match(
                    name, f"{rid}_logs", ""):
                original = self.log_dir / rid
            elif src.is_dir() and self._trash_suffix_match(
                    name, f"{rid}_v1logs", ""):
                original = self.log_dir / "v1" / rid
            elif (src.is_file() and name.startswith(f"state_{rid}")
                  and name.endswith(".state.json")):
                original = self.state_dir / "runs" / name[len("state_"):]
            elif src.is_file() and name.startswith("cache_llm_"):
                original = self.llm_cache_dir / name[len("cache_llm_"):]
            elif src.is_file() and name.startswith("cache_research_"):
                original = (self.state_dir / "research_cache"
                            / name[len("cache_research_"):])
            elif (snapshot_name and src.is_file()
                  and self._trash_suffix_match(
                      name, f"text_{snapshot_name}", "")):
                original = self.state_dir / "text" / snapshot_name
            if original is not None:
                items.append((src, original))
        if not dedupe:
            return items
        newest = {}
        for src, original in items:
            try:
                stamp = src.stat().st_mtime
            except OSError:
                stamp = 0
            previous = newest.get(original)
            if previous is None or stamp > previous[0]:
                newest[original] = (stamp, src)
        return [(src, original) for original, (_stamp, src) in sorted(
            newest.items(), key=lambda item: str(item[0]))]

    def _trash_items(self, rid, report=None, dedupe=True):
        manifest = self._load_trash_manifest(rid)
        if manifest:
            items = []
            for row in manifest.get("files", []):
                if not isinstance(row, dict):
                    continue
                rel = str(row.get("from") or "")
                name = str(row.get("to") or "")
                if not rel or not name or Path(name).name != name:
                    continue
                original = self.root / rel
                if not self._root_relative(original):
                    continue
                src = self.trash / name
                if src.exists():
                    items.append((src, original))
            if items:
                return items
        return self._legacy_trash_items(rid, report=report, dedupe=dedupe)

    def _move_derived(self, src, prefix, moved, label, manifest=None):
        try:
            dst = self._trash_destination(f"{prefix}_{src.name}")
            shutil.move(str(src), str(dst))
            self._record_trash_move(manifest, src, dst)
            moved.append(f"{label}/{src.name}")
        except OSError:
            pass

    def _purge_owned_cache(self, directory, owner, prefix, label, moved,
                          manifest=None):
        if not owner or not directory.is_dir():
            return
        for src in sorted(directory.glob("*.json")):
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            raw = data.get("_owners")
            owners = [str(x) for x in raw] if isinstance(raw, list) else []
            if owner not in owners:
                continue
            remaining = [x for x in owners if x != owner]
            if remaining:
                data["_owners"] = remaining
                try:
                    tmp = src.with_name(f"{src.name}.{os.getpid()}.tmp")
                    tmp.write_text(json.dumps(data, ensure_ascii=False,
                                             indent=1), encoding="utf-8")
                    os.replace(tmp, src)
                except OSError:
                    pass
            else:
                self._move_derived(src, prefix, moved, label,
                                   manifest=manifest)

    def _snapshot_ref(self, report):
        if not isinstance(report, dict):
            return ""
        target = report.get("target")
        if isinstance(target, dict) and target.get("snapshot_ref"):
            return str(target["snapshot_ref"])
        snap = report.get("target_snapshot")
        if isinstance(snap, dict) and snap.get("ref"):
            return str(snap["ref"])
        return ""

    def _snapshot_file(self, ref):
        if not ref:
            return None
        path = Path(ref)
        if path.is_absolute():
            return path if path.is_file() else None
        candidate = self.root / path
        if candidate.is_file():
            return candidate
        candidate = self.state_dir / "text" / path.name
        return candidate if candidate.is_file() else None

    def _snapshot_referenced(self, ref, exclude_rid):
        for base in (self.outputs, self.outputs / "v1"):
            for path in base.glob("R-*.json"):
                if path.stem == exclude_rid:
                    continue
                try:
                    report = self._read_json(path)
                except Exception:
                    continue
                if self._snapshot_ref(report) == ref:
                    return True
        return False

    def _find_report(self, rid):
        for path in (self.outputs / f"{rid}.json",
                     self.outputs / "v1" / f"{rid}.json",
                     self.trash / f"{rid}.json",
                     self.trash / f"v1_{rid}.json"):
            if path.is_file():
                try:
                    return self._read_json(path)
                except Exception:
                    continue
        return None

    def _purge_derived(self, rid, report=None, manifest=None):
        moved = []
        for suffix in (".json", ".md"):
            src = self.outputs / "v1" / f"{rid}{suffix}"
            if src.exists():
                dst = self._trash_destination(f"v1_{src.name}")
                shutil.move(str(src), str(dst))
                self._record_trash_move(manifest, src, dst)
                moved.append(f"outputs/v1/{src.name}")
        for src in sorted((self.state_dir / "runs").glob(
                f"{rid}*.state.json")):
            dst = self._trash_destination(f"state_{src.name}")
            shutil.move(str(src), str(dst))
            self._record_trash_move(manifest, src, dst)
            moved.append(f"state/runs/{src.name}")
        v1_logs = self.log_dir / "v1" / rid
        if v1_logs.is_dir():
            dst = self._trash_destination(f"{rid}_v1logs")
            shutil.move(str(v1_logs), str(dst))
            self._record_trash_move(manifest, v1_logs, dst)
            moved.append(f"logs/v1/{rid}/")

        report = report if isinstance(report, dict) else self._find_report(rid)
        ref = self._snapshot_ref(report)
        if ref and not self._snapshot_referenced(ref, rid):
            snapshot = self._snapshot_file(ref)
            if snapshot:
                self._move_derived(snapshot, "text", moved, "state/text",
                                   manifest=manifest)

        self._purge_owned_cache(self.llm_cache_dir, rid, "cache_llm",
                                "cache/llm", moved, manifest=manifest)
        self._purge_owned_cache(self.state_dir / "research_cache", rid,
                                "cache_research", "state/research_cache",
                                moved, manifest=manifest)
        if self._purge_gap is not None:
            try:
                removed = self._purge_gap(
                    rid, path=self.state_dir / "gap_log.jsonl")
            except OSError:
                removed = 0
            if removed:
                moved.append(f"state/gap_log.jsonl（移除 {removed} 行）")
        return moved

    def _mark_ledger_deleted(self, rid):
        wrote = False
        for chain in (self.ledger_dir / "v1-chain.log",
                      self.ledger_dir / "chain.log"):
            if not chain.exists():
                continue
            try:
                records = ledger_store.read_records(chain)
            except ledger_store.LedgerError as exc:
                print(f"[ledger] 删除墓碑跳过：{exc}", flush=True)
                continue
            target = next(
                (r.get("seq") for r in ledger_store.report_records(records)
                 if r.get("review_id") == rid and r.get("op") != "delete"),
                None,
            )
            if target is None:
                continue
            try:
                ledger_store.tombstone(chain, int(target), actor="web",
                                       reason=f"删除报告 {rid}")
                wrote = True
            except ledger_store.LedgerError as exc:
                print(f"[ledger] 删除墓碑失败：{exc}", flush=True)
        return wrote

    def _mark_ledger_restored(self, rid):
        wrote = False
        for chain in (self.ledger_dir / "v1-chain.log",
                      self.ledger_dir / "chain.log"):
            if not chain.exists():
                continue
            try:
                records = ledger_store.read_records(chain)
            except ledger_store.LedgerError as exc:
                print(f"[ledger] 恢复事件跳过：{exc}", flush=True)
                continue
            target = next(
                (r.get("seq") for r in ledger_store.report_records(records)
                 if r.get("review_id") == rid),
                None,
            )
            if target is None:
                continue
            try:
                ledger_store.restore(chain, int(target), actor="web",
                                     reason=f"从回收站恢复 {rid}")
                wrote = True
            except ledger_store.LedgerError as exc:
                print(f"[ledger] 恢复事件失败：{exc}", flush=True)
        return wrote
