import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("vl_build", SCRIPTS / "vl_build.py")
MODULE = importlib.util.module_from_spec(spec)
sys.modules["vl_build"] = MODULE
spec.loader.exec_module(MODULE)


def test_chinese_number_reads_page_numbers_and_rejects_noise() -> None:
    assert MODULE.chinese_number("一五二") == 152
    assert MODULE.chinese_number("二十四") == 24
    assert MODULE.chinese_number("一〇") == 10 and MODULE.chinese_number("二O") == 20
    assert MODULE.chinese_number("！！！！") is None
    assert MODULE.chinese_number("Ⅲ1") is None


def page(number: str | None) -> dict:
    blocks = [{"block_label": "number", "block_content": number, "block_bbox": [0, 0, 1, 1]}] if number else []
    return {"width": 100, "height": 100, "parsing_res_list": blocks}


def test_infer_printed_drops_a_short_misread_run_and_fills_unnumbered_openings() -> None:
    # 物理 10–20 页印刷 1–11；其中 14–16 页的「一三」「一四」这类只读出了末位，自洽却错；10 页（篇首）不印页码
    numbers = {10: None, 11: "二", 12: "三", 13: "四", 14: "三", 15: "四", 16: "五", 17: "八", 18: "九", 19: "一〇", 20: "一一"}
    printed, runs = MODULE.infer_printed({p: page(n) for p, n in numbers.items()})
    assert [printed[p] for p in range(10, 21)] == list(range(1, 12))
    assert runs == [{"first": 10, "last": 20, "offset": 9, "printed": [1, 11]}]


def test_suspect_reason_catches_invented_modern_text_but_not_a_copyright_page() -> None:
    assert MODULE.suspect_reason("1. 2023年，中国GDP达到14万亿元，同比增长6.5%。") is not None
    assert MODULE.suspect_reason("[Unabelled] A 1000-A 1000-A 1000-A 1000-A 1") is not None
    assert MODULE.suspect_reason("2017年7月北京第1版 2017年7月北京第1次印刷 ISBN 978-7-101-12145-2") is None
    assert MODULE.suspect_reason("臣戶素多慙，感恩滋媿，執筆九載，未效纖塵。") is None


def test_page_items_takes_notes_only_beyond_the_rule() -> None:
    # 竖排：分隔线在 x=0.3；右边的「（一）泛濫詩賦」是小标题，不是注；左边以〔一〕开头的才是注
    def block(label, content, x0, x1):
        return {"block_label": label, "block_content": content, "block_bbox": [x0 * 100, 10, x1 * 100, 90]}

    data = {"width": 100, "height": 100, "parsing_res_list": [
        block("vertical_text", "黄季生黄道周（一）。", 0.8, 0.9),
        block("vertical_text", "（一）泛濫詩賦遨遊山水", 0.7, 0.75),
        block("vertical_text", "〔一〕皆見《乞言自序狀》。", 0.2, 0.25),
        block("vertical_text", "《黄子傳》，卷首。", 0.1, 0.15),
    ]}
    items = MODULE.page_items(data, True, {}, 0.3, strict=True)
    assert [(item["kind"], item.get("local")) for item in items] == [("para", None), ("heading", None), ("note", 1), ("note", 2)]
    assert items[1]["level"] == 3  # 「（一）」小节
    assert items[2]["text"] == "皆見《乞言自序狀》。"
    # 没有分隔线：一条注都不认
    assert not any(item["kind"] == "note" for item in MODULE.page_items(data, True, {}, None))


def test_classify_levels_and_volume_blocks() -> None:
    body = (0.5, 0.10, 0.55, 0.93)
    title = (0.5, 0.22, 0.55, 0.45)
    assert MODULE.classify("vertical_text", "卷三", (0.8, 0.1, 0.85, 0.17), True, False) == ("volume", 1)
    assert MODULE.classify("vertical_text", "疏", (0.7, 0.2, 0.75, 0.23), True, True) == ("heading", 2)
    assert MODULE.classify("vertical_text", "讓賢疏崇禎十年十二月。", title, True, True) == ("heading", 3)
    assert MODULE.classify("vertical_text", "石齋黃公逸事", title, True, False) == ("heading", 2)
    assert MODULE.classify("vertical_text", "一、黃道周的生平", title, True, False) == ("heading", 2)
    assert MODULE.classify("vertical_text", "臣前以給事，蒙恩召對，", body, True, True) == ("para", 0)
    assert MODULE.suspect_reason("F 05-4-872", strict=True) is not None
    assert MODULE.suspect_reason("1", strict=True) is not None and MODULE.suspect_reason("~~~", strict=True) is not None
    assert MODULE.suspect_reason("□□", strict=True) is None  # 原书缺字框不是编造
    assert MODULE.classify("vertical_text", "疏", (0.7, 0.11, 0.75, 0.24), True, True) == ("heading", 2)
    assert MODULE.suspect_reason("2023年高考数学模拟试卷") is not None
    assert MODULE.to_chinese(2) == "二" and MODULE.to_chinese(12) == "十二" and MODULE.to_chinese(21) == "二十一"
