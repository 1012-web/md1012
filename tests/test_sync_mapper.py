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


def test_markdown_mask_drops_the_obsidian_block_identifier() -> None:
    block = "## 注释 ^tb-c01-h0053"
    mask = MODULE.markdown_mask(block)

    assert "".join(char for char, keep in zip(block, mask) if keep) == "注释"
    assert MODULE.normalized_title(block) == "注释"


def test_markdown_mask_keeps_a_caret_that_is_not_a_block_identifier() -> None:
    block = "热力学里写作 2^n 的地方"

    assert all(MODULE.markdown_mask(block)[block.index("2") : block.index("n") + 1])


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


def test_chars_from_word_info_scales_by_the_columns_it_was_given() -> None:
    word_info = [20, [["甲", "乙"], ["A", "B"]], [[2, 6], [12, 16]], ["cn", "en&num"]]

    chars = MODULE.chars_from_word_info("甲乙AB", word_info, (0.0, 0.2, 1.0, 0.3))

    assert [char.value for char in chars] == ["甲", "乙", "A", "B"]
    # 首列到末列各外扩半步，铺满整条行框：与 word_info[0] 无关
    assert chars[0].rect == (0.0, 0.2, 0.222222, 0.3)
    assert chars[-1].rect == (0.777778, 0.2, 1.0, 0.3)


def test_chars_from_word_info_ignores_a_mismatched_valid_column_count() -> None:
    """PaddleX 回填的有效列数是别行的，长行曾因此只剩开头几个字。"""
    columns = list(range(2, 132, 5))
    text = "字" * len(columns)
    word_info = [24.867956265769557, [list(text)], [columns], ["cn"]]

    chars = MODULE.chars_from_word_info(text, word_info, (0.1, 0.2, 0.9, 0.23))

    assert len(chars) == len(columns)
    assert chars[0].rect[0] == pytest.approx(0.1)
    assert chars[-1].rect[2] == pytest.approx(0.9)
    widths = [char.rect[2] - char.rect[0] for char in chars]
    assert max(widths) - min(widths) < 1e-5  # 字框按 6 位小数取整，等宽只到这个量级


def test_chars_from_word_info_falls_back_to_even_spacing_for_one_character() -> None:
    chars = MODULE.chars_from_word_info("甲", [7, [["甲"]], [[3]], ["cn"]], (0.1, 0.2, 0.5, 0.3))

    assert [char.rect for char in chars] == [(0.1, 0.2, 0.5, 0.3)]


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


# --- 悬挂式小标题：标题占左半幅，正文从同一行右半幅接排 ------------------------


def ocr_line(text, top, bottom, left, right):
    """一条识别行：整行的字框共享同一条厚度跨度，沿走向轴均分。"""
    step = (right - left) / len(text)
    return [
        MODULE.OCRChar(value, (left + index * step, top, left + (index + 1) * step, bottom))
        for index, value in enumerate(text)
    ]


def test_reading_order_puts_a_hanging_heading_before_the_text_beside_it() -> None:
    body_first = ocr_line("在工业革命发展", 0.294, 0.316, 0.50, 0.85)
    heading = ocr_line("工人阶级自发的阶级意识", 0.303, 0.335, 0.16, 0.47)
    body_second = ocr_line("过程中由于大工业", 0.322, 0.344, 0.50, 0.85)

    ordered = MODULE.reading_order_chars(body_first + heading + body_second)

    assert "".join(char.value for char in ordered) == (
        "工人阶级自发的阶级意识在工业革命发展过程中由于大工业"
    )


def test_reading_order_bands_a_heading_whose_overlap_is_only_a_third_of_a_line() -> None:
    """下卷第 95 页的真实行框：标题与正文行只重叠正文行高的四成，仍必须并成一条横带。"""
    body_first = ocr_line("19世纪中叶是英国近代历史上的重要时", 0.3001, 0.3231, 0.423, 0.889)
    heading = ocr_line("工业革命的完成", 0.3120, 0.3376, 0.187, 0.394)
    body_second = ocr_line("期。这时，英国已完成了工业革命，正处", 0.3265, 0.3504, 0.423, 0.889)

    ordered = MODULE.reading_order_chars(body_first + heading + body_second)

    assert "".join(char.value for char in ordered).startswith("工业革命的完成19世纪")


def test_reading_order_leaves_an_ordinary_page_alone() -> None:
    chars = (
        ocr_line("第一行文字", 0.10, 0.13, 0.11, 0.85)
        + ocr_line("第二行文字", 0.14, 0.17, 0.11, 0.85)
        + ocr_line("第三行文字", 0.18, 0.21, 0.11, 0.85)
    )

    assert MODULE.reading_order_chars(chars) == chars


def test_reading_order_keeps_one_column_even_when_lines_overlap() -> None:
    """行框上下略微交叠但左右同栏时不许拆栏，否则整页会被重排。"""
    chars = ocr_line("上行文字", 0.10, 0.14, 0.11, 0.85) + ocr_line("下行文字", 0.13, 0.17, 0.11, 0.85)

    assert MODULE.reading_order_chars(chars) == chars


def test_reading_order_reads_vertical_columns_right_to_left_band_by_band() -> None:
    right = [MODULE.OCRChar(v, (0.80, 0.1 + i * 0.05, 0.86, 0.15 + i * 0.05)) for i, v in enumerate("右栏")]
    left = [MODULE.OCRChar(v, (0.70, 0.1 + i * 0.05, 0.76, 0.15 + i * 0.05)) for i, v in enumerate("左栏")]

    ordered = MODULE.reading_order_chars(right + left, "vertical-rl")

    assert "".join(char.value for char in ordered) == "右栏左栏"


def test_align_page_maps_a_hanging_heading_that_shares_lines_with_the_body() -> None:
    heading_text = "工人阶级自发的阶级意识"
    body_text = "在工业革命发展过程中由于大工业"
    markdown = [
        MODULE.VisibleChar(value, index, index + 1, 26, 32)
        for index, value in enumerate(heading_text + body_text)
    ]
    ocr = (
        ocr_line(body_text[:7], 0.294, 0.316, 0.50, 0.85)
        + ocr_line(heading_text, 0.303, 0.335, 0.16, 0.47)
        + ocr_line(body_text[7:], 0.322, 0.344, 0.50, 0.85)
    )

    mapped = MODULE.align_page(markdown, ocr)

    assert mapped == len(markdown)
    assert markdown[0].rect[0] == pytest.approx(0.16)
    assert markdown[len(heading_text)].rect[0] == pytest.approx(0.50)


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


# --- 目录页的引导点 -----------------------------------------------------------


def dot_leader_entry(title, dots, number, left, right, top=0.2, bottom=0.23):
    """一条目录行的识别结果：标题、一长串引导点、页码。"""
    return ocr_line(title + "." * dots + number, top, bottom, left, right)


def test_dot_leaders_collapse_to_one_unit_on_both_sides() -> None:
    units, owners = MODULE.normalized_chars(
        [MODULE.OCRChar(value, (0.1, 0.2, 0.2, 0.3)) for value in "甲……乙"]
    )

    # Markdown 的「……」经 NFKC 展开成六个句点，折叠后只剩一个
    assert units == "甲.乙"
    # 折叠出来的那一个单元把两个「…」都挂着，对上了两个都能拿到坐标
    assert [len(group) for group in owners] == [1, 2, 1]


def test_a_single_middle_dot_is_never_collapsed() -> None:
    units, owners = MODULE.normalized_chars(
        [MODULE.OCRChar(value, (0.1, 0.2, 0.2, 0.3)) for value in "发展·宪章"]
    )

    assert units == "发展·宪章"
    assert all(len(group) == 1 for group in owners)


def test_dot_leader_alignment_survives_a_page_number_after_the_dots() -> None:
    markdown = [
        MODULE.VisibleChar(value, index, index + 1, 3, 5)
        for index, value in enumerate("第一节英国工业革命……23")
    ]
    # 纸面上引导点有三十个，Markdown 只写两个「……」
    ocr = dot_leader_entry("第一节英国工业革命", 30, "23", 0.15, 0.85)

    mapped = MODULE.align_page(markdown, ocr)

    assert mapped == len(markdown)
    assert [char.value for char in markdown if char.rect is None] == []
    # 页码「23」拿到的是页码本身的框，没有被前面的点挤偏
    assert markdown[-1].rect[0] > markdown[-3].rect[0]


def test_collapsed_dot_run_gets_the_whole_leader_rectangle() -> None:
    markdown = [MODULE.VisibleChar(value, index, index + 1, 3, 5)
                for index, value in enumerate("甲……9")]
    ocr = dot_leader_entry("甲", 10, "9", 0.10, 0.90)

    MODULE.align_page(markdown, ocr)

    leader = [char.rect for char in markdown if char.value == "…"]
    assert len(leader) == 2 and leader[0] == leader[1]
    # 整串点的外接框，不是头一个点的小框
    assert leader[0][2] - leader[0][0] == pytest.approx(0.8 * 10 / 12, abs=1e-6)


# --- ONNX Runtime 识别后端 -----------------------------------------------------


class FakeOnnxRuntime:
    """够用的 onnxruntime 替身：只回答「有哪些 provider」。"""

    def __init__(self, providers):
        self._providers = providers

    def get_available_providers(self):
        return list(self._providers)


def test_both_backends_place_a_character_identically_for_the_same_columns() -> None:
    values = list("甲乙丙")
    columns = [2.0, 5.0, 8.0]
    line = (0.1, 0.3, 0.7, 0.34)
    # PaddleOCR 的 word_info 拆成词组，ONNX 直接给平铺的列号，两条路必须落到同一批框上
    word_info = [40, [values], [columns], ["cn"]]

    from_paddle = MODULE.chars_from_word_info("甲乙丙", word_info, line)
    from_onnx = MODULE.chars_from_columns(values, columns, line)

    assert [char.rect for char in from_paddle] == [char.rect for char in from_onnx]
    assert [char.value for char in from_onnx] == values


def test_ctc_decoding_reports_the_column_each_character_was_decoded_at() -> None:
    numpy = pytest.importorskip("numpy")
    charset = ["blank", "甲", "乙"]
    # 时间轴: blank, 甲, 甲(重复), blank, 乙
    probabilities = numpy.array([
        [0.9, 0.05, 0.05],
        [0.1, 0.8, 0.1],
        [0.1, 0.8, 0.1],
        [0.9, 0.05, 0.05],
        [0.1, 0.1, 0.8],
    ])

    values, columns, scores = MODULE.onnx_ctc_decode(probabilities, charset)

    assert values == ["甲", "乙"]
    assert columns == [1.0, 4.0]
    assert scores == pytest.approx([0.8, 0.8])


def test_device_choice_prefers_the_adapter_with_the_most_dedicated_memory(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "dxgi_adapters", lambda: [
        (0, "Intel(R) Graphics", 128 * 1024 ** 2),
        (1, "AMD Radeon RX 9070 GRE", 12 * 1024 ** 3),
    ])
    runtime = FakeOnnxRuntime(["DmlExecutionProvider", "CPUExecutionProvider"])

    assert MODULE.resolve_onnx_device(runtime, "auto", None) == ("dml", 1, "AMD Radeon RX 9070 GRE")
    # 明写 device id 就照办，哪怕那是核显
    assert MODULE.resolve_onnx_device(runtime, "dml", 0) == ("dml", 0, "Intel(R) Graphics")
    assert MODULE.resolve_onnx_device(runtime, "cpu", None) == ("cpu", 0, "cpu")


def test_auto_falls_back_to_cpu_without_a_discrete_adapter(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "dxgi_adapters", lambda: [(0, "Intel(R) Graphics", 128 * 1024 ** 2)])
    runtime = FakeOnnxRuntime(["DmlExecutionProvider", "CPUExecutionProvider"])

    provider, _, name = MODULE.resolve_onnx_device(runtime, "auto", None)

    assert provider == "cpu" and "no discrete adapter" in name


def test_auto_falls_back_to_cpu_without_directml() -> None:
    runtime = FakeOnnxRuntime(["CPUExecutionProvider"])

    assert MODULE.resolve_onnx_device(runtime, "auto", None)[0] == "cpu"
    with pytest.raises(RuntimeError, match="DmlExecutionProvider is unavailable"):
        MODULE.resolve_onnx_device(runtime, "dml", None)


def test_a_missing_onnx_model_names_the_file_it_looked_for(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="inference.onnx"):
        MODULE.onnx_model_directory(str(tmp_path))


def test_onnx_charset_puts_blank_first_and_space_last(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    (tmp_path / "inference.yml").write_text(
        "PostProcess:\n  character_dict:\n    - 甲\n    - 乙\n", encoding="utf-8"
    )

    assert MODULE.onnx_charset(tmp_path) == ["blank", "甲", "乙", " "]


def test_onnx_shape_comes_from_the_model_yml(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    (tmp_path / "inference.yml").write_text(
        "PreProcess:\n  transform_ops:\n"
        "    - DecodeImage:\n        img_mode: BGR\n"
        "    - RecResizeImg:\n        image_shape:\n          - 3\n          - 64\n          - 512\n",
        encoding="utf-8",
    )

    assert MODULE.onnx_recognition_shape(tmp_path) == (3, 64, 512)


def test_onnx_shape_falls_back_when_the_yml_says_nothing(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    (tmp_path / "inference.yml").write_text("PostProcess:\n  name: CTCLabelDecode\n", encoding="utf-8")

    assert MODULE.onnx_recognition_shape(tmp_path) == MODULE.ONNX_RECOGNITION_SHAPE


def test_onnx_input_shape_check_catches_a_mismatched_yml() -> None:
    directory = Path("C:/x/PP-OCRv6_medium_rec_onnx")
    # 批量与宽是动态维，只有通道与高能比；高对不上就是 yml 和 onnx 配错了对
    MODULE.verify_onnx_input_shape((3, 48, 320), ["N", 3, 48, "W"], directory)
    with pytest.raises(RuntimeError, match="do not match"):
        MODULE.verify_onnx_input_shape((3, 64, 512), ["N", 3, 48, "W"], directory)
    # 图里一个静态维都没有就没法比，不报错
    MODULE.verify_onnx_input_shape((3, 64, 512), ["N", "C", "H", "W"], directory)


def test_onnx_resize_honours_a_taller_model() -> None:
    numpy = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    # 同一张图换成高 64 的网络：目标宽按 64 * 10 = 640 算，而不是 48 那套
    prepared = MODULE.onnx_resize_normalize(numpy.zeros((24, 240, 3), dtype=numpy.uint8), (3, 64, 512))

    assert prepared.shape == (3, 64, 640)


def test_onnx_resize_keeps_the_paddle_geometry() -> None:
    numpy = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    # 高 24、宽 240 的行图：宽高比 10 大于基准 320/48，目标宽按自身比例算成 480
    prepared = MODULE.onnx_resize_normalize(numpy.zeros((24, 240, 3), dtype=numpy.uint8))

    assert prepared.shape == (3, 48, 480)
    # 归一化后全零输入变成 -1
    assert prepared.min() == pytest.approx(-1.0)


def test_line_crops_pad_the_box_and_report_what_was_cut(monkeypatch) -> None:
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((200, 400, 3), dtype=numpy.uint8)

    crops, rects = MODULE.line_crops(image, [(0.25, 0.5, 0.75, 0.6)], "horizontal-tb")

    # 左右各让出 4 像素：100-4 .. 300+4，纵向 100-4 .. 120+4
    assert crops[0].shape[:2] == (28, 208)
    assert rects[0] == (96 / 400, 96 / 200, 304 / 400, 124 / 200)


def test_onnx_cache_scope_separates_model_provider_and_batch() -> None:
    engine = MODULE.OnnxCharacterOCR.__new__(MODULE.OnnxCharacterOCR)
    engine.model_directory = Path("C:/x/PP-OCRv5_server_rec_onnx")
    engine.provider = "dml"
    engine.batch_size = 8
    engine.device_name = "AMD Radeon RX 9070 GRE"

    assert engine.cache_scope == "onnx-PP-OCRv5_server_rec_onnx-dml-b8"
    # 收尾行要能看出活派给了哪块卡
    assert engine.describe() == "dml:AMD Radeon RX 9070 GRE"
    assert MODULE.OnnxCharacterOCR.backend == "onnx"


# ---------- 逐字识别置信度 ----------


def test_ctc_score_takes_the_highest_column_a_character_spans() -> None:
    numpy = pytest.importorskip("numpy")
    charset = ["blank", "甲"]
    probabilities = numpy.array([[0.3, 0.7], [0.1, 0.9], [0.9, 0.1]])

    _, _, scores = MODULE.onnx_ctc_decode(probabilities, charset)

    assert scores == pytest.approx([0.9])


def test_chars_from_columns_carries_scores_and_drops_spaces_with_theirs() -> None:
    chars = MODULE.chars_from_columns(
        ["甲", " ", "乙"], [1.0, 3.0, 5.0], (0.0, 0.1, 1.0, 0.2), "horizontal-tb", [0.5, 0.1, 0.99]
    )

    assert [char.value for char in chars] == ["甲", "乙"]
    assert [char.score for char in chars] == [0.5, 0.99]


def test_chars_from_columns_without_scores_leaves_them_empty() -> None:
    chars = MODULE.chars_from_columns(["甲", "乙"], [1.0, 5.0], (0.0, 0.1, 1.0, 0.2))

    assert [char.score for char in chars] == [None, None]


def test_align_page_hands_ocr_scores_to_mapped_and_candidate_characters() -> None:
    markdown = [MODULE.VisibleChar("甲", 0, 1, 1, 41), MODULE.VisibleChar("乙", 1, 2, 1, 41)]
    ocr = [
        MODULE.OCRChar("甲", (0.1, 0.2, 0.3, 0.4), 0.62),
        MODULE.OCRChar("丙", (0.3, 0.2, 0.5, 0.4), 0.97),
    ]

    MODULE.align_page(markdown, ocr)

    assert markdown[0].score == 0.62
    assert markdown[1].score is None
    assert markdown[1].candidate_score == 0.97


def test_ocr_cache_round_trips_scores_and_reads_old_two_item_entries(tmp_path: Path) -> None:
    class Engine:
        backend = "onnx"
        cache_scope = "onnx-test"

        def recognize(self, image, rects):
            return [MODULE.OCRChar("甲", (0.1, 0.1, 0.2, 0.2), 0.73456), MODULE.OCRChar("乙", (0.2, 0.1, 0.3, 0.2))]

    rects = [(0.1, 0.1, 0.9, 0.2)]
    first = MODULE.load_or_run_ocr(Engine(), None, 1, rects, tmp_path, 2.0, image_loader=lambda: None)
    again = MODULE.load_or_run_ocr(Engine(), None, 1, rects, tmp_path, 2.0, image_loader=lambda: None)

    assert [char.score for char in first] == [0.73456, None]
    assert [char.score for char in again] == [0.7346, None]
    legacy = tmp_path / "legacy.json"
    legacy.write_text('[["甲", [0.1, 0.1, 0.2, 0.2]]]', "utf-8")
    assert MODULE.read_ocr_cache(legacy)[0].score is None


def test_write_outputs_keeps_only_low_scores_and_marks_the_ceiling(tmp_path: Path) -> None:
    import gzip

    confident = MODULE.VisibleChar("甲", 0, 1, 1, 1, rect=(0.1, 0.1, 0.2, 0.2), score=0.995)
    doubtful = MODULE.VisibleChar("乙", 1, 2, 1, 1, rect=(0.2, 0.1, 0.3, 0.2), score=0.41)
    disputed = MODULE.VisibleChar(
        "丙", 2, 3, 1, 1, candidate_value="两", candidate_rect=(0.3, 0.1, 0.4, 0.2), candidate_score=0.93
    )
    block = MODULE.MarkdownBlock("a.md", 0, 3, "paragraph", [confident, doubtful, disputed], block_id="b1")
    output = tmp_path / "sync" / "out"

    MODULE.write_outputs(tmp_path, output, "book", "书", "a.pdf", "", {"a.md": "甲乙丙"}, [block], 1, 0)

    index = json.loads((output / "index.json").read_text("utf-8"))
    entry = index["chapters"]["a.md"]
    assert entry["scoredCharacters"] == 2
    with gzip.open(tmp_path / entry["map"], "rt", encoding="utf-8") as handle:
        chapter = json.load(handle)
    assert chapter["scoreCeiling"] == MODULE.SCORE_CEILING
    chars = chapter["blocks"][0]["chars"]
    assert "score" not in chars[0]
    assert chars[1]["score"] == 0.41
    assert chapter["blocks"][0]["candidates"][0]["score"] == 0.93


def test_write_outputs_without_any_scores_writes_no_ceiling(tmp_path: Path) -> None:
    import gzip

    plain = MODULE.VisibleChar("甲", 0, 1, 1, 1, rect=(0.1, 0.1, 0.2, 0.2))
    block = MODULE.MarkdownBlock("a.md", 0, 1, "paragraph", [plain], block_id="b1")
    output = tmp_path / "sync" / "out"

    MODULE.write_outputs(tmp_path, output, "book", "书", "a.pdf", "", {"a.md": "甲"}, [block], 1, 0)

    index = json.loads((output / "index.json").read_text("utf-8"))
    with gzip.open(tmp_path / index["chapters"]["a.md"]["map"], "rt", encoding="utf-8") as handle:
        assert "scoreCeiling" not in json.load(handle)


def test_backflow_markers_are_not_visible_text() -> None:
    mask = MODULE.markdown_mask("甲%%ha-bf:abcdef12%%乙")

    assert "".join(char for char, keep in zip("甲%%ha-bf:abcdef12%%乙", mask) if keep) == "甲乙"
