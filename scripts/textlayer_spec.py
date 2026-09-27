"""从逐字映射导出文字层清单（pdf_textlayer.py build 用的 spec），横排竖排都行。

笔记1012 插件的「校订版 PDF」也能导出清单，但它给没坐标的字「挨着邻字横向排」，竖排书上会排进隔壁一列；
竖排书（或不开 Obsidian 时）用本脚本。每个字的位置按顺序取：
  读出的字位（chars）→ 两家读法不同时的 OCR 位置（candidates，写的仍是正文的字）
  → 推定框（estimated，标点注号）→ 同块邻字沿书写方向排一格（兜底，报告里计数）。
写进清单的永远是 Markdown 当前的字；映射过期（正文改过而没重建映射）就停下，不导出。
脚注号 [^n] 不是纸面文字，不写（纸面上的〔一〕因此不进文字层）。

用法：
  python -X utf8 textlayer_spec.py --book <书文件夹> --output <清单.json.gz> [--report <报告.json>]
stdout 只打一行 ASCII。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

BOOK_FILE = "_数据/book.json"


def utf16_slice(text: str, start: int, end: int) -> str:
    data = text.encode("utf-16-le")
    return data[start * 2:end * 2].decode("utf-16-le")


def load_sidecar(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def step(rect: list[float], vertical: bool, forward: bool) -> list[float]:
    """沿书写方向挪一格（竖排向下，横排向右；forward=False 反向）。"""
    x0, y0, x1, y1 = rect
    if vertical:
        size = (y1 - y0) * (1 if forward else -1)
        return [x0, round(y0 + size, 6), x1, round(y1 + size, 6)]
    size = (x1 - x0) * (1 if forward else -1)
    return [round(x0 + size, 6), y0, round(x1 + size, 6), y1]


def export(book: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    entry = json.loads((book / BOOK_FILE).read_text(encoding="utf-8"))
    index = json.loads((book / entry["index"]).read_text(encoding="utf-8"))
    writing_mode = entry.get("writingMode", "horizontal-tb")
    vertical = writing_mode == "vertical-rl"
    pages: dict[str, list[list[Any]]] = defaultdict(list)
    counts = {"mapped": 0, "candidate": 0, "estimated": 0, "neighbour": 0, "dropped": 0}
    stale: list[str] = []
    for file, chapter in index["chapters"].items():
        text = (book / file).read_text(encoding="utf-8")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != chapter["hash"]:
            stale.append(file)
            continue
        sidecar = load_sidecar(book / chapter["map"])
        for block in sidecar["blocks"]:
            if block.get("kind") in {"image", "table"}:
                continue
            placed: dict[int, tuple[int, int, int, list[float], str]] = {}
            for char in block.get("chars", []):
                placed[char["from"]] = (char["from"], char["to"], char["page"], char["rect"], "mapped")
            for char in block.get("candidates", []):
                placed.setdefault(char["from"], (char["from"], char["to"], char["page"], char["rect"], "candidate"))
            for char in block.get("estimated", []):
                placed.setdefault(char["from"], (char["from"], char["to"], char["page"], char["rect"], "estimated"))
            spans = sorted(
                [(item[0], item[1]) for item in placed.values()]
                + [(start, end) for start, end in block.get("missing", []) if start not in placed]
            )
            items: list[list[Any]] = []
            for start, end in spans:
                value = utf16_slice(text, start, end)
                if not value.strip():
                    continue
                if start in placed:
                    _, _, page, rect, kind = placed[start]
                    items.append([start, value, page, rect, kind])
                else:
                    items.append([start, value, None, None, "neighbour"])
            # 兜底：没有任何坐标的字挨着同块最近的已定位字，沿书写方向排一格
            for number, item in enumerate(items):
                if item[2] is not None:
                    continue
                before = next((items[k] for k in range(number - 1, -1, -1) if items[k][2] is not None), None)
                after = next((items[k] for k in range(number + 1, len(items)) if items[k][4] != "neighbour"), None)
                if before is not None:
                    item[2], item[3] = before[2], step(before[3], vertical, True)
                elif after is not None:
                    item[2], item[3] = after[2], step(after[3], vertical, False)
            for start, value, page, rect, kind in items:
                if page is None:
                    counts["dropped"] += 1
                    continue
                counts[kind] += 1
                pages[str(page)].append([value, *[round(float(v), 6) for v in rect]])
    spec = {"version": 1, "bookId": entry.get("id"), "pdf": entry.get("pdf"), "writingMode": writing_mode,
            "pages": dict(sorted(pages.items(), key=lambda item: int(item[0])))}
    report = {"book": entry.get("title"), "writingMode": writing_mode, "pages": len(pages), **counts,
              "staleChapters": stale}
    return spec, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", type=Path, required=True, help="书文件夹（含 _数据/book.json）")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    spec, report = export(args.book)
    if report["staleChapters"]:
        target = args.report or args.output.with_suffix(".report.json")
        target.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        raise SystemExit("mapping is stale for some chapters (Markdown changed since the map was built); rerun pdf_sync generate")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as handle:
        json.dump(spec, handle, ensure_ascii=False, separators=(",", ":"))
    target = args.report or args.output.parent / (args.output.name.split(".")[0] + ".report.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"SPEC pages={report['pages']} mapped={report['mapped']} candidate={report['candidate']} "
          f"estimated={report['estimated']} neighbour={report['neighbour']} dropped={report['dropped']}")


if __name__ == "__main__":
    main()
