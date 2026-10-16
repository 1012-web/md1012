from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import line_map  # noqa: E402

pymupdf = pytest.importorskip("pymupdf")
FONT = "china-s"


def horizontal_book(tmp_path: Path):
    """第一页是目录；第二页三行正文（MinerU 把三行记成一条 line，VLM 后端常这样）加一条页下注。"""
    doc = pymupdf.open()
    toc = doc.new_page(width=400, height=600)
    toc.insert_text((40, 100), "第一章甲乙一", fontname=FONT, fontsize=20)
    page = doc.new_page(width=400, height=600)
    rows = ["甲乙丙丁戊己庚辛壬癸子丑", "寅卯辰巳午未申酉戌亥天地", "玄黄宇宙洪荒日月"]
    for i, row in enumerate(rows):
        page.insert_text((40, 100 + i * 30), row, fontname=FONT, fontsize=20)
    page.insert_text((40, 500), "①注文在此处說明", fontname=FONT, fontsize=12)
    pdf = tmp_path / "book.pdf"
    doc.save(pdf)
    layout = {"pdf_info": [
        {"page_size": [400, 600], "para_blocks": [
            {"type": "text", "bbox": [38, 78, 200, 108], "lines": [
                {"bbox": [38, 78, 200, 108], "spans": [{"type": "text", "content": "第一章甲乙一"}]}]}],
         "discarded_blocks": []},
        {"page_size": [400, 600], "para_blocks": [
            {"type": "text", "bbox": [38, 78, 300, 168], "lines": [
                {"bbox": [38, 78, 300, 168], "spans": [{"type": "text", "content": "".join(rows)}]}]}],
         "discarded_blocks": [
            {"type": "page_footnote", "bbox": [38, 486, 200, 504], "lines": [
                {"bbox": [38, 486, 200, 504], "spans": [{"type": "text", "content": "①注文在此处說明"}]}]},
            {"type": "page_number", "bbox": [190, 560, 210, 575], "lines": []}]},
    ]}
    (tmp_path / "layout.json").write_text(json.dumps(layout, ensure_ascii=False), encoding="utf-8")
    body = tmp_path / "02-正文.md"
    body.write_text("---\ntitle: 测试\n---\n\n<!--p.1-->\n\n甲乙丙丁戊己庚辛壬癸子丑，寅卯辰巳午未申酉戌亥天地，"
                    "玄黄宇宙洪荒日月。[^1]\n\n---\n\n## 注释\n\n[^1]: <!--p.1--> 注文在此处说明\n", encoding="utf-8")
    toc_md = tmp_path / "01-目录.md"
    toc_md.write_text("# 目录\n\n第一章　甲乙 …… 一\n", encoding="utf-8")
    return pdf, [toc_md, body]


def test_mineru_paragraph_line_is_cut_into_rows_and_notes_find_their_definition(tmp_path: Path) -> None:
    pdf, files = horizontal_book(tmp_path)
    doc = pymupdf.open(pdf)
    blocks = line_map.mineru_blocks([(tmp_path / "layout.json", 1)], doc)
    out, report = line_map.build(blocks, files)
    assert report["unmapped"] == []
    body = files[1].read_text(encoding="utf-8")
    lines = [entry for entry in out["02-正文.md"]["lines"] if entry["kind"] == "line"]
    rows = [entry for entry in lines if entry["rect"][1] < 0.5]
    assert len(rows) == 3 and all(entry["method"] == "ink" and entry["estimated"] for entry in rows)
    assert {entry["page"] for entry in out["02-正文.md"]["lines"]} == {2}  # 「注释」二字退到逐页，也在第 2 页
    assert [round(entry["rect"][1], 2) for entry in rows] == sorted(round(entry["rect"][1], 2) for entry in rows)
    texts = [body[entry["from"]:entry["to"]] for entry in rows]  # 全是 BMP 字，UTF-16 偏移即下标
    assert texts[0].startswith("甲乙丙") and texts[1].startswith("寅卯") and texts[2].startswith("玄黄")
    assert texts[0].endswith("，")  # 行尾标点归上一行
    note = [entry for entry in lines if entry["rect"][1] > 0.7]
    assert len(note) == 1 and body[note[0]["from"]:note[0]["to"]].startswith("注文")
    toc = out["01-目录.md"]["lines"]
    assert len(toc) == 1 and toc[0]["kind"] == "block" and toc[0]["page"] == 1
    assert {item["reason"] for item in report["blockLevel"]} == {"toc-file"}


def test_moved_line_of_a_cross_page_paragraph_keeps_its_own_page(tmp_path: Path) -> None:
    doc = pymupdf.open()
    for text in ("甲乙丙丁戊己庚辛", "壬癸子丑寅卯辰巳"):
        doc.new_page(width=400, height=600).insert_text((40, 100), text, fontname=FONT, fontsize=20)
    pdf = tmp_path / "book.pdf"
    doc.save(pdf)
    layout = {"pdf_info": [
        {"page_size": [400, 600], "discarded_blocks": [], "para_blocks": [
            {"type": "text", "bbox": [38, 78, 220, 108], "lines": [
                {"bbox": [38, 78, 220, 108], "spans": [{"type": "text", "content": "甲乙丙丁戊己庚辛"}]},
                {"bbox": [38, 79, 221, 109], "spans": [{"type": "text", "content": "壬癸子丑寅卯辰巳"}]}]}]},
        {"page_size": [400, 600], "discarded_blocks": [], "para_blocks": [
            {"type": "text", "bbox": [38, 79, 221, 109], "lines_deleted": True}]},
    ]}
    (tmp_path / "layout.json").write_text(json.dumps(layout, ensure_ascii=False), encoding="utf-8")
    md = tmp_path / "正文.md"
    md.write_text("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳。\n", encoding="utf-8")
    out, _ = line_map.build(line_map.mineru_blocks([(tmp_path / "layout.json", 1)], pymupdf.open(pdf)), [md])
    assert [entry["page"] for entry in out["正文.md"]["lines"]] == [1, 2]


def test_vl_vertical_block_is_cut_into_columns_and_overlapping_blocks_fall_back(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    columns = ["天地玄黄宇宙洪荒", "日月盈昃辰宿列张"]
    for i, column in enumerate(columns):  # 右起第一列
        for j, ch in enumerate(column):
            page.insert_text((300 - i * 40, 100 + j * 30), ch, fontname=FONT, fontsize=24)
    pdf = tmp_path / "book.pdf"
    doc.save(pdf)
    vl = tmp_path / "VL"
    vl.mkdir()
    page_json = {"page": 1, "width": 800, "height": 1200, "parsing_res_list": [
        {"block_label": "vertical_text", "block_bbox": [500, 120, 680, 760], "block_content": "".join(columns)},
        {"block_label": "header", "block_bbox": [0, 0, 100, 50], "block_content": "書眉"},
    ]}
    (vl / "p0001.json").write_text(json.dumps(page_json, ensure_ascii=False), encoding="utf-8")
    md = tmp_path / "正文.md"
    md.write_text("天地玄黄，宇宙洪荒。日月盈昃，辰宿列张。\n", encoding="utf-8")
    blocks = line_map.vl_blocks(vl, [1], pymupdf.open(pdf), vertical_book=True)
    assert len(blocks) == 1 and blocks[0].vertical and len(blocks[0].lines) == 2
    out, report = line_map.build(blocks, [md])
    first, second = out["正文.md"]["lines"]
    assert first["rect"][0] > second["rect"][0]  # 右边那列先读
    text = md.read_text(encoding="utf-8")
    assert text[first["from"]:first["to"]].startswith("天地") and text[second["from"]:second["to"]].startswith("日月")

    page_json["parsing_res_list"].append(
        {"block_label": "vertical_text", "block_bbox": [520, 140, 690, 700], "block_content": "重叠的块"})
    (vl / "p0001.json").write_text(json.dumps(page_json, ensure_ascii=False), encoding="utf-8")
    blocks = line_map.vl_blocks(vl, [1], pymupdf.open(pdf), vertical_book=True)
    assert [b.block_level for b in blocks] == ["overlapping-blocks", "overlapping-blocks"]


def test_split_by_length_follows_line_lengths_and_keeps_punctuation_with_the_previous_char() -> None:
    assert line_map.split_by_length("甲乙，丙丁。", [2, 2]) == ["甲乙，", "丙丁。"]
    assert line_map.split_by_length("甲乙丙丁", [3, 1]) == ["甲乙丙", "丁"]


def test_scattered_matches_are_not_accepted_and_resync_needs_two_agreeing_probes() -> None:
    assert line_map.compact({0: 10, 1: 11, 2: 12}, 3)
    assert not line_map.compact({0: 10, 1: 500, 2: 900}, 3)
    stream = "无关" * 50 + "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏" + "无关" * 50
    text = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
    assert line_map.resync(text, stream, 0) == 100
    assert line_map.resync("短", stream, 0) is None


def test_text_no_block_reaches_falls_back_to_the_page_of_its_anchor(tmp_path: Path) -> None:
    md = tmp_path / "正文.md"
    md.write_text("<!--p.1-->\n\n天地玄黄宇宙洪荒日月盈昃\n\n<!--p.2-->\n\n寒来暑往秋收冬藏\n", encoding="utf-8")
    block = line_map.Block(5, [0.1, 0.1, 0.9, 0.2], "天地玄黄宇宙洪荒日月盈昃", False, False,
                           [line_map.Line([0.1, 0.1, 0.9, 0.2], "天地玄黄宇宙洪荒日月盈昃", "mineru-line")])
    out, report = line_map.build([block], [md])
    line, page = out["正文.md"]["lines"]
    assert line["kind"] == "line" and line["page"] == 5
    # 印刷页 1 在物理页 5，所以印刷页 2 的那段推到物理页 6
    assert page["kind"] == "page" and page["page"] == 6 and page["rect"] is None
    assert md.read_text(encoding="utf-8")[page["from"]:page["to"]] == "寒来暑往秋收冬藏"
    assert report["pageOnly"] == 1 and report["noPage"] == 0
