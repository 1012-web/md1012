import gzip
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("textlayer_spec", SCRIPTS / "textlayer_spec.py")
MODULE = importlib.util.module_from_spec(spec)
sys.modules["textlayer_spec"] = MODULE
spec.loader.exec_module(MODULE)


def make_book(root: Path, markdown: str, blocks: list[dict], writing_mode: str = "vertical-rl") -> Path:
    (root / "_数据" / "映射" / "chapters").mkdir(parents=True)
    (root / "01-正文.md").write_text(markdown, encoding="utf-8")
    with gzip.open(root / "_数据" / "映射" / "chapters" / "a.json.gz", "wt", encoding="utf-8") as handle:
        json.dump({"version": 1, "blocks": blocks}, handle, ensure_ascii=False)
    index = {"version": 1, "chapters": {"01-正文.md": {
        "hash": hashlib.sha256(markdown.encode("utf-8")).hexdigest(), "map": "_数据/映射/chapters/a.json.gz"}}}
    (root / "_数据" / "映射" / "index.json").write_text(json.dumps(index), encoding="utf-8")
    book = {"id": "b", "title": "书", "pdf": "书.pdf", "index": "_数据/映射/index.json", "writingMode": writing_mode}
    (root / "_数据" / "book.json").write_text(json.dumps(book), encoding="utf-8")
    return root


def test_export_takes_read_candidate_estimated_and_neighbour_positions(tmp_path: Path) -> None:
    # 正文「甲，乙丙」：甲读出、，推定、乙是两家读法不同（写正文的字）、丙无坐标（挨着乙往下排一格）
    block = {
        "kind": "paragraph",
        "chars": [{"from": 0, "to": 1, "page": 3, "rect": [0.5, 0.1, 0.55, 0.13]}],
        "candidates": [{"from": 2, "to": 3, "value": "已", "page": 3, "rect": [0.5, 0.145, 0.55, 0.175]}],
        "estimated": [{"from": 1, "to": 2, "page": 3, "rect": [0.5, 0.13, 0.55, 0.145]}],
        "missing": [[1, 2], [2, 3], [3, 4]],
    }
    book = make_book(tmp_path, "甲，乙丙", [block])

    result, report = MODULE.export(book)

    assert result["writingMode"] == "vertical-rl"
    assert [item[0] for item in result["pages"]["3"]] == ["甲", "，", "乙", "丙"]
    assert result["pages"]["3"][3][1:] == [0.5, 0.175, 0.55, 0.205]  # 竖排：沿列往下一格
    assert (report["mapped"], report["estimated"], report["candidate"], report["neighbour"]) == (1, 1, 1, 1)


def test_export_neighbour_step_is_horizontal_for_horizontal_books(tmp_path: Path) -> None:
    block = {"kind": "paragraph", "chars": [{"from": 0, "to": 1, "page": 1, "rect": [0.1, 0.2, 0.13, 0.23]}],
             "missing": [[1, 2]]}
    book = make_book(tmp_path, "甲乙", [block], "horizontal-tb")

    result, _ = MODULE.export(book)

    assert result["pages"]["1"][1][1:] == [0.13, 0.2, 0.16, 0.23]


def test_export_refuses_a_stale_mapping(tmp_path: Path) -> None:
    book = make_book(tmp_path, "甲乙", [{"kind": "paragraph", "chars": [], "missing": []}])
    (book / "01-正文.md").write_text("甲乙丙", encoding="utf-8")

    _, report = MODULE.export(book)

    assert report["staleChapters"] == ["01-正文.md"]
