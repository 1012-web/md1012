from pathlib import Path
import hashlib
import importlib.util
import json
import sys

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_mapper.py"
SPEC = importlib.util.spec_from_file_location("generate_map", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_utf16_offsets() -> None:
    assert MODULE.utf16_offsets("甲A\U00020000") == [0, 1, 2, 4]


def test_markdown_mask_keeps_text_only() -> None:
    source = "### 标题\n\n正文[^1] [可见文字](https://example.com) <!--p.12-->继续"
    mask = MODULE.markdown_mask(source)
    visible = "".join(char for char, keep in zip(source, mask) if keep)
    assert "标题" in visible
    assert "正文" in visible
    assert "可见文字" in visible
    assert "https" not in visible
    assert "[" not in visible
    assert "[^1]" not in visible
    assert "p.12" not in visible


def test_box_to_rect() -> None:
    assert MODULE.box_to_rect([10, 20, 30, 40], 100, 200) == (0.1, 0.1, 0.3, 0.2)


def test_lines_inside_region() -> None:
    lines = [
        (0.1, 0.1, 0.8, 0.2),
        (0.1, 0.3, 0.8, 0.4),
        (0.1, 0.7, 0.8, 0.8),
    ]
    assert MODULE.lines_inside_region((0.05, 0.05, 0.9, 0.45), lines) == lines[:2]


def test_heading_region_prefers_character_boxes_over_parent_block() -> None:
    first = MODULE.VisibleChar("标", 0, 1, 1, 41, rect=(0.2, 0.3, 0.3, 0.4))
    second = MODULE.VisibleChar("题", 1, 2, 1, 41, rect=(0.3, 0.3, 0.4, 0.4))
    heading = MODULE.MarkdownBlock("chapter.md", 0, 2, "heading", chars=[first, second])
    parent = MODULE.LayoutBlock(41, (0.1, 0.2, 0.9, 0.8), "标题和整段正文", "text")

    assert MODULE.preferred_text_region(heading, [first, second], [parent]) == (0.2, 0.3, 0.4, 0.4)


def test_block_media_candidates_accepts_exported_table_images() -> None:
    candidates = [
        MODULE.LayoutBlock(1, (0.1, 0.4, 0.9, 0.6), "table", "table"),
        MODULE.LayoutBlock(1, (0.1, 0.1, 0.9, 0.2), "", "image"),
        MODULE.LayoutBlock(1, (0.1, 0.3, 0.9, 0.35), "caption", "image_caption"),
    ]

    assert [candidate.kind for candidate in MODULE.block_media_candidates(candidates)] == [
        "image",
        "table",
    ]


def test_parse_markdown_tracks_cross_page_text_and_footnote_pages(tmp_path: Path) -> None:
    source = "---\npages: 10-11\n---\n甲<!--p.11-->乙\n\n[^1]: <!--p.3-->注"
    path = tmp_path / "01-test.md"
    MODULE.write_text(path, source)

    _, blocks = MODULE.parse_markdown_file(path, tmp_path, lambda page: page + 40)
    body = next(block for block in blocks if block.kind == "paragraph")
    footnote = next(block for block in blocks if block.kind == "footnote")

    assert [(char.value, char.printed_page, char.physical_page) for char in body.chars] == [
        ("甲", 10, 50),
        ("乙", 11, 51),
    ]
    assert [(char.value, char.printed_page, char.physical_page) for char in footnote.chars] == [
        ("注", 3, 43),
    ]


def test_parse_markdown_keeps_images_as_block_only(tmp_path: Path) -> None:
    path = tmp_path / "01-test.md"
    MODULE.write_text(path, "<!--p.8-->\n\n![](images/map.jpg)")

    _, blocks = MODULE.parse_markdown_file(path, tmp_path, lambda page: page + 40)
    image = next(block for block in blocks if block.kind == "image")

    assert image.chars == []
    assert image.page_hints == [48]


def test_page_mapper_supports_front_matter_page_exceptions() -> None:
    mapper = MODULE.page_mapper_for_file(
        "02-总序.md",
        40,
        {"02-总序.md": 3},
        {("02-总序.md", -36): 4},
    )

    assert mapper(-36) == 4
    assert mapper(2) == 5


def test_align_page_leaves_ocr_misses_unmapped() -> None:
    markdown = [
        MODULE.VisibleChar("甲", 0, 1, 1, 41),
        MODULE.VisibleChar("乙", 1, 2, 1, 41),
    ]
    rectangle = (0.1, 0.2, 0.3, 0.4)
    ocr = [
        MODULE.OCRChar("甲", rectangle),
        MODULE.OCRChar("丙", (0.3, 0.2, 0.5, 0.4)),
    ]

    assert MODULE.align_page(markdown, ocr) == 1
    assert markdown[0].rect == rectangle
    assert markdown[1].rect is None
    assert markdown[1].candidate_value == "丙"
    assert markdown[1].candidate_rect == (0.3, 0.2, 0.5, 0.4)


def test_load_model_parts_keeps_blocks_and_visual_lines(tmp_path: Path) -> None:
    path = tmp_path / "sample_model.json"
    payload = [[
        {"type": "text", "bbox": [0.1, 0.1, 0.9, 0.4], "content": "正文"},
        {"type": "ocr_text", "bbox": [0.1, 0.1, 0.9, 0.2], "text": "", "score": 1.0},
    ]]
    with path.open("w", encoding="utf-8", errors="strict", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False)

    blocks, lines = MODULE.load_model_parts([(path, 201)])

    assert blocks[201][0].text == "正文"
    assert lines[201] == [(0.1, 0.1, 0.9, 0.2)]


def test_chars_from_word_info_uses_ctc_character_columns() -> None:
    word_info = [20, [["甲", "乙"], ["A", "B"]], [[2, 6], [12, 16]], ["cn", "en&num"]]

    chars = MODULE.chars_from_word_info("甲乙AB", word_info, (0.0, 0.2, 1.0, 0.3))

    assert [char.value for char in chars] == ["甲", "乙", "A", "B"]
    assert chars[0].rect == (0.025, 0.2, 0.225, 0.3)
    assert chars[-1].rect == (0.725, 0.2, 0.925, 0.3)


def test_chars_from_word_info_supports_vertical_columns() -> None:
    word_info = [10, [["甲", "乙"]], [[1, 6]], ["cn"]]

    chars = MODULE.chars_from_word_info(
        "甲乙", word_info, (0.7, 0.1, 0.8, 0.9), "vertical-rl"
    )

    assert [char.value for char in chars] == ["甲", "乙"]
    assert chars[0].rect[0] == 0.7
    assert chars[0].rect[2] == 0.8
    assert chars[0].rect[1] < chars[1].rect[1]


def test_normalized_polygon_keeps_nonrectangular_geometry() -> None:
    polygon = MODULE.normalized_polygon([[10, 20], [30, 20], [25, 40]], 100, 200)

    assert polygon == [(0.1, 0.1), (0.3, 0.1), (0.25, 0.2)]


def test_vertical_interlinear_lines_are_marked_in_pairs() -> None:
    lines = [
        (0.8, 0.1, 0.9, 0.9),
        (0.68, 0.2, 0.715, 0.7),
        (0.72, 0.2, 0.755, 0.7),
        (0.55, 0.1, 0.65, 0.9),
    ]

    marked = MODULE.interlinear_lines(lines, "vertical-rl")

    assert marked == {lines[1], lines[2]}
    assert MODULE.interlinear_lines(lines, "horizontal-tb") == set()


def test_write_outputs_merges_catalog_books(tmp_path: Path) -> None:
    first = tmp_path / "sync" / "first"
    second = tmp_path / "sync" / "second"
    MODULE.write_outputs(tmp_path, first, "first", "甲", "a.pdf", "a", {}, [], 1, 0)
    MODULE.write_outputs(tmp_path, second, "second", "乙", "b.pdf", "b", {}, [], 1, 0)

    with (tmp_path / "sync" / "catalog.json").open(
        "r", encoding="utf-8", errors="strict"
    ) as handle:
        catalog = json.load(handle)
    assert [book["id"] for book in catalog["books"]] == ["first", "second"]


def test_page_range_offsets_apply_before_default_offset() -> None:
    mapper = MODULE.page_mapper_for_file(
        "02-正文.md",
        15,
        {},
        {("02-正文.md", 1): 3},
        {"02-正文.md": [(423, 893, 26)]},
    )

    assert mapper(1) == 3
    assert mapper(2) == 17
    assert mapper(421) == 436
    assert mapper(423) == 449
    assert mapper(893) == 919


def test_parse_page_range_offset_supports_negative_start() -> None:
    assert MODULE.parse_page_range_offset("a/b.md|423-893|26") == ("a/b.md", 423, 893, 26)
    assert MODULE.parse_page_range_offset("b.md|-30--10|34") == ("b.md", -30, -10, 34)
    try:
        MODULE.parse_page_range_offset("b.md|10-5|1")
    except ValueError:
        pass
    else:
        raise AssertionError("descending range must be rejected")


def test_write_outputs_records_only_vault_relative_rebuild_config(tmp_path: Path) -> None:
    out = tmp_path / "sync" / "book"
    MODULE.write_outputs(tmp_path, out, "book", "书", "b.pdf", "b", {}, [], 1, 0,
                         rebuild={"config": "sync/_config/book.json"})
    with (tmp_path / "sync" / "catalog.json").open("r", encoding="utf-8", errors="strict") as handle:
        catalog = json.load(handle)
    assert catalog["books"][0]["rebuild"] == {"config": "sync/_config/book.json"}
    assert "python" not in catalog["books"][0]["rebuild"] and "cache" not in catalog["books"][0]["rebuild"]


def test_merged_body_chapter_headings_reset_page_from_chapter_metadata(tmp_path: Path) -> None:
    source = tmp_path / "03-正文.md"
    source.write_text(
        "---\n"
        "title: 书 · 正文\n"
        "pages: 1-277\n"
        "chapter_metadata:\n"
        "- original_file: 03-第一章 甲.md\n"
        "  title: 第一章 甲\n"
        "  pages: 1-15\n"
        "- original_file: 04-第二章 乙乙.md\n"
        "  title: '第二章 乙乙'\n"
        "  pages: 16-54\n"
        "---\n"
        "# 第一章 甲\n\n正文一<!--p.2-->正文二\n\n[^1]: <!--p.10--> 注一\n\n# 第二章 乙乙\n\n## 第一节 丙\n\n开篇<!--p.17-->下一页\n",
        encoding="utf-8",
    )
    _, blocks = MODULE.parse_markdown_file(source, tmp_path, lambda p: p + 10)
    by_text = {"".join(c.value for c in b.chars): b for b in blocks}
    assert by_text["第一章甲"].chars[0].printed_page == 1
    assert by_text["注一"].chars[0].printed_page == 10
    # The chapter heading and its opening text sit on printed page 16, not the last footnote's page 10.
    assert by_text["第二章乙乙"].chars[0].printed_page == 16
    assert by_text["第一节丙"].chars[0].printed_page == 16
    opening = by_text["开篇下一页"]
    assert [c.printed_page for c in opening.chars] == [16, 16, 17, 17, 17]


def test_chapter_start_pages_and_heading_title_normalization() -> None:
    starts = MODULE.chapter_start_pages("pages: 1-9\nchapter_metadata:\n- original_file: a.md\n  title: 第三章  资产阶级革命的时代(1776—1849)\n  pages: 200-329\n")
    assert starts == {MODULE.normalize_units("第三章资产阶级革命的时代(1776—1849)"): 200}
    assert MODULE.heading_title("## **第三章** 资产阶级革命的时代(1776—1849) <!--p.200-->") == MODULE.normalize_units("第三章资产阶级革命的时代(1776—1849)")


def test_chapter_start_pages_reads_indented_entries_flow_pages_and_quotes() -> None:
    frontmatter = (
        "pages: 1-400\n"
        "chapter_metadata:\n"
        "  - original_file: a.md\n"
        "    title: 第一章 甲\n"
        "    aliases:\n"
        "      - 别名甲\n"
        "      - 别名乙\n"
        "    pages: [61, 80]\n"
        "  - original_file: b.md\n"
        "    pages: 101-120\n"
        "    title: \"第二章 '乙'\"\n"
        "  - original_file: c.md\n"
        "    title: 第一章 甲\n"
        "    pages: 300-310\n"
        "title: 整书\n"
        "pages: 999\n"
    )

    starts = MODULE.chapter_start_pages(frontmatter)

    # A nested aliases list must not be mistaken for the next chapter entry.
    assert starts[MODULE.normalize_units("第一章甲")] == 61
    # Only one matching quote pair is stripped; the inner quotes belong to the title.
    assert starts[MODULE.normalize_units("第二章'乙'")] == 101
    # A repeated title keeps the first entry's page, and top-level keys never leak in.
    assert len(starts) == 2


def test_chapter_start_pages_ignores_deeper_keys_and_bare_numbers() -> None:
    frontmatter = (
        "chapter_metadata:\n"
        "- title: 第三章\n"
        "  source:\n"
        "    title: 不是章标题\n"
        "    pages: 999\n"
        "  pages: 5\n"
    )

    assert MODULE.chapter_start_pages(frontmatter) == {MODULE.normalize_units("第三章"): 5}


def test_heading_title_uses_only_the_first_line_of_a_block() -> None:
    raw = "## 第一章 甲\n开篇正文乙丙"

    assert MODULE.heading_title(raw) == MODULE.normalize_units("第一章甲")


def test_parse_page_range_offset_rejects_incomplete_forms_with_one_message() -> None:
    for value in ("bad", "a.md|61|3", "a.md|61", "|1-2|3", "a.md|1-2|x", "a.md|1-|3"):
        try:
            MODULE.parse_page_range_offset(value)
        except ValueError as error:
            assert "page range offset must look like 'file|start-end|offset'" in str(error), value
        else:
            raise AssertionError(f"must be rejected: {value}")
    assert MODULE.parse_page_range_offset(" a.md | 423-893 | 26 ") == ("a.md", 423, 893, 26)


def test_parse_offset_config_is_shared_by_both_entry_points() -> None:
    offsets, overrides, ranges = MODULE.parse_offset_config(
        ["02-附录.md|42"], ["01-正文.md|-36|4"], ["01-正文.md|423-893|26", "01-正文.md|1-10|0"]
    )

    assert offsets == {"02-附录.md": 42}
    assert overrides == {("01-正文.md", -36): 4}
    assert ranges == {"01-正文.md": [(423, 893, 26), (1, 10, 0)]}
    for bad in (["a.md"], ["a.md|x"]):
        try:
            MODULE.parse_offset_config(bad, [], [])
        except ValueError as error:
            assert "file offset must look like" in str(error)
        else:
            raise AssertionError(f"must be rejected: {bad}")
    try:
        MODULE.parse_offset_config([], ["a.md|1"], [])
    except ValueError as error:
        assert "page override must look like" in str(error)
    else:
        raise AssertionError("incomplete page override must be rejected")


def test_catalog_merge_keeps_fields_the_new_run_did_not_supply(tmp_path: Path) -> None:
    first = tmp_path / "sync" / "v1"
    second = tmp_path / "sync" / "v2"
    MODULE.write_outputs(tmp_path, first, "book", "书", "b.pdf", "b", {}, [], 1, 0,
                         ocr_backend="tesseract", rebuild={"config": "sync/_config/book.json"})
    MODULE.write_outputs(tmp_path, second, "book", "书 第二版", "b.pdf", "b", {}, [], 1, 7)

    with (tmp_path / "sync" / "catalog.json").open("r", encoding="utf-8", errors="strict") as handle:
        catalog = json.load(handle)
    entry = catalog["books"][0]
    assert entry["title"] == "书 第二版"
    assert entry["printedPageOffset"] == 7
    assert entry["index"] == "sync/v2/index.json"
    # rebuild came from the earlier run only; dropping it would strand the plugin's rerun path.
    assert entry["rebuild"] == {"config": "sync/_config/book.json"}
    # ocrBackend is supplied by both runs, so the newer value wins rather than being preserved.
    assert entry["ocrBackend"] == "paddle"


def test_catalog_is_replaced_atomically_without_leftovers(tmp_path: Path) -> None:
    out = tmp_path / "sync" / "book"
    MODULE.write_outputs(tmp_path, out, "book", "书", "b.pdf", "b", {}, [], 1, 0)
    catalog_path = tmp_path / "sync" / "catalog.json"
    original = catalog_path.read_bytes()

    def explode(*args, **kwargs):
        raise OSError("disk full")

    real_replace = MODULE.os.replace
    MODULE.os.replace = explode
    try:
        try:
            MODULE.write_outputs(tmp_path, out, "book", "书", "b.pdf", "b", {}, [], 1, 0)
        except OSError:
            pass
        else:
            raise AssertionError("a failed replace must surface")
    finally:
        MODULE.os.replace = real_replace

    assert catalog_path.read_bytes() == original
    assert [p.name for p in (tmp_path / "sync").iterdir() if p.name.endswith(".tmp")] == []


# --- 补行框：没有 ocr_text 行框的文本块（主要是页脚脚注）----------------------------


class StubDetector:
    """A detection stand-in: every call yields fixed crop-pixel polygons, so no model is loaded in unit tests."""

    def __init__(self, polygons_per_call: list[list[list[tuple[float, float]]]]) -> None:
        self.queue = list(polygons_per_call)
        self.crops: list[tuple[int, int]] = []

    def detect(self, crop):
        self.crops.append(crop.shape[:2])
        return self.queue.pop(0) if self.queue else []


class StubEngine:
    backend = "paddle"

    def __init__(self, values: str = "") -> None:
        self.values = values
        self.requests: list[list[tuple[float, float, float, float]]] = []

    def recognize(self, image, line_rects):
        self.requests.append(list(line_rects))
        return [
            MODULE.OCRChar(value, (0.1, 0.8 + index * 0.01, 0.2, 0.81 + index * 0.01))
            for index, value in enumerate(self.values)
        ]


def blank_image(width: int = 400, height: int = 600):
    import numpy as np

    return np.zeros((height, width, 3), dtype=np.uint8)


def test_blocks_missing_lines_picks_text_blocks_without_visual_lines() -> None:
    body = MODULE.LayoutBlock(24, (0.15, 0.15, 0.91, 0.74), "正文", "text")
    footnote = MODULE.LayoutBlock(24, (0.157, 0.778, 0.91, 0.938), "① 仅就明清", "page_footnote")
    header = MODULE.LayoutBlock(24, (0.23, 0.11, 0.36, 0.14), "中国近现代史", "header")
    number = MODULE.LayoutBlock(24, (0.17, 0.11, 0.21, 0.14), "12", "page_number")
    table = MODULE.LayoutBlock(24, (0.2, 0.4, 0.8, 0.5), "表", "table")
    empty = MODULE.LayoutBlock(24, (0.2, 0.95, 0.8, 0.97), "   ", "text")
    caption = MODULE.LayoutBlock(24, (0.2, 0.76, 0.8, 0.77), "图一", "image_caption")
    lines = [(0.16, 0.156, 0.9, 0.172)]

    pending = MODULE.blocks_missing_lines(
        [body, footnote, header, number, table, empty, caption], lines
    )

    assert [block.kind for block in pending] == ["image_caption", "page_footnote"]


def test_detect_block_lines_maps_crop_polygons_back_to_the_page() -> None:
    image = blank_image(400, 600)
    detector = StubDetector([[[(0, 0), (200, 0), (200, 20), (0, 20)]]])

    lines = MODULE.detect_block_lines(detector, image, (0.25, 0.5, 0.75, 0.6), pad=0, margin=0)

    assert detector.crops == [(60, 200)]
    assert lines == [(0.25, 0.5, 0.75, 0.533333)]


def test_detect_block_lines_bleeds_white_space_in_and_drops_what_it_dragged_along() -> None:
    # A one-line block cropped tight is a strip the detector cannot read, so the crop grows by half the block
    # height on every side; anything centred outside the original block belongs to a neighbour, not to it.
    image = blank_image(400, 600)
    detector = StubDetector([[
        [(0, 0), (200, 0), (200, 20), (0, 20)],
        [(40, 40), (240, 40), (240, 60), (40, 60)],
    ]])

    lines = MODULE.detect_block_lines(detector, image, (0.25, 0.5, 0.75, 0.6))

    assert detector.crops == [(120, 260)]
    assert lines == [(0.275, 0.516667, 0.75, 0.55)]


def test_line_supplementer_adds_footnote_lines_after_the_body_and_caches_them(tmp_path: Path) -> None:
    image = blank_image(400, 600)
    footnote = MODULE.LayoutBlock(24, (0.1, 0.8, 0.9, 0.9), "① 脚注", "page_footnote")
    body_lines = [(0.1, 0.2, 0.9, 0.23), (0.1, 0.25, 0.9, 0.28)]
    detector = StubDetector([[
        [(40, 40), (360, 40), (360, 60), (40, 60)],
        [(40, 70), (360, 70), (360, 90), (40, 90)],
    ]])
    supplementer = MODULE.LineSupplementer(tmp_path, factory=lambda: detector)

    extra = supplementer.lines_for(24, [footnote], body_lines, lambda: image)

    assert len(extra) == 2
    assert extra == sorted(extra, key=lambda rect: rect[1])
    assert min(rect[1] for rect in extra) > max(rect[3] for rect in body_lines)
    assert MODULE.lines_follow(body_lines, extra, "horizontal-tb")
    assert supplementer.pages == 1 and supplementer.lines == 2

    cached = MODULE.LineSupplementer(tmp_path, factory=lambda: StubDetector([]))
    assert cached.lines_for(24, [footnote], body_lines, lambda: image) == extra
    assert cached.detector is None


def test_line_supplementer_skips_pages_whose_blocks_all_have_lines(tmp_path: Path) -> None:
    body = MODULE.LayoutBlock(24, (0.1, 0.15, 0.9, 0.3), "正文", "text")
    supplementer = MODULE.LineSupplementer(tmp_path, factory=lambda: StubDetector([]))

    def explode():
        raise AssertionError("a page with complete line boxes must not be rendered")

    assert supplementer.lines_for(24, [body], [(0.1, 0.16, 0.9, 0.18)], explode) == []
    assert supplementer.detector is None


def test_rects_overlap_only_flags_a_real_share_of_the_smaller_box() -> None:
    line = (0.1, 0.50, 0.9, 0.53)
    assert MODULE.rects_overlap(line, (0.1, 0.505, 0.9, 0.535))
    assert not MODULE.rects_overlap(line, (0.1, 0.54, 0.9, 0.57))
    assert not MODULE.rects_overlap(line, (0.1, 0.529, 0.9, 0.56))


def test_line_supplementer_drops_a_second_detection_of_the_same_line(tmp_path: Path) -> None:
    # MinerU can emit overlapping blocks; the same physical line must not enter the page twice.
    image = blank_image(400, 600)
    upper = MODULE.LayoutBlock(24, (0.0, 0.5, 1.0, 0.7), "上块", "page_footnote")
    lower = MODULE.LayoutBlock(24, (0.0, 0.6, 1.0, 0.8), "下块", "page_footnote")
    detector = StubDetector([
        [[(0, 42), (400, 42), (400, 162), (0, 162)]],
        [[(0, 0), (400, 0), (400, 110), (0, 110)]],
    ])
    supplementer = MODULE.LineSupplementer(tmp_path, factory=lambda: detector)

    extra = supplementer.lines_for(24, [upper, lower], [], lambda: image)

    # 下块的检测结果整条压在上块那条上，丢弃；块矩形本身也已被覆盖，不再兜底补一条
    assert extra == [(0.0, 0.516667, 1.0, 0.7)]


def test_lines_follow_rejects_a_supplement_that_would_be_read_first() -> None:
    body = [(0.1, 0.2, 0.9, 0.23)]
    assert MODULE.lines_follow(body, [(0.1, 0.8, 0.9, 0.83)], "horizontal-tb")
    assert not MODULE.lines_follow(body, [(0.1, 0.05, 0.9, 0.08)], "horizontal-tb")
    assert MODULE.lines_follow(body, [], "horizontal-tb")
    assert MODULE.lines_follow([], [(0.1, 0.05, 0.9, 0.08)], "horizontal-tb")


def test_ocr_cache_filename_tracks_the_line_set(tmp_path: Path) -> None:
    body = [(0.1, 0.2, 0.9, 0.23)]
    engine = StubEngine("甲")
    MODULE.load_or_run_ocr(engine, None, 24, body, tmp_path, 2.0, image_loader=lambda: None)
    first = [path.name for path in tmp_path.glob("p0024-*.json")]

    MODULE.load_or_run_ocr(
        engine, None, 24, body + [(0.1, 0.8, 0.9, 0.83)], tmp_path, 2.0, image_loader=lambda: None
    )
    names = sorted(path.name for path in tmp_path.glob("p0024-*.json"))

    assert len(first) == 1 and len(names) == 2
    assert all(name.startswith("p0024-") and len(name) == len("p0024-abcdef12.json") for name in names)


def test_legacy_page_cache_is_reused_and_only_the_supplement_is_recognized(tmp_path: Path) -> None:
    body = [(0.1, 0.2, 0.9, 0.23), (0.1, 0.25, 0.9, 0.28)]
    extra = [(0.1, 0.8, 0.9, 0.83)]
    MODULE.write_text(
        tmp_path / "p0024.json",
        json.dumps([["甲", [0.1, 0.2, 0.2, 0.23]], ["乙", [0.2, 0.2, 0.3, 0.23]]], ensure_ascii=False),
    )
    engine = StubEngine("①注")

    chars = MODULE.load_or_run_ocr(
        engine, None, 24, body + extra, tmp_path, 2.0, "horizontal-tb", False, body, lambda: None
    )

    assert engine.requests == [extra]
    assert [char.value for char in chars] == ["甲", "乙", "①", "注"]
    fresh = tmp_path / f"p0024-{MODULE.line_digest(body + extra)}.json"
    assert fresh.exists()
    assert (tmp_path / "p0024.json").exists()
    # 第二趟走新文件名，不再识别
    again = MODULE.load_or_run_ocr(
        engine, None, 24, body + extra, tmp_path, 2.0, "horizontal-tb", False, body, lambda: None
    )
    assert engine.requests == [extra]
    assert [char.value for char in again] == ["甲", "乙", "①", "注"]


def test_legacy_cache_is_rejected_when_the_supplement_would_come_first(tmp_path: Path) -> None:
    body = [(0.1, 0.5, 0.9, 0.53)]
    extra = [(0.1, 0.05, 0.9, 0.08)]
    MODULE.write_text(tmp_path / "p0024.json", json.dumps([["甲", [0.1, 0.5, 0.2, 0.53]]]))
    engine = StubEngine("乙丙")

    chars = MODULE.load_or_run_ocr(
        engine, None, 24, body + extra, tmp_path, 2.0, "horizontal-tb", False, body, lambda: None
    )

    assert engine.requests == [MODULE.ordered_lines(body + extra, "horizontal-tb")]
    assert [char.value for char in chars] == ["乙", "丙"]


def test_legacy_cache_without_any_supplement_is_copied_under_the_new_name(tmp_path: Path) -> None:
    body = [(0.1, 0.2, 0.9, 0.23)]
    MODULE.write_text(tmp_path / "p0024.json", json.dumps([["甲", [0.1, 0.2, 0.2, 0.23]]]))
    engine = StubEngine("乙")

    chars = MODULE.load_or_run_ocr(
        engine, None, 24, body, tmp_path, 2.0, "horizontal-tb", False, body, lambda: None
    )

    assert engine.requests == []
    assert [char.value for char in chars] == ["甲"]
    assert (tmp_path / f"p0024-{MODULE.line_digest(body)}.json").exists()


def test_appended_footnote_characters_do_not_disturb_body_alignment() -> None:
    body_text = "明清农业发展"
    note_text = "①脚注文字"
    markdown = [
        MODULE.VisibleChar(value, index, index + 1, 12, 24)
        for index, value in enumerate(body_text + note_text)
    ]
    ocr = [MODULE.OCRChar(value, (0.1, 0.2 + i * 0.01, 0.2, 0.21 + i * 0.01))
           for i, value in enumerate(body_text)]
    ocr += [MODULE.OCRChar(value, (0.1, 0.8 + i * 0.01, 0.2, 0.81 + i * 0.01))
            for i, value in enumerate(note_text)]

    mapped = MODULE.align_page(markdown, ocr)

    assert mapped == len(markdown)
    assert [char.rect for char in markdown] == [char.rect for char in ocr]
    assert markdown[len(body_text)].rect[1] >= 0.8


# --- det-v2：短脚注的兜底与投影切行 -------------------------------------------


def ink_image(bands, width=400, height=600, columns=(50, 300)):
    """白底黑带的合成页：每条 band 是 (起始行, 结束行)，用来喂水平投影。"""
    import numpy as np

    image = np.full((height, width, 3), 255, dtype=np.uint8)
    for top, bottom in bands:
        image[top:bottom, columns[0]:columns[1]] = 0
    return image


def test_covered_span_merges_overlapping_line_ranges() -> None:
    assert MODULE.covered_span([]) == 0.0
    assert MODULE.covered_span([(0, 0.1, 1, 0.2), (0, 0.3, 1, 0.4)]) == pytest.approx(0.2)
    assert MODULE.covered_span([(0, 0.1, 1, 0.3), (0, 0.2, 1, 0.4)]) == pytest.approx(0.3)


def test_lines_cover_block_rejects_detection_that_read_only_a_sliver() -> None:
    block = (0.15, 0.8, 0.9, 0.9)
    assert not MODULE.lines_cover_block([], block)
    assert not MODULE.lines_cover_block([(0.15, 0.8, 0.9, 0.82)], block)
    assert MODULE.lines_cover_block([(0.15, 0.8, 0.9, 0.86)], block)


def test_fallback_reads_a_single_line_block_whole() -> None:
    # 世界史古代史编（下卷）的真实形态：每条脚注自成一块，块高约等于一个正文行高
    rect = (0.15, 0.911, 0.652, 0.929)
    assert MODULE.fallback_block_lines(blank_image(), rect, 0.016) == [rect]
    # 页面上没有行框可当尺子时退回固定阈值
    assert MODULE.fallback_block_lines(blank_image(), rect, 0.0) == [rect]


def test_fallback_splits_a_taller_block_by_the_blank_rows_between_lines() -> None:
    image = ink_image([(305, 321), (335, 351), (365, 379)])
    rect = (0.1, 0.5, 0.9, 0.65)

    lines = MODULE.fallback_block_lines(image, rect, 0.02)

    assert lines == [
        (0.125, 0.508333, 0.75, 0.535),
        (0.125, 0.558333, 0.75, 0.585),
        (0.125, 0.608333, 0.75, 0.631667),
    ]


def test_projection_calibrates_its_row_gap_from_the_block_when_no_body_line_exists() -> None:
    # 目录页在 model JSON 里一条行框都没有，没有正文行可当尺子；按块高猜会把整页并成一行
    image = ink_image([(305, 321), (335, 351), (365, 379)])
    rect = (0.1, 0.5, 0.9, 0.65)

    assert MODULE.project_block_rows(image, rect, 0.0) == MODULE.project_block_rows(image, rect, 0.02)
    assert len(MODULE.project_block_rows(image, rect, 0.0)) == 3


def test_merge_row_boxes_joins_the_marker_to_its_sentence() -> None:
    marker = (0.153, 0.9159, 0.167, 0.9275)
    sentence = (0.183, 0.9160, 0.489, 0.9298)
    other_row = (0.15, 0.877, 0.58, 0.896)

    assert MODULE.merge_row_boxes([sentence, marker, other_row]) == [
        other_row, (0.153, 0.9159, 0.489, 0.9298)
    ]


def test_a_marker_sized_detection_does_not_count_as_covering_the_line() -> None:
    # 页 14 的真实形态：检测只咬到 ①，高度满格但宽度只有二十分之一
    block = (0.15, 0.911, 0.652, 0.929)
    assert not MODULE.lines_cover_block([(0.1556, 0.9131, 0.177, 0.9262)], block)
    assert MODULE.lines_cover_block([(0.1556, 0.9131, 0.6, 0.9262)], block)


def test_projection_returns_nothing_on_a_blank_crop_and_the_block_is_used_whole() -> None:
    rect = (0.1, 0.5, 0.9, 0.65)
    assert MODULE.project_block_rows(blank_image(), rect, 0.02) == []
    assert MODULE.fallback_block_lines(blank_image(), rect, 0.02) == [rect]


def test_empty_detection_never_leaves_a_text_block_without_a_line(tmp_path: Path) -> None:
    footnote = MODULE.LayoutBlock(14, (0.15, 0.911, 0.652, 0.929), "① “单”，匈奴语义为大。", "page_footnote")
    body = [(0.15, 0.2 + index * 0.02, 0.9, 0.216 + index * 0.02) for index in range(5)]
    detector = StubDetector([[]])
    supplementer = MODULE.LineSupplementer(tmp_path, factory=lambda: detector)

    extra = supplementer.lines_for(14, [footnote], body, lambda: blank_image())

    assert extra == [footnote.rect]
    assert MODULE.lines_follow(body, extra, "horizontal-tb")


def test_thin_detection_is_replaced_by_the_projected_rows(tmp_path: Path) -> None:
    image = ink_image([(305, 321), (335, 351), (365, 379)])
    block = MODULE.LayoutBlock(9, (0.1, 0.5, 0.9, 0.65), "三行脚注", "page_footnote")
    # 检测只咬到一条，覆盖不到块高一半，整块改用投影切行
    detector = StubDetector([[[(0, 10), (300, 10), (300, 22), (0, 22)]]])
    supplementer = MODULE.LineSupplementer(tmp_path, factory=lambda: detector)

    extra = supplementer.lines_for(9, [block], [], lambda: image)

    assert len(extra) == 3
    assert extra[0][1] == pytest.approx(0.508333)


def test_detection_cache_key_changes_with_the_algorithm_version(tmp_path: Path) -> None:
    footnote = MODULE.LayoutBlock(14, (0.15, 0.911, 0.652, 0.929), "① 注", "page_footnote")
    supplementer = MODULE.LineSupplementer(tmp_path, factory=lambda: StubDetector([[]]))
    current = supplementer.plan_digest([footnote])
    previous = hashlib.sha256(json.dumps(
        {"model": MODULE.DEFAULT_DETECTION_MODEL, "blocks": [[round(v, 6) for v in footnote.rect]]},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:8]
    assert current != previous

    # det-v1 留下的空结果不能再被读回来
    MODULE.write_text(tmp_path / "lines" / f"p0014-{previous}.json", "[]")
    assert supplementer.lines_for(14, [footnote], [], lambda: blank_image()) == [footnote.rect]
