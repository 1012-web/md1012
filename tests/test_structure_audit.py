import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SA = load("structure_audit")
AD = load("apply_doubts")

DOC = """---
title: x
---

<!--p.1-->

# 甲篇

臣聞人心爲立國之根。〔一〕又曰：「作《九盭傳》「一。」

<!--p.2-->

### 乙疏[^2]

正文二[^1]。

---

## 注释

[^1]: <!--p.2--> 第二條。

[^2]: <!--p.2--> 第一條。
"""


def test_page_segments_split_at_anchors():
    body, notes = SA.split_notes(DOC)
    pages = SA.page_segments(body)
    assert [p for p, _, _ in pages] == [1, 2]
    start, end = SA.segment_of(body, 1)
    assert "臣聞人心" in body[start:end] and "乙疏" not in body[start:end]
    assert [d[0] for d in SA.note_defs(notes)] == ["1", "2"]


def test_apply_page_requires_unique_find():
    seg = "臣聞人心。臣聞人心。"
    same, problems = SA.apply_page(seg, [{"find": "臣聞人心", "replace": "X"}])
    assert same == seg and problems
    changed, problems = SA.apply_page("作《九盭傳》「一。」", [{"find": "九盭傳》「一", "replace": "九盭傳》」[^9001]"}])
    assert changed == "作《九盭傳》」[^9001]。」" and not problems


def test_apply_page_rejects_edit_broken_by_earlier_edit():
    seg = "甲乙丙丁"
    _, problems = SA.apply_page(seg, [{"find": "乙丙", "replace": "X"}, {"find": "丙丁", "replace": "Y"}])
    assert problems


def test_renumber_orders_by_first_reference_and_keeps_unused():
    text = DOC.replace("正文二[^1]。", "正文二[^1]。[^9001]") + "\n[^9001]: <!--p.2--> 補的注。\n\n[^77]: <!--p.1--> 沒人引用。\n"
    new, unused, undefined = SA.renumber_text(text)
    body, notes = SA.split_notes(new)
    assert "### 乙疏[^1]" in body and "正文二[^2]。[^3]" in body
    assert "[^1]: <!--p.2--> 第一條。" in notes and "[^3]: <!--p.2--> 補的注。" in notes
    assert unused == ["77"] and "[^4]: <!--p.1--> 沒人引用。" in notes and undefined == []


def test_toc_check_lists_missing_titles_from_start_entry():
    toc = "- 整理説明 …… 七九\n- 自明試務疏 …… 一五一\n- 救錢龍錫疏　某 …… 一五二\n- 乞歸疏 …… 一七一\n"
    entries = SA.toc_entries(toc)
    assert entries[2] == ("救錢龍錫疏", "一五二")
    titles = SA.heading_titles("### 自明試務疏 <small>崇禎三年。</small>\n\n### 救錢龍錫疏[^3]\n")
    assert SA.missing_titles(entries, titles, "自明試務疏") == [("乞歸疏", "一七一")]
    assert SA.chinese_page("一七一") == 171 and SA.chinese_page("二〇七") == 207


def test_residue_hits_skip_heading_lines():
    seg = "\n\n### （一）泛濫詩賦\n\n雅集課藝，黄道周的生平與思想因文證聖」（一），問業者\n"
    hits = SA.residue_hits(seg, ["黄道周的生平與思想"])
    assert hits == ["（一）", "黄道周的生平與思想"]


def test_book_forms_uses_first_of_each_group(tmp_path):
    (tmp_path / "_数据").mkdir()
    (tmp_path / "_数据" / "config.json").write_text(json.dumps({
        "pdf": "a.pdf", "markdownFiles": ["02-前言.md"], "fileOffsets": ["02-前言.md|16"],
        "writingMode": "vertical-rl", "variantEquivalents": ["黄黃", "録錄"]}), encoding="utf-8")
    book = SA.Book(tmp_path)
    assert book.offsets["02-前言.md"] == 16 and book.vertical
    assert book.to_book_forms("《黃子錄》") == "《黄子録》"


def test_decide_follows_rules():
    base = {"ocr": "二", "minerU": "一"}
    assert AD.decide({**base, "verdict": "B", "confidence": "高"}, "一", "keep") == ("change", "二")
    assert AD.decide({**base, "verdict": "B", "confidence": "低"}, "一", "keep") == ("pending", None)
    assert AD.decide({**base, "verdict": "A", "confidence": "高", "misprint": "戍"}, "一", "keep") == ("misprint", None)
    variant = {**base, "verdict": "C", "confidence": "高", "correct": "爲", "variant": {"printed": "爲", "standard": "為"}}
    assert AD.decide(variant, "為", "keep") == ("change", "爲")
    assert AD.decide(variant, "爲", "keep") == ("verify", None)
    assert AD.decide(variant, "为", "normalize") == ("change", "為")


def test_apply_edits_back_to_front_and_checks_old_text():
    text = "一𫖮一二"  # 𫖮 占两个 UTF-16 单位
    new, bad = AD.apply_edits(text, [(0, 1, "一", "二"), (3, 4, "一", "三"), (4, 5, "錯", "X")])
    assert new == "二𫖮三二" and bad == [(4, 5, "錯", "X")]
