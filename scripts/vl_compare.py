"""最终正文与 PaddleOCR-VL 的读法逐页比对，列出分歧，作疑字来源（交 AI 看原图或站长判）。

正文每个字的物理页取自逐字映射（读出的字位、候选、推定；都没有的跟前一个字同页）；VL 一页的文字取
vl_parse.py 存下的各块（页眉页脚页码图表公式除外）。两边都：NFKC、去空白、按本书字形表把异体折成原书字形
（「黃」算「黄」，config 的 variantEquivalents）、去掉注号（〔一〕（二）之类，正文里注号是 [^n]、不在字里）。
剩下的差异分两类列出：汉字分歧（最该看）与只差标点。VL 按上下文写字，会把原书异体、原书误印「改对」，
所以分歧只是线索：判定一律看原图，不许拿 VL 的读法直接改正文。

用法：
  python -X utf8 vl_compare.py --book <书文件夹> --vl-dir <书>/_数据/VL [--out <书>/_数据/过程报告/_vl_compare.md]
stdout 只打一行 ASCII；同名 .json 给程序用。
"""
from __future__ import annotations

import argparse
import difflib
import gzip
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

SKIP_LABELS = {"header", "footer", "number", "header_image", "footer_image", "image", "table", "chart",
               "formula", "display_formula", "inline_formula", "seal", "formula_number"}
# 注号：VL 常把〔一〕写成（一）、「一〕」（丢了开口）甚至《三》，都当注号去掉；要求有收口，「卷二十三」不受影响
MARKER_RE = re.compile(r"[〔（(《]?\s*[一二三四五六七八九十〇0-9]{1,3}\s*[〕）)》]")
LOOKALIKES = {"○": "〇"}  # 〇与○在原图上难分，不算分歧
PUNCT = set("，。、；：！？「」『』《》（）〔〕…—·,.;:!?()[]\"'“”‘’")
ASCII_TO_WIDE = {",": "，", "(": "（", ")": "）", "!": "！", ":": "：", ";": "；", "?": "？"}


def utf16_slice(text: str, start: int, end: int) -> str:
    return text.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")


def variant_table(book: Path, entry: dict) -> dict[str, str]:
    config = (entry.get("rebuild") or {}).get("config")
    if not config:
        return {}
    raw = json.loads((book / config).read_text(encoding="utf-8"))
    table: dict[str, str] = {}
    for group in raw.get("variantEquivalents", []):
        chars = [c for c in group if not c.isspace()]
        for char in chars[1:]:
            table[char] = chars[0]
    return table


def normalize(text: str, table: dict[str, str]) -> str:
    text = MARKER_RE.sub("", unicodedata.normalize("NFKC", text))
    out = []
    for char in text:
        if char.isspace():
            continue
        char = LOOKALIKES.get(ASCII_TO_WIDE.get(char, char), ASCII_TO_WIDE.get(char, char))
        out.append(table.get(char, char))
    return "".join(out)


def markdown_pages(book: Path, entry: dict) -> dict[int, str]:
    index = json.loads((book / entry["index"]).read_text(encoding="utf-8"))
    pages: dict[int, list[str]] = defaultdict(list)
    for file, chapter in index["chapters"].items():
        text = (book / file).read_text(encoding="utf-8")
        raw = (book / chapter["map"]).read_bytes()
        sidecar = json.loads((gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8"))
        last_page = None
        for block in sidecar["blocks"]:
            if block.get("kind") in {"image", "table"}:
                continue
            page_of = {c["from"]: c["page"] for key in ("chars", "candidates", "estimated") for c in block.get(key, [])}
            spans = sorted({(c["from"], c["to"]) for key in ("chars", "candidates", "estimated") for c in block.get(key, [])}
                           | {tuple(m) for m in block.get("missing", [])})
            for start, end in spans:
                page = page_of.get(start, last_page)
                if page is None:
                    continue
                last_page = page
                pages[page].append(utf16_slice(text, start, end))
    return {page: "".join(chars) for page, chars in pages.items()}


def vl_page(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return "".join((b.get("block_content") or "") for b in data.get("parsing_res_list", [])
                   if (b.get("block_label") or "") not in SKIP_LABELS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", type=Path, required=True)
    parser.add_argument("--vl-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--context", type=int, default=8)
    args = parser.parse_args()
    entry = json.loads((args.book / "_数据" / "book.json").read_text(encoding="utf-8"))
    table = variant_table(args.book, entry)
    md = markdown_pages(args.book, entry)
    rows = []
    compared = 0
    for page in sorted(md):
        path = args.vl_dir / f"p{page:04d}.json"
        if not path.exists():
            continue
        compared += 1
        a, b = normalize(md[page], table), normalize(vl_page(path), table)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            ours, theirs = a[i1:i2], b[j1:j2]
            kind = "标点" if all(c in PUNCT for c in ours + theirs) else "汉字"
            rows.append({"page": page, "kind": kind, "text": ours, "vl": theirs,
                         "before": a[max(0, i1 - args.context):i1], "after": a[i2:i2 + args.context]})
    out = args.out or args.book / "_数据" / "过程报告" / "_vl_compare.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    hanzi = [r for r in rows if r["kind"] == "汉字"]
    punct = [r for r in rows if r["kind"] == "标点"]
    lines = ["# 正文与 PaddleOCR-VL 读法比对", "",
             f"比了 {compared} 页。异体按本书字形表折叠（{len(table)} 个异体字），注号两边都去掉。",
             "分歧只是线索：VL 会把原书异体、原书误印「改对」，判定一律看原图。", "",
             f"## 汉字分歧（{len(hanzi)} 处）", "", "| 物理页 | 正文 | VL | 上下文 |", "|---|---|---|---|"]
    lines += [f"| {r['page']} | {r['text'] or '（无）'} | {r['vl'] or '（无）'} | {r['before']}【{r['text']}】{r['after']} |"
              for r in hanzi]
    lines += ["", f"## 只差标点（{len(punct)} 处）", "", "| 物理页 | 正文 | VL | 上下文 |", "|---|---|---|---|"]
    lines += [f"| {r['page']} | {r['text'] or '（无）'} | {r['vl'] or '（无）'} | {r['before']}【{r['text']}】{r['after']} |"
              for r in punct]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"VL_COMPARE pages={compared} hanzi={len(hanzi)} punct={len(punct)}")


if __name__ == "__main__":
    main()
