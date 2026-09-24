import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("pdf_textlayer", SCRIPTS / "pdf_textlayer.py")
MODULE = importlib.util.module_from_spec(spec)
sys.modules["pdf_textlayer"] = MODULE
spec.loader.exec_module(MODULE)


def scanned_page(doc, old_text: str | None, visible_text: str | None = None) -> None:
    """一页「扫描图」：整页一张图，上面可选叠一层隐形旧文字，或一段看得见的字。"""
    page = doc.new_page(width=200, height=100)
    if visible_text is None:
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100), False)
        pixmap.set_rect(pixmap.irect, (240, 240, 230))
        pixmap.set_rect(pymupdf.IRect(20, 30, 120, 60), (20, 20, 20))
        page.insert_image(page.rect, pixmap=pixmap)
    if old_text:
        page.insert_text((20, 50), old_text, fontname="china-s", fontsize=12, render_mode=3)
    if visible_text:
        page.insert_text((20, 50), visible_text, fontname="china-s", fontsize=16, render_mode=0)


def make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    scanned_page(doc, "旧的错字")
    scanned_page(doc, "封面文字")
    scanned_page(doc, None, visible_text="看得见")
    doc.save(path)


SPEC = {
    "version": 1,
    "bookId": "book",
    "pdf": "书.pdf",
    "pages": {
        "1": [["新", 0.1, 0.3, 0.2, 0.45], ["的", 0.2, 0.3, 0.3, 0.45], ["正", 0.3, 0.3, 0.4, 0.45], ["字", 0.4, 0.3, 0.5, 0.45]],
        "3": [["换", 0.1, 0.3, 0.2, 0.45]],
    },
}


def text_of(path: Path, number: int) -> str:
    with pymupdf.open(path) as doc:
        return "".join(doc[number - 1].get_text().split())


def test_build_replaces_hidden_text_keeps_image_and_leaves_unlisted_pages(tmp_path: Path) -> None:
    source = tmp_path / "书.pdf"
    make_pdf(source)
    before = source.read_bytes()
    output = tmp_path / "校订版.pdf"

    report = MODULE.build(source, SPEC, output)

    assert source.read_bytes() == before
    assert report["sourceUnchanged"] is True
    assert text_of(output, 1) == "新的正字"
    assert text_of(output, 2) == "封面文字"
    assert report["pagesWithText"] == 1
    assert report["lowestMatch"] == 1.0
    with pymupdf.open(source) as a, pymupdf.open(output) as b:
        assert a[0].get_pixmap(dpi=72).samples == b[0].get_pixmap(dpi=72).samples


def test_page_whose_text_is_visible_is_kept(tmp_path: Path) -> None:
    source = tmp_path / "书.pdf"
    make_pdf(source)
    output = tmp_path / "校订版.pdf"

    report = MODULE.build(source, SPEC, output)

    assert report["pagesKeptOldLayer"] == [3]
    assert text_of(output, 3) == "看得见"


def test_new_text_sits_on_its_box(tmp_path: Path) -> None:
    source = tmp_path / "书.pdf"
    make_pdf(source)
    output = tmp_path / "校订版.pdf"
    MODULE.build(source, SPEC, output)

    with pymupdf.open(output) as doc:
        chars = [char for block in doc[0].get_text("rawdict")["blocks"] for line in block.get("lines", [])
                 for span in line["spans"] for char in span["chars"]]
    first = pymupdf.Rect(chars[0]["bbox"])
    assert chars[0]["c"] == "新"
    assert abs(first.x0 - 20) < 1.5
    assert 28 <= (first.y0 + first.y1) / 2 <= 47


def test_replace_backs_up_and_refuses_a_changed_target(tmp_path: Path) -> None:
    target = tmp_path / "原件.pdf"
    target.write_bytes(b"%PDF-original")
    corrected = tmp_path / "校订版.pdf"
    corrected.write_bytes(b"%PDF-corrected")
    expected = hashlib.sha256(b"%PDF-original").hexdigest()

    result = MODULE.replace(target, corrected, tmp_path / "backups", expected)

    assert target.read_bytes() == b"%PDF-corrected"
    assert Path(result["backup"]).read_bytes() == b"%PDF-original"
    with pytest.raises(ValueError):
        MODULE.replace(target, corrected, tmp_path / "backups", expected)


def test_find_copies_matches_identical_bytes_only(tmp_path: Path) -> None:
    source = tmp_path / "vault" / "书.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF-same")
    storage = tmp_path / "storage"
    (storage / "A").mkdir(parents=True)
    (storage / "B").mkdir()
    (storage / "A" / "《书》.pdf").write_bytes(b"%PDF-same")
    (storage / "B" / "别的.pdf").write_bytes(b"%PDF-diff")

    assert MODULE.find_copies(source, storage) == [str(storage / "A" / "《书》.pdf")]


def test_spec_loads_gzip_and_rejects_other_versions(tmp_path: Path) -> None:
    import gzip

    path = tmp_path / "spec.json.gz"
    path.write_bytes(gzip.compress(json.dumps(SPEC).encode("utf-8")))
    assert MODULE.load_spec(path)["pages"]["1"][0][0] == "新"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 2, "pages": {}}), "utf-8")
    with pytest.raises(ValueError):
        MODULE.load_spec(bad)
