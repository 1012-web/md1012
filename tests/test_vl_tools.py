import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


COMPARE = load("vl_compare")
SUPPLEMENT = load("vl_supplement")


def test_compare_normalize_folds_variants_markers_and_zero_lookalikes() -> None:
    table = {"黃": "黄", "說": "説"}
    # VL 写的注号五花八门（〔一〕、（二）、丢了开口的「三〕」、《四》），都去掉；○与〇不算分歧
    assert COMPARE.normalize("黃道周（二）說：二○○三 年〔一〕，三〕《四》", table) == "黄道周説：二〇〇三年，"
    assert COMPARE.normalize("卷二十三。", {}) == "卷二十三。"  # 没有收口的数字不是注号


def test_supplement_skips_blocks_that_already_have_mineru_lines() -> None:
    lines = [(0.5, 0.1, 0.55, 0.9)]
    assert SUPPLEMENT.covered((0.45, 0.05, 0.6, 0.95), lines)
    assert not SUPPLEMENT.covered((0.1, 0.5, 0.3, 0.9), lines)


def test_supplement_cuts_a_vertical_block_into_columns_right_to_left() -> None:
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    # 两列「字」：左列 x 40..50，右列 x 70..80，都在 y 20..180
    for x0 in (40, 70):
        page.draw_rect(pymupdf.Rect(x0, 20, x0 + 10, 180), color=(0, 0, 0), fill=(0, 0, 0))

    columns = SUPPLEMENT.ink_lines(page, (0.1, 0.05, 0.5, 0.95), vertical=True)

    assert len(columns) == 2
    right, left = columns
    assert right[0] == pytest.approx(0.35, abs=0.01) and left[0] == pytest.approx(0.2, abs=0.01)
    assert right[1] == pytest.approx(0.1, abs=0.01) and right[3] == pytest.approx(0.9, abs=0.01)
