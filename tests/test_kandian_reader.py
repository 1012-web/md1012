from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from kandian_reader import kandian_reader
from content_state import text_hash


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8", errors="strict")


def test_line_order_headers_and_physical_page_offset(tmp_path):
    save(tmp_path / "p0003.json", {"message": "success", "data": {"text_lines": [
        {"text": "右列", "layout_classes_id": 0, "position": [[900, 0], [950, 900]]},
        {"text": "左列", "layout_classes_id": 0},
        {"text": "书眉不比", "layout_classes_id": 1},
    ]}})
    assert kandian_reader([f"{tmp_path}|21"]) == {23: "右列\n左列"}


def test_plain_and_structured_text_without_line_objects(tmp_path):
    save(tmp_path / "p0001.json", {"message": "success", "data": ["甲", "乙"]})
    save(tmp_path / "p0002.json", {"message": "success", "data": {"texts": ["丙"]}})
    assert kandian_reader([str(tmp_path)]) == {1: "甲\n乙", 2: "丙"}


def test_failed_or_stale_pages_are_not_compared(tmp_path):
    save(tmp_path / "p0001.json", {"message": "error", "info": "token无效"})
    save(tmp_path / "p0002.json", {"message": "success", "data": ["旧的成功输出"]})
    save(tmp_path / "_run.json", {"source": "kandianguji", "pages": {"1": {"ok": False}, "2": {"ok": False}}})
    assert kandian_reader([str(tmp_path)]) == {}


def test_explicit_sample_page_list_and_duplicate_rejection(tmp_path):
    save(tmp_path / "p0001.json", {"message": "success", "data": ["甲"]})
    save(tmp_path / "p0002.json", {"message": "success", "data": ["乙"]})
    assert kandian_reader([f"{tmp_path}|107,142"]) == {107: "甲", 142: "乙"}
    with pytest.raises(ValueError, match="Duplicate"):
        kandian_reader([f"{tmp_path}|107,107"])
    with pytest.raises(ValueError, match="must match"):
        kandian_reader([f"{tmp_path}|107,142,145"])


def test_other_provider_directory_is_rejected(tmp_path):
    save(tmp_path / "p0001.json", {"message": "success", "data": ["甲"]})
    save(tmp_path / "_run.json", {"source": "gjcool-chat"})
    with pytest.raises(ValueError, match="different OCR source"):
        kandian_reader([str(tmp_path)])


def test_cli_produces_compatible_doubt_offsets_and_missing_page_report(tmp_path):
    body = "银行事业的发展。\n第二页。"
    path = tmp_path / "正文.md"
    path.write_text(body, encoding="utf-8", errors="strict")
    boundary = body.index("第二")
    line_map = tmp_path / "line-map.json"
    save(line_map, {"writingMode": "vertical-rl", "files": {path.name: {"hash": text_hash(body), "lines": [
        {"from": 0, "to": boundary, "page": 21, "rect": [0.8, 0.1, 0.9, 0.8]},
        {"from": boundary, "to": len(body), "page": 22, "rect": [0.7, 0.1, 0.8, 0.8]},
    ]}}})
    reader = tmp_path / "看典"
    reader.mkdir()
    save(reader / "p0001.json", {"message": "success", "data": ["银行业的发展。﹂"]})
    out = tmp_path / "比对"
    result = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPTS / "vl_compare.py"),
                             "--book", str(tmp_path), "--line-map", str(line_map),
                             "--kandian", f"{reader}|21", "--out", str(out)],
                            capture_output=True, encoding="utf-8", errors="strict")
    assert result.returncode == 2, result.stderr  # second requested mapped page has no reader
    doubts = json.loads((out / "doubts.json").read_text(encoding="utf-8", errors="strict"))
    assert len(doubts) == 1
    doubt = doubts[0]
    assert doubt["minerU"] == "事" and doubt["ocr"] == ""
    assert body[doubt["from"]:doubt["to"]] == "事"
    assert doubt["pdfPage"] == 21 and doubt["writingMode"] == "vertical-rl"
    coverage = json.loads((out / "_reader_coverage.json").read_text(encoding="utf-8", errors="strict"))
    assert coverage["reader"] == "Kandian" and coverage["compared_pages"] == 1
    assert coverage["missing_physical_pages"] == [22] and not coverage["complete"]
    report = (out / "_vl_compare.md").read_text(encoding="utf-8", errors="strict")
    assert "」×1" in report
    assert path.read_text(encoding="utf-8", errors="strict") == body
