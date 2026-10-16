"""2026-10 四本书试跑后修的几处 vl_build.py 缺陷：每处一条回归测试。"""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("vl_build_fixes", SCRIPTS / "vl_build.py")
MODULE = importlib.util.module_from_spec(spec)
sys.modules["vl_build_fixes"] = MODULE
spec.loader.exec_module(MODULE)


def block(label, content, x0=0.4, x1=0.5, y0=0.1, y1=0.9):
    return {"block_label": label, "block_content": content, "block_bbox": [x0 * 100, y0 * 100, x1 * 100, y1 * 100]}


def test_item_numbers_are_not_invented_text() -> None:
    # 《明通鉴》：条首的阿拉伯条目号曾让 1239 段正文被整段当成编造扣掉
    assert MODULE.suspect_reason("10夏，四月，丁卯，大祀南郊。", strict=True) is None
    assert MODULE.suspect_reason("……秋糧三十五萬有奇14六月，癸丑，禁勢家侵占民田。", strict=True) is None
    assert MODULE.suspect_reason("免山東濟南等府七十州縣夏税。39", strict=True) is None
    # 真编造照样拦
    assert MODULE.suspect_reason("2018年我国出口额为1.3亿元", strict=True) is not None
    assert MODULE.suspect_reason("F 05-4-872", strict=True) is not None


def test_mei_is_not_a_simplified_only_character() -> None:
    assert MODULE.suspect_reason("其後沒于王事，没齒不忘。") is None


def test_item_number_paragraphs_are_never_headings() -> None:
    title = (0.5, 0.22, 0.55, 0.45)
    assert MODULE.classify("vertical_text", "26免四川江西被災税糧", title, True, True) == ("para", 0)
    assert MODULE.classify("vertical_text", "孝宗達天明道純誠中正聖文神武至仁大德敬皇帝", title, True, True) == ("heading", 2)


def test_footer_blocks_that_open_with_a_note_number_are_notes() -> None:
    # 《黄道周集》：VL 把注区标成 footer，曾被当书口整块删掉
    data = {"width": 100, "height": 100, "parsing_res_list": [
        block("vertical_text", "臣聞之（一）。", 0.6, 0.9),
        block("footer", "〔一〕見《漳浦集》卷首。", 0.1, 0.2),
    ]}
    items = MODULE.page_items(data, True, {}, 0.3, strict=True)
    assert [(i["kind"], i.get("local")) for i in items] == [("para", None), ("note", 1)]


def test_running_heads_that_change_every_page_are_removed() -> None:
    heads = [f"明通鑑卷三十四紀三十四憲宗成化{n}年（一四八{k}）" for n, k in (("十", "〇"), ("十一", "一"), ("十二", "二"),
                                                                ("十三", "三"), ("十四", "四"), ("十五", "五"))]
    pages = {i: {"width": 100, "height": 100, "parsing_res_list": [block("header", h, 0.9, 0.95)]}
             for i, h in enumerate(heads, 1)}
    prefixes = MODULE.head_prefixes(pages)
    assert "明通鑑卷" in prefixes and "通鑑卷" in prefixes
    # 页码只认本页的（1197）：紧跟着的「三十五」是正文，不能一起删
    text, cut = MODULE.strip_head("免山東秋糧明通鑑卷三十四 紀三十四 憲宗成化十八年（一四八二） 一一九七三十五萬。", prefixes, 1197)
    assert text == "免山東秋糧三十五萬。" and len(cut) == 1
    # 不知道页码时宁可留下页码，也不吃正文
    text, _ = MODULE.strip_head("免山東秋糧明通鑑卷三十四 紀三十四 憲宗成化十八年（一四八二） 一一九七三十五萬。", prefixes)
    assert text.endswith("一一九七三十五萬。")


def test_cover_pages_before_the_first_page_number_get_no_printed_number() -> None:
    def page(number=None, blocks=()):
        res = [block("vertical_text", text) for text in blocks]
        if number:
            res.append({"block_label": "number", "block_content": number, "block_bbox": [0, 0, 1, 1]})
        return {"width": 100, "height": 100, "parsing_res_list": res}
    body = "春正月丁卯大祀南郊庚辰免山東被災秋糧辛巳振山東饑是月加吏部尚書"
    pages = {1: page(blocks=["〔清〕夏燮撰", "明通鑑", "中華書局"]),
             2: page(blocks=["明通鑑", "第三册"]),
             3: page("一一七五", [body]), 4: page("一一七六", [body]), 5: page("一一七七", [body])}
    printed, _ = MODULE.infer_printed(pages)
    assert printed[1] is None and printed[2] is None and printed[3] == 1175
