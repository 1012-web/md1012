"""从已保存的 VL 逐页结果生成可核图的按页 Markdown 草稿。

build 默认保守整理：保留可疑文字和未核分类，不自动改卷号。
每页始终标 <!--pdf-page.N-->；没有已核印刷页报告时不猜印刷页。
目录标题层级由 AI 看目录原页并核正文，见 references/toc-headings.md。
原有高级版面启发式仅在显式 --legacy-heuristics 时启用。

pages 子命令输出页码分析候选；不是人工核验结果，不能直接冒充原页码。
原始 JSON、原页码识别值、起稿日志与后续 diff_report 都应保留。
字词核对使用 vl_compare.py，不直接根据另一家读法改字。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

DIGITS = {"〇": 0, "○": 0, "O": 0, "o": 0, "0": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}
SKIP_LABELS = {"header", "footer", "number", "header_image", "footer_image", "seal", "formula_number"}
FIGURE_LABELS = {"image", "table", "chart", "figure"}
NOTE_START = re.compile(r"^\s*[〔（(《]?\s*([一二三四五六七八九十]{1,3})\s*[〕）)》]\s*")
REF_RE = re.compile(r"[〔（(]\s*([一二三四五六七八九十]{1,3})\s*[〕）)]")
SENTENCE_END = set("。！？」』）…；：!?")
VOLUME_RE = re.compile(r"^卷[一二三四五六七八九十百]+$")
# 各卷开头的文体标签（单独一个字、字小）：疏、狀、箋、表、詔、制、誥……
GENRE_CHARS = set("疏狀箋表詔制誥檄策對議論辨考說説序記傳碑碣墓銘贊頌啓書詩賦箴誄約揭題辭")
TITLE_LABELS = {"doc_title", "paragraph_title", "title"}
SECTION_RE = re.compile(r"^[一二三四五六七八九十]+、")
SUBSECTION_RE = re.compile(r"^（[一二三四五六七八九十]+）")
INNER_PUNCT = re.compile(r"[。，；：！？]")
# 简体专用字（繁体排印本里不该出现）：VL 编造内容时写的是现代简体，据此认出
# 「强」「万」这类繁体排印本里也照常出现的字不收（第 453 页的「强」曾被误判）
# 「没」不收：中华本等繁体排印本照印「没」（《明通鉴》p.1466 一段因此被误判扣掉）
SIMPLIFIED_ONLY = set("们这个国为说时会对发经过还进样应现实开关长门问间见东车书写义习乐买卖变听导张总战报机权条极构气汉济满热爱独环电疗础称积类纪线绝统续维网罗职脑药觉认讨让论设证识词试话语请读谁调谈边达运远选邮钱铁错闻阳际陆队难须顾领风飞马验鱼鸡龙拨场亿币数拟题")
# 条目号：编年体点校本（《资治通鉴》《明通鉴》等）在每条之首印小号阿拉伯数字（「10夏，四月」）。
# 只认段首、或句末标点之后的 1–3 位数字，后面紧跟汉字。并非所有点校本都有，按书开关（--item-numbers）。
# VL 常漏掉句号、把两条并成一段（「……秋糧三十五萬有奇14六月，癸丑」），所以汉字后面的也认：
# 竖排古籍正文的数字都写成汉字，夹在汉字中间的阿拉伯数字只会是条目号
# 段末孤零零的「……夏税。39」是 VL 把下一条的号读到了上一条末尾，也算
ITEM_NUMBER_RE = re.compile(r"(^|(?<=[。！？」』）\]一-鿿]))\s*([0-9]{1,3})\s*(?=[一-鿿〔（「【]|$)")
# 帝号行：编年体每帝之首单独一行「孝宗達天明道……敬皇帝」，不带标点
EMPEROR_RE = re.compile(r"^[一-鿿]{2,30}(?:皇帝|皇后)$")
NUMERALS = "〇○零一二三四五六七八九十百千"
# 篇题后面原书印成小字的注记：年月、「凡三章」「節録」之类，标题里用 <small> 标出
TITLE_NOTE_RE = re.compile(
    r"(?<=.)((?:崇禎|天啓|萬曆|泰昌|弘光|隆武|永曆|順治|康熙|乾隆|嘉慶|道光)[^，。]*"
    r"|[甲乙丙丁戊己庚辛壬癸][子丑寅卯辰巳午未申酉戌亥](?:年|[正一二三四五六七八九十閏]+月)[^，。]*"
    r"|凡[一二三四五六七八九十]+[章首篇]|節録|修漳志作)。?$")
CJK = re.compile(r"[一-鿿]")


def suspect_reason(text: str, strict: bool = False) -> str | None:
    """VL 的「幻觉」：凭空写出原书没有的现代文字。硬特征任一条即不进正文。

    strict（有印刷页码的正文页）：竖排排印本正文里不该有拉丁字母、LaTeX 记号或阿拉伯数字串——
    《黄道周集》各卷卷首的大字位置被编成「F 05-4-872」「$E_{1}=1.5m/s$」「2023.01.01」。
    封面、版权页（无页码）不用这一条：那里本来就有 ISBN、网址。
    """
    # 条目号（段首或句末之后的 1–3 位数字）不算：《明通鉴》曾因此有 1239 段正文被当成编造整段扣掉
    probe = ITEM_NUMBER_RE.sub(lambda m: m.group(1), text)
    if strict and (re.search(r"[A-Za-z$\\]", probe) or re.search(r"[0-9]{2,}", probe)):
        return "正文页出现拉丁字母、公式记号或阿拉伯数字"
    # 空白或透印区被读成一串零碎符号（「1」「4」「~~~」「....」）：正文页上一个汉字都没有的短块
    if strict and len(probe) <= 12 and not re.search(r"[一-鿿□〇]", probe):
        return "正文页上没有汉字的零碎块（多为空白或背面透印）"
    simplified = sum(1 for ch in text if ch in SIMPLIFIED_ONLY)
    if simplified >= 3:
        return f"简体专用字 {simplified} 个"
    ascii_run = re.findall(r"[0-9A-Za-z]{2,}", text)
    # 版权页本来就有 ISBN、年份；编造的内容则是同一串反复出现（「A 1000-A 1000-…」）
    if ascii_run and max(ascii_run.count(item) for item in ascii_run) >= 4:
        return "同一串数字或字母反复出现"
    sentences = [s for s in re.split(r"[。！？]", text) if len(s) >= 6]
    if sentences and max(sentences.count(s) for s in sentences) >= 3:
        return "同一句重复多遍"
    return None


def running_heads(pages: dict[int, dict]) -> set[str]:
    """页眉：在页边、字数少、在三页以上重复出现的块（VL 常把竖排页边的篇名标成正文）。"""
    seen: Counter = Counter()
    for data in pages.values():
        for block in data["parsing_res_list"]:
            content = re.sub(r"\s+", "", block.get("block_content") or "")
            if not content or len(content) > 25:
                continue
            x0, y0, x1, y1 = block_rect(block, data)
            if x0 > 0.85 or x1 < 0.15 or y0 > 0.9 or y1 < 0.1:
                seen[content] += 1
    return {content for content, count in seen.items() if count >= 3}


def head_prefixes(pages: dict[int, dict]) -> set[str]:
    """逐页变化的书眉（「明通鑑卷三十四 紀三十四 憲宗成化十八年（一四八二） 一一九七」）整串不重复，
    running_heads 认不出。它们开头几个字是固定的：页边短块（含 VL 标成 header 的）里，开头 4 个字
    在 5 页以上出现、且后面跟着卷次或数字的，记成书眉前缀；连同去掉首字的形态（VL 常丢「明」）。"""
    starts: Counter = Counter()
    for data in pages.values():
        for block in data["parsing_res_list"]:
            content = re.sub(r"\s+", "", block.get("block_content") or "")
            if not 6 <= len(content) <= 40:
                continue
            x0, y0, x1, y1 = block_rect(block, data)
            label = block.get("block_label") or ""
            if label == "header" or x0 > 0.85 or x1 < 0.15 or y0 > 0.9 or y1 < 0.1:
                starts[content[:4]] += 1
    prefixes = {p for p, n in starts.items() if n >= 5 and re.search(r"[卷紀纪第]", p) and CJK.match(p)}
    return prefixes | {p[1:] for p in prefixes if len(p) == 4}


def digit_forms(number: int | None) -> list[str]:
    """印刷页码在书眉里的写法：「一一九七」「一二○七」（〇、○ 两种零都收）。"""
    if not number:
        return []
    plain = "".join("〇一二三四五六七八九"[int(ch)] for ch in str(number))
    return [plain, plain.replace("〇", "○")]


def strip_head(text: str, prefixes: set[str], page_number: int | None = None) -> tuple[str, list[str]]:
    """段落里夹着的书眉：前缀 + 卷次、年号、括注公元年（+ 本页页码），一串删去。返回（删后文本, 删掉的串）。
    页码只认本页的印刷页码：书眉后面紧跟的正文也可能是数字（「一一九七三十五萬」里只有「一一九七」是页码）。"""
    removed: list[str] = []
    near = (page_number, page_number - 1, page_number + 1) if page_number and page_number > 1 else ()
    tail = "|".join(re.escape(form) for number in near for form in digit_forms(number))
    for prefix in sorted(prefixes, key=len, reverse=True):
        pattern = re.compile(re.escape(prefix) + r"[^。，；：！？「」『』]{0,30}?[（(][" + NUMERALS + r"—\s]{2,12}[）)]?"
                             + (r"\s*(?:" + tail + ")?" if tail else "") + r"\s*")

        def cut(match: re.Match) -> str:
            removed.append(match.group(0))
            return ""
        text = pattern.sub(cut, text)
    return text, removed


def chinese_number(text: str) -> int | None:
    """「一五二」「二十四」「一〇」这类页码；夹了别的字就不认。"""
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
    value = value.replace("O", "〇").replace("o", "〇").replace("○", "〇")
    if not value or any(ch not in DIGITS and ch not in "十百" for ch in value):
        return None
    if "十" in value or "百" in value:
        total, current = 0, 0
        for ch in value:
            if ch == "百":
                total += (current or 1) * 100
                current = 0
            elif ch == "十":
                total += (current or 1) * 10
                current = 0
            else:
                current = DIGITS[ch]
        return total + current or None
    number = 0
    for ch in value:
        number = number * 10 + DIGITS[ch]
    return number or None


def local_number(text: str) -> int | None:
    return chinese_number(text)


def load_pages(vl_dir: Path, first: int, last: int) -> dict[int, dict]:
    pages = {}
    for page in range(first, last + 1):
        path = vl_dir / f"p{page:04d}.json"
        if path.exists():
            pages[page] = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    return pages


def infer_printed(pages: dict[int, dict]) -> tuple[dict[int, int | None], list[dict]]:
    raw: dict[int, int | None] = {}
    for page, data in pages.items():
        numbers = [chinese_number(b.get("block_content")) for b in data["parsing_res_list"] if b.get("block_label") == "number"]
        numbers = [n for n in numbers if n]
        raw[page] = numbers[0] if len(numbers) == 1 else None
    ordered = sorted(pages)
    offsets = {p: p - n for p, n in raw.items() if n}
    reliable: dict[int, int] = {}
    for page, offset in offsets.items():
        near = [offsets.get(page + d) for d in (-3, -2, -1, 1, 2, 3)]
        if near.count(offset) >= 1:
            reliable[page] = offset
    # 短段误读：竖排页码「一三」只读出「三」这类错，连续几页都错就前后自洽、骗过邻页检查。
    # 一小段（≤7 页）的偏移与前后两段都不同、而前后两段彼此相同时，这一小段不算数。
    segments: list[list[int]] = []
    for page in sorted(reliable):
        if segments and reliable[segments[-1][-1]] == reliable[page] and page - segments[-1][-1] <= 4:
            segments[-1].append(page)
        else:
            segments.append([page])
    # 推广到连着几小段都错：夹在两段相同偏移之间、合计不超过 15 页的各小段，偏移都与两端不同，一并不算数
    # （《黄道周集》物理 210–218 页读成 12–15、126–130 两小段，实际 112–120）
    offset_of = [reliable[run[0]] for run in segments]
    drop: list[list[int]] = []
    for left in range(len(segments)):
        for right in range(left + 2, len(segments)):
            middle = segments[left + 1:right]
            if sum(len(run) for run in middle) > 15:
                break
            if offset_of[left] == offset_of[right] and all(offset_of[k] != offset_of[left] for k in range(left + 1, right)):
                drop.extend(middle)
                break
    for run in drop:
        for page in run:
            reliable.pop(page, None)
    printed: dict[int, int | None] = {}
    reliable_pages = sorted(reliable)
    for page in ordered:
        if page in reliable:
            printed[page] = page - reliable[page]
            continue
        before = [p for p in reliable_pages if p < page]
        after = [p for p in reliable_pages if p > page]
        if before and after and reliable[before[-1]] == reliable[after[0]]:
            printed[page] = page - reliable[before[-1]]
        else:
            printed[page] = None
    # 篇首页常不印页码：未知页紧挨着一段页码（2 页以内）就顺延；先看后面（新一段的开头），印刷页须 ≥ 1。
    # 全书第一个可信页码之前、字很少的页（封面、书名页）不顺延：分册本页码接着上册，
    # 封面会被推成 1173、1174 这类假页码（《明通鉴》第 3 册）
    first_reliable = min(reliable) if reliable else None

    def cover_like(page: int) -> bool:
        """有字、但没有一块成段（≥20 个汉字）的页：封面、书名页、扉页。"""
        blocks = [b.get("block_content") or "" for b in pages[page]["parsing_res_list"]]
        return bool(blocks) and all(len(CJK.findall(text)) < 20 for text in blocks)

    for page in ordered:
        if printed[page] is not None:
            continue
        if first_reliable is not None and page < first_reliable and cover_like(page):
            continue
        after = next((p for p in range(page + 1, page + 3) if printed.get(p) is not None and p in reliable), None)
        before = next((p for p in range(page - 1, page - 3, -1) if printed.get(p) is not None and p in reliable), None)
        if after is not None and page - (after - printed[after]) >= 1:
            printed[page] = page - (after - printed[after])
        elif before is not None:
            printed[page] = page - (before - printed[before])
    runs: list[dict] = []
    for page in ordered:
        offset = None if printed[page] is None else page - printed[page]
        if runs and runs[-1]["offset"] == offset and runs[-1]["last"] == page - 1:
            runs[-1]["last"] = page
        else:
            runs.append({"first": page, "last": page, "offset": offset})
    for run in runs:
        if run["offset"] is not None:
            run["printed"] = [run["first"] - run["offset"], run["last"] - run["offset"]]
    return printed, runs


def block_rect(block: dict, data: dict) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = block["block_bbox"]
    return (x0 / data["width"], y0 / data["height"], x1 / data["width"], y1 / data["height"])


def fold(text: str, table: dict[str, str]) -> str:
    return "".join(table.get(ch, ch) for ch in text)


def note_separator(pdf_page, vertical: bool) -> float | None:
    """页下注与正文之间的分隔线（竖排是一条竖线，横排是一条横线），返回它的归一化位置；找不到返回 None。

    以注号开头作判据不可靠（「（一）泛濫詩賦」这种小标题也以（一）开头），分隔线才是版面自己的声明：
    一条又细又长的墨线，长度占页面 8%–50%，两侧几个像素都是白的。字的笔画一个字高就断，长不到这么长。
    """
    import numpy as np
    import pymupdf

    pix = pdf_page.get_pixmap(dpi=144, colorspace=pymupdf.csGRAY)
    ink = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < 170  # 细线在扫描图上是浅灰
    if not vertical:
        ink = ink.T  # 横排：找横线，转置成找竖线
    height, width = ink.shape
    best = None
    candidates = np.where(ink.sum(axis=0) >= height * 0.08)[0]
    for x in candidates:
        if not width * 0.03 <= x <= width * 0.97:
            continue
        column = ink[:, x].astype(np.int8)
        # 最长连续墨段：找 0→1、1→0 的边界
        edges = np.diff(np.concatenate(([0], column, [0])))
        starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
        lengths = ends - starts
        k = int(lengths.argmax())
        longest, y0, y1 = int(lengths[k]), int(starts[k]), int(ends[k])
        if not height * 0.08 <= longest <= height * 0.5:
            continue
        # 两侧只看线本身那一段：隔 5 像素的两条竖带里墨很少，才是孤立的细线
        side = max(int(ink[y0:y1, max(0, x - 5)].sum()), int(ink[y0:y1, min(width - 1, x + 5)].sum()))
        if side < longest * 0.3:
            best = int(x)
    return None if best is None else best / width


def char_size(pdf_page, rect: tuple[float, float, float, float], vertical: bool) -> float | None:
    """块里字的大小：按墨迹投影切出的列宽（竖排）或行高（横排）的中位数，归一化到页面。小字夹注据此认出。"""
    try:
        from vl_common import ink_lines
    except ImportError:  # 作为模块被测试加载时
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from vl_common import ink_lines
    lines = ink_lines(pdf_page, rect, vertical)
    sizes = sorted((line[2] - line[0]) if vertical else (line[3] - line[1]) for line in lines)
    return sizes[len(sizes) // 2] if sizes else None


def classify(label: str, content: str, rect: tuple[float, float, float, float], vertical: bool,
             in_volumes: bool, numbered: bool = True) -> tuple[str, int]:
    """块是什么：("volume"|"heading"|"para", 标题层级)。

    层级：前言里 文章题 #、「一、」##、「（一）」###；正文里 卷首各篇与「卷N」#、子篇与文体标签（疏狀詔制）##、
    卷内篇题 ###。竖排的篇题比正文低三四格起排（上沿在页高 17%–32%）、短、没有句读，VL 多把它标成正文。
    """
    height = rect[3] - rect[1]
    bare = content.rstrip("。")
    # 以条目号开头的是编年体的一条记事，不是标题（「26免四川、江西被災税糧。」曾被升成标题）
    if re.match(r"^\s*[0-9]{1,3}\s*[一-鿿]", content) or content.startswith('<sup class="item">'):
        return "para", 0
    if not numbered:
        # 封面、版权页（无页码）：只认 VL 标的大标题，其余（「一」「*」、版权信息的小标题）都当普通段落
        return ("heading", 1) if label == "doc_title" else ("para", 0)
    if VOLUME_RE.match(content) and rect[1] < 0.16 and height < 0.12:
        return "volume", 1
    # 卷首的文体标签有时印成大字（卷五「疏」块高 0.13），放宽到 0.16
    if in_volumes and len(content) == 1 and content in GENRE_CHARS and height < 0.16:
        return "heading", 2
    if label == "doc_title":
        return "heading", 1
    if label in TITLE_LABELS and len(CJK.findall(content)) >= 2:
        return "heading", 3 if in_volumes else 1
    at_edge = rect[0] > 0.85 or rect[2] < 0.15
    balanced = all(bare.count(a) == bare.count(b) for a, b in ("《》", "「」", "（）", "〔〕"))  # 半截注文「漳浦集》卷首」不配对
    real = balanced and len(CJK.findall(bare)) >= 2 and not re.match(r"^[》」』）〕)、，。…．.]", bare)
    short = real and len(bare) <= 40 and not INNER_PUNCT.search(bare)
    # 编年体每帝之首的帝号行（「孝宗達天明道……敬皇帝」）：卷名之下一级
    if short and EMPEROR_RE.match(bare):
        return "heading", 2
    if short and SECTION_RE.match(bare):
        return "heading", 2
    if short and SUBSECTION_RE.match(bare):
        return "heading", 3
    if vertical and short and not at_edge and 0.17 <= rect[1] <= 0.32 and height <= 0.65:
        return "heading", 3 if in_volumes else 2
    return "para", 0


def effective_label(block: dict) -> str:
    """VL 有时把页下注标成 footer（页脚）：以〔一〕（一）开头的 footer 块按正文块对待，交给注区判定，
    不能当书口删掉（《黄道周集》试核丢的 4 条注里 3 条是这样丢的）。"""
    label = block.get("block_label") or ""
    if label == "footer" and NOTE_START.match((block.get("block_content") or "").strip()):
        return "text"
    return label


def page_items(data: dict, vertical: bool, table: dict[str, str], separator: float | None = None,
               heads: set[str] | None = None, in_volumes: bool = False, strict: bool = False,
               pdf_page=None, prefixes: set[str] | None = None, small: bool = True,
               log: list | None = None, page_number: int | None = None, conservative: bool = False) -> list[dict]:
    """一页的块：正文段、标题、注（local 号）、图表记号。

    有分隔线时，线那一侧（竖排在左、横排在下）的正文块都是注；以注号开头的开一条新注，不带注号的也开一条新注
    （VL 常把注号读丢），号码接着上一条。没有分隔线的页不认注。
    """
    items: list[dict] = []
    log = log if log is not None else []
    if conservative:
        # Keep source text and explicit labels; geometric guesses become review tasks.
        safe_items = []
        for block in data["parsing_res_list"]:
            label = block.get("block_label", "text")
            content = (block.get("block_content") or "").strip()
            if not content:
                continue
            rect = block_rect(block, data)
            verified = block.get("_structure_verified", False)
            if label in {"number", "page_number"}:
                log.append({"kind": "pageNumberSource", "text": content})
                continue
            if label in {"header", "footer", "seal"} and verified:
                log.append({"kind": "reviewedExcluded", "text": content})
                continue
            if label in FIGURE_LABELS:
                safe_items.append({"kind": "figure", "label": label, "rect": rect, "html": content})
                continue
            content = fold(re.sub(r"\s*\n\s*", "", content), table)
            if label in {"footnote", "vision_footnote"} and verified:
                marker = NOTE_START.match(content)
                if marker:
                    safe_items.append({"kind":"note", "local":local_number(marker.group(1)), "text":content[marker.end():], "rect":rect})
                    continue
            pending = label in {"header", "footer", "footnote", "vision_footnote", "seal"} or block.get("_needs_review", False)
            reason = suspect_reason(content, strict)
            entry = {"kind": "heading" if label in TITLE_LABELS else "para", "level": 1 if label in TITLE_LABELS else 0,
                     "text": content, "rect": rect, "needsReview": pending, "suspect": reason}
            if pending:
                log.append({"kind":"structureCandidate", "sourceId":block.get("_source_id"), "label":label,"text":content})
            if reason:
                log.append({"kind":"suspectCandidate", "reason":reason,"text":content})
            safe_items.append(entry)
        return safe_items
    if separator is not None:
        # 线那一侧至少有一块以注号开头，才承认是注（版权页的表格线、插图的边框也是细长线）
        marked = False
        for block in data["parsing_res_list"]:
            if effective_label(block) in SKIP_LABELS or not block.get("block_content"):
                continue
            rect = block_rect(block, data)
            center = (rect[0] + rect[2]) / 2 if vertical else (rect[1] + rect[3]) / 2
            if (center < separator if vertical else center > separator) and NOTE_START.match(block["block_content"].strip()):
                marked = True
                break
        if not marked:
            separator = None
    for block in data["parsing_res_list"]:
        label = effective_label(block)
        content = (block.get("block_content") or "").strip()
        if label in SKIP_LABELS:
            continue
        if label in FIGURE_LABELS:
            items.append({"kind": "figure", "label": label, "rect": block_rect(block, data), "html": content})
            continue
        if not content:
            continue
        rect = block_rect(block, data)
        at_edge = rect[0] > 0.85 or rect[2] < 0.15 or rect[1] > 0.9 or rect[3] < 0.1
        # 页眉过滤只管正文块：篇题本身（doc_title）也常印在页边第一列，不能当页眉删
        if heads and at_edge and label not in TITLE_LABELS and re.sub(r"\s+", "", content) in heads:
            continue
        flat = re.sub(r"\s+", "", content)
        if prefixes and len(flat) <= 40 and any(flat.startswith(p) for p in prefixes) and label not in TITLE_LABELS:
            log.append({"kind": "headBlock", "text": flat})
            continue  # 逐页变化的书眉整块
        lines = [line.strip() for line in content.split("\n") if line.strip()]
        if strict and len(lines) == 2 and VOLUME_RE.match(lines[0]) and lines[1] in GENRE_CHARS and at_edge:
            continue  # 页边的「卷一 疏」是书口（页脚），VL 有时标成正文
        # VL 偶尔把「卷一」「疏」「自明試務疏」三行并成一块：拆开，各自定层级。
        # 只拆三行以上的：「卷二十 説」这种两行是《石齋十二書部次》里的书目条目，不是卷名
        # 条件收紧：第二行正好是一个文体字、且在页面右侧（「卷四十一 / 墓誌 / 文」这种书目条目也是三行）
        if (strict and len(lines) >= 3 and VOLUME_RE.match(lines[0]) and rect[1] < 0.16
                and lines[1] in GENRE_CHARS and (not vertical or rect[2] > 0.7)):
            items.append({"kind": "volume", "level": 1, "text": lines[0], "rect": rect})
            items.append({"kind": "heading", "level": 2, "text": lines[1]})
            items.append({"kind": "heading", "level": 3, "text": fold("".join(lines[2:]), table)})
            continue
        content = fold(re.sub(r"\s*\n\s*", "", content), table)
        if prefixes:
            content, cut = strip_head(content, prefixes, page_number)
            log.extend({"kind": "headInText", "text": piece} for piece in cut)
            if not content.strip():
                continue
        reason = suspect_reason(content, strict)
        if reason:
            items.append({"kind": "suspect", "text": content, "reason": reason})
            continue
        start = NOTE_START.match(content)
        center = (rect[0] + rect[2]) / 2 if vertical else (rect[1] + rect[3]) / 2
        in_note_area = separator is not None and (center < separator if vertical else center > separator)
        if label in {"text", "vertical_text", "footnote", "vision_footnote"} and in_note_area:
            number = local_number(start.group(1)) if start else None
            body = content[start.end():] if start else content
            # VL 常把相邻两条注并成一块：「……補。〔二〕「又」，底本原闕……」，在句末之后的注号处拆开
            pieces = re.split(r"(?<=[。」』）])\s*([〔（(]\s*[一二三四五六七八九十]{1,3}\s*[〕）)])", body)
            items.append({"kind": "note", "local": number, "text": pieces[0].strip()})
            for marker, text in zip(pieces[1::2], pieces[2::2]):
                items.append({"kind": "note", "local": local_number(re.sub(r"[^一二三四五六七八九十]", "", marker)),
                              "text": text.strip()})
            continue
        kind, level = classify(label, content, rect, vertical, in_volumes, strict)
        if kind == "volume" and any(item["kind"] == "volume" for item in items):
            continue  # 同一页读出两次卷名（卷首大字与书口），只认一次
        entry = {"kind": kind, "level": level, "text": content, "rect": rect}
        if kind == "para" and re.match(r"^\s*[0-9]{1,3}\s*[一-鿿〔（「【]", content):
            entry["itemStart"] = True  # 以条目号开头：新的一条，不与上页末段接
        items.append(entry)
    # 卷名要站得住：竖排在页面右侧第一列，且本页另有文体标签（疏、狀……）或篇题；
    # 《石齋十二書部次》这类书目页里单独一行的「卷四十一」不算
    for item in items:
        if item["kind"] != "volume" or not vertical:
            continue
        rect = item.get("rect")
        right = rect is None or rect[2] > 0.7
        anchored = any(
            other is not item and (
                (other["kind"] == "heading" and other.get("level", 0) >= 2)
                or (len(other.get("text", "")) == 1 and other.get("text") in GENRE_CHARS)
            )
            for other in items
        ) or any((block.get("block_content") or "").strip() in GENRE_CHARS for block in data["parsing_res_list"])
        if not (right and anchored):
            item["kind"], item["level"] = "para", 0
    # 小字夹注（「洪思曰」之类）：字比本页正文小两成以上的段落，用 <small> 标出
    if pdf_page is not None and small:
        sizes = {}
        for index, item in enumerate(items):
            # 以条目号开头的是一条记事（正文），不按字号判小字：短条只有一两列，列宽量不准
            if item["kind"] == "para" and item.get("rect") and len(item["text"]) >= 8 and not item.get("itemStart"):
                sizes[index] = char_size(pdf_page, item["rect"], vertical)
        known = sorted(size for size in sizes.values() if size)
        if len(known) >= 2:
            body = known[min(len(known) - 1, int(len(known) * 0.75))]  # 夹注再多也压不过正文：取偏大的四分位
            for index, size in sizes.items():
                if size and size < 0.82 * body:
                    items[index]["small"] = True
    # 没带注号的注接着上一条编号
    last = 0
    for item in items:
        if item["kind"] == "note":
            if item["local"] is None:
                item["local"] = last + 1
            last = item["local"]
    return items


def to_chinese(number: int) -> str:
    digits = "〇一二三四五六七八九"
    if number < 10:
        return digits[number]
    if number < 20:
        return "十" + (digits[number % 10] if number % 10 else "")
    if number < 100:
        return digits[number // 10] + "十" + (digits[number % 10] if number % 10 else "")
    return "".join(digits[int(ch)] for ch in str(number))


def new_state(images_dir: Path | None = None) -> dict:
    return {"inVolumes": False, "volume": 0, "volumeFixes": [], "lastHeading": "", "openSmall": False,
            "imagesDir": images_dir, "images": []}


def figure_markdown(item: dict, page: int, pdf_page, state: dict, stats: Counter) -> str:
    """图、表：从原 PDF 按 VL 的块坐标裁出来存进 images/，原位嵌入；表格另把 VL 读出的表格结构照录。"""
    import pymupdf

    stats[f"{item['label']}s"] += 1
    alt = state["lastHeading"] or ("表" if item["label"] == "table" else "图")
    parts = []
    if pdf_page is not None and state["imagesDir"] is not None:
        state["imagesDir"].mkdir(parents=True, exist_ok=True)
        x0, y0, x1, y1 = item["rect"]
        width, height = pdf_page.rect.width, pdf_page.rect.height
        clip = pymupdf.Rect(x0 * width - 4, y0 * height - 4, x1 * width + 4, y1 * height + 4) & pdf_page.rect
        name = f"p{page:04d}-{len([i for i in state['images'] if i['page'] == page]) + 1}.png"
        pdf_page.get_pixmap(dpi=200, clip=clip).save(state["imagesDir"] / name)
        state["images"].append({"page": page, "file": name, "label": item["label"], "alt": alt})
        parts.append(f"![{alt}](images/{name})")
    html = item.get("html") or ""
    if item["label"] == "table" and html.count("<tr") >= 2 and "<img" not in html:
        parts.append(re.sub(r"\s*\n\s*", "", html))
    elif item["label"] == "table" and html:
        parts.append("<!--?table VL 读出的表格含图或只有一行，只保留原图-->")
    if item["label"] == "chart":
        parts.append("<!--?chart VL 把这一块当成图、没读文字；若原是文字（如目录）须对原图补录-->")
    return "\n\n".join(parts) or "<!--?figure 未能裁图-->"


def build(pages: dict[int, dict], printed: dict[int, int | None], segment: tuple[int, int], vertical: bool,
          table: dict[str, str], title: str, name: str, document=None, heads: set[str] | None = None,
          state: dict | None = None) -> tuple[str, dict]:
    state = state if state is not None else new_state()
    first, last = segment
    out: list[str] = []
    suspects: list[dict] = []
    notes: list[str] = []
    counter = 0
    stats = Counter()
    paragraph_open = False
    # 整段都没有页码（封面、版权页）：物理页即页码（书配置里这个文件偏移为 0）
    unnumbered = all(printed.get(page) is None for page in range(first, last + 1))
    last_offset = None
    for page in range(first, last + 1):
        data = pages.get(page)
        guessed = False
        if unnumbered:
            number = None
        elif printed.get(page):
            number = printed[page]
            last_offset = page - number
        else:
            # 有页码的段里推不出的页（多是插图页）：按前面那段的偏移推一个，标出来
            number = page - last_offset if last_offset is not None else None
            guessed = True
        anchor = f"<!--pdf-page.{page}-->" + (f"<!--p.{number}-->" + ("<!--?page-guess-->" if guessed else "") if number else "")
        if data is None:
            stats["missingPages"] += 1
            out.append(f"\n\n{anchor}<!--?vl-missing-->")
            paragraph_open = False
            continue
        if guessed and not any(
            item["kind"] in {"para", "heading", "volume"} for item in page_items(data, vertical, table, None, heads)
        ):
            stats["unnumberedPagesWithoutText"] += 1
            continue  # 没有文字的插图页：不写锚点，免得占用别页的页码
        if guessed:
            stats["guessedPages"] += 1
        pdf_page = document[page - 1] if document is not None else None
        separator = note_separator(pdf_page, vertical) if pdf_page is not None else None
        options = {"prefixes": state.get("prefixes"), "small": state.get("small", True),
                   "page_number": number if isinstance(number, int) else None, "conservative": state.get("conservative", True)}
        page_log: list = []
        items = page_items(data, vertical, table, separator, heads, state["inVolumes"], not unnumbered, pdf_page,
                           log=page_log, **options)
        if not state["inVolumes"] and any(item["kind"] == "volume" for item in items):
            state["inVolumes"] = True  # 进入分卷：本页的篇题按卷内层级重新定
            page_log = []
            items = page_items(data, vertical, table, separator, heads, True, not unnumbered, pdf_page,
                               log=page_log, **options)
        state.setdefault("headLog", []).extend({**entry, "page": page} for entry in page_log)
        if separator is not None:
            if any(item["kind"] == "note" for item in items):
                stats["pagesWithNotes"] += 1
            else:
                # 有一条细长线、却没认出注：可能是专名线、表格线，也可能是注号全被读丢的注区——结构核查时逐页看
                state.setdefault("ruleWithoutNotes", []).append(page)
        for item in items:
            if item.get("itemStart"):
                stats["itemNumberParas"] += 1
        # 本页注：local 号 -> 全局号
        global_of: dict[int, int] = {}
        for item in items:
            if item["kind"] == "note":
                counter += 1
                global_of[item["local"]] = counter
                notes.append(f"[^{counter}]: {anchor} {item['text']}")
                stats["notes"] += 1
        used: set[int] = set()

        def replace_ref(match: re.Match) -> str:
            number = local_number(match.group(1))
            if number in global_of and number not in used:
                used.add(number)
                return f"[^{global_of[number]}]"
            return match.group(0)

        body = [item for item in items if item["kind"] != "note"]
        placed_anchor = False
        for index, item in enumerate(body):
            if item["kind"] == "figure":
                out.append(("\n\n" + anchor if not placed_anchor else "") + "\n\n" + figure_markdown(item, page, pdf_page, state, stats))
                placed_anchor = True
                paragraph_open = False
                continue
            if item["kind"] == "suspect":
                preview = item["text"][:30].replace("--", "－－")
                out.append(f"\n\n<!--?vl-suspect {item['reason']}：{preview}…-->\n\n{item['text']}")
                stats["suspectBlocks"] += 1
                suspects.append({"page": page, "reason": item["reason"], "text": item["text"][:80]})
                paragraph_open = False
                continue
            # 注号也会出现在篇题后面（「時務疏（一）」「……八月（一）。」）
            text = REF_RE.sub(replace_ref, item["text"]) if item["kind"] in {"para", "heading"} else item["text"]
            if item.get("needsReview"):
                out.append("\n\n<!--?structure-->")
                stats["structureCandidates"] += 1
            if item.get("suspect"):
                out.append("\n\n<!--?vl-suspect-->")
                stats["suspectBlocks"] += 1
            if item["kind"] == "heading" and item["level"] >= 2:
                refs = "".join(re.findall(r"\[\^\d+\]", text))
                plain = re.sub(r"\[\^\d+\]", "", text)
                note = TITLE_NOTE_RE.search(plain)
                if note:
                    # 整段匹配（含末尾句号）都放进 <small>：一个字也不能丢
                    text = plain[: note.start()] + " <small>" + note.group(0) + "</small>" + refs
                    stats["titleNotesMarkedSmall"] += 1
            if item["kind"] == "volume":
                item = {**item, "kind": "heading", "level": 1}
            if item["kind"] == "heading" and item["level"] == 1:
                vol = re.search(r"卷([" + NUMERALS + r"]+)", text)
                read = chinese_number(vol.group(1)) if vol else None
                if read and read <= state["volume"]:
                    state.setdefault("volumeCandidates", []).append({"page": page, "read": text, "reason": "non-increasing volume; check original"})
                    out.append("\n\n<!--?volume-->")
                    stats["volumeCandidates"] += 1
                if read:
                    state["volume"] = read
            if item["kind"] == "heading":
                out.append(("\n\n" + anchor if not placed_anchor else "") + "\n\n" + "#" * item["level"] + " " + text)
                placed_anchor = True
                paragraph_open = False
                stats[f"headingsLevel{item['level']}"] += 1
                state["lastHeading"] = text
                continue
            small = bool(item.get("small"))
            if state.get("itemNumbers"):
                # 按书开关：条目号套上 <sup class="item">，读得出是条目号、又不和正文粘连；每个都记进报告备查
                def mark(match: re.Match) -> str:
                    state.setdefault("itemLog", []).append({"page": page, "number": match.group(2), "text": text[:20]})
                    return f'{match.group(1)}<sup class="item">{match.group(2)}</sup>'
                text = ITEM_NUMBER_RE.sub(mark, text)
            if index == 0 and paragraph_open and out and small == state["openSmall"] and not item.get("itemStart"):
                # 跨页接段：锚点落在段中；小字段接小字段时接进 </small> 里面
                if small and out[-1].endswith("</small>"):
                    out[-1] = out[-1][: -len("</small>")] + anchor + text + "</small>"
                else:
                    out[-1] += anchor + text
                stats["joinedAcrossPages"] += 1
            else:
                body_text = f"<small>{text}</small>" if small else text
                out.append(("\n\n" + anchor if not placed_anchor else "") + "\n\n" + body_text)
            if small:
                stats["smallParagraphs"] += 1
            state["openSmall"] = small
            placed_anchor = True
            paragraph_open = text[-1:] not in SENTENCE_END
            stats["paragraphs"] += 1
        if not placed_anchor:
            out.append("\n\n" + anchor)
        for number, global_number in global_of.items():
            if number not in used:
                out[-1] += f"<!--?fnref {global_number}-->"
                stats["unplacedNoteRefs"] += 1
    # 书配置要的页码偏移：文件基本偏移（第一段），插页之后偏移变了的各段写成 pageRangeOffsets
    runs: list[list[int]] = []  # [印刷起, 印刷止, 偏移]
    for page in range(first, last + 1):
        number = printed.get(page)
        if not number:
            continue
        offset = page - number
        if runs and runs[-1][2] == offset and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number, offset])
    file_name = f"{name}.md"
    offsets = {"fileOffset": f"{file_name}|{runs[0][2]}" if runs else None,
               "pageRangeOffsets": [f"{file_name}|{a}-{b}|{o}" for a, b, o in runs[1:] if o != runs[0][2]],
               "conflicts": [[a, b] for i, (a, b, _) in enumerate(runs) for (c, d, _) in runs[i + 1:] if not (b < c or d < a)]}
    stats["offsets"] = offsets
    numbers = [printed.get(page) for page in range(first, last + 1)]
    numbers = [n for n in numbers if n]
    front = ["---", f'book: "{title}"', *[f"{key}: {value}" for key, value in (state.get("meta") or {}).items()],
             f'篇章: "{name.split("-", 1)[-1]}"', f"物理页: {first}-{last}",
             *([f"印刷页: {min(numbers)}-{max(numbers)}"] if numbers and not unnumbered else []),
             "来源: PaddleOCR-VL 逐页读（vl_build.py 整理），本书字形表改回原书字形", "---"]
    text = "\n".join(front) + "".join(out) + ("\n\n---\n\n## 注释\n\n" + "\n\n".join(notes) if notes else "") + "\n"
    return text, {**dict(stats), **({"suspects": suspects} if suspects else {})}


def heading_outline(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"^#{1,6} ", line)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("pages")
    p.add_argument("--vl-dir", type=Path, required=True)
    p.add_argument("--pages", required=True)
    p.add_argument("--report", type=Path, required=True)
    b = sub.add_parser("build")
    b.add_argument("--vl-dir", type=Path, required=True)
    b.add_argument("--pdf", type=Path, required=True, help="原 PDF：在页面图上找页下注的分隔线")
    b.add_argument("--pages-report", type=Path, help="已核验印刷页码报告；省略时只标 PDF 物理页")
    b.add_argument("--segment", action="append", required=True, help="起-止:文件名，如 99-497:02-正文.md")
    b.add_argument("--variant-equivalent", action="append", default=[], help="本书字形表，如 黄黃（第一个是原书字形）")
    b.add_argument("--writing-mode", choices=("horizontal-tb", "vertical-rl"), default="horizontal-tb")
    b.add_argument("--out-dir", type=Path, required=True)
    b.add_argument("--title", default="")
    b.add_argument("--meta", action="append", default=[], help="写进每个文件 frontmatter 的书目信息，如 作者=黄道周")
    b.add_argument("--report", type=Path, default=None,
                   help="整理报告路径；默认 <书>/_数据/过程报告/_vl_build_report.json（--out-dir 在 _数据 下时自动找回书文件夹）")
    b.add_argument("--item-numbers", action="store_true",
                   help="本书有条首阿拉伯条目号（编年体点校本）：套 <sup class=\"item\"> 标出。默认关；报告会提示要不要开")
    b.add_argument("--legacy-heuristics", action="store_true", help="启用旧版结构推断；仍不自动改卷号或删疑似编造正文")
    b.add_argument("--no-small", action="store_true", help="不按字号标 <small> 小字（误标太多时用；占比过高时也会自动停用）")
    args = parser.parse_args()

    if args.action == "pages":
        first, _, last = args.pages.partition("-")
        pages = load_pages(args.vl_dir, int(first), int(last or first))
        printed, runs = infer_printed(pages)
        # 推不出页码的页，写明原因：没读到页码块、读到好几个、还是读到了但与前后页对不上
        unknown = {}
        for page, value in printed.items():
            if value is not None:
                continue
            numbers = [b.get("block_content") for b in pages[page]["parsing_res_list"] if b.get("block_label") == "number"]
            parsed = [chinese_number(n) for n in numbers]
            if not numbers:
                unknown[str(page)] = "没有页码块（篇首页、插图页常不印页码）"
            elif not any(parsed):
                unknown[str(page)] = f"页码块读不成数字：{numbers}"
            elif len(numbers) > 1:
                unknown[str(page)] = f"页码块不止一个：{numbers}"
            else:
                unknown[str(page)] = f"读到 {numbers[0]}，与前后页的偏移对不上（误读或插页边界）"
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"printed": {str(k): v for k, v in printed.items()}, "runs": runs,
                                           "unknownReasons": unknown},
                                          ensure_ascii=False, indent=1), encoding="utf-8", errors="strict")
        print(f"VL_PAGES pages={len(pages)} runs={len(runs)} unknown={sum(1 for v in printed.values() if v is None)}")
        return

    report = json.loads(args.pages_report.read_text(encoding="utf-8", errors="strict")) if args.pages_report else {}
    printed = {int(k): v for k, v in report.get("printed", {}).items()}
    table: dict[str, str] = {}
    for group in args.variant_equivalent:
        chars = [c for c in group if not c.isspace()]
        for ch in chars[1:]:
            table[ch] = chars[0]
    segments = []
    for value in args.segment:
        span, _, name = value.partition(":")
        first, _, last = span.partition("-")
        segments.append((int(first), int(last or first), name))
    pages = load_pages(args.vl_dir, min(s[0] for s in segments), max(s[1] for s in segments))
    import pymupdf

    document = pymupdf.open(args.pdf)
    heads = running_heads(pages)
    prefixes = head_prefixes(pages)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    state = new_state(args.out_dir / "images")
    state["conservative"] = not args.legacy_heuristics
    state["meta"] = dict(item.split("=", 1) for item in args.meta)
    state["prefixes"] = prefixes
    state["itemNumbers"] = args.item_numbers
    summary = {"_runningHeads": sorted(heads), "_headPrefixes": sorted(prefixes),
               "_sourcePageLabels": {str(n): [b.get("block_content", "") for b in d["parsing_res_list"] if b.get("block_label") in {"number", "page_number"}] for n,d in pages.items()},
               "_pageLabelPolicy": "PDF physical anchors always; printed labels only from an explicitly supplied report"}
    warnings: list[str] = []
    vertical = args.writing_mode == "vertical-rl"
    for first, last, name in segments:
        snapshot = json.loads(json.dumps({k: v for k, v in state.items() if k not in ("imagesDir", "prefixes")}))
        state["small"] = not args.no_small
        text, stats = build(pages, printed, (first, last), vertical, table, args.title, Path(name).stem, document, heads, state)
        paragraphs = stats.get("paragraphs", 0) or 1
        if state["small"] and stats.get("smallParagraphs", 0) > 0.15 * paragraphs:
            # 小字占到一成半以上，多半是按字号误标（《明通鉴》352 处大多是正文字号）：这一段不标小字重来
            warnings.append(f"{name}：按字号认出的小字段 {stats['smallParagraphs']}/{paragraphs}，占比过高，判为误标，本段已停用 <small>；"
                            "原书真有成段小字的，核原图后手工标")
            state.update({k: v for k, v in snapshot.items()})
            state["small"] = False
            text, stats = build(pages, printed, (first, last), vertical, table, args.title, Path(name).stem, document, heads, state)
            stats["smallDisabled"] = True
            paragraphs = stats.get("paragraphs", 0) or 1
        elif state["small"] and stats.get("smallParagraphs", 0) > 0.05 * paragraphs:
            warnings.append(f"{name}：小字段 {stats['smallParagraphs']}/{paragraphs}。按字号认小字会误标（短段列宽量不准），"
                            "抽 5 处看原图；多数不是小字就加 --no-small 重跑")
        (args.out_dir / name).write_text(text, encoding="utf-8", errors="strict")
        summary[name] = {**stats, "outline": heading_outline(text)}
        items = stats.get("itemNumberParas", 0)
        if items > 0.3 * paragraphs and not args.item_numbers:
            warnings.append(f"{name}：{items}/{paragraphs} 段以阿拉伯数字开头，本书疑似带条目号（编年体点校本）。"
                            "问用户要不要用 --item-numbers 标出；不开则照原样保留")
        suspects = stats.get("suspectBlocks", 0)
        if suspects > max(5, 0.01 * paragraphs):
            warnings.append(f"{name}：{suspects} 块被标为疑似编造、原文保留待核，超过正文段数的 1%。先抽几块看原图，"
                            "若是真正文被误判，停下来报告，不要交付")
    summary["_volumeFixes"] = state["volumeFixes"]
    summary["_volumeCandidates"] = state.get("volumeCandidates", [])
    summary["_conservative"] = state["conservative"]
    summary["_images"] = state["images"]
    summary["_headsRemoved"] = state.get("headLog", [])
    summary["_ruleWithoutNotes"] = state.get("ruleWithoutNotes", [])
    summary["_itemNumbers"] = state.get("itemLog", [])
    summary["_warnings"] = warnings
    report = args.report
    if report is None:
        parts = args.out_dir.resolve().parts
        root = Path(*parts[: len(parts) - 1 - parts[::-1].index("_数据")]) if "_数据" in parts else args.out_dir
        report = root / "_数据" / "过程报告" / "_vl_build_report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8", errors="strict")
    print("VL_BUILD " + " ".join(f"{k.encode('ascii', 'backslashreplace').decode()}={v.get('paragraphs', 0)}p/{v.get('notes', 0)}n/{v.get('suspectBlocks', 0)}s"
                                for k, v in summary.items() if not k.startswith("_")) + f" warnings={len(warnings)}")


if __name__ == "__main__":
    main()
