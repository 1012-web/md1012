"""按原图判定结果安全更新正文，保留原始快照和批次执行状态。

manifest.json 必须包含 sourceHash。apply-state.json 记录已执行决定；
同一批次重复运行不重复插字，后续确认的 pending 项仍以原始快照偏移应用。
已执行决定被改写、正文被外部编辑、manifest 换批次时拒绝继续，需重新生成新批次。
备份保留原文件换行；报告保存改动、未判及 already_applied。
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
from pathlib import Path

from content_state import book_file, read_json, read_text, text_hash, write_json, write_text


def load_judgements(folder: Path) -> dict[int, dict]:
    judged: dict[int, dict] = {}
    for path in sorted(folder.glob("judgement-b*.json")):
        for item in read_json(path):
            if item["n"] in judged:
                raise SystemExit(f"编号重复：{item['n']}（{path.name}）")
            judged[item["n"]] = item
    return judged


def decide(judgement: dict, current: str, variants: str) -> tuple[str, str | None]:
    """返回（去向, 新字）：去向为 change / verify / misprint / pending。"""
    verdict, confidence = judgement["verdict"], judgement["confidence"]
    if confidence != "高" or verdict == "D":
        return "pending", None
    if verdict == "A":
        return ("misprint" if judgement.get("misprint") else "verify"), None
    new = judgement["ocr"] if verdict == "B" else judgement["correct"]
    variant = judgement.get("variant")
    if variant:
        new = variant["printed"] if variants == "keep" else variant["standard"]
    return ("verify", None) if new == current else ("change", new)


def apply_edits(text: str, edits: list[tuple[int, int, str, str]]) -> tuple[str, list[tuple[int, int, str, str]]]:
    """edits = [(起, 止, 旧字, 新字)]（UTF-16 偏移）；从后往前改，返回新文本与对不上的项。"""
    data = text.encode("utf-16-le")
    previous_end = -1
    previous_start = -1
    for start, end, old, new in sorted(edits):
        if start < 0 or end < start or end * 2 > len(data):
            raise ValueError("Invalid UTF-16 edit range")
        if start < previous_end or start == previous_start:
            raise ValueError("Overlapping corrections; resolve them before applying")
        previous_start, previous_end = start, end
    mismatched = []
    for start, end, old, new in sorted(edits, reverse=True):
        if data[start * 2:end * 2].decode("utf-16-le") != old:
            mismatched.append((start, end, old, new))
            continue
        data = data[:start * 2] + new.encode("utf-16-le") + data[end * 2:]
    return data.decode("utf-16-le"), mismatched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", type=Path, required=True)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--variants", choices=["keep", "normalize"], required=True,
                        help="异体字方针：keep 保留原书字形，normalize 统一为规范字形（开工时用户选的）")
    parser.add_argument("--skip", default="", help="不执行的编号，逗号分隔（如整页要重录、混进正文的注文）")
    parser.add_argument("--skip-file", action="append", default=[], help="整个文件不执行（如整篇重录的目录）")
    args = parser.parse_args()
    manifest_path = args.dir / "manifest.json"
    manifest_items = read_json(manifest_path)
    manifest = {m["n"]: m for m in manifest_items}
    if len(manifest) != len(manifest_items):
        raise SystemExit("Duplicate manifest numbers")
    judged = load_judgements(args.dir)
    if set(manifest) != set(judged):
        raise SystemExit("Judgement numbers must match the manifest")
    skip = {int(x) for x in args.skip.split(",") if x.strip()}
    report: dict[str, list] = {"changed": [], "verified": [], "misprint": [], "pending": [], "skipped": [], "mismatch": [], "already_applied": []}
    batch_hash = text_hash(json.dumps(manifest_items, ensure_ascii=False, sort_keys=True))
    state_path = args.dir / "apply-state.json"
    state = read_json(state_path) if state_path.exists() else {"version": 1, "manifestHash": batch_hash, "files": {}}
    if state.get("manifestHash") != batch_hash:
        raise SystemExit("Manifest changed after correction began; create a new judgement directory")
    # A pending write is committed only when its exact target revision is present.
    for name, record in state["files"].items():
        path = book_file(args.book, name)
        current_hash = text_hash(read_text(path))
        planned = record.get("planned")
        if planned and current_hash == planned["targetHash"]:
            record.update(currentHash=planned["targetHash"], applied=planned["applied"])
            record.pop("planned", None)
        if current_hash != record["currentHash"]:
            raise SystemExit(f"Source changed outside this correction batch: {name}; regenerate mapping and doubts")
    sources = {}
    for name in {m["file"] for m in manifest.values()}:
        path = book_file(args.book, name)
        hashes = {m.get("sourceHash") for m in manifest.values() if m["file"] == name}
        if len(hashes) != 1 or None in hashes or "" in hashes:
            raise SystemExit(f"Missing or inconsistent sourceHash for {name}; regenerate doubts and crops")
        expected = hashes.pop()
        record = state["files"].get(name)
        current = read_text(path)
        if record is None:
            if text_hash(current) != expected:
                raise SystemExit(f"Stale manifest for {name}; regenerate doubts and crops")
            record = {"sourceHash": expected, "currentHash": expected, "applied": {}}
            state["files"][name] = record
            sources[name] = current
        else:
            if record["sourceHash"] != expected:
                raise SystemExit(f"Source revision changed in manifest: {name}")
            saved = args.dir / "backup" / name
            sources[name] = read_text(saved) if saved.exists() else current
            if text_hash(sources[name]) != expected:
                raise SystemExit(f"Original correction snapshot is unavailable: {name}")
    edits: dict[str, list] = collections.defaultdict(list)
    for n in sorted(judged):
        j, m = judged[n], manifest[n]
        row = {**j, "file": m["file"], "pdfPage": m["pdfPage"], "page": m["page"], "context": m["context"]}
        if n in skip or m["file"] in args.skip_file:
            report["skipped"].append(row)
            continue
        where, new = decide(j, m["minerU"], args.variants)
        applied = state["files"][m["file"]]["applied"].get(str(n))
        if applied:
            if where != "change" or new != applied["new"]:
                raise SystemExit(f"Already-applied judgement {n} was changed; create a new batch")
            report["already_applied"].append(row)
            continue
        if where != "change":
            report["verified" if where == "verify" else where].append(row)
            continue
        edits[m["file"]].append((m["from"], m["to"], m["minerU"], new, n))
    backup = args.dir / "backup"
    backup.mkdir(exist_ok=True)
    plans = {}
    for name, items in edits.items():
        record = state["files"][name]
        cumulative = dict(record["applied"])
        cumulative.update({str(n): {"from": start, "to": end, "old": old, "new": new}
                           for start, end, old, new, n in items})
        all_edits = [(e["from"], e["to"], e["old"], e["new"]) for e in cumulative.values()]
        text, mismatched = apply_edits(sources[name], all_edits)
        if mismatched:
            raise SystemExit(f"Manifest offsets do not match the original text: {name}")
        plans[name] = (text, cumulative, items)
    # Check every source and edit before writing any book file.
    write_json(state_path, state)
    for name, (text, cumulative, items) in plans.items():
        path = book_file(args.book, name)
        saved = backup / name
        saved.parent.mkdir(parents=True, exist_ok=True)
        if not saved.exists():
            shutil.copy2(path, saved)
        elif text_hash(read_text(saved)) != state["files"][name]["sourceHash"]:
            raise SystemExit(f"Backup belongs to another revision: {name}")
        record = state["files"][name]
        record["planned"] = {"targetHash": text_hash(text), "applied": cumulative}
        write_json(state_path, state)
        write_text(path, text)
        record.update(currentHash=text_hash(text), applied=cumulative)
        record.pop("planned", None)
        write_json(state_path, state)
        mismatched = []
        bad = {(s, e) for s, e, _, _ in mismatched}
        for start, end, old, new, n in items:
            m = manifest[n]
            row = {"n": n, "file": name, "pdfPage": m["pdfPage"], "page": m["page"], "old": old, "new": new,
                   "verdict": judged[n]["verdict"], "reason": judged[n]["reason"], "context": m["context"]}
            report["mismatch" if (start, end) in bad else "changed"].append(row)
    report["changed"].sort(key=lambda r: r["n"])
    write_json(args.dir / "apply-report.json", report)
    print("APPLY_DOUBTS " + " ".join(f"{k}={len(v)}" for k, v in report.items()))


if __name__ == "__main__":
    main()
