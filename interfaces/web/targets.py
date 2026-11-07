#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待审文件列表 / 软删除 / 撤销的 HTTP 适配层。

路径解析、根目录、回收区和文件类型判断全部注入；这个模块不 import server。
上传仍留在 Handler：它直接读请求体，属于另一条切片。
"""
from __future__ import annotations

import json
import os
import shutil
import time


class TargetFiles:
    def __init__(self, *, root, targets, uploads, trash, target_rel_path,
                 target_role, read_json):
        self._root = root
        self._targets = targets
        self._uploads = uploads
        self._trash = trash
        self._target_rel_path = target_rel_path
        self._target_role = target_role
        self._read_json = read_json

    def list(self):
        items = []
        paths = [p for p in self._targets.glob("*") if p.is_file()]
        paths += [p for p in self._uploads.glob("*") if p.is_file()]
        for p in paths:
            st = p.stat()
            role = self._target_role(p)
            items.append({
                "name": p.name,
                "rel": p.relative_to(self._root).as_posix(),
                "size": st.st_size,
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "mtime_ts": st.st_mtime,
                "uploaded": p.parent == self._uploads,
                "role": role,
                "reviewable": role != "labels",
            })
        # 排序规则（顺序即语义）：
        #   ① 可审对象在前 —— `*.labels.json` 是机读标注（测试用的标准答案），
        #      不是待审原文，绝不能因为字母序靠前就占住「默认选中」这个位置；
        #   ② 同一组内按修改时间**降序** —— 刚上传的简历排第一，
        #      而不是把 targets/ 里的历史样本顶到最前。
        items.sort(key=lambda x: (not x["reviewable"], -x["mtime_ts"], x["name"]))
        for it in items:
            it.pop("mtime_ts", None)
        return {"items": items}

    def delete(self, rel):
        """删除待审列表中的顶层文件或上传文件，统一切到可撤销回收区。"""
        path, err = self._target_rel_path(rel)
        if path is None:
            return {"ok": False, "error": err}
        if path.parent not in (self._targets.resolve(), self._uploads.resolve()):
            return {"ok": False, "error": "只能删除待审列表中的文件"}
        self._trash.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self._trash / f"{stamp}__{path.name}"
        n = 1
        while dest.exists():                     # 同一秒删多个同名文件不能互相覆盖
            n += 1
            dest = self._trash / f"{stamp}-{n}__{path.name}"
        original_rel = path.relative_to(self._root).as_posix()
        manifest = {
            "schema": 1,
            "original_rel": original_rel,
            "trashed": dest.relative_to(self._root).as_posix(),
            "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        manifest_path = self._trash / ".manifests" / f"{dest.name}.json"
        try:
            shutil.move(str(path), str(dest))
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = manifest_path.with_name(
                f"{manifest_path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, manifest_path)
        except OSError as exc:
            # 文件已搬走但清单没写成时必须搬回，避免留下无法从界面撤销的孤儿文件。
            if dest.exists() and not path.exists():
                try:
                    shutil.move(str(dest), str(path))
                except OSError:
                    pass
            return {"ok": False, "error": f"删除失败：{exc}"}
        return {"ok": True, "rel": rel, "name": path.name,
                "trashed": manifest["trashed"],
                "items": self.list()["items"]}

    def restore(self, data):
        """撤销删除：按清单把回收区文件搬回原目录；拒绝覆盖同名文件。"""
        trashed = str((data or {}).get("trashed") or "")
        path, err = self._target_rel_path(trashed)
        if path is None:
            return {"ok": False, "error": err}
        if path.parent != self._trash.resolve():
            return {"ok": False, "error": "该路径不在待审文件回收区"}
        name = path.name.split("__", 1)[-1]      # <时间戳>__<原名>
        manifest_path = self._trash / ".manifests" / f"{path.name}.json"
        original_rel = f"targets/uploads/{name}"
        if manifest_path.is_file():
            try:
                manifest = self._read_json(manifest_path)
                original_rel = str(manifest.get("original_rel") or original_rel)
            except Exception:
                return {"ok": False, "error": "撤销信息已损坏，无法恢复"}
        dest, err = self._target_rel_path(original_rel, must_exist=False)
        if dest is None:
            return {"ok": False, "error": err}
        if dest.parent not in (self._targets.resolve(), self._uploads.resolve()):
            return {"ok": False, "error": "恢复目标不是待审文件位置"}
        if dest.exists():
            return {"ok": False, "error": f"同名文件已存在：{name}"}
        try:
            shutil.move(str(path), str(dest))
        except OSError as exc:
            return {"ok": False, "error": f"恢复失败：{exc}"}
        try:
            manifest_path.unlink()
        except OSError:
            pass
        return {"ok": True, "rel": original_rel, "name": name,
                "items": self.list()["items"]}
