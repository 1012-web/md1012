"""将正文与所选第二读者逐页比对，产生核图线索。

--vl-dir、--kandian、--mineru 三选一，与材料横竖排或工作流归属无强绑定。
默认从本书 config.json 读取字形表，--variant 显式覆盖；--pages 限定请求范围。
CLI 验证映射中的正文哈希，缺少哈希或版本过期均须重新生成映射。
疑字中的 minerU 是兼容字段，表示正文现字；ocr 表示 reader 指定的第二读者。
每条保存 sourceHash、UTF-16 偏移和页/行位置，供后续版本校验和裁图。
分歧只是线索，不直接判错或改文；大段分歧另列，缺页返回退出码2。
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
from kandian_reader import kandian_reader  # noqa: E402
from content_state import book_file, page_selection, text_hash, validate_mapping  # noqa: E402

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
    data = json.loads(path.read_text(encoding="utf-8", errors="strict"))
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


def compare(files: list[Path], line_map: dict, reader: dict[int, str] | Path, table: dict[str, str],
            selected: set[int] | None = None, reader_name: str = "second-reader"):
    validate_mapping(files, line_map, require_hash=False)
    if isinstance(reader, Path):  # 老写法：直接给 VL 逐页目录
        reader = vl_reader(reader)
    texts = [path.read_text(encoding="utf-8", errors="strict") for path in files]
    raw16 = [text.encode("utf-16-le") for text in texts]
    pages = markdown_pages(files, line_map, table)
    doubts, large, compared = [], [], 0
    for page in sorted(pages):
        if selected is not None and page not in selected:
            continue
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
                "reader": reader_name, "sourceHash": text_hash(texts[number]),
            })
    for n, item in enumerate(doubts, start=1):
        item["n"] = n
    return doubts, large, compared


def page_punct(files: list[Path], line_map: dict) -> dict[int, dict[str, int]]:
    """成品每个物理页上的引号、顿号个数（按逐行映射把正文切到页）。"""
    counts: dict[int, dict[str, int]] = defaultdict(lambda: dict.fromkeys(PUNCT_WATCH, 0))
    for path in files:
        raw16 = path.read_text(encoding="utf-8", errors="strict").encode("utf-16-le")
        for entry in line_map["files"].get(path.name, {}).get("lines", []):
            piece = raw16[entry["from"] * 2:entry["to"] * 2].decode("utf-16-le", "ignore")
            for mark in PUNCT_WATCH:
                counts[entry["page"]][mark] += piece.count(mark)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", type=Path, required=True, help="书文件夹（Markdown 在顶层）")
    parser.add_argument("--line-map", type=Path, required=True)
    readers = parser.add_mutually_exclusive_group(required=True)
    readers.add_argument("--kandian", action="append", default=[],
                         help="看典逐页结果：目录|起始物理页；默认 1，可重复指定分卷")
    readers.add_argument("--vl-dir", type=Path, help="横排书：VL 逐页结果当第二读者")
    readers.add_argument("--mineru", action="append", default=[],
                        help="竖排书：MinerU 输出目录当第二读者，写成 目录|起始物理页；拆卷的每卷一个")
    parser.add_argument("--variant", action="append", default=[], help="本书字形表的一组，原书字形在前，如 黄黃")
    parser.add_argument("--config", type=Path, help="书配置，默认 <书>/_数据/config.json；--variant 显式覆盖配置字形表")
    parser.add_argument("--pages", help="仅比较这些原 PDF 物理页，如 21-30 或 3,7,12-15")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    line_map = json.loads(args.line_map.read_text(encoding="utf-8", errors="strict"))
    try:
        files = [book_file(args.book, name) for name in line_map["files"]]
        validate_mapping(files, line_map)
        selected = page_selection(args.pages)
        config_path = args.config or args.book / "_数据" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8", errors="strict")) if config_path.exists() else {}
        groups = args.variant if args.variant else config.get("variantEquivalents", [])
        table = variant_table(groups)
        reader = (kandian_reader(args.kandian) if args.kandian else
                  vl_reader(args.vl_dir) if args.vl_dir else mineru_reader(args.mineru))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    name = "Kandian" if args.kandian else "VL" if args.vl_dir else "MinerU"
    mapped = set(markdown_pages(files, line_map, table))
    scope = selected if selected is not None else mapped
    missing = sorted(scope - set(reader))
    unmapped = sorted(scope - mapped)
    reader = {p: text for p, text in reader.items() if p in scope}
    doubts, large, compared = compare(files, line_map, reader, table, selected=scope, reader_name=name)
    out = args.out or args.book / "_数据" / "疑字判定"
    out.mkdir(parents=True, exist_ok=True)
    (out / "doubts.json").write_text(json.dumps(doubts, ensure_ascii=False, indent=1), encoding="utf-8", errors="strict")
    coverage = {"reader": name, "compared_pages": compared, "missing_physical_pages": missing,
                "requested_physical_pages": sorted(scope), "unmapped_requested_pages": unmapped,
                "complete": not missing and not unmapped, "variant_groups": groups}
    (out / "_reader_coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8", errors="strict")
    lines = [f"# 正文与 {name} 读法比对", "",
             f"本次请求物理页：{','.join(map(str, sorted(scope)))}。",
             f"第二读者未覆盖的物理页：{','.join(map(str, missing)) or '无'}。",
             f"底稿未映射的请求页：{','.join(map(str, unmapped)) or '无'}。未覆盖的页不算核对完成。", "",
             f"比了 {compared} 页。异体按本书字形表折叠（{len(table)} 个异体字），注号去掉，标点不比。",
             f"分歧只是线索：{name} 也会读错、会把原书异体写成通行字形，判定一律看原图。", "",
             f"## 疑字（{len(doubts)} 处，一处不超过 {SMALL} 字）", "",
             f"| # | 物理页 | 正文 | {name} | 上下文 |", "|---|---|---|---|---|"]
    lines += [f"| {d['n']} | {d['pdfPage']} | {d['minerU'] or '（无）'} | {d['ocr'] or '（无）'} | {d['context']} |"
              for d in doubts]
    invented = []
    if args.mineru:
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
    if args.kandian:
        ours, punct_rows = page_punct(files, line_map), []
        for page in sorted(set(reader) & set(ours)):
            text = unicodedata.normalize("NFKC", reader[page])
            more = {mark: text.count(mark) - ours[page][mark] for mark in PUNCT_WATCH
                    if text.count(mark) > ours[page][mark]}
            if more:
                punct_rows.append((page, more))
        lines += ["", f"## 补标点线索（{len(punct_rows)} 页）：看典读出的后引号、顿号比正文多", "",
                  "只作核图线索；看典也会漏标点、倒读条目号，不能据此自动改文。", "",
                  "| 物理页 | 多出 |", "|---|---|"]
        lines += [f"| {p} | {'、'.join(f'{m}×{n}' for m, n in more.items())} |" for p, more in punct_rows]
    lines += ["", f"## 大段差异（{len(large)} 处，交结构核查）", "", f"| 物理页 | 正文 | {name} |", "|---|---|---|"]
    lines += [f"| {r['pdfPage']} | {r['minerU'][:40] or '（无）'} | {r['ocr'][:40] or '（无）'} |" for r in large]
    (out / "_vl_compare.md").write_text("\n".join(lines) + "\n", encoding="utf-8", errors="strict")
    print(f"VL_COMPARE reader={name} pages={compared} doubts={len(doubts)} large={len(large)} invented={len(invented)} missing={len(missing)}")
    return 0 if coverage["complete"] else 2


if __name__ == "__main__":
    sys.exit(main())
