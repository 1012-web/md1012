"""逐行映射：把成品 Markdown 的每一行原书文字对到 PDF 页面上的行框（竖排是列框）。

不装任何 OCR 模型、不上传文件：行框来自底稿本身——
  - MinerU 底稿（横排书）：layout.json / *_middle.json 的行框与行内文字。VLM 后端的「一行」常是整段，
    所以每条都在框内按墨迹投影再切；切出几行，文字按各行长度分摊（记为 estimated），只切出一行就照用原框。
  - VL 底稿（竖排书，或 MinerU 不行改用 VL 的书）：VL 只给块框，块内按墨迹投影切行／切列，块文字按长度分摊。
    切的方向逐块定：块标签是 vertical_text 就竖切，否则跟全书书写方向（横排书的封底竖排字也能切对）。
退回逐块（整块一个框，kind=block）的情形，报告逐条列出：
  - 对到文件名含「目录」的 Markdown 上的块、带引导点（……）的块、与同页别的块大面积重叠的块
    （目录页上篇名与页码排在一行，按长度分摊不准；VL 在目录页上块框也画得乱）；
  - 块内切不出行。
逐行、逐块都没对上的字退到逐页（kind=page，只有页码、没有框）：按前面最近的页码锚点 <!--p.N--> 推。图、表、页眉页脚页码不映射。

对齐：块的文字（只比字母数字与汉字，标点、空白、Markdown 记号都不算）在正文里从上一个块的落点往后找，
SequenceMatcher 逐字配对；脚注块先在「[^n]:」注文里找，找不到再到正文里找（VL 不一定认得出注）。
一块里配上的字不到六成就算没对上，不硬塞位置。行的范围延伸到下一行起点，行尾标点归上一行。

输出（--out 目录）：
  line-map.json   {"version":1, "granularity":"line", "writingMode", "pdf", "files": {文件: {"hash", "lines": [
                   {"from","to"（UTF-16 偏移）,"page"（物理页，1 起）,"rect"（页面 0–1 比例）,
                    "kind": "line"|"block"|"page", "method": "mineru-line"|"ink"|"block"|"anchor",
                    "estimated": bool}]}}}（逐页的 rect 为 null）
  line-map.report.json  块数、逐行／逐块／没对上的块（页码与开头几个字）、正文覆盖率、退到逐页的段数
--spec 另出文字层清单（pdf_textlayer.py build 读）：每行的字沿书写方向在行框内均分，是推定位置，
选中、搜索能落到行，字与字的边界不精确。

用法：
  python -X utf8 line_map.py --pdf 书.pdf --md 02-正文.md [--md ...] --layout <MinerU>/layout.json|1 [--layout ...] \
      --out <书>/_数据/逐行映射 [--spec <书>/_数据/逐行映射/textlayer.json.gz]
  python -X utf8 line_map.py --pdf 书.pdf --md ... --vl-dir <书>/_数据/VL --writing-mode vertical-rl --out ...
stdout 只打一行 ASCII。
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import gzip
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vl_common import SKIP_LABELS, ink_lines  # noqa: E402

FOOTNOTE_TYPES = {"page_footnote", "footnote", "vision_footnote"}
MINERU_SKIP = {"header", "footer", "page_number", "image_body", "table_body", "interline_equation", "discarded"}
LEADER = re.compile(r"(…{2,}|\.{4,}|·{3,}|．{3,}|・{3,}|⋯{2,})")
MIN_COVERAGE = 0.6
TOC_FILE = re.compile(r"目[录録錄]")
RESYNC_MIN_CHARS = 40
RESYNC_COVERAGE = 0.8


@dataclass
class Line:
    rect: list[float]
    text: str | None  # None：文字按长度从块里分摊
    method: str
    page: int | None = None  # 跨页段落挪来的行在下一页；None 即块所在页


@dataclass
class Block:
    page: int
    rect: list[float]
    text: str
    footnote: bool
    vertical: bool
    lines: list[Line] = field(default_factory=list)
    block_level: str | None = None  # 退回逐块的原因


KEEP_SYMBOLS = {"○", "□"}  # 〇的常见写法、原书缺字框：不是字母数字，但在古籍里是字，要参与对齐与比对


def comparable(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LN" or ch in KEEP_SYMBOLS


def clean(text: str) -> str:
    return "".join(ch for ch in text.lower() if comparable(ch))


# ---------- Markdown → 可比字流（逐字记回 文件 + UTF-16 偏移） ----------

STRIP = re.compile(
    r"\A---\n.*?\n---\n"            # frontmatter
    r"|<!--.*?-->"                  # 页码锚点与注释
    r"|%%.*?%%"                     # Obsidian 注释
    r"|!\[\[[^\]]*\]\]"             # 嵌入
    r"|!\[[^\]]*\]\([^)]*\)"        # 图片
    r"|\[\^[^\]]+\]:?"              # 注号与注文开头
    r"|</?[A-Za-z][^>]*>"           # HTML 标签（<small> <mark> 等，内容保留）
    r"|\]\([^)]*\)"                 # 链接目标
    r"|\^[A-Za-z0-9-]+$",           # 块标识
    re.S | re.M,
)


@dataclass
class Stream:
    chars: list[str] = field(default_factory=list)
    where: list[tuple[int, int]] = field(default_factory=list)  # (文件序号, UTF-16 偏移)
    ends: list[int] = field(default_factory=list)                # 该字之后的 UTF-16 偏移

    def text(self) -> str:
        return "".join(self.chars)


def utf16_offsets(text: str) -> list[int]:
    offsets, total = [], 0
    for ch in text:
        offsets.append(total)
        total += 2 if ord(ch) > 0xFFFF else 1
    offsets.append(total)
    return offsets


def markdown_streams(files: list[Path]) -> tuple[Stream, Stream, list[str]]:
    """正文流与注文流。注文是以 [^n]: 开头的行（及其续行缩进）；其余都是正文。"""
    body, notes = Stream(), Stream()
    texts = []
    for number, path in enumerate(files):
        text = path.read_text(encoding="utf-8")
        texts.append(text)
        offsets = utf16_offsets(text)
        masked = [False] * len(text)
        for match in STRIP.finditer(text):
            for i in range(match.start(), match.end()):
                masked[i] = True
        in_note = False
        line_start = 0
        for i, ch in enumerate(text):
            if i == line_start:
                rest = text[i:i + 3]
                if rest.startswith("[^"):
                    in_note = True
                elif text[i:i + 1] not in (" ", "\t", "\n") and not rest.startswith("[^"):
                    in_note = False
            if ch == "\n":
                line_start = i + 1
            if masked[i] or not comparable(ch):
                continue
            target = notes if in_note else body
            target.chars.append(ch.lower())
            target.where.append((number, offsets[i]))
            target.ends.append(offsets[i + 1])
    return body, notes, texts


# ---------- 源：MinerU 行框 / VL 块框 + 墨迹切行 ----------

def norm(bbox, width, height) -> list[float]:
    x0, y0, x1, y1 = bbox
    return [round(x0 / width, 6), round(y0 / height, 6), round(x1 / width, 6), round(y1 / height, 6)]


def overlaps(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / smaller if smaller > 0 else 0.0


def mineru_blocks(parts: list[tuple[Path, int]], doc) -> list[Block]:
    blocks: list[Block] = []
    pages: dict[int, dict[str, Any]] = {}
    for path, start in parts:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for index, page in enumerate(payload["pdf_info"]):
            pages[start + index] = page
    # 跨页段落：MinerU 把下一页开头的行挪进上一页的段尾（坐标仍是下一页的），下一页留一个
    # lines_deleted 的空块、框与挪走的行相同。按这个认出挪来的行属于哪一页。
    moved: dict[int, list[list[float]]] = {
        number: [b["bbox"] for b in page.get("para_blocks", []) if b.get("lines_deleted")]
        for number, page in pages.items()
    }
    for number, page in sorted(pages.items()):
        width, height = page["page_size"]
        raw = []
        for block in page.get("para_blocks", []):
            raw.extend(block.get("blocks") or [block]) if block.get("type") in ("image", "table") else raw.append(block)
        raw.extend(b for b in page.get("discarded_blocks", []) if b.get("type") in FOOTNOTE_TYPES)
        for block in raw:
            kind = block.get("type", "")
            if kind in MINERU_SKIP or not block.get("lines"):
                continue
            lines = []
            for position, ln in enumerate(block["lines"]):
                text = "".join(span.get("content", "") for span in ln.get("spans", []) if span.get("type") != "image")
                on = number
                if position:
                    on = next((n for n in (number + 1, number + 2) if ln["bbox"] in moved.get(n, [])), number)
                w, h = pages[on]["page_size"] if on in pages else (width, height)
                rect = norm(ln["bbox"], w, h)
                # VLM 后端的「一行」常是整段（2026 年实测：一段一条 line），流水线后端才是真的一行；
                # 一律按墨迹再切，切出一条就说明本来就是一行，照用 MinerU 的框
                pieces = ink_lines(doc[on - 1], rect, False)
                if len(pieces) > 1:
                    shares = split_by_length(text, [p[2] - p[0] for p in pieces])
                    lines.extend(Line(p, s, "ink", on) for p, s in zip(pieces, shares))
                else:
                    lines.append(Line(rect, text, "mineru-line", on))
            text = "".join(line.text or "" for line in lines)
            blocks.append(Block(number, norm(block["bbox"], width, height), text, kind in FOOTNOTE_TYPES, False, lines))
    return blocks


def vl_blocks(vl_dir: Path, pages: list[int], doc, vertical_book: bool) -> list[Block]:
    blocks: list[Block] = []
    for number in pages:
        path = vl_dir / f"p{number:04d}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        kept = [b for b in data.get("parsing_res_list", [])
                if b.get("block_label") not in SKIP_LABELS and (b.get("block_content") or "").strip()]
        rects = [norm(b["block_bbox"], data["width"], data["height"]) for b in kept]
        for i, (block, rect) in enumerate(zip(kept, rects)):
            label = block["block_label"]
            vertical = label == "vertical_text" or (vertical_book and label != "text")
            item = Block(number, rect, block["block_content"], label in FOOTNOTE_TYPES, vertical)
            if LEADER.search(item.text):
                item.block_level = "leader-dots"
            elif any(overlaps(rect, other) > 0.3 for j, other in enumerate(rects) if j != i):
                item.block_level = "overlapping-blocks"
            else:
                pieces = ink_lines(doc[number - 1], rect, vertical)
                if not pieces:
                    item.block_level = "no-ink-lines"
                else:
                    axis = (1, 3) if vertical else (0, 2)
                    shares = split_by_length(item.text, [p[axis[1]] - p[axis[0]] for p in pieces])
                    item.lines = [Line(p, s, "ink") for p, s in zip(pieces, shares)]
            blocks.append(item)
    return blocks


def split_by_length(text: str, lengths: list[float]) -> list[str]:
    """按各行长度比例把文字分给各行（只数可比字；标点跟着前一个字走）。"""
    total = sum(lengths) or 1.0
    marks = [i for i, ch in enumerate(text) if comparable(ch)]
    count = len(marks)
    cuts, acc = [], 0.0
    for length in lengths[:-1]:
        acc += length
        k = round(count * acc / total)
        cuts.append(marks[k] if k < count else len(text))
    bounds = [0, *cuts, len(text)]
    return [text[bounds[i]:bounds[i + 1]] for i in range(len(lengths))]


# ---------- 对齐 ----------

def align(block_text: str, stream: str, cursor: int, wide: bool) -> tuple[dict[int, int], float]:
    if not block_text:
        return {}, 0.0
    end = min(len(stream), cursor + len(block_text) * 3 + (3000 if wide else 300))
    window = stream[cursor:end]
    matcher = difflib.SequenceMatcher(None, block_text, window, autojunk=False)
    pairs = {}
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            pairs[a + k] = cursor + b + k
    return pairs, len(pairs) / len(block_text)


def locate(block: Block, texts: dict[str, str], cursors: dict[str, int]) -> tuple[str, dict[int, int]] | None:
    text = clean(block.text)
    order = ["notes", "body"] if block.footnote else ["body", "notes"]
    # 宽窗口只给长块：短块（目录里的篇名）在后文里常有一模一样的标题，一跳就越过整篇前言
    for wide in (False, True) if len(text) >= RESYNC_MIN_CHARS else (False,):
        for name in order:
            pairs, coverage = align(text, texts[name], cursors[name], wide)
            if coverage >= MIN_COVERAGE and compact(pairs, len(text)):
                cursors[name] = max(pairs.values()) + 1
                return name, pairs
    # 落点跟丢了（前面有几块正文里没有、或底稿与成品的篇章顺序不同）：拿块里几段 8 字串
    # 往后整篇找，找到就在那里重新对齐，落点跟过去。只给够长的块、且要对得很齐：
    # 目录里的短篇名在正文里也有，拿它们跳会把落点一下带到书尾（黄道周集实测）
    if len(text) < RESYNC_MIN_CHARS:
        return None
    for name in order:
        start = resync(text, texts[name], cursors[name])
        if start is None:
            continue
        pairs, coverage = align(text, texts[name], max(0, start - 50), False)
        if coverage >= RESYNC_COVERAGE and compact(pairs, len(text)):
            cursors[name] = max(pairs.values()) + 1
            return name, pairs
    return None


def compact(pairs: dict[int, int], size: int) -> bool:
    """配上的字要挤在一处：散落在窗口各处的零星同字（目录里的短篇名常这样）不算对上。"""
    if not pairs:
        return False
    return max(pairs.values()) - min(pairs.values()) + 1 <= size * 1.5 + 20


def resync(text: str, stream: str, cursor: int, gram: int = 8) -> int | None:
    if len(text) < gram * 2:
        return None
    starts = []
    for offset in (0, len(text) // 3, 2 * len(text) // 3):
        probe = text[offset:offset + gram]
        found = stream.find(probe, cursor)
        if found >= 0:
            starts.append(found - offset)
    if len(starts) < 2:
        return None
    starts.sort()
    # 至少两段落在同一处附近才算数，免得撞上一句常见话
    for a, b in zip(starts, starts[1:]):
        if b - a <= len(text) // 2 + 20:
            return max(cursor, a)
    return None


def build(blocks: list[Block], files: list[Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    body, notes, texts = markdown_streams(files)
    streams = {"body": body, "notes": notes}
    stream_texts = {name: stream.text() for name, stream in streams.items()}
    cursors = {"body": 0, "notes": 0}
    entries: dict[int, list[dict[str, Any]]] = {i: [] for i in range(len(files))}
    covered: dict[str, set[int]] = {"body": set(), "notes": set()}
    report = {"blocks": len(blocks), "lineBlocks": 0, "blockLevel": [], "unmapped": [], "lines": 0}
    for block in blocks:
        found = locate(block, stream_texts, cursors)
        if not found:
            report["unmapped"].append({"page": block.page, "text": block.text[:20]})
            continue
        name, pairs = found
        stream = streams[name]
        covered[name].update(pairs.values())
        if not block.block_level and TOC_FILE.search(files[stream.where[min(pairs.values())][0]].stem):
            block.block_level = "toc-file"  # 目录：篇名、引导点、页码在纸面上排成一行，按长度分摊不准
        if block.block_level or not block.lines:
            spans = [(block.rect, block.page, 0, len(clean(block.text)), "block", "block", False)]
            report["blockLevel"].append({"page": block.page, "reason": block.block_level, "text": block.text[:20]})
        else:
            spans, pos = [], 0
            for line in block.lines:
                size = len(clean(line.text or ""))
                spans.append((line.rect, line.page or block.page, pos, pos + size, "line", line.method, line.method == "ink"))
                pos += size
            report["lineBlocks"] += 1
        placed = []
        for rect, page, start, end, kind, method, estimated in spans:
            hits = [pairs[k] for k in range(start, end) if k in pairs]
            if not hits:
                continue
            first, last = min(hits), max(hits)
            file_number, from16 = stream.where[first]
            if stream.where[last][0] != file_number:
                last = max(h for h in hits if stream.where[h][0] == file_number)
            placed.append({"file": file_number, "from": from16, "to": stream.ends[last], "page": page,
                           "rect": rect, "kind": kind, "method": method, "estimated": estimated})
        for current, following in zip(placed, placed[1:]):  # 行尾标点归上一行
            if following["file"] == current["file"] and following["from"] > current["to"]:
                current["to"] = following["from"]
        for item in placed:
            entries[item.pop("file")].append(item)
            report["lines"] += 1
    total = len(body.chars) + len(notes.chars)
    report["coverage"] = round((len(covered["body"]) + len(covered["notes"])) / total, 4) if total else 0.0
    report["pageOnly"] = 0
    report["noPage"] = 0
    files_out = {}
    for number, path in enumerate(files):
        found = sorted(entries[number], key=lambda e: e["from"])
        gaps, unplaced = page_fallback(texts[number], found, streams, number)
        report["pageOnly"] += len(gaps)
        report["noPage"] += unplaced
        files_out[path.name] = {"hash": hashlib.sha256(texts[number].encode("utf-8")).hexdigest(),
                                "lines": sorted(found + gaps, key=lambda e: e["from"])}
    return files_out, report


ANCHOR = re.compile(r"<!--p\.(\d+)-->")


def page_fallback(text: str, found: list[dict[str, Any]], streams: dict[str, Stream],
                  file_number: int) -> tuple[list[dict[str, Any]], int]:
    """逐行、逐块都没对上的字退到逐页：按它前面最近的页码锚点 <!--p.N-->（印刷页）定页，
    印刷页换成物理页用离它最近的已对上的行（两者之差在插页处会变，所以取就近的，不取全书一个数）。
    只有页码、没有框（kind=page, rect=null）。锚点与已对上的行都没有的，算不出页，计数报告。"""
    offsets = utf16_offsets(text)
    anchors = [(offsets[m.start()], int(m[1])) for m in ANCHOR.finditer(text)]
    if not anchors:
        return [], 0

    anchor_at = [at for at, _ in anchors]

    def printed_at(pos16: int) -> int | None:
        index = bisect.bisect_right(anchor_at, pos16) - 1
        return anchors[index][1] if index >= 0 else None

    deltas = [(e["from"], e["page"] - p) for e in found if (p := printed_at(e["from"])) is not None]
    starts = [e["from"] for e in found]
    ends = [e["to"] for e in found]

    def covered(pos16: int) -> bool:
        index = bisect.bisect_right(starts, pos16) - 1
        return index >= 0 and pos16 < ends[index]

    groups: list[list[int]] = []  # [from16, to16, printed]
    for stream in streams.values():
        for (number, pos16), end16 in zip(stream.where, stream.ends):
            if number != file_number or covered(pos16):
                continue
            printed = printed_at(pos16)
            # 中间隔着已对上的行，或换了页，就另起一段
            if (groups and groups[-1][2] == printed
                    and bisect.bisect_left(starts, groups[-1][1]) == bisect.bisect_left(starts, pos16)):
                groups[-1][1] = end16
            else:
                groups.append([pos16, end16, printed])
    out, unplaced = [], 0
    for start, end, printed in groups:
        # 试过「夹在同一页两行之间就取那一页」，黄道周集上反而更差（页码对的字 74% 对 82%），不用
        if printed is not None and deltas:
            page, method = printed + min(deltas, key=lambda d: abs(d[0] - start))[1], "anchor"
        else:
            unplaced += 1
            continue
        out.append({"from": start, "to": end, "page": page, "rect": None,
                    "kind": "page", "method": method, "estimated": True})
    return out, unplaced


VISIBLE_STRIP = re.compile(r"<!--.*?-->|%%.*?%%|\[\^[^\]]+\]:?|</?[A-Za-z][^>]*>|[*_#>`]", re.S)


def textlayer_spec(files_out: dict[str, Any], files: list[Path], vertical_book: bool, pdf: Path) -> dict[str, Any]:
    pages: dict[str, list[list[Any]]] = {}
    for path in files:
        data = path.read_text(encoding="utf-8").encode("utf-16-le")
        for entry in files_out[path.name]["lines"]:
            if entry["kind"] != "line":
                continue
            value = data[entry["from"] * 2:entry["to"] * 2].decode("utf-16-le")
            chars = [ch for ch in VISIBLE_STRIP.sub("", value) if not ch.isspace()]
            if not chars:
                continue
            x0, y0, x1, y1 = entry["rect"]
            vertical = (y1 - y0) > (x1 - x0)
            step = ((y1 - y0) if vertical else (x1 - x0)) / len(chars)
            for i, ch in enumerate(chars):
                rect = [x0, y0 + i * step, x1, y0 + (i + 1) * step] if vertical else [x0 + i * step, y0, x0 + (i + 1) * step, y1]
                pages.setdefault(str(entry["page"]), []).append([ch, *[round(v, 6) for v in rect]])
    return {"version": 1, "pdf": pdf.name, "writingMode": "vertical-rl" if vertical_book else "horizontal-tb",
            "pages": dict(sorted(pages.items(), key=lambda kv: int(kv[0])))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--md", type=Path, action="append", required=True, help="成品 Markdown，按原书顺序给")
    parser.add_argument("--layout", action="append", default=[], help="MinerU layout.json|物理起页")
    parser.add_argument("--vl-dir", type=Path, help="VL 逐页结果目录（paddle_api.py 的输出）")
    parser.add_argument("--pages", help="只映射这些物理页（VL 源），如 1-497；默认全部")
    parser.add_argument("--writing-mode", choices=("horizontal-tb", "vertical-rl"), default="horizontal-tb")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--spec", type=Path, help="另出文字层清单（.json.gz）")
    args = parser.parse_args()
    if bool(args.layout) == bool(args.vl_dir):
        raise SystemExit("give either --layout (MinerU source) or --vl-dir (VL source)")

    import pymupdf

    doc = pymupdf.open(args.pdf)
    vertical_book = args.writing_mode == "vertical-rl"
    if args.layout:
        parts = [(Path(v.rsplit("|", 1)[0]), int(v.rsplit("|", 1)[1])) for v in args.layout]
        blocks = mineru_blocks(parts, doc)
        source = "mineru"
    else:
        if args.pages:
            first, _, last = args.pages.partition("-")
            pages = list(range(int(first), int(last or first) + 1))
        else:
            pages = list(range(1, doc.page_count + 1))
        blocks = vl_blocks(args.vl_dir, pages, doc, vertical_book)
        source = "vl"
    files_out, report = build(blocks, args.md)
    report.update({"source": source, "writingMode": args.writing_mode,
                   "blockLevelCount": len(report["blockLevel"]), "unmappedCount": len(report["unmapped"])})
    args.out.mkdir(parents=True, exist_ok=True)
    result = {"version": 1, "granularity": "line", "source": source, "writingMode": args.writing_mode,
              "pdf": args.pdf.name, "files": files_out}
    (args.out / "line-map.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    (args.out / "line-map.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.spec:
        spec = textlayer_spec(files_out, args.md, vertical_book, args.pdf)
        args.spec.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(args.spec, "wt", encoding="utf-8") as handle:
            json.dump(spec, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"LINEMAP source={source} blocks={report['blocks']} lines={report['lines']} "
          f"blockLevel={report['blockLevelCount']} unmapped={report['unmappedCount']} coverage={report['coverage']}")


if __name__ == "__main__":
    main()
