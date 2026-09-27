"""读丢的注号：把 `<!--?fnref n-->` 待定的注按页归组、截原图，交子代理看图定位；再按结果把 [^n] 放回原位。

两步：
  prepare  扫书文件夹里的编号篇章（[0-9][0-9]-*.md），每个待定注号找到它的注文与所在印刷页、换算物理页，
           取那一页的正文文字（两个页码锚点之间），把原页裁成右、左两半（竖排；横排裁上、下两半）存图，
           写 tasks.json：每页一条 {page, file, printed, images, body, notes:[{n, local, text}]}。
  apply    读子代理写的 result-*.json（[{n, after, confidence, reason}]，after 是正文里紧挨注号之前的一串字），
           在那一页正文里找到 after（必须唯一），把 [^n] 插在它后面、删掉对应的待定记号；
           找不到、不唯一、把握低的一律不动，列进报告交用户。改动前把原文件备份到任务目录。

用法：
  python -X utf8 note_tasks.py prepare --book <书文件夹> --config <书>/_数据/config.json --out <任务目录>
  python -X utf8 note_tasks.py apply --book <书文件夹> --out <任务目录>
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

ANCHOR = re.compile(r"<!--p\.(\d+)-->")
PLACEHOLDER = re.compile(r"<!--\?fnref (\d+)-->")
DEF = re.compile(r"^\[\^(\d+)\]:\s*<!--p\.(\d+)-->\s*(.*)$", re.M)
LOCAL_DIGITS = "一二三四五六七八九十"


def split_body(text: str) -> tuple[str, str]:
    marker = "\n---\n\n## 注释\n"
    return (text.split(marker)[0], text.split(marker, 1)[1]) if marker in text else (text, "")


def page_segment(body: str, printed: int) -> tuple[int, int] | None:
    """这一印刷页在正文里的起止（锚点到下一个锚点）。"""
    anchors = list(ANCHOR.finditer(body))
    for index, match in enumerate(anchors):
        if int(match.group(1)) == printed:
            end = anchors[index + 1].start() if index + 1 < len(anchors) else len(body)
            return match.end(), end
    return None


def prepare(book: Path, config: Path, out: Path) -> None:
    import pymupdf

    cfg = json.loads(config.read_text(encoding="utf-8"))
    offsets = {item.split("|")[0]: int(item.split("|")[1]) for item in cfg.get("fileOffsets", [])}
    default = cfg.get("printedPageOffset", 0)
    vertical = cfg.get("writingMode") == "vertical-rl"
    doc = pymupdf.open(book / cfg["pdf"])
    out.mkdir(parents=True, exist_ok=True)
    pages: dict[tuple[str, int], dict] = {}
    for path in sorted(book.glob("[0-9][0-9]-*.md")):
        text = path.read_text(encoding="utf-8")
        body, notes = split_body(text)
        defs = {int(n): (int(p), t) for n, p, t in DEF.findall(notes)}
        for match in PLACEHOLDER.finditer(body):
            n = int(match.group(1))
            if n not in defs:
                continue
            printed, note_text = defs[n]
            same_page = sorted(k for k, (p, _) in defs.items() if p == printed)
            key = (path.name, printed)
            entry = pages.setdefault(key, {
                "file": path.name, "printed": printed, "page": printed + offsets.get(path.name, default), "notes": []})
            entry["notes"].append({"n": n, "local": LOCAL_DIGITS[same_page.index(n)] if same_page.index(n) < 10 else str(same_page.index(n) + 1),
                                   "text": note_text})
    tasks = []
    for (name, printed), entry in sorted(pages.items(), key=lambda item: (item[0][0], item[0][1])):
        text = (book / name).read_text(encoding="utf-8")
        body, _ = split_body(text)
        span = page_segment(body, printed)
        if span is None:
            continue
        segment = PLACEHOLDER.sub("", body[span[0]:span[1]])
        page = doc[entry["page"] - 1]
        width, height = page.rect.width, page.rect.height
        halves = ([pymupdf.Rect(width * 0.48, 0, width, height), pymupdf.Rect(0, 0, width * 0.52, height)] if vertical
                  else [pymupdf.Rect(0, 0, width, height * 0.52), pymupdf.Rect(0, height * 0.48, width, height)])
        images = []
        for index, clip in enumerate(halves):
            image = out / f"p{entry['page']:04d}-{'RL'[index] if vertical else 'TB'[index]}.png"
            page.get_pixmap(dpi=150, clip=clip).save(image)
            images.append(str(image))
        tasks.append({**entry, "images": images, "body": segment.strip()})
    (out / "tasks.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"NOTE_TASKS pages={len(tasks)} notes={sum(len(t['notes']) for t in tasks)}")


def apply(book: Path, out: Path) -> None:
    tasks = json.loads((out / "tasks.json").read_text(encoding="utf-8"))
    # 注号在各文件里各自从 1 编起，前言的 [^5] 与正文的 [^5] 是两条注：结果要按「哪批、哪项任务」对回去，
    # 不能只按编号合并（batches.json 记每批负责的任务下标范围，如 {"b1": [0, 9], ...}）
    batches = json.loads((out / "batches.json").read_text(encoding="utf-8-sig")) if (out / "batches.json").exists() else {}
    results: dict[tuple[str, int], dict] = {}
    for path in sorted(out.glob("result-*.json")):
        batch = path.stem.split("-", 1)[1]
        items = json.loads(path.read_text(encoding="utf-8"))
        first, last = batches.get(batch, [0, len(tasks) - 1])
        pending = {}
        for item in items:
            pending.setdefault(int(item["n"]), []).append(item)
        for index in range(first, min(last, len(tasks) - 1) + 1):
            for note in tasks[index]["notes"]:
                queue = pending.get(note["n"])
                if queue:
                    results[(tasks[index]["file"], note["n"])] = queue.pop(0)
    backup = out / "backup"
    backup.mkdir(exist_ok=True)
    report = {"placed": [], "skipped": []}
    by_file: dict[str, list[dict]] = {}
    for task in tasks:
        by_file.setdefault(task["file"], []).append(task)
    for name, file_tasks in by_file.items():
        path = book / name
        if not (backup / name).exists():
            shutil.copy2(path, backup / name)
        text = path.read_text(encoding="utf-8")
        body, rest = (text.split("\n---\n\n## 注释\n", 1) + [""])[:2]
        for task in file_tasks:
            span = page_segment(body, task["printed"])
            for note in task["notes"]:
                n = note["n"]
                result = results.get((task["file"], n))
                placeholder = f"<!--?fnref {n}-->"
                if not result or not result.get("after") or result.get("confidence") not in ("high", "高"):
                    report["skipped"].append({"n": n, "page": task["page"], "why": (result or {}).get("reason", "无结果或把握不高")})
                    continue
                span = page_segment(body, task["printed"])
                segment = body[span[0]:span[1]]
                plain = PLACEHOLDER.sub("", segment)
                after = result["after"]
                if plain.count(after) != 1:
                    report["skipped"].append({"n": n, "page": task["page"], "why": f"「{after}」在本页出现 {plain.count(after)} 次"})
                    continue
                # 在带记号的原文里找同一处：去掉记号后的位置换回原文位置
                position = segment.find(after)
                if position < 0:
                    report["skipped"].append({"n": n, "page": task["page"], "why": "被待定记号隔开，找不到连续的字串"})
                    continue
                insert_at = span[0] + position + len(after)
                body = body[:insert_at] + f"[^{n}]" + body[insert_at:]
                body = body.replace(placeholder, "", 1)
                report["placed"].append({"n": n, "page": task["page"], "after": after})
        path.write_text(body + ("\n---\n\n## 注释\n" + rest if rest else ""), encoding="utf-8")
    (out / "apply-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"NOTE_APPLY placed={len(report['placed'])} skipped={len(report['skipped'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--book", type=Path, required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = sub.add_parser("apply")
    a.add_argument("--book", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.book, args.config, args.out)
    else:
        apply(args.book, args.out)


if __name__ == "__main__":
    main()
