#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""holdout_eval.py —— 冻结式真实文档 holdout 验收器（只验收，不参与调参）

为什么需要它：
  tests/golden 是作者自己出的 38 道题，只适合回归和调参；在它上面 100% 召回
  不代表真实文档召回。真实召回必须用「没参与过设计/调参」的文档测，而且必须
  防止「边测边改」——否则 holdout 会慢慢退化成第二个 golden。

本工具的三条硬约束：
  1. 物理隔离：只读 holdout/，与 tests/golden/ 没有代码或数据依赖。
  2. 冻结清单：holdout/MANIFEST.json 记录每个 case/label 的 sha256。
     清单缺失、文件增删、任一字节变化都直接退出 2，拒绝评测。
  3. 可复现：纯规则审查（use_llm=False），不联网、不调用 LLM。

目录约定：
  holdout/cases/<name>.txt                 待审文本
  holdout/labels/<name>.labels.json        标注（与 tests/golden/labels 同格式）
                                           没有 label 文件 = 干净对照（期望 0 issue）
  holdout/MANIFEST.json                    冻结清单（由 --save-manifest 生成）

用法：
  python tools/holdout_eval.py --save-manifest           # 标注完成后冻结
  python tools/holdout_eval.py                           # 按冻结清单验收
  python tools/holdout_eval.py --min-recall 0.8 --min-precision 0.9
  python tools/holdout_eval.py --json outputs/holdout/run.json

退出码：
  0 = 评测完成且达到门槛（默认门槛为 0，即不拦）
  1 = 评测完成但低于 --min-recall / --min-precision
  2 = 无法评测（空 holdout、清单缺失/损坏/被改动、标注非法等）

指标口径：
  recall    = 命中标注数 / 标注总数
  precision = 命中标注数 / (命中标注数 + 标注外 issue 数)
              （沿用 tools/golden_eval.py 的标注级口径；同一标注命中多条 issue
                只算一次命中。干净对照上的所有 issue 都计入标注外 issue。）
  两者均给 Wilson 95% 置信区间；分母为 0 时 pct 为 null、区间按 [0, 0] 记录。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import eval_review  # noqa: E402

# 路径常量的默认值。测试用 configure() 注入临时目录，避免碰真实 holdout。
HOLDOUT = ROOT / "holdout"
CASES = HOLDOUT / "cases"
LABELS = HOLDOUT / "labels"
MANIFEST = HOLDOUT / "MANIFEST.json"
OUT_DIR = ROOT / "outputs" / "holdout"

MANIFEST_SCHEMA = "review-agent/holdout-manifest/1"
RESULT_SCHEMA = "review-agent/holdout-result/1"
Z_95 = 1.959963984540054  # NormalDist().inv_cdf(0.975)

EMPTY_MESSAGE = (
    "没有真实 holdout 文档；请按 holdout/README.md 放入 30–50 份"
    "未参与调参的文档"
)


class HoldoutError(Exception):
    """可预期的输入/清单错误：main() 会转成退出码 2。"""


def configure(holdout_dir=None, out_dir=None):
    """注入 holdout / 输出目录。仅供测试或迁移使用，CLI 不暴露。

    生产环境不要用它把 tests/golden 指成 holdout——那等于自己骗自己。
    """
    global HOLDOUT, CASES, LABELS, MANIFEST, OUT_DIR
    if holdout_dir is not None:
        HOLDOUT = Path(holdout_dir)
        CASES = HOLDOUT / "cases"
        LABELS = HOLDOUT / "labels"
        MANIFEST = HOLDOUT / "MANIFEST.json"
    if out_dir is not None:
        OUT_DIR = Path(out_dir)


# ---------------- Wilson 区间 ----------------

def wilson_interval(k, n, z=Z_95):
    """Wilson score 95% 区间，返回 (low, high)，取值 [0, 1]。

    n == 0 时返回 (0.0, 0.0)。k 会被截断到 [0, n]，防止调用方传错。
    公式（无连续性校正）：
      p = k/n
      center = (p + z²/(2n)) / (1 + z²/n)
      margin = z * sqrt(p(1-p)/n + z²/(4n²)) / (1 + z²/n)
    """
    if n <= 0:
        return 0.0, 0.0
    k = max(0, min(int(k), int(n)))
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def rate_block(k, n):
    """把 k/n 包装成带 Wilson 区间的 JSON 友好结构。"""
    low, high = wilson_interval(k, n)
    if n <= 0:
        return {
            "k": int(k),
            "n": 0,
            "pct": None,
            "ci95": [0.0, 0.0],
            "ci95_pct": [0.0, 0.0],
            "method": "wilson",
            "level": 0.95,
            "note": "分母为 0，无法定义比例；置信区间按 [0, 0] 记录。",
        }
    return {
        "k": int(k),
        "n": int(n),
        "pct": round(k / n * 100.0, 1),
        "ci95": [round(low, 6), round(high, 6)],
        "ci95_pct": [round(low * 100.0, 1), round(high * 100.0, 1)],
        "method": "wilson",
        "level": 0.95,
        "note": None,
    }


def format_rate(name, block):
    """控制台展示：分母为 0 时明确写 n/a，不冒充 0%。"""
    if block["n"] <= 0:
        return f"{name}：n/a（分母 0；Wilson 区间按 [0, 0] 记）"
    low, high = block["ci95_pct"]
    return (f"{name}：{block['pct']:.1f}%  95% CI [{low:.1f}%, {high:.1f}%]"
            f"  ({block['k']}/{block['n']})")


# ---------------- 文件与冻结清单 ----------------

def _rel(path):
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_files():
    """收集参与冻结的文件：cases/*.txt 与 labels/*.labels.json。

    .gitkeep、README 等占位文件不参与；只认目录约定里的两种扩展名。
    键固定用 POSIX 相对路径，跨平台生成的清单一致。
    """
    files = {}
    if CASES.is_dir():
        for path in sorted(CASES.glob("*.txt")):
            files[f"cases/{path.name}"] = _sha256(path)
    if LABELS.is_dir():
        for path in sorted(LABELS.glob("*.labels.json")):
            files[f"labels/{path.name}"] = _sha256(path)
    return files


def build_manifest():
    files = _collect_files()
    return {
        "schema": MANIFEST_SCHEMA,
        "frozen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": sum(1 for key in files if key.startswith("cases/")),
        "label_count": sum(1 for key in files if key.startswith("labels/")),
        "total_files": len(files),
        "files": files,
    }


def verify_manifest():
    """校验磁盘与 MANIFEST 完全一致；任何差异都抛 HoldoutError。"""
    if not MANIFEST.exists():
        raise HoldoutError(
            f"缺少冻结清单：{MANIFEST}\n"
            "请先完成标注并冻结：python tools/holdout_eval.py --save-manifest"
        )
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutError(f"冻结清单无法读取或不是合法 JSON：{MANIFEST}（{exc}）") from exc
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        raise HoldoutError(
            f"冻结清单格式不识别：{MANIFEST}\n"
            "请删除后重新冻结：python tools/holdout_eval.py --save-manifest"
        )
    expected = data.get("files")
    if not isinstance(expected, dict) or not expected:
        raise HoldoutError("冻结清单里没有 files 记录，拒绝评测。")

    expected = {str(k): str(v) for k, v in expected.items()}
    actual = _collect_files()
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        key for key in set(expected) & set(actual) if expected[key] != actual[key]
    )

    if missing or extra or changed:
        lines = ["holdout 与冻结清单不一致，拒绝评测（防止边测边改）："]
        if extra:
            lines.append("  清单外多出文件：" + "、".join(extra))
        if missing:
            lines.append("  清单内文件缺失：" + "、".join(missing))
        if changed:
            lines.append("  sha256 变化：" + "、".join(changed))
        lines.append(
            "如确实要变更 holdout（历史结果将不可比），请显式重新冻结："
            "python tools/holdout_eval.py --save-manifest --force"
        )
        raise HoldoutError("\n".join(lines))

    case_count = sum(1 for key in expected if key.startswith("cases/"))
    if data.get("count") != case_count:
        raise HoldoutError(
            f"冻结清单的 count={data.get('count')!r} 与 cases 文件数 {case_count} 不符，"
            "清单被改动过，拒绝评测。"
        )
    return data


# ---------------- 标注 ----------------

def load_labels(path, case_stem):
    """读取并校验一份 labels.json（与 tests/golden/labels 同格式）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutError(f"{Path(path).name} 无法读取或不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("errors"), list):
        raise HoldoutError(f"{Path(path).name} 缺少 errors 数组。")

    seen = set()
    for index, error in enumerate(data["errors"], 1):
        where = f"{case_stem}/{Path(path).name} 第 {index} 条标注"
        if not isinstance(error, dict):
            raise HoldoutError(f"{where} 不是对象。")
        label_id = str(error.get("id") or "").strip()
        if not label_id:
            raise HoldoutError(f"{where} 缺少 id。")
        if label_id in seen:
            raise HoldoutError(f"{where} 的 id={label_id} 重复。")
        seen.add(label_id)
        if not str(error.get("desc") or "").strip():
            raise HoldoutError(f"{where}（id={label_id}）缺少 desc。")
        match = error.get("match")
        if not isinstance(match, list) or not match:
            raise HoldoutError(f"{where}（id={label_id}）的 match 为空。")
        for group in match:
            if not isinstance(group, list) or not group:
                raise HoldoutError(f"{where}（id={label_id}）存在空 OR 组。")
            for pattern in group:
                if not isinstance(pattern, str) or not pattern:
                    raise HoldoutError(f"{where}（id={label_id}）存在空正则。")
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise HoldoutError(
                        f"{where}（id={label_id}）正则非法 {pattern!r}：{exc}"
                    ) from exc
        dims = error.get("exclude_dimensions")
        if dims is not None and (not isinstance(dims, list)
                                 or any(not isinstance(d, str) for d in dims)):
            raise HoldoutError(f"{where}（id={label_id}）的 exclude_dimensions 必须是字符串数组。")
    return data["errors"]


def validate_layout(case_files):
    """冻结/评测前检查：没有孤儿标注，所有标注可解析、可执行。"""
    case_stems = {path.stem for path in case_files}
    for label_path in sorted(LABELS.glob("*.labels.json")):
        stem = label_path.name[: -len(".labels.json")]
        if stem not in case_stems:
            raise HoldoutError(
                f"标注 {label_path.name} 没有对应的 cases/{stem}.txt（孤儿标注），"
                "请补回文档或删除标注。"
            )
        load_labels(label_path, stem)


# ---------------- 评测 ----------------

def _load_items(case_files):
    items = []
    for case in case_files:
        label_path = LABELS / f"{case.stem}.labels.json"
        labels = load_labels(label_path, case.stem) if label_path.exists() else []
        items.append((case, labels))
    return items


def _is_under_root(path):
    """review.run_one 用 relative_to(ROOT) 生成报告里的目标路径。"""
    try:
        Path(path).resolve().relative_to(ROOT.resolve())
        return True
    except (ValueError, OSError):
        return False


def _bridge_dir(items):
    """holdout 在仓库外（测试/迁移）时，复制一份到仓库内的临时快照。

    生产环境的 holdout 就在 ROOT/holdout，不需要复制。复制只为了让
    eval_review.run_once 能拿到 relative_to(ROOT) 的样本路径，同时保证
    审查读到的是一份快照，而不是运行中被改动的文件。
    """
    if all(_is_under_root(case) for case, _labels in items):
        return None
    base = ROOT / "outputs"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=".holdout-eval-", dir=base))
    except OSError as exc:
        raise HoldoutError(f"无法为仓库外 holdout 创建临时快照目录：{exc}") from exc


def run_cases(items):
    bridge = _bridge_dir(items)
    rows = []
    try:
        for case, labels in items:
            run_path = case
            if bridge is not None and not _is_under_root(case):
                run_path = bridge / case.name
                try:
                    shutil.copyfile(case, run_path)
                except OSError as exc:
                    raise HoldoutError(
                        f"复制 holdout 样本 {case.name} 到临时快照失败：{exc}"
                    ) from exc
            try:
                result = eval_review.run_once(run_path, labels, use_llm=False)
            except Exception as exc:  # 审查链路本身出错：当作无法评测，而不是 0 分
                raise HoldoutError(f"审查 {case.name} 时失败：{exc}") from exc
            fp_samples = [
                f"{sample.get('rule_id')}|{sample.get('quote')}"
                for sample in (result.get("unlabeled_samples") or [])
            ]
            rows.append({
                "case": case.name,
                "kind": "positive" if labels else "clean",
                "labels_total": len(labels),
                "labels_hit": len(result.get("hit_labels") or []),
                "hit_labels": sorted(result.get("hit_labels") or []),
                "fp": int(result.get("unlabeled_issues") or 0),
                "fp_samples": fp_samples,
                "issue_count": int(result.get("issues") or 0),
            })
    finally:
        if bridge is not None:
            shutil.rmtree(bridge, ignore_errors=True)
    return rows


def summarize(rows):
    labels_total = sum(row["labels_total"] for row in rows)
    labels_hit = sum(row["labels_hit"] for row in rows)
    fp_total = sum(row["fp"] for row in rows)
    reported = labels_hit + fp_total
    return {
        "cases": len(rows),
        "positive_cases": sum(1 for row in rows if row["kind"] == "positive"),
        "clean_cases": sum(1 for row in rows if row["kind"] == "clean"),
        "labels_total": labels_total,
        "labels_hit": labels_hit,
        "fp_total": fp_total,
        "issue_count": sum(row["issue_count"] for row in rows),
        "recall": rate_block(labels_hit, labels_total),
        "precision": rate_block(labels_hit, reported),
    }


def _write_json(path, payload):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        raise HoldoutError(f"结果 JSON 写入失败：{path}（{exc}）") from exc


def _resolve_out(path_text):
    path = Path(path_text)
    return path if path.is_absolute() else ROOT / path


def render_console(payload):
    metrics = payload["metrics"]
    lines = [
        f"holdout：{metrics['cases']} 份文档"
        f"（阳性 {metrics['positive_cases']}，干净对照 {metrics['clean_cases']}）",
        f"标注：{metrics['labels_total']} 条，命中 {metrics['labels_hit']} 条；"
        f"标注外 issue：{metrics['fp_total']} 条",
        "",
    ]
    for row in payload["cases"]:
        lines.append(
            f"  {row['case']}  [{row['kind']}]  命中 {row['labels_hit']}/"
            f"{row['labels_total']}  FP {row['fp']}  issue {row['issue_count']}"
        )
    lines += [
        "",
        format_rate("召回", metrics["recall"]),
        format_rate("精确率", metrics["precision"]),
    ]
    if metrics["cases"] < 30:
        lines.append(
            f"注意：只有 {metrics['cases']} 份文档，Wilson 区间会很宽；"
            "建议按 holdout/README.md 收集 30–50 份。"
        )
    if payload["failures"]:
        lines.append("")
        lines.append("门槛未通过：")
        lines.extend(f"  - {item}" for item in payload["failures"])
    return "\n".join(lines)


def _run(args):
    case_files = sorted(CASES.glob("*.txt"))
    if not case_files:
        print(EMPTY_MESSAGE)
        if not CASES.exists():
            print(f"（目录不存在：{CASES}）")
        return 2

    if args.save_manifest:
        validate_layout(case_files)
        if MANIFEST.exists() and not args.force:
            raise HoldoutError(
                f"冻结清单已存在：{MANIFEST}\n"
                "重新冻结会让历史 holdout 成绩不可比；确需重冻结请加 --force。"
            )
        manifest = build_manifest()
        _write_json(MANIFEST, manifest)
        print(
            f"已冻结 {manifest['count']} 份文档 / {manifest['label_count']} 份标注"
            f" → {_rel(MANIFEST)}"
            f"（frozen_at={manifest['frozen_at']}）"
        )
        print("从现在起不要修改 holdout 文件；验收通过前也不要拿它调参。")
        return 0

    manifest = verify_manifest()
    validate_layout(case_files)
    items = _load_items(case_files)

    rows = run_cases(items)
    # 评测期间文件若被改动，结果同样不可信：再校验一次。
    verify_manifest()

    metrics = summarize(rows)
    recall_value = (metrics["labels_hit"] / metrics["labels_total"]
                    if metrics["labels_total"] else 0.0)
    reported = metrics["labels_hit"] + metrics["fp_total"]
    precision_value = metrics["labels_hit"] / reported if reported else 0.0

    failures = []
    if recall_value + 1e-12 < args.min_recall:
        failures.append(
            f"召回 {recall_value * 100:.1f}% < 门槛 {args.min_recall * 100:.1f}%"
        )
    if precision_value + 1e-12 < args.min_precision:
        failures.append(
            f"精确率 {precision_value * 100:.1f}% < 门槛 "
            f"{args.min_precision * 100:.1f}%"
        )

    payload = {
        "tool": "tools/holdout_eval.py",
        "schema": RESULT_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "holdout_dir": _rel(HOLDOUT),
        "manifest": {
            "path": _rel(MANIFEST),
            "frozen_at": manifest.get("frozen_at"),
            "count": manifest.get("count"),
            "label_count": manifest.get("label_count"),
            "sha256_verified": True,
        },
        "config": {
            "use_llm": False,
            "min_recall": args.min_recall,
            "min_precision": args.min_precision,
        },
        "metrics": metrics,
        "thresholds": {
            "min_recall": args.min_recall,
            "min_precision": args.min_precision,
        },
        "passed": not failures,
        "failures": failures,
        "cases": rows,
    }

    if args.json:
        out_path = _resolve_out(args.json)
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = OUT_DIR / f"holdout-{stamp}.json"
    try:
        out_path.resolve().relative_to(HOLDOUT.resolve())
    except ValueError:
        pass
    else:
        raise HoldoutError(
            f"结果 JSON 不能写进 holdout/（会污染冻结目录）：{out_path}\n"
            "请改用 outputs/holdout/ 下的路径。"
        )
    _write_json(out_path, payload)

    print(render_console(payload))
    print(f"\n结果 JSON：{_rel(out_path)}")
    if failures:
        return 1
    return 0


def _rate_arg(text):
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"不是数字：{text!r}") from exc
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(
            f"门槛必须在 0..1 之间（0.8 表示 80%），收到 {text!r}"
        )
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="冻结式真实文档 holdout 验收器（纯规则、不联网、不调参）"
    )
    parser.add_argument(
        "--save-manifest", action="store_true",
        help="为当前 cases/labels 计算 sha256 并写入 holdout/MANIFEST.json",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="只与 --save-manifest 搭配：允许覆盖已有冻结清单（历史结果将不可比）",
    )
    parser.add_argument(
        "--min-recall", type=_rate_arg, default=0.0,
        help="最低召回，0..1（默认 0，不设门槛）",
    )
    parser.add_argument(
        "--min-precision", type=_rate_arg, default=0.0,
        help="最低精确率，0..1（默认 0，不设门槛）",
    )
    parser.add_argument(
        "--json", default=None, metavar="PATH",
        help="结果 JSON 路径；默认 outputs/holdout/holdout-<timestamp>.json",
    )
    args = parser.parse_args(argv)

    try:
        return _run(args)
    except HoldoutError as exc:
        print(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
