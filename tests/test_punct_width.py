import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("punct_width", SCRIPTS / "punct_width.py")
MODULE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MODULE)


def test_half_width_punctuation_next_to_han_becomes_full_width() -> None:
    text, counts = MODULE.convert("秦令(5)规定, 后捕诇告者: 与盗同法; 何以然? 曰 治 生")
    assert text == "秦令（5）规定，后捕诇告者：与盗同法；何以然？曰治生"
    assert counts["punct"] == 4 and counts["paren"] == 1 and counts["space"] == 2


def test_western_text_numbers_and_markup_are_left_alone() -> None:
    source = ("参见 Smith, J. (2001), pp. 3-5, 共 1,000 人。[^3] <!--p.108--> "
              "[链接](http://a.b/c?x=1,2) `code, here`")
    text, _ = MODULE.convert(source)
    assert "Smith, J. (2001), pp. 3-5" in text
    assert "1,000" in text and "<!--p.108-->" in text and "(http://a.b/c?x=1,2)" in text and "`code, here`" in text
