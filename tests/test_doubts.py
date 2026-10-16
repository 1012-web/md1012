from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import vl_compare  # noqa: E402


def book(tmp_path: Path, vl_text: str) -> tuple[list[Path], dict, Path]:
    md = tmp_path / "正文.md"
    md.write_text("<!--p.12-->\n\n银行事业的发展。第纳尔[^1]与第尔汗。\n\n[^1]: 金币\n", encoding="utf-8")
    text = md.read_text(encoding="utf-8")
    line_map = {"writingMode": "horizontal-tb", "files": {"正文.md": {"lines": [
        {"from": text.index("银"), "to": text.index("[^1]:"), "page": 22, "rect": [0.1, 0.2, 0.9, 0.23],
         "kind": "line", "method": "ink", "estimated": True},
        {"from": text.index("金币"), "to": len(text), "page": 22, "rect": [0.1, 0.9, 0.5, 0.92],
         "kind": "line", "method": "ink", "estimated": True},
    ]}}}
    vl = tmp_path / "VL"
    vl.mkdir(exist_ok=True)
    (vl / "p0022.json").write_text(json.dumps({"page": 22, "width": 100, "height": 100, "parsing_res_list": [
        {"block_label": "text", "block_bbox": [0, 0, 1, 1], "block_content": vl_text},
        {"block_label": "header", "block_bbox": [0, 0, 1, 1], "block_content": "书眉不比"},
    ]}, ensure_ascii=False), encoding="utf-8")
    return [md], line_map, vl


def test_small_disagreements_become_doubts_with_offsets_and_page(tmp_path: Path) -> None:
    files, line_map, vl = book(tmp_path, "银行业的发展。第纳尔①与第尔汗。①金币")
    doubts, large, compared = vl_compare.compare(files, line_map, vl, {})
    assert compared == 1 and large == []
    (doubt,) = doubts
    text = files[0].read_text(encoding="utf-8")
    assert doubt["minerU"] == "事" and doubt["ocr"] == "" and text[doubt["from"]:doubt["to"]] == "事"
    assert doubt["page"] == "p.12" and doubt["pdfPage"] == 22 and doubt["rect"] == [0.1, 0.2, 0.9, 0.23]
    assert doubt["context"].startswith("银行**事**业")  # 页码锚点不进前后文


def test_missing_char_is_an_insertion_and_markup_spans_go_to_large(tmp_path: Path) -> None:
    files, line_map, vl = book(tmp_path, "银行事业的发展。第纳尔与第尔汗。金币币")
    doubts, large, _ = vl_compare.compare(files, line_map, vl, {})
    (doubt,) = doubts
    assert doubt["minerU"] == "" and doubt["ocr"] == "币" and doubt["from"] == doubt["to"]
    # 跨过注号的分歧不进疑字：照偏移替换会把 [^1] 一起删掉
    files, line_map, vl = book(tmp_path, "银行事业的发展。第纳乙丙第尔汗。金币")
    doubts, large, _ = vl_compare.compare(files, line_map, vl, {})
    assert doubts == [] and large == [{"pdfPage": 22, "minerU": "尔与", "ocr": "乙丙"}]


def test_doubt_sheets_crop_whole_lines_and_write_manifest(tmp_path: Path) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    pytest.importorskip("PIL")
    import doubt_sheets

    doc = pymupdf.open()
    for _ in range(22):
        doc.new_page(width=400, height=600)
    pdf = tmp_path / "book.pdf"
    doc.save(pdf)
    files, line_map, vl = book(tmp_path, "银行业的发展。第纳尔与第尔汗。金币")
    doubts, _, _ = vl_compare.compare(files, line_map, vl, {})
    doubts.append({**doubts[0], "n": 2, "rect": None})  # 逐页退路：截整页
    path = tmp_path / "doubts.json"
    path.write_text(json.dumps(doubts, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "判定"
    doubt_sheets.crops(pdf, path, out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert [m["line"] for m in manifest] == ["0001-line.png", "0002-line.png"]
    assert (out / "sheet-1.png").exists()
    (tmp_path / "groups.json").write_text(json.dumps({"疑漏字": [1, 2]}, ensure_ascii=False), encoding="utf-8")
    doubt_sheets.owner(out, tmp_path / "groups.json")
    assert (out / "用户拍板" / "组1-1.png").exists()
