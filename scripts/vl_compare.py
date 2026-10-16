"""正文与第二位读者的读法逐页比对，列出汉字分歧，作疑字来源（交 AI 看原图或用户判）。

两条路线各有一位第二读者：
  横排书：底稿是 MinerU，第二读者是 PaddleOCR-VL（--vl-dir）。
  竖排书：底稿是 VL，第二读者是 MinerU vlm 后端（--mineru <输出目录>|<起始物理页>，拆卷的每卷一个）。
    MinerU 竖排认字比 VL 差，但实测不凭空编造（《黄道周集》10 页 VL 有 4 页编造、MinerU 0 页），
    所以竖排书另出两份线索：成品里有、MinerU 同页完全没有的大段文字（疑似 VL 编造）；
    MinerU 读出的引号、顿号比成品多的页（VL 常漏后引号、删书名号之间的顿号，看原图补）。
正文每个字在哪一页、哪一行，取自逐行映射（line_map.py 的 line-map.json）；VL 一页的文字取逐页结果
（paddle_api.py 存的 pNNNN.json，页眉页脚页码图表公式除外），MinerU 一页取 layout.json 的正文块与页下注。
两边都只比汉字与字母数字：NFKC、
按本书字形表把异体折成原书字形（--variant 黄黃 说說，第一个是原书字形）、VL 那边先去掉注号（〔一〕（二）之类）。

分歧分两档：
  - 疑字（doubts.json）：一处只差一到四个字，最该看原图。每条记 n、file、from/to（UTF-16 偏移；
    VL 多出而正文没有的「疑漏字」from=to）、pdfPage、page（印刷页，按前面最近的页码锚点）、
    minerU（正文现在的字）、ocr（VL 读的字）、context（前后文，疑字用 ** 标出）、rect（所在行的框，
    逐页退路的为 null）、writingMode。doubt_sheets.py crops 读它截图。
  - 大段差异：五个字以上，多是整块漏读、注与正文错位，交结构核查，不进疑字。
VL 按上下文写字，会把原书异体、原书误印「改对」，所以分歧只是线索：判定一律看原图，不许拿 VL 的读法直接改正文。

用法：
  python -X utf8 vl_compare.py --book <书文件夹> --line-map <书>/_数据/逐行映射/line-map.json \
      --vl-dir <书>/_数据/VL [--variant 黄黃 --variant 说說] [--out <书>/_数据/疑字判定]
  # 竖排书
  python -X utf8 vl_compare.py --book <书> --line-map … --mineru "<书>/_数据/MinerU原始/<卷>|1" [--mineru "<卷2>|201"] …
stdout 只打一行 ASCII；报告写 <out>/_vl_compare.md。
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from line_map import ANCHOR, VISIBLE_STRIP, comparable, markdown_streams, utf16_offsets  # noqa: E402
from vl_common import SKIP_LABELS  # noqa: E402

# 注号：VL 常把〔一〕写成（一）、「一〕」（丢了开口）甚至《三》，都当注号去掉；要求有收口，「卷二十三」不受影响
MARKER_RE = re.compile(r"[〔（(《]?\s*[一二三四五六七八九十〇0-9]{1,3}\s*[〕）)》]")
LOOKALIKES = {"○": "〇"}  # 〇与○在原图上难分，不算分歧
SMALL = 4  # 一处分歧不超过这么多字才进疑字


def variant_table(groups: list[str]) -> dict[str, str]:
    table: dict[str, str] = {}
    for group in groups:
        chars = [c for c in group if not c.isspace()]
        for char in chars[1:]:
            table[char] = chars[0]
    return table


def fold(char: str, table: dict[str, str]) -> str:
    char = unicodedata.normalize("NFKC", char).lower()
    char = LOOKALIKES.get(char, char)
    return table.get(char, char)


CIRCLED = re.compile(r"[①-⓿❶-➓㉑-㉟㊱-㊿]")  # ①⑴⒈ 等，NFKC 会变成数字


def normalize(text: str, table: dict[str, str]) -> str:
    """VL 一边：去注号（圈码先去，免得 NFKC 变成数字）、去空白标点，NFKC，折异体。"""
    text = MARKER_RE.sub("", unicodedata.normalize("NFKC", CIRCLED.sub("", text)))
    return "".join(fold(c, table) for c in text if comparable(c))


def marker_spans(text: str) -> list[tuple[int, int]]:
    """正文一边也去掉同样的记号（列表编号（1）、残留的圈码），两边才对得齐。返回 UTF-16 区间。"""
    offsets = utf16_offsets(text)
    spans = [(offsets[m.start()], offsets[m.end()]) for m in MARKER_RE.finditer(text)]
    spans += [(offsets[m.start()], offsets[m.end()]) for m in CIRCLED.finditer(text)]
    return sorted(spans)


def vl_page(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return "".join((b.get("block_content") or "") for b in data.get("parsing_res_list", [])
                   if (b.get("block_label") or "") not in SKIP_LABELS)


def mineru_reader(specs: list[str]) -> dict[int, str]:
    """MinerU 各卷 → {物理页: 文字}。spec 是「输出目录|这一卷第一页的物理页码」（不写默认 1）。
    取正文块与页下注（竖排书的注常被 MinerU 丢进 discarded），页眉、页码不要。"""
    from _common import block_text, body_blocks, footnote_blocks, load_layout, strip_tags
    pages: dict[int, str] = {}
    for spec in specs:
        folder, _, first = spec.partition("|")
        layout = load_layout(folder)
        # 「目录|107,142,145」：从原书挑页另存后解析的，逐页写明对应的物理页
        numbers = ([int(x) for x in first.split(",")] if "," in first
                   else [int(first or 1) + i for i in range(len(layout))])
        for number, page in zip(numbers, layout):
            blocks = list(body_blocks(page)) + list(footnote_blocks(page))
            pages[number] = "".join(strip_tags(block_text(b)) for b in blocks)
    return pages


def vl_reader(vl_dir: Path) -> dict[int, str]:
    return {int(path.stem[1:]): vl_page(path) for path in sorted(vl_dir.glob("p[0-9][0-9][0-9][0-9].json"))}


# 补标点线索只数这几种：VL 最常读丢的
PUNCT_WATCH = "」』、"


def markdown_pages(files: list[Path], line_map: dict, table: dict[str, str]):
    """按逐行映射把正文的字分到各物理页。每页返回 [(字, 文件序号, UTF-16 起, UTF-16 止, 行框)]。"""
    body, notes, texts = markdown_streams(files)
    index, markers = {}, {}
    for number, path in enumerate(files):
        entries = sorted(line_map["files"].get(path.name, {}).get("lines", []), key=lambda e: e["from"])
        index[number] = ([e["from"] for e in entries], entries)
        spans = marker_spans(texts[number])
        markers[number] = ([a for a, _ in spans], [b for _, b in spans])
    pages: dict[int, list[tuple]] = defaultdict(list)
    for stream in (body, notes):
        for char, (number, pos16), end16 in zip(stream.chars, stream.where, stream.ends):
            m_starts, m_ends = markers[number]
            m = bisect.bisect_right(m_starts, pos16) - 1
            if m >= 0 and pos16 < m_ends[m]:
                continue
            starts, entries = index[number]
            k = bisect.bisect_right(starts, pos16) - 1
            if k < 0 or pos16 >= entries[k]["to"]:
                continue
            pages[entries[k]["page"]].append((fold(char, table), number, pos16, end16, entries[k]["rect"]))
    return pages


def printed_page(text: str, pos16: int) -> str:
    offsets = utf16_offsets(text)
    printed = None
    for match in ANCHOR.finditer(text):
        if offsets[match.start()] > pos16:
            break
        printed = match[1]
    return f"p.{printed}" if printed else ""


def visible(text16: bytes, start: int, end: int) -> str:
    start, end = max(0, start), max(0, end)
    return VISIBLE_STRIP.sub("", text16[start * 2:end * 2].decode("utf-16-le", "ignore")).replace("\n", "")


def context(text16: bytes, start: int, end: int, size: int = 12) -> str:
    """前后各取一大截再去记号，免得切在页码锚点、注号中间；去完再各留 size 个字。"""
    before = visible(text16, start - size * 4, start)[-size:]
    after = visible(text16, end, end + size * 4)[:size]
    return before + "**" + visible(text16, start, end) + "**" + after


def compare(files: list[Path], line_map: dict, reader: dict[int, str] | Path, table: dict[str, str]):
    if isinstance(reader, Path):  # 老写法：直接给 VL 逐页目录
        reader = vl_reader(reader)
    texts = [path.read_text(encoding="utf-8") for path in files]
    raw16 = [text.encode("utf-16-le") for text in texts]
    pages = markdown_pages(files, line_map, table)
    doubts, large, compared = [], [], 0
    for page in sorted(pages):
        if page not in reader:
            continue
        compared += 1
        ours = pages[page]
        a = "".join(c[0] for c in ours)
        b = normalize(reader[page], table)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            mine, theirs = a[i1:i2], b[j1:j2]
            if len(mine) > SMALL or len(theirs) > SMALL:
                large.append({"pdfPage": page, "minerU": mine, "ocr": theirs})
                continue
            anchor = ours[i1] if i1 < len(ours) else ours[-1]
            number = anchor[1]
            start = anchor[2] if i1 < len(ours) else anchor[3]
            end = ours[i2 - 1][3] if i2 > i1 else start
            data = raw16[number]
            if VISIBLE_STRIP.search(data[start * 2:end * 2].decode("utf-16-le")):
                # 分歧跨过注号、标签、页码锚点：照偏移整段替换会把它们一起删掉，交人看
                large.append({"pdfPage": page, "minerU": mine, "ocr": theirs})
                continue
            doubts.append({
                "file": files[number].name, "from": start, "to": end, "pdfPage": page,
                "page": printed_page(texts[number], start),
                "minerU": data[start * 2:end * 2].decode("utf-16-le"), "ocr": theirs,
                "context": context(data, start, end),
                "rect": anchor[4], "writingMode": line_map.get("writingMode", "horizontal-tb"),
            })
    for n, item in enumerate(doubts, start=1):
        item["n"] = n
    return doubts, large, compared


def page_punct(files: list[Path], line_map: dict) -> dict[int, dict[str, int]]:
    """成品每个物理页上的引号、顿号个数（按逐行映射把正文切到页）。"""
    counts: dict[int, dict[str, int]] = defaultdict(lambda: dict.fromkeys(PUNCT_WATCH, 0))
    for path in files:
        raw16 = path.read_text(encoding="utf-8").encode("utf-16-le")
        for entry in line_map["files"].get(path.name, {}).get("lines", []):
            piece = raw16[entry["from"] * 2:entry["to"] * 2].decode("utf-16-le", "ignore")
            for mark in PUNCT_WATCH:
                counts[entry["page"]][mark] += piece.count(mark)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", type=Path, required=True, help="书文件夹（Markdown 在顶层）")
    parser.add_argument("--line-map", type=Path, required=True)
    parser.add_argument("--vl-dir", type=Path, help="横排书：VL 逐页结果当第二读者")
    parser.add_argument("--mineru", action="append", default=[],
                        help="竖排书：MinerU 输出目录当第二读者，写成 目录|起始物理页；拆卷的每卷一个")
    parser.add_argument("--variant", action="append", default=[], help="本书字形表的一组，原书字形在前，如 黄黃")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if bool(args.vl_dir) == bool(args.mineru):
        parser.error("第二读者给一个：横排书 --vl-dir，竖排书 --mineru")
    line_map = json.loads(args.line_map.read_text(encoding="utf-8"))
    files = [args.book / name for name in line_map["files"]]
    table = variant_table(args.variant)
    reader = vl_reader(args.vl_dir) if args.vl_dir else mineru_reader(args.mineru)
    name = "VL" if args.vl_dir else "MinerU"
    doubts, large, compared = compare(files, line_map, reader, table)
    out = args.out or args.book / "_数据" / "疑字判定"
    out.mkdir(parents=True, exist_ok=True)
    (out / "doubts.json").write_text(json.dumps(doubts, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# 正文与 {name} 读法比对", "",
             f"比了 {compared} 页。异体按本书字形表折叠（{len(table)} 个异体字），注号去掉，标点不比。",
             f"分歧只是线索：{name} 也会读错、会把原书异体写成通行字形，判定一律看原图。", "",
             f"## 疑字（{len(doubts)} 处，一处不超过 {SMALL} 字）", "",
             f"| # | 物理页 | 正文 | {name} | 上下文 |", "|---|---|---|---|---|"]
    lines += [f"| {d['n']} | {d['pdfPage']} | {d['minerU'] or '（无）'} | {d['ocr'] or '（无）'} | {d['context']} |"
              for d in doubts]
    invented = []
    if not args.vl_dir:
        # 正文里有、MinerU 同页一个字都对不上的成段文字：VL 在空白、透印处编造的典型样子
        invented = [r for r in large if len(r["minerU"]) >= 8 and not r["ocr"]]
        large = [r for r in large if r not in invented]
        lines += ["", f"## 疑似 VL 编造（{len(invented)} 处）：正文有、MinerU 同页没有的成段文字", "",
                  "逐条看原图：原书没有就删，写进交付报告；是 MinerU 漏读就保留。", "",
                  "| 物理页 | 正文 |", "|---|---|"]
        lines += [f"| {r['pdfPage']} | {r['minerU'][:60]} |" for r in invented]
        ours, punct_rows = page_punct(files, line_map), []
        pages_with_text = set(ours)
        for page in sorted(reader):
            theirs = {mark: reader[page].count(mark) for mark in PUNCT_WATCH}
            more = {mark: theirs[mark] - ours[page][mark] for mark in PUNCT_WATCH if theirs[mark] > ours[page][mark]}
            if more and page in pages_with_text:
                punct_rows.append((page, more))
        lines += ["", f"## 补标点线索（{len(punct_rows)} 页）：MinerU 读出的引号、顿号比正文多", "",
                  "VL 常漏后引号、删掉书名号之间的顿号。逐页看原图补；MinerU 也会多读，不照它改。", "",
                  "| 物理页 | 多出 |", "|---|---|"]
        lines += [f"| {p} | {'、'.join(f'{m}×{n}' for m, n in more.items())} |" for p, more in punct_rows]
    lines += ["", f"## 大段差异（{len(large)} 处，交结构核查）", "", f"| 物理页 | 正文 | {name} |", "|---|---|---|"]
    lines += [f"| {r['pdfPage']} | {r['minerU'][:40] or '（无）'} | {r['ocr'][:40] or '（无）'} |" for r in large]
    (out / "_vl_compare.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"VL_COMPARE reader={name} pages={compared} doubts={len(doubts)} large={len(large)} invented={len(invented)}")


if __name__ == "__main__":
    main()
