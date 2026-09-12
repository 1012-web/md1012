#!/usr/bin/env python3
"""Build Obsidian sidecars linking Markdown source offsets to PDF coordinates."""

from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
from io import BytesIO
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol


ANCHOR_RE = re.compile(r"<!--p\.(-?\d+)-->")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.S)
PAGES_RE = re.compile(r"^pages:\s*(-?\d+)", re.M)
COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
ANNOTATION_TAG_RE = re.compile(r"</?(?:mark|u|s)\b[^<>]*>")
ANNOTATION_NOTE_RE = re.compile(r"%%@[0-9a-z]{6,12}[\s\S]*?%%")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
FOOTNOTE_REF_RE = re.compile(r"\[\^\d+\](?::)?")
MARKDOWN_LINK_RE = re.compile(r"\[([^]]+)\]\([^)]*\)")
WIKILINK_TARGET_RE = re.compile(r"\[\[([^]|]+)\|([^]]+)\]\]")
# Obsidian 的块标识（行尾的 ^tb-c01-h0053）只供块链接使用，纸面上并没有这串字；
# 不遮掉它，标题就永远差这十几个字的坐标，块文本也会带着它去找版面区域。
BLOCK_ID_RE = re.compile(r"(?m)[ \t]+\^[A-Za-z0-9][A-Za-z0-9-]*[ \t]*$")
BLOCK_SPLIT_RE = re.compile(r"(?:\r?\n){2,}")
SUBSTANTIVE_RE = re.compile(r"[\w\u3400-\u9fff]", re.UNICODE)
YAML_ITEM_RE = re.compile(r"(\s*)-\s")
YAML_ITEM_PREFIX_RE = re.compile(r"\s*-\s+")
YAML_KEY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]*)\s*:\s*(.*)$")
# 目录页的引导点：Markdown 写两个「……」，NFKC 展开成 6 个句点，纸面上却是几十个点。
# 两边点数永远对不上，逐字对齐把多出来的点当成删除，连带把点后面的页码也挤错位。
# 把连续的点号整段折叠成一个记号，两边就都只剩一个点，点两侧的文字照常逐字对齐。
# 单个「·」是正文里真正的间隔号（「工人运动的发展·宪章运动」），长度不足 2 的一律不碰。
LEADER_DOT_CHARS = frozenset(".·•‧・⋯")
PAGE_RANGE_HINT = "page range offset must look like 'file|start-end|offset': "
FILE_OFFSET_HINT = "file offset must look like 'file|offset': "
PAGE_OVERRIDE_HINT = "page override must look like 'file|printed|physical': "
DEFAULT_DETECTION_MODEL = "PP-OCRv5_mobile_det"
# ONNX 后端的默认识别模型。Paddle 后端（--rec-model）仍默认 PP-OCRv5_mobile_rec：
# 本机只留着那一份 Paddle 格式的权重，换默认值会让「装了 paddleocr 就能跑」的回退路径去联网下载。
DEFAULT_ONNX_MODEL = "PP-OCRv6_medium_rec"
# PaddleX 官方源里 ONNX 版的目录名：识别模型名后面缀 _onnx，与 Paddle 原生权重并排放着。
ONNX_MODEL_DIRECTORIES = {
    "PP-OCRv5_mobile_rec": "PP-OCRv5_mobile_rec_onnx",
    "PP-OCRv5_server_rec": "PP-OCRv5_server_rec_onnx",
    "PP-OCRv6_medium_rec": "PP-OCRv6_medium_rec_onnx",
}
# 识别网络输入规格 (通道, 高, 基准宽) 的兜底值；实际按模型目录 inference.yml 的 RecResizeImg 读。
# PP-OCRv5 与 PP-OCRv6 目前都是 3x48x320，但写死就等于赌下一代不改，所以只当默认值用。
ONNX_RECOGNITION_SHAPE = (3, 48, 320)
ONNX_MAX_IMAGE_WIDTH = 3200
# Bump whenever the line-recovery recipe changes: it keys the detection cache, so old results stop being replayed.
# det-v1 cropped tight and trusted the detector; det-v2 bleeds white space in and falls back when it reads nothing.
LINE_DETECTION_ALGORITHM = "det-v2"
# Blocks MinerU describes with one outer rectangle but no visual lines; the page footnote is the bulk of them.
# Running heads, page numbers, footers and tables are deliberately absent: their text is not in the Markdown,
# so supplementing them would only feed noise into the sequence alignment.
SUPPLEMENTABLE_BLOCK_KINDS = frozenset({
    "text", "paragraph_title", "title", "doc_title", "list",
    "page_footnote", "index", "ref_text", "aside_text",
    "image_caption", "image_footnote",
})


@dataclass
class VisibleChar:
    value: str
    source_from: int
    source_to: int
    printed_page: int
    physical_page: int
    block_id: str = ""
    rect: tuple[float, float, float, float] | None = None
    candidate_value: str | None = None
    candidate_rect: tuple[float, float, float, float] | None = None


@dataclass
class MarkdownBlock:
    file: str
    source_from: int
    source_to: int
    kind: str
    chars: list[VisibleChar] = field(default_factory=list)
    regions: list[tuple[int, tuple[float, float, float, float]]] = field(default_factory=list)
    lines: list[tuple[int, tuple[float, float, float, float]]] = field(default_factory=list)
    page_hints: list[int] = field(default_factory=list)
    block_id: str = ""
    region_polygons: dict[tuple[int, tuple[float, float, float, float]], list[tuple[float, float]]] = field(
        default_factory=dict
    )
    line_roles: dict[tuple[int, tuple[float, float, float, float]], str] = field(default_factory=dict)


@dataclass
class LayoutBlock:
    page: int
    rect: tuple[float, float, float, float]
    text: str
    kind: str
    polygon: list[tuple[float, float]] | None = None


@dataclass
class OCRChar:
    value: str
    rect: tuple[float, float, float, float]


class CharacterOCR(Protocol):
    backend: str

    def recognize(
        self,
        image: Any,
        line_rects: list[tuple[float, float, float, float]],
    ) -> list[OCRChar]: ...


def read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="strict", newline="\n") as handle:
        handle.write(text)


def write_text_atomic(path: Path, text: str) -> None:
    """Replace a shared file in one step so a crash mid-write cannot leave a truncated catalog."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", errors="strict", newline="\n") as target:
            target.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def utf16_offsets(text: str) -> list[int]:
    out = [0]
    total = 0
    for char in text:
        total += len(char.encode("utf-16-le")) // 2
        out.append(total)
    return out


def normalize_units(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", value) if not char.isspace())


def normalized_chars(chars: Iterable[VisibleChar | OCRChar]) -> tuple[str, list[tuple[Any, ...]]]:
    """展开成对齐用的单元序列；每个单元记下它背后的一个或多个来源字符。

    一个来源字符可以展开成多个单元（「…」→「...」），连续的引导点又会被折叠回一个
    单元，所以单元与字符不是一一对应，owners 统一按元组给出。
    """
    units: list[str] = []
    owners: list[tuple[Any, ...]] = []
    for char in chars:
        for unit in normalize_units(char.value):
            units.append(unit)
            owners.append((char,))
    return collapse_dot_leaders(units, owners)


def collapse_dot_leaders(
    units: list[str],
    owners: list[tuple[Any, ...]],
) -> tuple[str, list[tuple[Any, ...]]]:
    """把长度 >= 2 的点号连串压成单个「.」，该串覆盖的来源字符全挂在这一个单元上。"""
    out_units: list[str] = []
    out_owners: list[tuple[Any, ...]] = []
    index = 0
    while index < len(units):
        end = index
        while end < len(units) and units[end] in LEADER_DOT_CHARS:
            end += 1
        if end - index >= 2:
            merged: list[Any] = []
            for position in range(index, end):
                for owner in owners[position]:
                    # 按身份去重：同一个「…」展开出的三个点共用一个来源字符。
                    if not any(owner is kept for kept in merged):
                        merged.append(owner)
            out_units.append(".")
            out_owners.append(tuple(merged))
            index = end
            continue
        out_units.append(units[index])
        out_owners.append(owners[index])
        index += 1
    return "".join(out_units), out_owners


def mask_range(mask: list[bool], start: int, end: int) -> None:
    for index in range(max(0, start), min(len(mask), end)):
        mask[index] = False


def markdown_mask(block: str) -> list[bool]:
    mask = [True] * len(block)
    for pattern in (
        COMMENT_RE, IMAGE_RE, FOOTNOTE_REF_RE, ANNOTATION_TAG_RE, ANNOTATION_NOTE_RE, BLOCK_ID_RE,
    ):
        for match in pattern.finditer(block):
            mask_range(mask, match.start(), match.end())
    for match in MARKDOWN_LINK_RE.finditer(block):
        mask_range(mask, match.start(), match.start(1))
        mask_range(mask, match.end(1), match.end())
    for match in WIKILINK_TARGET_RE.finditer(block):
        mask_range(mask, match.start(), match.start(2))
        mask_range(mask, match.end(2), match.end())
    for match in re.finditer(r"\[\[([^\]]+)\]\]", block):
        mask_range(mask, match.start(), match.start() + 2)
        mask_range(mask, match.end() - 2, match.end())
    for match in re.finditer(r"(?m)^(?:#{1,6}|>|[-+*]|\d+[.)])\s+", block):
        mask_range(mask, match.start(), match.end())
    for index, char in enumerate(block):
        if char.isspace() or char in "*_~`":
            mask[index] = False
    if block.strip() in {"---", "***", "___"}:
        mask_range(mask, 0, len(mask))
    return mask


def block_kind(block: str) -> str:
    stripped = block.lstrip()
    if re.match(r"\[\^\d+\]:", stripped):
        return "footnote"
    if stripped.startswith("#"):
        return "heading"
    if stripped.startswith("!"):
        return "image"
    if stripped.startswith("|") or "<table" in stripped:
        return "table"
    return "paragraph"


def strip_yaml_quotes(value: str) -> str:
    """Remove one matching pair of surrounding quotes; inner quotes are part of the title."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def normalized_title(raw: str) -> str:
    """Fold a heading or a frontmatter title into the single key both sides are matched on."""
    text = COMMENT_RE.sub("", raw)
    text = BLOCK_ID_RE.sub("", text)
    text = ANNOTATION_NOTE_RE.sub("", text)
    text = ANNOTATION_TAG_RE.sub("", text)
    text = WIKILINK_TARGET_RE.sub(r"\2", text)
    text = re.sub(r"\[\[([^\]|]+)\]\]", r"\1", text)
    text = MARKDOWN_LINK_RE.sub(r"\1", text)
    text = re.sub(r"^\s*#+\s*", "", text.strip())
    return normalize_units(re.sub(r"[\s*_`]+", "", text))


def first_page_number(value: str) -> int | None:
    """Read the first page of ``1-15``, ``[16, 54]``, ``'-36-5'`` or a bare number."""
    text = strip_yaml_quotes(value)
    if text.startswith("["):
        text = strip_yaml_quotes(text[1:].split(",", 1)[0].rstrip("]"))
    match = re.match(r"\s*(-?\d+)", text)
    return int(match.group(1)) if match else None


def chapter_start_pages(frontmatter: str) -> dict[str, int]:
    """Map normalized chapter titles to their first printed page from a merged body's ``chapter_metadata``.

    Merged bodies carry the page anchors of every page except a chapter's first one (that page number
    only lived in the chapter file's frontmatter), so the heading and opening paragraphs would otherwise
    inherit the previous chapter's last footnote page.

    Only ``title``/``pages`` at a chapter entry's own indentation count: nested lists (aliases and the
    like) must not be mistaken for the next chapter, and a deeper mapping must not overwrite the entry.
    """
    starts: dict[str, int] = {}
    entry: dict[str, Any] = {}
    in_metadata = False
    sequence_indent: int | None = None
    entry_indent: int | None = None

    def flush() -> None:
        title, page = entry.get("title"), entry.get("page")
        # The first entry under a repeated title wins: a later duplicate must not move the anchor.
        if title and page is not None and title not in starts:
            starts[title] = page
        entry.clear()

    def absorb(text: str) -> None:
        match = YAML_KEY_RE.match(text)
        if not match:
            return
        key, value = match.group(1), match.group(2)
        if key == "title" and "title" not in entry:
            title = normalized_title(strip_yaml_quotes(value))
            if title:
                entry["title"] = title
        elif key == "pages" and "page" not in entry:
            page = first_page_number(value)
            if page is not None:
                entry["page"] = page

    for line in frontmatter.splitlines():
        if not line.strip():
            continue
        item = YAML_ITEM_RE.match(line)
        indent = len(line) - len(line.lstrip())
        if item is None and indent == 0:
            flush()
            in_metadata = bool(re.match(r"chapter_metadata\s*:", line))
            sequence_indent = entry_indent = None
            continue
        if not in_metadata:
            continue
        if item is not None:
            dash_indent = len(item.group(1))
            if sequence_indent is None:
                sequence_indent = dash_indent
            if dash_indent != sequence_indent:
                continue  # A nested list such as aliases, not the next chapter.
            flush()
            prefix = YAML_ITEM_PREFIX_RE.match(line)
            entry_indent = prefix.end() if prefix else dash_indent + 2
            absorb(line[entry_indent:])
            continue
        if entry_indent is not None and indent == entry_indent:
            absorb(line[indent:])
    flush()
    return starts


def heading_title(raw: str) -> str:
    # A block starts with its heading; later lines of the same block are not part of the title.
    return normalized_title(raw.split("\n", 1)[0])


def parse_markdown_file(
    path: Path,
    vault_root: Path,
    printed_to_physical: Callable[[int], int],
) -> tuple[str, list[MarkdownBlock]]:
    text = read_text(path)
    offsets = utf16_offsets(text)
    front = FRONTMATTER_RE.match(text)
    body_start = front.end() if front else 0
    first_page = 1
    chapter_starts: dict[str, int] = {}
    if front:
        page_match = PAGES_RE.search(front.group(1))
        if page_match:
            first_page = int(page_match.group(1))
        chapter_starts = chapter_start_pages(front.group(1))
    current_page = first_page
    blocks: list[MarkdownBlock] = []
    ranges: list[tuple[int, int]] = []
    cursor = body_start
    note_spans = [(match.start(), match.end()) for match in ANNOTATION_NOTE_RE.finditer(text)]
    ignored_spans = note_spans + [(match.start(), match.end()) for match in COMMENT_RE.finditer(text)]
    for separator in BLOCK_SPLIT_RE.finditer(text, body_start):
        if any(start <= separator.start() < end for start, end in ignored_spans):
            continue
        ranges.append((cursor, separator.start()))
        cursor = separator.end()
    ranges.append((cursor, len(text)))

    for absolute_start, absolute_end in ranges:
        if absolute_end <= absolute_start:
            continue
        raw = text[absolute_start:absolute_end]
        mask = markdown_mask(raw)
        block = MarkdownBlock(
            file=path.relative_to(vault_root).as_posix(),
            source_from=offsets[absolute_start],
            source_to=offsets[absolute_end],
            kind=block_kind(raw),
        )
        if block.kind == "heading" and chapter_starts:
            chapter_start = chapter_starts.get(heading_title(raw))
            if chapter_start is not None:
                current_page = chapter_start
        anchor_by_start = {
            anchor.start(): int(anchor.group(1)) for anchor in ANCHOR_RE.finditer(raw)
            if not any(start <= absolute_start + anchor.start() < end for start, end in note_spans)
        }
        for local_index, char in enumerate(raw):
            if local_index in anchor_by_start:
                current_page = anchor_by_start[local_index]
            if not mask[local_index]:
                continue
            physical = printed_to_physical(current_page)
            block.chars.append(VisibleChar(
                value=char,
                source_from=offsets[absolute_start + local_index],
                source_to=offsets[absolute_start + local_index + 1],
                printed_page=current_page,
                physical_page=physical,
            ))
        if block.chars or block.kind == "image":
            block.block_id = "b" + hashlib.sha1(
                f"{block.file}:{block.source_from}:{block.source_to}".encode("utf-8")
            ).hexdigest()[:12]
            for char in block.chars:
                char.block_id = block.block_id
            block.page_hints = sorted({char.physical_page for char in block.chars}) or [
                printed_to_physical(current_page)
            ]
            blocks.append(block)
    return text, blocks


def parse_page_range_offset(value: str) -> tuple[str, int, int, int]:
    """Parse ``file|start-end|offset`` into (file, start, end, offset).

    A missing field or a single page (``file|61|3``) is a configuration mistake, not a shorthand:
    both raise the same message so the user sees the expected shape instead of an int() traceback.
    """
    if not isinstance(value, str):
        raise ValueError(PAGE_RANGE_HINT + repr(value))
    parts = value.rsplit("|", 2)
    if len(parts) != 3:
        raise ValueError(PAGE_RANGE_HINT + value)
    file_text, range_text, offset_text = (part.strip() for part in parts)
    match = re.fullmatch(r"(-?\d+)-(-?\d+)", range_text)
    if not file_text or not match or not re.fullmatch(r"[+-]?\d+", offset_text):
        raise ValueError(PAGE_RANGE_HINT + value)
    start, end = int(match.group(1)), int(match.group(2))
    if start > end:
        raise ValueError(f"page range must be ascending: {value}")
    return Path(file_text).as_posix(), start, end, int(offset_text)


def parse_offset_config(
    file_offsets: Iterable[str],
    page_overrides: Iterable[str],
    page_range_offsets: Iterable[str],
) -> tuple[dict[str, int], dict[tuple[str, int], int], dict[str, list[tuple[int, int, int]]]]:
    """Turn the three ``file|...`` CLI/config lists into the lookups ``page_mapper_for_file`` wants.

    Shared by the mapper CLI and the pdf_sync entry point so a dry-run check cannot disagree with the
    pagination the real run applies.
    """
    offsets: dict[str, int] = {}
    for value in file_offsets:
        if not isinstance(value, str) or len(value.rsplit("|", 1)) != 2:
            raise ValueError(FILE_OFFSET_HINT + repr(value))
        name, offset_text = (part.strip() for part in value.rsplit("|", 1))
        if not name or not re.fullmatch(r"[+-]?\d+", offset_text):
            raise ValueError(FILE_OFFSET_HINT + value)
        offsets[Path(name).as_posix()] = int(offset_text)
    overrides: dict[tuple[str, int], int] = {}
    for value in page_overrides:
        if not isinstance(value, str) or len(value.rsplit("|", 2)) != 3:
            raise ValueError(PAGE_OVERRIDE_HINT + repr(value))
        name, printed_text, physical_text = (part.strip() for part in value.rsplit("|", 2))
        if not name or not all(re.fullmatch(r"[+-]?\d+", text) for text in (printed_text, physical_text)):
            raise ValueError(PAGE_OVERRIDE_HINT + value)
        overrides[(Path(name).as_posix(), int(printed_text))] = int(physical_text)
    ranges: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for value in page_range_offsets:
        name, start, end, offset = parse_page_range_offset(value)
        ranges[name].append((start, end, offset))
    return offsets, overrides, dict(ranges)


def page_mapper_for_file(
    file: str,
    default_offset: int,
    file_offsets: dict[str, int],
    page_overrides: dict[tuple[str, int], int],
    range_offsets: dict[str, list[tuple[int, int, int]]] | None = None,
) -> Callable[[int], int]:
    offset = file_offsets.get(file, default_offset)
    ranges = (range_offsets or {}).get(file, [])

    def map_page(printed_page: int) -> int:
        override = page_overrides.get((file, printed_page))
        if override is not None:
            return override
        for start, end, range_offset in ranges:
            if start <= printed_page <= end:
                return printed_page + range_offset
        return printed_page + offset

    return map_page


def block_text(block: dict[str, Any]) -> str:
    values: list[str] = []
    for line in block.get("lines") or []:
        for span in line.get("spans") or []:
            values.append(str(span.get("content") or span.get("text") or ""))
    for child in block.get("blocks") or []:
        values.append(block_text(child))
    return "".join(values)


def load_layout_parts(parts: list[tuple[Path, int]]) -> dict[int, list[LayoutBlock]]:
    pages: dict[int, list[LayoutBlock]] = defaultdict(list)
    for layout_path, physical_start in parts:
        with layout_path.open("r", encoding="utf-8", errors="strict") as handle:
            payload = json.load(handle)
        for page in payload["pdf_info"]:
            width, height = page["page_size"]
            physical_page = physical_start + int(page["page_idx"])
            candidates = list(page.get("preproc_blocks") or page.get("para_blocks") or [])
            candidates.extend(
                block for block in (page.get("discarded_blocks") or [])
                if block.get("type") == "page_footnote"
            )
            for candidate in candidates:
                bbox = candidate.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                rect = (
                    round(float(bbox[0]) / width, 6),
                    round(float(bbox[1]) / height, 6),
                    round(float(bbox[2]) / width, 6),
                    round(float(bbox[3]) / height, 6),
                )
                pages[physical_page].append(LayoutBlock(
                    page=physical_page,
                    rect=rect,
                    text=block_text(candidate),
                    kind=str(candidate.get("type") or "text"),
                    polygon=normalized_polygon(
                        candidate.get("poly") or candidate.get("polygon"), width, height
                    ),
                ))
    return pages


def load_model_parts(
    parts: list[tuple[Path, int]],
) -> tuple[dict[int, list[LayoutBlock]], dict[int, list[tuple[float, float, float, float]]]]:
    blocks: dict[int, list[LayoutBlock]] = defaultdict(list)
    lines: dict[int, list[tuple[float, float, float, float]]] = defaultdict(list)
    for model_path, physical_start in parts:
        with model_path.open("r", encoding="utf-8", errors="strict") as handle:
            payload = json.load(handle)
        for page_index, page in enumerate(payload):
            physical_page = physical_start + page_index
            for item in page:
                bbox = item.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                rect = tuple(round(float(value), 6) for value in bbox)
                kind = str(item.get("type") or "text")
                if kind == "ocr_text":
                    lines[physical_page].append(rect)
                    continue
                text = str(item.get("content") or item.get("text") or "")
                if text or kind in {"image", "table"}:
                    blocks[physical_page].append(LayoutBlock(
                        page=physical_page,
                        rect=rect,
                        text=text,
                        kind=kind,
                        polygon=normalized_polygon(item.get("poly") or item.get("polygon")),
                    ))
    return blocks, lines


def lines_inside_region(
    region: tuple[float, float, float, float],
    lines: Iterable[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    out = []
    for line in lines:
        center_x = (line[0] + line[2]) / 2
        center_y = (line[1] + line[3]) / 2
        if region[0] <= center_x <= region[2] and region[1] <= center_y <= region[3]:
            out.append(line)
    return sorted(out, key=lambda rect: (rect[1], rect[0]))


def median_line_height(lines: Iterable[tuple[float, float, float, float]]) -> float:
    """Typical height of the page's own body lines; the yardstick for calling a block single-line."""
    heights = [line[3] - line[1] for line in lines if line[3] > line[1]]
    return statistics.median(heights) if heights else 0.0


def rects_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    threshold: float = 0.3,
) -> bool:
    width = min(first[2], second[2]) - max(first[0], second[0])
    height = min(first[3], second[3]) - max(first[1], second[1])
    if width <= 0 or height <= 0:
        return False
    smallest = min(
        (first[2] - first[0]) * (first[3] - first[1]),
        (second[2] - second[0]) * (second[3] - second[1]),
    )
    return smallest > 0 and (width * height) / smallest > threshold


def blocks_missing_lines(
    blocks: Iterable[LayoutBlock],
    lines: Iterable[tuple[float, float, float, float]],
) -> list[LayoutBlock]:
    """Text blocks that carry content but enclose no visual line box, so their characters would never be read."""
    known = list(lines)
    pending = [
        block for block in blocks
        if block.kind in SUPPLEMENTABLE_BLOCK_KINDS
        and normalize_units(block.text).strip()
        and not lines_inside_region(block.rect, known)
    ]
    return sorted(pending, key=lambda block: (block.rect[1], block.rect[0]))


def block_media_candidates(candidates: Iterable[LayoutBlock]) -> list[LayoutBlock]:
    return sorted(
        (candidate for candidate in candidates if candidate.kind in {"image", "table"}),
        key=lambda candidate: (candidate.rect[1], candidate.rect[0]),
    )


def choose_layout_region(text: str, candidates: list[LayoutBlock]) -> LayoutBlock | None:
    needle = normalize_units(text)
    if not needle:
        return None
    best: LayoutBlock | None = None
    best_score = 0.0
    for candidate in candidates:
        haystack = normalize_units(candidate.text)
        if not haystack:
            continue
        if needle in haystack or haystack in needle:
            score = min(len(needle), len(haystack)) / max(len(needle), len(haystack)) + 1.0
        else:
            score = difflib.SequenceMatcher(None, needle, haystack, autojunk=False).ratio()
        if score > best_score:
            best = candidate
            best_score = score
    return best if best_score >= 0.35 else None


class PaddleLineDetector:
    """PP-OCR text detection, used only to recover line boxes MinerU never emitted (page footnotes above all)."""

    model_name = DEFAULT_DETECTION_MODEL

    def __init__(self, model_name: str = DEFAULT_DETECTION_MODEL, cpu_threads: int = 9) -> None:
        from paddleocr import TextDetection

        self.model_name = model_name
        options: dict[str, Any] = {
            "model_name": model_name,
            # The detection graph carries an attribute the oneDNN executor cannot convert, so it runs on the
            # plain CPU kernels. Recognition keeps oneDNN: the two predictors are configured independently.
            "enable_mkldnn": False,
            "cpu_threads": cpu_threads,
        }
        local = Path.home() / ".paddlex" / "official_models" / model_name
        if local.is_dir():
            # Name the already downloaded weights so a run never reaches the network for a different build.
            options["model_dir"] = str(local)
        self.engine = TextDetection(**options)

    def detect(self, image: Any) -> list[list[tuple[float, float]]]:
        polygons: list[list[tuple[float, float]]] = []
        for item in self.engine.predict(image):
            payload = item.json if hasattr(item, "json") else item
            if callable(payload):
                payload = payload()
            if isinstance(payload, dict) and "res" in payload:
                payload = payload["res"]
            if not isinstance(payload, dict):
                continue
            for box in payload.get("dt_polys") or []:
                points = [
                    (float(point[0]), float(point[1]))
                    for point in box
                    if isinstance(point, (list, tuple)) and len(point) >= 2
                ]
                if len(points) >= 3:
                    polygons.append(points)
        return polygons


def line_crops(
    image: Any,
    line_rects: list[tuple[float, float, float, float]],
    writing_mode: str,
) -> tuple[list[Any], list[tuple[float, float, float, float]]]:
    """把归一化行框裁成识别用的小图，返回（小图列表，实际裁到的归一化行框）。

    两个识别后端共用这一份裁剪，喂进模型的像素才真的是同一批，
    字框差异才能归因于模型本身而不是裁剪口径。
    """
    height, width = image.shape[:2]
    crops: list[Any] = []
    rects: list[tuple[float, float, float, float]] = []
    for rect in ordered_lines(line_rects, writing_mode):
        # 四周各让出 4 像素：MinerU 的行框贴得很紧，不留白边会切掉笔画的末梢。
        x0 = max(0, int(rect[0] * width) - 4)
        y0 = max(0, int(rect[1] * height) - 4)
        x1 = min(width, int(rect[2] * width) + 4)
        y1 = min(height, int(rect[3] * height) + 4)
        if x1 <= x0 or y1 <= y0:
            continue
        crop = image[y0:y1, x0:x1]
        if writing_mode == "vertical-rl":
            import numpy as np

            crop = np.rot90(crop, k=1)
        crops.append(crop)
        rects.append((x0 / width, y0 / height, x1 / width, y1 / height))
    return crops, rects


class PaddleCharacterOCR:
    backend = "paddle"

    def __init__(
        self,
        recognition_model: str,
        cpu_threads: int,
        batch_size: int,
        writing_mode: str = "horizontal-tb",
    ) -> None:
        from paddleocr import TextRecognition

        self.engine = TextRecognition(
            model_name=recognition_model,
            # oneDNN is stable for recognition-only inference. The incompatible
            # PP-OCR detection graph is intentionally not loaded: MinerU model
            # output already provides the authoritative visual line rectangles.
            enable_mkldnn=True,
            cpu_threads=cpu_threads,
        )
        self.batch_size = batch_size
        self.writing_mode = writing_mode

    def recognize(
        self,
        image: Any,
        line_rects: list[tuple[float, float, float, float]],
    ) -> list[OCRChar]:
        crops, recognition_rects = line_crops(image, line_rects, self.writing_mode)
        result = self.engine.predict(
            crops,
            batch_size=self.batch_size,
            return_word_box=True,
        )
        chars: list[OCRChar] = []
        for item, line_rect in zip(result, recognition_rects):
            payload = item.json if hasattr(item, "json") else item
            if callable(payload):
                payload = payload()
            if isinstance(payload, dict) and "res" in payload:
                payload = payload["res"]
            if not isinstance(payload, dict):
                continue
            rec_text = payload.get("rec_text")
            if not isinstance(rec_text, (list, tuple)) or len(rec_text) != 2:
                continue
            chars.extend(chars_from_word_info(
                str(rec_text[0]), rec_text[1], line_rect, self.writing_mode
            ))
        return chars


def chars_from_word_info(
    text: str,
    word_info: Any,
    line_rect: tuple[float, float, float, float],
    writing_mode: str = "horizontal-tb",
) -> list[OCRChar]:
    if not isinstance(word_info, (list, tuple)) or len(word_info) < 3:
        return []
    groups = word_info[1]
    column_groups = word_info[2]
    values: list[str] = []
    columns: list[float] = []
    for group, group_columns in zip(groups, column_groups):
        group_values = list(group) if isinstance(group, (list, tuple)) else list(str(group))
        if len(group_values) != len(group_columns):
            continue
        for value, column in zip(group_values, group_columns):
            values.append(str(value))
            columns.append(float(column))
    if not values or normalize_units("".join(values)) != normalize_units(text):
        return []
    return chars_from_columns(values, columns, line_rect, writing_mode)


def chars_from_columns(
    values: list[str],
    columns: list[float],
    line_rect: tuple[float, float, float, float],
    writing_mode: str = "horizontal-tb",
) -> list[OCRChar]:
    """把 CTC 输出里每个字所占的列号铺回行框，得到逐字坐标。

    两个识别后端都走这里：PaddleOCR 的列号来自 word_info，ONNX 的来自 argmax 序列的
    下标，语义相同（该字在 CTC 时间轴上被解码出来的位置），所以同一行给出同样的字框。
    """
    pairs = [(value, column) for value, column in zip(values, columns) if not value.isspace()]
    if not pairs:
        return []
    values = [value for value, _ in pairs]
    columns = [column for _, column in pairs]

    axis_start, axis_end = (
        (line_rect[1], line_rect[3])
        if writing_mode == "vertical-rl"
        else (line_rect[0], line_rect[2])
    )
    positive_steps = [right - left for left, right in zip(columns, columns[1:]) if right > left]
    if not positive_steps or columns[-1] <= columns[0]:
        # 只认出一个字，或列号退化得没有方向：整行平分，好过按一个猜来的比例摆放。
        return evenly_spaced_chars("".join(values), line_rect, writing_mode)
    # 不拿 word_info[0]（本行的有效列数）定标：PaddleX 先把这批行图按宽高比排序算出比例表，
    # 回填时却用原始次序去取，每行拿到的是别行的比例——长行被当成只剩开头几个字（其余字框
    # 宽度归零后被丢掉），短行则被整体压在行首。改用列号自身定标：把 [首列-半步, 末列+半步]
    # 线性铺到 MinerU 给的行框上，不依赖模型或 PaddleX 版本。
    typical_step = statistics.median(positive_steps)
    span_start = columns[0] - typical_step / 2
    span_end = columns[-1] + typical_step / 2
    cell = (axis_end - axis_start) / (span_end - span_start)
    half_width = typical_step * cell / 2
    centers = [axis_start + (column - span_start) * cell for column in columns]
    out = []
    for index, (value, center) in enumerate(zip(values, centers)):
        left = center - half_width
        right = center + half_width
        if index > 0:
            left = max(left, (centers[index - 1] + center) / 2)
        if index + 1 < len(centers):
            right = min(right, (center + centers[index + 1]) / 2)
        rect = (
            (
                round(line_rect[0], 6),
                round(max(axis_start, left), 6),
                round(line_rect[2], 6),
                round(min(axis_end, right), 6),
            )
            if writing_mode == "vertical-rl"
            else (
                round(max(axis_start, left), 6),
                round(line_rect[1], 6),
                round(min(axis_end, right), 6),
                round(line_rect[3], 6),
            )
        )
        if rect[2] > rect[0] and rect[3] > rect[1]:
            out.append(OCRChar(value, rect))
    return out


def evenly_spaced_chars(
    text: str,
    line_rect: tuple[float, float, float, float],
    writing_mode: str,
) -> list[OCRChar]:
    values = [char for char in text if not char.isspace()]
    if not values:
        return []
    out = []
    for index, value in enumerate(values):
        start = index / len(values)
        end = (index + 1) / len(values)
        if writing_mode == "vertical-rl":
            y0 = line_rect[1] + (line_rect[3] - line_rect[1]) * start
            y1 = line_rect[1] + (line_rect[3] - line_rect[1]) * end
            rect = (line_rect[0], y0, line_rect[2], y1)
        else:
            x0 = line_rect[0] + (line_rect[2] - line_rect[0]) * start
            x1 = line_rect[0] + (line_rect[2] - line_rect[0]) * end
            rect = (x0, line_rect[1], x1, line_rect[3])
        out.append(OCRChar(value, tuple(round(number, 6) for number in rect)))
    return out


def dxgi_adapters() -> list[tuple[int, str, int]]:
    """枚举本机显卡，返回 (序号, 名称, 专用显存字节数)。非 Windows 或取不到时返回空表。

    DirectML 的 device_id 就是 DXGI 的适配器序号，所以这里的序号可以直接当 device_id 用。
    本机实测：0 号是 Intel 核显，1 号才是独显——默认的 0 会把活派给最慢的那块。
    """
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import POINTER, byref, c_void_p
    from ctypes.wintypes import LONG, UINT, WCHAR

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", LONG)]

    class AdapterDescription(ctypes.Structure):
        _fields_ = [
            ("Description", WCHAR * 128),
            ("VendorId", UINT),
            ("DeviceId", UINT),
            ("SubSysId", UINT),
            ("Revision", UINT),
            ("DedicatedVideoMemory", ctypes.c_size_t),
            ("DedicatedSystemMemory", ctypes.c_size_t),
            ("SharedSystemMemory", ctypes.c_size_t),
            ("AdapterLuid", LUID),
            ("Flags", UINT),
        ]

    class Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8),
        ]

    def call(interface: Any, slot: int, restype: Any, argtypes: list[Any], *arguments: Any) -> Any:
        # COM 没有 C 接口，只能按虚表槽位取函数指针：IDXGIFactory1::EnumAdapters1 在 12 号槽，
        # IDXGIAdapter1::GetDesc1 在 10 号，IUnknown::Release 在 2 号。
        table = ctypes.cast(interface, POINTER(POINTER(c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(table[slot])(interface, *arguments)

    identifier = Guid(0x770AAE78, 0xF26F, 0x4DBA,
                      (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
    try:
        dxgi = ctypes.WinDLL("dxgi.dll")
    except OSError:
        return []
    dxgi.CreateDXGIFactory1.argtypes = [POINTER(Guid), POINTER(c_void_p)]
    dxgi.CreateDXGIFactory1.restype = ctypes.c_long
    factory = c_void_p()
    if dxgi.CreateDXGIFactory1(byref(identifier), byref(factory)) != 0:
        return []
    out: list[tuple[int, str, int]] = []
    try:
        index = 0
        while True:
            adapter = c_void_p()
            if call(factory, 12, ctypes.c_long, [UINT, POINTER(c_void_p)], index, byref(adapter)) != 0:
                break
            description = AdapterDescription()
            call(adapter, 10, ctypes.c_long, [POINTER(AdapterDescription)], byref(description))
            software = bool(description.Flags & 0x2)
            if not software:
                out.append((index, description.Description, int(description.DedicatedVideoMemory)))
            call(adapter, 2, ctypes.c_ulong, [])
            index += 1
    finally:
        call(factory, 2, ctypes.c_ulong, [])
    return out


def onnx_model_directory(name: str) -> Path:
    """把模型名或目录路径解析成含 inference.onnx 的目录。"""
    candidate = Path(name).expanduser()
    if candidate.is_dir():
        directory = candidate.resolve()
    else:
        folder = ONNX_MODEL_DIRECTORIES.get(name, name)
        directory = Path.home() / ".paddlex" / "official_models" / folder
    if not (directory / "inference.onnx").is_file():
        raise RuntimeError(
            f"ONNX recognition model not found: {directory}\\inference.onnx. "
            f"Fetch it with paddlex official_models.get_model_path(name, model_formats=['onnx']), "
            f"or pass --onnx-model with a directory path."
        )
    return directory


def onnx_inference_config(model_directory: Path) -> dict[str, Any]:
    """读模型目录里的 inference.yml；字典与输入规格都从这里来，不按模型名去猜。"""
    try:
        import yaml
    except ImportError as error:
        raise RuntimeError("PyYAML is required to read the ONNX model dictionary") from error
    with (model_directory / "inference.yml").open(encoding="utf-8", errors="strict") as handle:
        return yaml.safe_load(handle)


def onnx_charset(model_directory: Path) -> list[str]:
    """从 inference.yml 取字典，按 PaddleX 的 CTCLabelDecode 补上空格与 blank。"""
    config = onnx_inference_config(model_directory)
    characters = list(config["PostProcess"]["character_dict"])
    # use_space_char 先追加空格，add_special_char 再把 blank 放到 0 号；顺序反了整本书都会错位。
    characters.append(" ")
    return ["blank"] + characters


def onnx_recognition_shape(model_directory: Path) -> tuple[int, int, int]:
    """从 inference.yml 的 RecResizeImg 取 (通道, 高, 基准宽)。

    PP-OCRv5 与 PP-OCRv6 眼下都是 3x48x320，但把 48 写死等于赌下一代不改高度：
    高度错了整页识别会变成一串乱码，而报错要等到推理才出现，很难回溯。
    """
    config = onnx_inference_config(model_directory)
    for operator in config.get("PreProcess", {}).get("transform_ops") or []:
        if not isinstance(operator, dict) or "RecResizeImg" not in operator:
            continue
        shape = (operator["RecResizeImg"] or {}).get("image_shape")
        if isinstance(shape, (list, tuple)) and len(shape) == 3:
            return (int(shape[0]), int(shape[1]), int(shape[2]))
    return ONNX_RECOGNITION_SHAPE


def verify_onnx_input_shape(shape: tuple[int, int, int], graph_shape: Any, model_directory: Path) -> None:
    """核对 inference.yml 的输入规格与 inference.onnx 图里声明的静态维。

    两份文件是分开下载的，配错对（把 v5 的 yml 留在 v6 目录里）不会有任何报错，
    只会安静地按错高度缩放，出一整本乱码。图里通道与高是静态维，正好能当校验；
    批量与宽是动态维，字符串形式，跳过不比。
    """
    dimensions = list(graph_shape or [])
    if len(dimensions) != 4:
        return
    for index, declared in enumerate(dimensions[1:3]):
        if isinstance(declared, int) and declared != shape[index]:
            raise RuntimeError(
                f"{model_directory.name}: inference.yml says image_shape={list(shape)} but "
                f"inference.onnx declares input {dimensions}; the two files do not match"
            )


def onnx_resize_normalize(
    image: Any,
    shape: tuple[int, int, int] = ONNX_RECOGNITION_SHAPE,
    max_image_width: int = ONNX_MAX_IMAGE_WIDTH,
) -> Any:
    """与 PaddleX OCRReisizeNormImg.resize 等价：逐图算宽高比，右侧补零。"""
    import cv2
    import numpy as np

    channels, target_height, base_width = shape
    height, width = image.shape[:2]
    ratio = width / float(height)
    target_width = int(target_height * max(base_width / target_height, ratio))
    if target_width > max_image_width:
        resized = cv2.resize(image, (max_image_width, target_height))
        resized_width = target_width = max_image_width
    else:
        resized_width = (
            target_width if math.ceil(target_height * ratio) > target_width
            else int(math.ceil(target_height * ratio))
        )
        resized = cv2.resize(image, (resized_width, target_height))
    resized = resized.astype("float32").transpose((2, 0, 1)) / 255
    resized -= 0.5
    resized /= 0.5
    out = np.zeros((channels, target_height, target_width), dtype=np.float32)
    out[:, :, :resized_width] = resized
    return out


def onnx_ctc_decode(probabilities: Any, charset: list[str]) -> tuple[list[str], list[float]]:
    """CTC 贪心解码，返回（字符列表，每个字在时间轴上的列号）。

    列号与 PaddleX word_info 的第三项同义：都是该字被解码出来的那一列，
    所以两个后端可以共用 chars_from_columns。
    """
    indices = probabilities.argmax(axis=1)
    values: list[str] = []
    columns: list[float] = []
    previous = -1
    for column, index in enumerate(indices):
        index = int(index)
        # 0 是 blank；与前一列相同的一律视作同一个字的延续。
        if index != 0 and index != previous:
            values.append(charset[index])
            columns.append(float(column))
        previous = index
    return values, columns


class OnnxCharacterOCR:
    """ONNX Runtime 上的 PP-OCR 识别，预处理与 CTC 解码逐字对齐 PaddleX。

    字典与输入规格都从模型目录的 inference.yml 读，所以同一份代码能跑 PP-OCRv5 与 PP-OCRv6。

    存在的理由是速度：同一批行图，DirectML 上的独显比 PaddlePaddle 的 CPU 路径快一个量级，
    纯 CPU 的 ONNX 也比 Paddle 原生快三四倍，而识别结果与 Paddle 逐字相同。
    """

    backend = "onnx"

    def __init__(
        self,
        model: str,
        device: str,
        device_id: int | None,
        batch_size: int,
        cpu_threads: int,
        writing_mode: str = "horizontal-tb",
    ) -> None:
        try:
            import onnxruntime
        except ImportError as error:
            raise RuntimeError(
                "onnxruntime is not installed in this Python environment; install "
                "onnxruntime-directml (Windows) or onnxruntime, see sync-requirements.txt"
            ) from error
        self.model_directory = onnx_model_directory(model)
        self.charset = onnx_charset(self.model_directory)
        self.shape = onnx_recognition_shape(self.model_directory)
        self.batch_size = max(1, batch_size)
        self.writing_mode = writing_mode
        self.provider, self.device_id, self.device_name = resolve_onnx_device(
            onnxruntime, device, device_id
        )
        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        if self.provider == "dml":
            # DirectML 不支持内存复用，也不支持并行执行；不关掉会在会话创建时直接抛错。
            options.enable_mem_pattern = False
            options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            providers: list[Any] = [("DmlExecutionProvider", {"device_id": self.device_id})]
        else:
            options.intra_op_num_threads = cpu_threads
            providers = ["CPUExecutionProvider"]
        self.session = onnxruntime.InferenceSession(
            str(self.model_directory / "inference.onnx"), sess_options=options, providers=providers
        )
        model_input = self.session.get_inputs()[0]
        self.input_name = model_input.name
        verify_onnx_input_shape(self.shape, model_input.shape, self.model_directory)

    @property
    def cache_scope(self) -> str:
        """识别结果随模型、设备与批量大小而变，缓存目录必须把三者都算进去。"""
        model = re.sub(r"[^A-Za-z0-9_.-]", "-", self.model_directory.name)
        return f"onnx-{model}-{self.provider}-b{self.batch_size}"

    def describe(self) -> str:
        detail = self.device_name if self.provider == "dml" else "cpu"
        return f"{self.provider}:{detail}"

    def run_batch(self, crops: list[Any]) -> list[tuple[list[str], list[float]]]:
        import numpy as np

        prepared = [onnx_resize_normalize(crop, self.shape) for crop in crops]
        width = max(item.shape[2] for item in prepared)
        batch = np.zeros((len(prepared), *self.shape[:2], width), dtype=np.float32)
        for index, item in enumerate(prepared):
            batch[index, :, :, : item.shape[2]] = item
        output = self.session.run(None, {self.input_name: batch})[0]
        return [onnx_ctc_decode(output[index], self.charset) for index in range(len(prepared))]

    def recognize(
        self,
        image: Any,
        line_rects: list[tuple[float, float, float, float]],
    ) -> list[OCRChar]:
        import cv2

        crops, recognition_rects = line_crops(image, line_rects, self.writing_mode)
        # PaddleX 的读图算子把送进来的数组当 BGR 再转 RGB；页面位图本来就是 RGB，
        # 于是模型实际看到的是通道反转过的图。要逐字等价，这里必须照做同一次反转。
        crops = [cv2.cvtColor(crop, cv2.COLOR_BGR2RGB) for crop in crops]
        # 宽高比相近的排在一批，组内补零的浪费最小；组内按各自宽度补到组内最大宽。
        order = sorted(range(len(crops)), key=lambda index: crops[index].shape[1] / crops[index].shape[0])
        decoded: list[tuple[list[str], list[float]] | None] = [None] * len(crops)
        for start in range(0, len(order), self.batch_size):
            chunk = order[start:start + self.batch_size]
            for index, result in zip(chunk, self.run_batch([crops[index] for index in chunk])):
                decoded[index] = result
        chars: list[OCRChar] = []
        for result, line_rect in zip(decoded, recognition_rects):
            if not result:
                continue
            values, columns = result
            chars.extend(chars_from_columns(values, columns, line_rect, self.writing_mode))
        return chars


def resolve_onnx_device(
    onnxruntime: Any,
    device: str,
    device_id: int | None,
) -> tuple[str, int, str]:
    """定下实际使用的 provider 与设备，返回 (provider, device_id, 设备名)。"""
    available = set(onnxruntime.get_available_providers())
    if device == "cpu":
        return "cpu", 0, "cpu"
    if "DmlExecutionProvider" not in available:
        if device == "dml":
            raise RuntimeError(
                "DmlExecutionProvider is unavailable; install onnxruntime-directml on Windows "
                "or use --onnx-device cpu"
            )
        return "cpu", 0, "cpu (DirectML unavailable)"
    adapters = dxgi_adapters()
    if device_id is not None:
        name = next((title for index, title, _ in adapters if index == device_id), f"device {device_id}")
        return "dml", device_id, name
    # 专用显存最大的那块才是独显；核显的专用显存通常是 0 或 128MiB 量级。
    # 不挑的话 DirectML 默认用 0 号，本机那是 Intel 核显，比独显慢一个量级。
    usable = [adapter for adapter in adapters if adapter[2] >= 1 << 30]
    if usable:
        index, name, _ = max(usable, key=lambda adapter: adapter[2])
        return "dml", index, name
    if device == "auto":
        return "cpu", 0, "cpu (no discrete adapter)"
    # 显式要了 dml 就不擅自退回 CPU：枚举不到独显也照 DirectML 的默认适配器跑。
    return "dml", 0, next((title for index, title, _ in adapters if index == 0), "device 0")


class TesseractCharacterOCR:
    backend = "tesseract"

    def __init__(self, language: str, writing_mode: str = "horizontal-tb") -> None:
        executable = shutil.which("tesseract")
        if not executable:
            raise RuntimeError("Tesseract is not installed or not available on PATH")
        self.executable = executable
        self.language = language
        self.writing_mode = writing_mode

    def recognize(
        self,
        image: Any,
        line_rects: list[tuple[float, float, float, float]],
    ) -> list[OCRChar]:
        from PIL import Image

        height, width = image.shape[:2]
        out = []
        for rect in ordered_lines(line_rects, self.writing_mode):
            x0 = max(0, int(rect[0] * width) - 4)
            y0 = max(0, int(rect[1] * height) - 4)
            x1 = min(width, int(rect[2] * width) + 4)
            y1 = min(height, int(rect[3] * height) + 4)
            crop = Image.fromarray(image[y0:y1, x0:x1])
            if self.writing_mode == "vertical-rl":
                crop = crop.rotate(90, expand=True)
            encoded = BytesIO()
            crop.save(encoded, format="PNG")
            result = subprocess.run(
                [self.executable, "stdin", "stdout", "-l", self.language, "--psm", "7"],
                input=encoded.getvalue(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode != 0:
                message = result.stderr.decode("utf-8", errors="strict").strip()
                raise RuntimeError(f"Tesseract failed: {message}")
            text = result.stdout.decode("utf-8", errors="strict").strip()
            out.extend(evenly_spaced_chars(text, rect, self.writing_mode))
        return out


class KrakenCharacterOCR:
    backend = "kraken"

    def __init__(self, model_path: str | None, writing_mode: str = "horizontal-tb") -> None:
        if not model_path:
            raise RuntimeError("Kraken requires --kraken-model pointing to a local .mlmodel file")
        try:
            from kraken.lib import models
        except ImportError as error:
            raise RuntimeError("Kraken is not installed in this Python environment") from error
        self.network = models.load_any(model_path)
        self.writing_mode = writing_mode

    def recognize(
        self,
        image: Any,
        line_rects: list[tuple[float, float, float, float]],
    ) -> list[OCRChar]:
        from PIL import Image
        from kraken import blla, rpred

        height, width = image.shape[:2]
        out = []
        for rect in ordered_lines(line_rects, self.writing_mode):
            x0 = max(0, int(rect[0] * width) - 4)
            y0 = max(0, int(rect[1] * height) - 4)
            x1 = min(width, int(rect[2] * width) + 4)
            y1 = min(height, int(rect[3] * height) + 4)
            crop = Image.fromarray(image[y0:y1, x0:x1])
            if self.writing_mode == "vertical-rl":
                crop = crop.rotate(90, expand=True)
            segmentation = blla.segment(crop)
            records = list(rpred.rpred(self.network, crop, segmentation))
            text = "".join(str(record.prediction) for record in records)
            out.extend(evenly_spaced_chars(text, rect, self.writing_mode))
        return out


def line_sort_key(rect: tuple[float, float, float, float], writing_mode: str) -> tuple[float, float]:
    return (-rect[0], rect[1]) if writing_mode == "vertical-rl" else (rect[1], rect[0])


def ordered_lines(
    line_rects: list[tuple[float, float, float, float]],
    writing_mode: str,
) -> list[tuple[float, float, float, float]]:
    return sorted(line_rects, key=lambda rect: line_sort_key(rect, writing_mode))


def lines_follow(
    base: Iterable[tuple[float, float, float, float]],
    extra: Iterable[tuple[float, float, float, float]],
    writing_mode: str,
) -> bool:
    """True when every supplementary line is read after every original one, so appending cannot reorder characters."""
    base_keys = [line_sort_key(rect, writing_mode) for rect in base]
    extra_keys = [line_sort_key(rect, writing_mode) for rect in extra]
    if not base_keys or not extra_keys:
        return True
    return max(base_keys) < min(extra_keys)


def line_digest(line_rects: Iterable[tuple[float, float, float, float]]) -> str:
    """Short fingerprint of a page's line set; it goes in the cache filename so a new line never hides behind an old file."""
    payload = json.dumps(
        sorted([round(float(value), 6) for value in rect] for rect in line_rects),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def interlinear_lines(
    line_rects: list[tuple[float, float, float, float]],
    writing_mode: str,
) -> set[tuple[float, float, float, float]]:
    if writing_mode != "vertical-rl" or len(line_rects) < 3:
        return set()
    widths = [rect[2] - rect[0] for rect in line_rects if rect[2] > rect[0]]
    if not widths:
        return set()
    typical = statistics.median(widths)
    narrow = [rect for rect in line_rects if rect[2] - rect[0] < typical * 0.72]
    marked = set()
    for rect in narrow:
        for peer in narrow:
            if peer == rect:
                continue
            overlap = min(rect[3], peer[3]) - max(rect[1], peer[1])
            height = min(rect[3] - rect[1], peer[3] - peer[1])
            gap = max(0.0, max(rect[0], peer[0]) - min(rect[2], peer[2]))
            if height > 0 and overlap / height >= 0.65 and gap <= typical * 0.45:
                marked.add(rect)
                marked.add(peer)
    return marked


def box_to_rect(box: Any, width: int, height: int) -> tuple[float, float, float, float] | None:
    values = list(box)
    if len(values) == 4 and all(isinstance(value, (int, float)) for value in values):
        x0, y0, x1, y1 = (float(value) for value in values)
    else:
        points = [list(point) for point in values if len(point) >= 2]
        if not points:
            return None
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return (
        round(x0 / width, 6),
        round(y0 / height, 6),
        round(x1 / width, 6),
        round(y1 / height, 6),
    )


def normalized_polygon(
    value: Any,
    width: float = 1.0,
    height: float = 1.0,
) -> list[tuple[float, float]] | None:
    if not isinstance(value, (list, tuple)):
        return None
    points = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return None
        points.append((round(float(point[0]) / width, 6), round(float(point[1]) / height, 6)))
    return points if len(points) >= 3 else None


def render_page(document: Any, page_number: int, scale: float) -> Any:
    import numpy as np
    import pymupdf

    page = document[page_number - 1]
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, pixmap.n)


def page_image_loader(document: Any, page: int, scale: float) -> Callable[[], Any]:
    """Render the page bitmap at most once; line detection and character recognition share the same image."""
    holder: list[Any] = []

    def load() -> Any:
        if not holder:
            holder.append(render_page(document, page, scale))
        return holder[0]

    return load


def detect_block_lines(
    detector: Any,
    image: Any,
    rect: tuple[float, float, float, float],
    pad: int = 4,
    min_height: float = 0.004,
    margin: float = 0.5,
    max_bleed: int = 32,
) -> list[tuple[float, float, float, float]]:
    """Run detection on one block's crop and return its line boxes in page-normalized coordinates."""
    height, width = image.shape[:2]
    # A one-line footnote cropped tight is a ~25px strip with no context, and the detector returns nothing at all.
    # Bleeding white space in on all four sides gives it a usable image; the centre test below drops whatever
    # neighbouring body line the bleed dragged in, so only lines belonging to this block survive.
    bleed = max(pad, min(int(round((rect[3] - rect[1]) * height * margin)), max_bleed))
    x0 = max(0, int(rect[0] * width) - bleed)
    y0 = max(0, int(rect[1] * height) - bleed)
    x1 = min(width, int(rect[2] * width) + bleed)
    y1 = min(height, int(rect[3] * height) + bleed)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    out = []
    for polygon in detector.detect(image[y0:y1, x0:x1]):
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        box = (
            (x0 + min(xs)) / width,
            (y0 + min(ys)) / height,
            (x0 + max(xs)) / width,
            (y0 + max(ys)) / height,
        )
        center_x = (box[0] + box[2]) / 2
        center_y = (box[1] + box[3]) / 2
        if not (rect[0] <= center_x <= rect[2] and rect[1] <= center_y <= rect[3]):
            continue
        line = (
            round(max(rect[0], box[0]), 6),
            round(max(rect[1], box[1]), 6),
            round(min(rect[2], box[2]), 6),
            round(min(rect[3], box[3]), 6),
        )
        if line[2] - line[0] > 0 and line[3] - line[1] >= min_height:
            out.append(line)
    return merge_row_boxes(out)


def merge_row_boxes(
    boxes: Iterable[tuple[float, float, float, float]],
    overlap: float = 0.6,
) -> list[tuple[float, float, float, float]]:
    """Join boxes sitting on one text row. Detection often returns the footnote's ① separately from its sentence,
    and two boxes for one line would both misplace characters and understate how much of the block was read."""
    rows: list[list[float]] = []
    for box in sorted(boxes, key=lambda value: (value[1], value[0])):
        merged = False
        for row in rows:
            shared = min(row[3], box[3]) - max(row[1], box[1])
            smallest = min(row[3] - row[1], box[3] - box[1])
            if smallest > 0 and shared / smallest >= overlap:
                row[0] = min(row[0], box[0])
                row[1] = min(row[1], box[1])
                row[2] = max(row[2], box[2])
                row[3] = max(row[3], box[3])
                merged = True
                break
        if not merged:
            rows.append(list(box))
    return sorted((tuple(round(value, 6) for value in row) for row in rows), key=lambda box: (box[1], box[0]))


def covered_span(lines: Iterable[tuple[float, float, float, float]]) -> float:
    """Total height the lines occupy once overlapping vertical ranges are merged."""
    intervals = sorted((line[1], line[3]) for line in lines)
    if not intervals:
        return 0.0
    total = 0.0
    start, end = intervals[0]
    for lower, upper in intervals[1:]:
        if lower > end:
            total += end - start
            start, end = lower, upper
        else:
            end = max(end, upper)
    return total + (end - start)


def lines_cover_block(
    lines: Iterable[tuple[float, float, float, float]],
    rect: tuple[float, float, float, float],
    ratio: float = 0.5,
) -> bool:
    """Detection that leaves most of a block unaccounted for has missed lines, not found them.

    Height alone is not enough: on a one-line footnote the detector often returns just the ① marker, which spans
    the full height while covering a twentieth of the width, so the area has to agree as well.
    """
    found = list(lines)
    if not found:
        return False
    span = rect[3] - rect[1]
    if span > 0 and covered_span(found) / span < ratio:
        return False
    area = (rect[2] - rect[0]) * span
    if area <= 0:
        return True
    covered = sum(max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1]) for box in found)
    return covered / area >= ratio


def project_block_rows(
    image: Any,
    rect: tuple[float, float, float, float],
    line_height: float = 0.0,
    min_height: float = 0.004,
) -> list[tuple[float, float, float, float]]:
    """Cut a block into lines by the blank rows between them, tightening each band to the ink it contains."""
    import numpy as np

    height, width = image.shape[:2]
    x0 = max(0, int(rect[0] * width))
    y0 = max(0, int(rect[1] * height))
    x1 = min(width, int(rect[2] * width) + 1)
    y1 = min(height, int(rect[3] * height) + 1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return []
    crop = image[y0:y1, x0:x1]
    gray = crop.mean(axis=2) if crop.ndim == 3 else crop.astype(float)
    light = float(np.percentile(gray, 95))
    dark = float(np.percentile(gray, 5))
    if light - dark < 24:
        return []
    threshold = (light + dark) / 2
    ink = gray < threshold
    rows = np.flatnonzero(ink.sum(axis=1) > max(1, int((x1 - x0) * 0.02)))
    if rows.size == 0:
        return []

    def split(gap: int) -> list[list[int]]:
        bands: list[list[int]] = []
        for row in rows.tolist():
            if bands and row - bands[-1][1] <= gap:
                bands[-1][1] = row
            else:
                bands.append([row, row])
        return bands

    # Rows inside one line of Chinese type can fall below the ink threshold, so close small gaps before splitting.
    # With no body line to measure against, the strict split supplies the yardstick: a block's own bands are the
    # only honest estimate of its line pitch, and guessing from the block height merges a whole page into one line.
    if line_height > 0:
        reference = line_height * height
    else:
        strict = [bottom - top + 1 for top, bottom in split(1)]
        reference = statistics.median(strict) if strict else 0.0
    bands = split(max(1, int(round(reference * 0.35))))
    out = []
    for top, bottom in bands:
        columns = np.flatnonzero(ink[top:bottom + 1].sum(axis=0) > 0)
        if columns.size == 0:
            continue
        line = (
            round(max(rect[0], (x0 + int(columns[0])) / width), 6),
            round(max(rect[1], (y0 + top) / height), 6),
            round(min(rect[2], (x0 + int(columns[-1]) + 1) / width), 6),
            round(min(rect[3], (y0 + bottom + 1) / height), 6),
        )
        if line[2] - line[0] > 0 and line[3] - line[1] >= min_height:
            out.append(line)
    return sorted(out, key=lambda box: (box[1], box[0]))


def fallback_block_lines(
    image: Any,
    rect: tuple[float, float, float, float],
    line_height: float = 0.0,
    single_line_ratio: float = 1.6,
    single_line_height: float = 0.025,
) -> list[tuple[float, float, float, float]]:
    """Last resort for a block the detector could not read: never leave text with no line box at all."""
    limit = line_height * single_line_ratio if line_height > 0 else single_line_height
    if rect[3] - rect[1] <= limit:
        return [rect]
    return project_block_rows(image, rect, line_height) or [rect]


class LineSupplementer:
    """Recover missing line boxes block by block, caching the result so a repeat run need not even render the page."""

    def __init__(
        self,
        cache_dir: Path,
        writing_mode: str = "horizontal-tb",
        model_name: str = DEFAULT_DETECTION_MODEL,
        cpu_threads: int = 9,
        factory: Callable[[], Any] | None = None,
    ) -> None:
        self.cache_dir = cache_dir / "lines"
        self.writing_mode = writing_mode
        self.model_name = model_name
        self.factory = factory or (lambda: PaddleLineDetector(model_name, cpu_threads))
        self.detector: Any | None = None
        self.pages = 0
        self.lines = 0

    def detector_instance(self) -> Any:
        if self.detector is None:
            self.detector = self.factory()
        return self.detector

    def plan_digest(self, blocks: list[LayoutBlock]) -> str:
        payload = json.dumps(
            {
                # The algorithm version is part of the key, so results from an earlier recipe (notably the empty
                # ones a tight single-line crop used to produce) cannot be replayed after the recipe changes.
                "algorithm": LINE_DETECTION_ALGORITHM,
                "model": self.model_name,
                "blocks": [[round(value, 6) for value in b.rect] for b in blocks],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]

    def lines_for(
        self,
        page: int,
        blocks: Iterable[LayoutBlock],
        lines: Iterable[tuple[float, float, float, float]],
        image_loader: Callable[[], Any],
        force: bool = False,
    ) -> list[tuple[float, float, float, float]]:
        known = list(lines)
        pending = blocks_missing_lines(blocks, known)
        if not pending:
            return []
        cache = self.cache_dir / f"p{page:04d}-{self.plan_digest(pending)}.json"
        if cache.exists() and not force:
            with cache.open("r", encoding="utf-8", errors="strict") as handle:
                extra = [tuple(item) for item in json.load(handle)]
        else:
            detector = self.detector_instance()
            image = image_loader()
            body_height = median_line_height(known)
            found: list[tuple[float, float, float, float]] = []
            for block in pending:
                boxes = detect_block_lines(detector, image, block.rect)
                if not lines_cover_block(boxes, block.rect):
                    boxes = fallback_block_lines(image, block.rect, body_height)
                kept = [line for line in boxes if not any(rects_overlap(line, other) for other in known)]
                if not kept and not any(rects_overlap(block.rect, other) for other in known):
                    # A block with text must never end up with no line box: read it whole rather than not at all.
                    kept = [block.rect]
                known.extend(kept)
                found.extend(kept)
            extra = ordered_lines(found, self.writing_mode)
            write_text_atomic(cache, json.dumps([list(line) for line in extra], ensure_ascii=False))
        self.pages += 1
        self.lines += len(extra)
        return extra


def read_ocr_cache(path: Path) -> list[OCRChar]:
    with path.open("r", encoding="utf-8", errors="strict") as handle:
        payload = json.load(handle)
    return [OCRChar(item[0], tuple(item[1])) for item in payload]


def load_or_run_ocr(
    engine: CharacterOCR,
    document: Any,
    page: int,
    line_rects: list[tuple[float, float, float, float]],
    cache_dir: Path,
    scale: float,
    writing_mode: str = "horizontal-tb",
    force: bool = False,
    base_rects: Iterable[tuple[float, float, float, float]] | None = None,
    image_loader: Callable[[], Any] | None = None,
) -> list[OCRChar]:
    # 后端可以自报一个更细的缓存分域（ONNX 的模型与设备会改变识别结果），默认就用后端名。
    scope = getattr(engine, "cache_scope", engine.backend)
    directory = (
        cache_dir
        if engine.backend == "paddle" and writing_mode == "horizontal-tb"
        else cache_dir / scope / writing_mode
    )
    ordered = ordered_lines(list(line_rects), writing_mode)
    cache = directory / f"p{page:04d}-{line_digest(ordered)}.json"
    # Caches written before line supplementation existed are named by page alone and cover the original lines only.
    legacy = directory / f"p{page:04d}.json"
    if cache.exists() and not force:
        return read_ocr_cache(cache)
    load_image = image_loader or (lambda: render_page(document, page, scale))
    chars: list[OCRChar] | None = None
    if legacy.exists() and not force:
        base = ordered_lines(list(base_rects), writing_mode) if base_rects is not None else ordered
        seen = set(base)
        extra = [rect for rect in ordered if rect not in seen]
        # Appending is only sound while every supplementary line is read last; otherwise redo the whole page.
        if lines_follow(base, extra, writing_mode):
            chars = read_ocr_cache(legacy) + (engine.recognize(load_image(), extra) if extra else [])
    if chars is None:
        chars = engine.recognize(load_image(), ordered)
    write_text_atomic(cache, json.dumps([[char.value, char.rect] for char in chars], ensure_ascii=False))
    return chars


def overlap_groups(
    spans: list[tuple[float, float]],
    min_ratio: float,
) -> list[list[int]]:
    """按区间重叠把下标并成组；min_ratio 是重叠长度相对较短区间的下限。"""
    parent = list(range(len(spans)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(spans)):
        for right in range(left + 1, len(spans)):
            first, second = spans[left], spans[right]
            overlap = min(first[1], second[1]) - max(first[0], second[0])
            shortest = min(first[1] - first[0], second[1] - second[0])
            if overlap > 0 and (shortest <= 0 or overlap >= min_ratio * shortest):
                parent[find(left)] = find(right)
    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(len(spans)):
        grouped[find(index)].append(index)
    return list(grouped.values())


def reading_order_chars(
    chars: list[OCRChar],
    writing_mode: str = "horizontal-tb",
) -> list[OCRChar]:
    """把纯按 y 排出来的识别结果，改排成版面的阅读顺序。

    这两卷世界史用的是悬挂式小标题：标题占左半幅，正文从同一行的右半幅接着排。
    行按 y 排序时，正文首行排在标题之前、次行排在标题之后，与 Markdown 的
    「标题在前、正文在后」交错；单调的序列对齐只能二选一，整串标题因此丢掉坐标。
    这里在同一条横带内先左栏后右栏，横带之间和栏内都保持原有次序，
    所以只有真正分栏的地方才会被重排。
    """
    if len(chars) < 2:
        return list(chars)
    vertical = writing_mode == "vertical-rl"
    # 横排时行的「厚度」轴是 y、走向轴是 x，竖排反过来。
    cross = (lambda rect: (rect[0], rect[2])) if vertical else (lambda rect: (rect[1], rect[3]))
    flow = (lambda rect: (rect[1], rect[3])) if vertical else (lambda rect: (rect[0], rect[2]))
    # 同一行的字框共享整行的厚度跨度，且在识别输出里前后相邻。
    lines: list[list[int]] = []
    for index, char in enumerate(chars):
        if lines and cross(chars[lines[-1][0]].rect) == cross(char.rect):
            lines[-1].append(index)
        else:
            lines.append([index])
    if len(lines) < 2:
        return list(chars)
    line_cross = [cross(chars[line[0]].rect) for line in lines]
    line_flow = [
        (
            min(flow(chars[index].rect)[0] for index in line),
            max(flow(chars[index].rect)[1] for index in line),
        )
        for line in lines
    ]
    out: list[OCRChar] = []
    # 阈值放到四分之一：悬挂式标题与它右侧正文的纵向重叠常常只有正文行高的三四成，
    # 而同栏的上下两行几乎从不重叠，所以放宽不会把普通正文并成一条横带；
    # 万一并了也无妨——整幅宽的正文行左右相连，分不出栏，次序原样输出。
    bands = sorted(overlap_groups(line_cross, 0.25), key=min)
    for band in bands:
        columns = [
            [band[position] for position in column]
            for column in overlap_groups([line_flow[index] for index in band], 0.0)
        ]
        columns.sort(key=lambda column: min(line_flow[index][0] for index in column))
        for column in columns:
            for line in sorted(column):
                out.extend(chars[index] for index in lines[line])
    return out


def align_page(
    markdown: list[VisibleChar],
    ocr: list[OCRChar],
    writing_mode: str = "horizontal-tb",
) -> int:
    md_text, md_owners = normalized_chars(markdown)
    ocr_text, ocr_owners = normalized_chars(reading_order_chars(ocr, writing_mode))
    matcher = difflib.SequenceMatcher(None, md_text, ocr_text, autojunk=False)
    mapped = 0
    seen: set[int] = set()
    candidate_seen: set[int] = set()
    for tag, md_start, md_end, ocr_start, ocr_end in matcher.get_opcodes():
        if tag == "equal":
            for md_index, ocr_index in zip(range(md_start, md_end), range(ocr_start, ocr_end)):
                ocr_group = ocr_owners[ocr_index]
                # 折叠过的引导点一个单元对多个字符：整串点共用它们在纸面上的外接框。
                # 一对一时原样取用，不走 union_rect——那里的取整会动到本来精确的坐标。
                rect = ocr_group[0].rect if len(ocr_group) == 1 else union_rect(
                    [owner.rect for owner in ocr_group]
                )
                for owner in md_owners[md_index]:
                    key = id(owner)
                    if key not in seen:
                        owner.rect = rect
                        seen.add(key)
                        mapped += 1
            continue
        if tag != "replace" or md_end - md_start != ocr_end - ocr_start:
            continue
        for md_index, ocr_index in zip(range(md_start, md_end), range(ocr_start, ocr_end)):
            md_group = md_owners[md_index]
            ocr_group = ocr_owners[ocr_index]
            # 猜测值只对一对一的单元有意义；折叠过的点串拿不出「这个字其实是什么」。
            if len(md_group) != 1 or len(ocr_group) != 1:
                continue
            owner, ocr_owner = md_group[0], ocr_group[0]
            key = id(owner)
            if (
                key not in seen
                and key not in candidate_seen
                and len(normalize_units(owner.value)) == 1
                and len(normalize_units(ocr_owner.value)) == 1
            ):
                owner.candidate_value = ocr_owner.value
                owner.candidate_rect = ocr_owner.rect
                candidate_seen.add(key)
    return mapped


def union_rect(rects: Iterable[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    values = list(rects)
    return (
        round(min(rect[0] for rect in values), 6),
        round(min(rect[1] for rect in values), 6),
        round(max(rect[2] for rect in values), 6),
        round(max(rect[3] for rect in values), 6),
    )


def preferred_text_region(
    block: MarkdownBlock,
    page_chars: list[VisibleChar],
    candidates: list[LayoutBlock],
) -> tuple[float, float, float, float] | None:
    char_rects = [char.rect for char in page_chars if char.rect]
    if block.kind == "heading" and char_rects:
        return union_rect(char_rects)
    region = choose_layout_region("".join(char.value for char in page_chars), candidates)
    if region:
        return region.rect
    return union_rect(char_rects) if char_rects else None


def preferred_text_polygon(
    block: MarkdownBlock,
    page_chars: list[VisibleChar],
    candidates: list[LayoutBlock],
) -> list[tuple[float, float]] | None:
    if block.kind == "heading":
        return None
    region = choose_layout_region("".join(char.value for char in page_chars), candidates)
    return region.polygon if region else None


def block_ocr_text(block: MarkdownBlock) -> str:
    return "".join(char.candidate_value or char.value for char in block.chars)


def write_outputs(
    vault_root: Path,
    output_root: Path,
    book_id: str,
    title: str,
    pdf_path: str,
    markdown_root: str,
    texts: dict[str, str],
    blocks: list[MarkdownBlock],
    page_count: int,
    printed_page_offset: int,
    ocr_backend: str = "paddle",
    writing_mode: str = "horizontal-tb",
    rebuild: dict[str, Any] | None = None,
) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    by_file: dict[str, list[MarkdownBlock]] = defaultdict(list)
    pages: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chapters: dict[str, Any] = {}
    kind_totals: dict[str, int] = defaultdict(int)
    kind_mapped: dict[str, int] = defaultdict(int)
    unmapped_blocks: list[MarkdownBlock] = []

    for block in blocks:
        by_file[block.file].append(block)
    for file, file_blocks in by_file.items():
        mapped_characters = 0
        candidate_characters = 0
        total_characters = 0
        page_map: dict[str, int] = {}
        encoded_blocks = []
        for block in file_blocks:
            kind_totals[block.kind] += 1
            if block.regions:
                kind_mapped[block.kind] += 1
            else:
                unmapped_blocks.append(block)
            chars = []
            candidates = []
            missing = []
            character_supported = block.kind not in {"image", "table"}
            for char in block.chars:
                page_map[str(char.printed_page)] = char.physical_page
                if character_supported:
                    total_characters += 1
                if character_supported and char.rect:
                    mapped_characters += 1
                    chars.append([char.source_from, char.source_to, char.physical_page, list(char.rect)])
                elif character_supported and normalize_units(char.value):
                    if char.candidate_value and char.candidate_rect:
                        candidate_characters += 1
                        candidates.append([
                            char.source_from,
                            char.source_to,
                            char.candidate_value,
                            char.physical_page,
                            list(char.candidate_rect),
                            [char.value, char.candidate_value],
                        ])
                    missing.append([char.source_from, char.source_to])
            encoded_regions = [[page, list(rect)] for page, rect in block.regions]
            encoded_lines = [[page, list(rect)] for page, rect in block.lines]
            block_exact = bool(block.regions) and character_supported and not missing
            encoded_blocks.append({
                "id": block.block_id,
                "from": block.source_from,
                "to": block.source_to,
                "kind": block.kind,
                "expectedPages": sorted({char.physical_page for char in block.chars} | set(block.page_hints)),
                "text": "".join(char.value for char in block.chars),
                "exact": block_exact,
                "regions": [
                    {
                        "page": page,
                        "rect": rect,
                        **(
                            {"polygon": block.region_polygons[(page, tuple(rect))]}
                            if (page, tuple(rect)) in block.region_polygons
                            else {}
                        ),
                    }
                    for page, rect in encoded_regions
                ],
                "lines": [
                    {
                        "page": page,
                        "rect": rect,
                        **(
                            {"role": block.line_roles[(page, tuple(rect))]}
                            if (page, tuple(rect)) in block.line_roles
                            else {}
                        ),
                    }
                    for page, rect in encoded_lines
                ],
                "chars": [
                    {"from": start, "to": end, "page": page, "rect": rect}
                    for start, end, page, rect in chars
                ],
                "candidates": [
                    {
                        "from": start,
                        "to": end,
                        "value": value,
                        "page": page,
                        "rect": rect,
                        "alternatives": alternatives,
                    }
                    for start, end, value, page, rect, alternatives in candidates
                ],
                "missing": missing,
                **(
                    {"ocrText": block_ocr_text(block)}
                    if any(char.candidate_value for char in block.chars)
                    else {}
                ),
                "writingMode": writing_mode,
            })
            for page, rect in encoded_regions:
                pages[str(page)].append({
                    "id": block.block_id,
                    "file": file,
                    "from": block.source_from,
                    "to": block.source_to,
                    "page": page,
                    "rect": rect,
                    "kind": block.kind,
                    "exact": block_exact,
                })
        digest = hashlib.sha256(texts[file].encode("utf-8")).hexdigest()
        map_name = hashlib.sha1(file.encode("utf-8")).hexdigest()[:12] + ".json.gz"
        map_path = output_root / "chapters" / map_name
        map_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "bookId": book_id,
            "markdown": file,
            "hash": digest,
            "blocks": encoded_blocks,
        }
        with gzip.open(map_path, "wt", encoding="utf-8", errors="strict", newline="\n", compresslevel=9) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        chapters[file] = {
            "hash": digest,
            "map": map_path.relative_to(vault_root).as_posix(),
            "blocks": len(file_blocks),
            "mappedCharacters": mapped_characters,
            "candidateCharacters": candidate_characters,
            "totalCharacters": total_characters,
            "pageMap": page_map,
        }

    index = {
        "version": 1,
        "bookId": book_id,
        "title": title,
        "pdf": pdf_path,
        "pageCount": page_count,
        "chapters": chapters,
        "pages": pages,
    }
    write_text(output_root / "index.json", json.dumps(index, ensure_ascii=False, indent=2) + "\n")
    catalog_path = output_root.parent / "catalog.json"
    catalog = {"version": 1, "books": []}
    if catalog_path.exists():
        with catalog_path.open("r", encoding="utf-8", errors="strict") as handle:
            current_catalog = json.load(handle)
        if isinstance(current_catalog, dict) and isinstance(current_catalog.get("books"), list):
            catalog = current_catalog
    entry = {
        "id": book_id,
        "title": title,
        "markdownRoot": markdown_root,
        "pdf": pdf_path,
        "index": (output_root / "index.json").relative_to(vault_root).as_posix(),
        "printedPageOffset": printed_page_offset,
        "ocrBackend": ocr_backend,
        "writingMode": writing_mode,
        **({"rebuild": rebuild} if rebuild else {}),
    }
    # A run started without --rebuild-config must not silently drop the rebuild record an earlier run
    # wrote: keep every old field this run did not supply, and let the supplied ones win.
    previous = next((book for book in catalog["books"] if book.get("id") == book_id), {})
    entry = {**previous, **entry}
    catalog["books"] = [book for book in catalog["books"] if book.get("id") != book_id] + [entry]
    write_text_atomic(catalog_path, json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")

    total = sum(entry["totalCharacters"] for entry in chapters.values())
    mapped = sum(entry["mappedCharacters"] for entry in chapters.values())
    candidates = sum(entry["candidateCharacters"] for entry in chapters.values())
    mapped_blocks = sum(kind_mapped.values())
    report = [
        "# MinerU PDF Sync 映射报告",
        "",
        f"- 教材：{title}",
        f"- PDF 物理页：{page_count}",
        f"- Markdown 文件：{len(chapters)}",
        f"- 块级映射：{mapped_blocks}/{len(blocks)}",
        f"- 可见字符：{total}",
        f"- 逐字坐标：{mapped}",
        f"- 逐字覆盖率：{(mapped / total * 100 if total else 0):.2f}%",
        f"- 纠错后可激活的候选坐标：{candidates}",
        "",
        "选区内未取得逐字坐标的字符不会被猜测；其余可靠字符仍按行显示，若整个选区均无坐标才退回段落框。",
        "",
        "## 块级覆盖",
        "",
        "| 类型 | 已映射 | 总数 |",
        "|---|---:|---:|",
    ]
    for kind in sorted(kind_totals):
        report.append(f"| {kind} | {kind_mapped[kind]} | {kind_totals[kind]} |")
    report.extend(["", "## 无块级映射", ""])
    if unmapped_blocks:
        for block in unmapped_blocks:
            report.append(f"- `{block.file}` `[{block.source_from}, {block.source_to})`")
    else:
        report.append("无。")
    report.extend([
        "",
        "逐字失败区间记录在各章 sidecar 的 `blocks[].missing` 中。",
        "",
    ])
    write_text(output_root / "coverage-report.md", "\n".join(report))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--pdf", required=True, help="Vault-relative PDF path")
    parser.add_argument("--markdown-root", required=True)
    parser.add_argument("--markdown-file", action="append", help="Explicit file relative to Markdown root; repeat for custom filenames")
    parser.add_argument("--layout-part", action="append", required=True, help="layout.json|physical-page-start")
    parser.add_argument("--model-part", action="append", help="*_model.json|physical-page-start")
    parser.add_argument("--offset", type=int, required=True, help="physical page = printed page + offset")
    parser.add_argument(
        "--file-offset",
        action="append",
        default=[],
        help="Markdown path relative to markdown root|physical-minus-printed offset",
    )
    parser.add_argument(
        "--page-override",
        action="append",
        default=[],
        help="Markdown path relative to markdown root|printed page|physical page",
    )
    parser.add_argument(
        "--page-range-offset",
        action="append",
        default=[],
        help="file|start-end|offset: printed pages in the inclusive range use this offset (e.g. a second volume)",
    )
    parser.add_argument(
        "--rebuild-config",
        help="Vault-relative path of the sync configuration; recorded in catalog.json so the plugin can rerun pages",
    )
    parser.add_argument("--output", required=True, help="Vault-relative output directory")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--render-scale", type=float, default=2.0)
    parser.add_argument("--rec-model", default="PP-OCRv5_mobile_rec")
    parser.add_argument(
        "--det-model",
        default=DEFAULT_DETECTION_MODEL,
        help="PP-OCR detection model used to recover line boxes MinerU omitted (page footnotes)",
    )
    parser.add_argument(
        "--no-line-detection",
        action="store_true",
        help="Skip line-box recovery; blocks without MinerU line boxes stay unrecognized",
    )
    parser.add_argument("--ocr-backend", choices=("paddle", "onnx", "tesseract", "kraken"), default="paddle")
    parser.add_argument(
        "--onnx-model",
        default=DEFAULT_ONNX_MODEL,
        help="ONNX recognition model: PP-OCRv6_medium_rec, PP-OCRv5_mobile_rec, PP-OCRv5_server_rec, "
             "or a directory containing inference.onnx and inference.yml",
    )
    parser.add_argument(
        "--onnx-device",
        choices=("cpu", "dml", "auto"),
        default="auto",
        help="auto picks the DirectML adapter with the most dedicated video memory and falls back to CPU",
    )
    parser.add_argument(
        "--onnx-device-id",
        type=int,
        help="DirectML adapter index; overrides the automatic choice (0 is often the integrated GPU)",
    )
    parser.add_argument("--writing-mode", choices=("horizontal-tb", "vertical-rl"), default="horizontal-tb")
    parser.add_argument("--tesseract-lang", default="chi_sim")
    parser.add_argument("--kraken-model")
    parser.add_argument("--cpu-threads", type=int, default=9)
    parser.add_argument("--rec-batch-size", type=int, default=16)
    # No --pages here: it only ever forced a cache refresh, and the name implied it limited the run.
    parser.add_argument(
        "--rerun-pages",
        help="Force an OCR cache refresh for this inclusive physical page range (e.g. 201-205); "
             "every other page is still loaded and the whole book is rebuilt",
    )
    args = parser.parse_args()

    vault = args.vault.resolve()
    pdf_file = vault / Path(args.pdf)
    markdown_dir = vault / Path(args.markdown_root)
    output_dir = vault / Path(args.output)
    if not pdf_file.is_file() or not markdown_dir.is_dir():
        raise SystemExit("missing input")

    parts = []
    for value in args.layout_part:
        path_text, start_text = value.rsplit("|", 1)
        parts.append((Path(path_text).resolve(), int(start_text)))
    layout_pages = load_layout_parts(parts)
    model_pages: dict[int, list[LayoutBlock]] = {}
    model_lines: dict[int, list[tuple[float, float, float, float]]] = {}
    if args.model_part:
        model_parts = []
        for value in args.model_part:
            path_text, start_text = value.rsplit("|", 1)
            model_parts.append((Path(path_text).resolve(), int(start_text)))
        model_pages, model_lines = load_model_parts(model_parts)

    texts: dict[str, str] = {}
    blocks: list[MarkdownBlock] = []
    file_offsets, page_overrides, range_offsets = parse_offset_config(
        args.file_offset, args.page_override, args.page_range_offset
    )
    markdown_files = (
        [markdown_dir / name for name in args.markdown_file]
        if args.markdown_file else sorted(markdown_dir.glob("[0-9][0-9]-*.md"))
    )
    if not markdown_files:
        raise SystemExit("no chapter Markdown inputs; use --markdown-file for custom filenames")
    for path in markdown_files:
        book_relative = path.relative_to(markdown_dir).as_posix()
        text, parsed = parse_markdown_file(
            path,
            vault,
            page_mapper_for_file(book_relative, args.offset, file_offsets, page_overrides, range_offsets),
        )
        relative = path.relative_to(vault).as_posix()
        texts[relative] = text
        blocks.extend(parsed)

    chars_by_page: dict[int, list[VisibleChar]] = defaultdict(list)
    for block in blocks:
        for char in block.chars:
            chars_by_page[char.physical_page].append(char)

    import pymupdf

    document = pymupdf.open(pdf_file)
    page_numbers = sorted(page for page in chars_by_page if 1 <= page <= document.page_count)
    rerun_pages: set[int] = set()
    if args.rerun_pages:
        match = re.fullmatch(r"([1-9][0-9]*)-([1-9][0-9]*)", args.rerun_pages)
        if not match or int(match[1]) > int(match[2]):
            raise SystemExit("--rerun-pages must be an ascending 1-based physical range, e.g. 201-205")
        rerun_pages = set(range(int(match[1]), int(match[2]) + 1))
    if not model_lines:
        raise SystemExit("model parts with visual line boxes are required for character OCR")
    if args.ocr_backend == "paddle":
        engine: CharacterOCR = PaddleCharacterOCR(
            args.rec_model, args.cpu_threads, args.rec_batch_size, args.writing_mode
        )
    elif args.ocr_backend == "onnx":
        engine = OnnxCharacterOCR(
            args.onnx_model, args.onnx_device, args.onnx_device_id,
            args.rec_batch_size, args.cpu_threads, args.writing_mode,
        )
    elif args.ocr_backend == "tesseract":
        engine = TesseractCharacterOCR(args.tesseract_lang, args.writing_mode)
    else:
        engine = KrakenCharacterOCR(args.kraken_model, args.writing_mode)
    # 补行框只用 PaddleOCR 的检测模型，与识别后端无关：ONNX 路径同样要靠它把脚注、目录这类
    # MinerU 没给出行框的块补齐。Tesseract 与 Kraken 是「不装 Paddle 也能用」的备选，不碰它。
    supplementer = (
        LineSupplementer(args.cache, args.writing_mode, args.det_model, args.cpu_threads)
        if args.ocr_backend in {"paddle", "onnx"} and not args.no_line_detection
        else None
    )
    mapped = 0
    for page in page_numbers:
        base_lines = list(model_lines.get(page, []))
        loader = page_image_loader(document, page, args.render_scale)
        if supplementer is not None:
            # Same block source the region loop below uses, so both see the page the same way.
            extra_lines = supplementer.lines_for(
                page,
                model_pages.get(page) or layout_pages.get(page, []),
                base_lines,
                loader,
                page in rerun_pages,
            )
            if extra_lines:
                model_lines[page] = base_lines + extra_lines
        ocr_chars = load_or_run_ocr(
            engine,
            document,
            page,
            model_lines.get(page, []),
            args.cache,
            args.render_scale,
            args.writing_mode,
            page in rerun_pages,
            base_lines,
            loader,
        )
        mapped += align_page(chars_by_page[page], ocr_chars, args.writing_mode)

    image_offsets: dict[int, int] = defaultdict(int)
    for block in blocks:
        if block.kind == "image":
            page = block.page_hints[0]
            image_candidates = block_media_candidates(model_pages.get(page, []))
            image_index = image_offsets[page]
            if image_index < len(image_candidates):
                candidate = image_candidates[image_index]
                block.regions.append((page, candidate.rect))
                if candidate.polygon:
                    block.region_polygons[(page, candidate.rect)] = candidate.polygon
                image_offsets[page] += 1
            continue
        grouped: dict[int, list[VisibleChar]] = defaultdict(list)
        for char in block.chars:
            grouped[char.physical_page].append(char)
        for page, page_chars in grouped.items():
            candidates = model_pages.get(page) or layout_pages.get(page, [])
            rect = preferred_text_region(block, page_chars, candidates)
            if rect:
                block.regions.append((page, rect))
                polygon = preferred_text_polygon(block, page_chars, candidates)
                if polygon:
                    block.region_polygons[(page, rect)] = polygon
                selected_lines = lines_inside_region(rect, model_lines.get(page, []))
                marked_interlinear = interlinear_lines(model_lines.get(page, []), args.writing_mode)
                block.lines.extend((page, line) for line in selected_lines)
                for line in selected_lines:
                    if line in marked_interlinear:
                        block.line_roles[(page, line)] = "interlinear"

    write_outputs(
        vault,
        output_dir,
        args.book_id,
        args.title,
        Path(args.pdf).as_posix(),
        Path(args.markdown_root).as_posix(),
        texts,
        blocks,
        document.page_count,
        args.offset,
        args.ocr_backend,
        args.writing_mode,
        # Only a vault-relative configuration path is recorded; machine paths never enter the vault.
        {"config": Path(args.rebuild_config).as_posix()} if args.rebuild_config else None,
    )
    detected = f" detectedLinePages={supplementer.pages} detectedLines={supplementer.lines}" if supplementer else ""
    device = ""
    if hasattr(engine, "describe"):
        # Windows 控制台是 GBK，显卡名里的非 ASCII 字符会直接让 print 抛异常。
        device = " device=" + engine.describe().encode("ascii", "backslashreplace").decode("ascii")
    print(
        f"backend={args.ocr_backend}{device} pages={len(page_numbers)} "
        f"blocks={len(blocks)} mapped={mapped}{detected}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"error={type(error).__name__}", file=sys.stderr)
        raise
