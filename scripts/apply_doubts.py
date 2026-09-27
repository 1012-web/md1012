"""把 AI 看原图判定的结果写回正文（书不在 Obsidian 库里、不走笔记1012 插件时用）。

读 <判定目录>/manifest.json（doubt_sheets.py crops 写的）与全部 judgement-b*.json，按 references/ai-judging.md 的执行规则：
把握高的 B/C 改成原书的字（有 variant 时按异体字方针：keep 用原书字形、normalize 用规范字）；A 认可；
把握低或 D 不动、列给站长；A 且 misprint 照录原书、列进校记。

位置用截图时记的 UTF-16 偏移：改前核对该处仍是 manifest 里的正文字，对不上就不改、记 mismatch；
同一文件从后往前改，前面的改动不会让后面的位置挪动。所以判定之后、执行之前，正文不能先动别的地方。
改前把原文件备份到 <判定目录>/backup/。结果写 <判定目录>/apply-report.json，附表可直接抄进校注。

用法：
  python -X utf8 apply_doubts.py --book <书> --dir <书>/_数据/疑字判定 --variants keep [--skip 142,150] [--skip-file 01-目录.md]
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
from pathlib import Path


def load_judgements(folder: Path) -> dict[int, dict]:
    judged: dict[int, dict] = {}
    for path in sorted(folder.glob("judgement-b*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")):
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
    manifest = {m["n"]: m for m in json.loads((args.dir / "manifest.json").read_text(encoding="utf-8"))}
    judged = load_judgements(args.dir)
    missing = sorted(set(manifest) - set(judged))
    if missing:
        raise SystemExit(f"缺号：{missing[:20]}")
    skip = {int(x) for x in args.skip.split(",") if x.strip()}
    report: dict[str, list] = {"changed": [], "verified": [], "misprint": [], "pending": [], "skipped": [], "mismatch": []}
    edits: dict[str, list] = collections.defaultdict(list)
    for n in sorted(judged):
        j, m = judged[n], manifest[n]
        row = {**j, "file": m["file"], "pdfPage": m["pdfPage"], "page": m["page"], "context": m["context"]}
        if n in skip or m["file"] in args.skip_file:
            report["skipped"].append(row)
            continue
        where, new = decide(j, m["minerU"], args.variants)
        if where != "change":
            report["verified" if where == "verify" else where].append(row)
            continue
        edits[m["file"]].append((m["from"], m["to"], m["minerU"], new, n))
    backup = args.dir / "backup"
    backup.mkdir(exist_ok=True)
    for name, items in edits.items():
        path = args.book / name
        if not (backup / name).exists():
            shutil.copy2(path, backup / name)
        text, mismatched = apply_edits(path.read_text(encoding="utf-8"), [e[:4] for e in items])
        path.write_text(text, encoding="utf-8")
        bad = {(s, e) for s, e, _, _ in mismatched}
        for start, end, old, new, n in items:
            m = manifest[n]
            row = {"n": n, "file": name, "pdfPage": m["pdfPage"], "page": m["page"], "old": old, "new": new,
                   "verdict": judged[n]["verdict"], "reason": judged[n]["reason"], "context": m["context"]}
            report["mismatch" if (start, end) in bad else "changed"].append(row)
    report["changed"].sort(key=lambda r: r["n"])
    (args.dir / "apply-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("APPLY_DOUBTS " + " ".join(f"{k}={len(v)}" for k, v in report.items()))


if __name__ == "__main__":
    main()
