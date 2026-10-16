"""MinerU 产物读取的共用工具。

Windows 说明：这些脚本一律把详细报告写成 UTF-8 文件，stdout 只打印 ASCII 状态行。
原因是 Windows 控制台默认 GBK，中文报告直接 print 会变成乱码，反而看不了。
详细内容请用 Read 工具读生成的文件。
"""
import json
import os
import re
import sys
import glob

CIRC = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
CIRC_RE = re.compile('[' + CIRC + ']')

# 实义字符：比对时只看这些，标点/空格/Markdown 语法一律忽略
KEEP_RE = re.compile(r'[㐀-䶿一-鿿A-Za-z0-9' + CIRC + r'□〈〉〔〕]')

TEXTLIKE = ('text', 'ref_text', 'title', 'list', 'index')

# ---- 跨页断段判定：probe 报告和 build 接合必须用同一套判据，否则报告与成品对不上
# 收尾标点里带上 ：；——「其职责：」后面换页跟一串列举，那是新块不是断句
TERMINAL = '。！？”』》）】…—：；'
OPENER = re.compile(r'^\s*(第[一二三四五六七八九十百]+[章节編编篇]|[一二三四五六七八九十]+[、．.]|'
                    r'[（(]\s*\d+\s*[)）]|\d+\s*[.．、]|附录|绪论|结语|参考|思考题|本章|学术)')
# 句末的脚注引用不是句末标记：「…近代化。[^11]」是正常段末，不是断句
TRAIL_REF = re.compile(r'(\[\^\d+\]\s*)+$')
ANCHOR_RE = re.compile(r'^<!--p\.(-?\d+)-->$')
# 图、图注、图下小字注：可能夹在断点中间，判定时要跨过去
FLOAT_PREFIX = ('![', '*', '<small>')


def is_break(prev, nxt):
    """prev 与 nxt 之间的换页是否切断了同一个段落。"""
    if not prev or not nxt:
        return False
    if prev.startswith(('#', '<!--') + FLOAT_PREFIX) or nxt.startswith(('#', '<!--') + FLOAT_PREFIX):
        return False
    tail = TRAIL_REF.sub('', prev).rstrip()
    # 目录行「后记/329」「第一章 溯源……9」以页码收尾、没有句末标点，但不是断句（曾把目录末行接进序论首段）
    if TOC_TAIL.search(tail):
        return False
    return bool(tail) and tail[-1] not in TERMINAL and not OPENER.match(nxt)


TOC_TAIL = re.compile(r'(?:[/／]|[…·．.]{2,})\s*\d{1,4}$')


def die(msg):
    print('ERROR: ' + msg)
    sys.exit(1)


def find_files(d):
    """定位 MinerU 输出目录里的各个产物。"""

    def one(pattern):
        hits = sorted(glob.glob(os.path.join(d, pattern)))
        if not hits:
            hits = sorted(glob.glob(os.path.join(d, '**', pattern), recursive=True))
        return hits[0] if hits else None

    images = os.path.join(d, 'images') if os.path.isdir(os.path.join(d, 'images')) else None
    if not images:
        nested_images = sorted(glob.glob(os.path.join(d, '**', 'images'), recursive=True))
        images = nested_images[0] if nested_images else None

    return {
        'layout': one('layout.json') or one('*_middle.json') or one('middle.json'),
        'block_list': one('block_list.json'),
        'full_md': one('full.md') or one('MinerU_markdown_*.md'),
        'content_list': one('*_content_list.json'),
        'content_list_v2': one('*_content_list_v2.json'),
        'model': one('*_model.json'),
        'origin_pdf': one('*_origin.pdf'),
        'images': images,
    }


def load_layout(d):
    f = find_files(d)['layout']
    if not f:
        die('找不到 layout.json / *_middle.json——它是正文的唯一可靠来源，没有它无法继续。')
    return json.load(open(f, encoding='utf-8'))['pdf_info']


def load_blocklist(d):
    """返回 (pages, merge_map)。merge_map: (page_idx, index) -> (page_idx, index)"""
    f = find_files(d)['block_list']
    if not f:
        return None, {}
    raw = json.load(open(f, encoding='utf-8'))
    pages = raw.get('pdfData', [])
    pos2key = {}
    for pi, pg in enumerate(pages):
        for b in pg:
            if 'block_position' in b:
                pos2key[b['block_position']] = (pi, b.get('index'))
    merge = {}
    for c in raw.get('mergeConnections', []):
        blocks = c.get('blocks', [])
        for a, b in zip(blocks, blocks[1:]):
            if a in pos2key and b in pos2key:
                merge[pos2key[a]] = pos2key[b]
    return pages, merge


def strip_tags(t):
    """MinerU 用 <sub>/<sup> 包裹部分标点和上下标，还原为普通字符。"""
    t = re.sub(r'<sub>(.*?)</sub>', r'\1', t)
    t = re.sub(r'<sup>(.*?)</sup>', r'\1', t)
    return t


def join_lines(lines):
    """按行拼接。只有两侧都是 ASCII 字母数字时才补空格。

    中文换行处不能加空格（会多出空格），但英文/数字换行处必须加，
    否则 '2020' + '2022' 会粘成 '20202022'——这正是 block_list.json 的通病。
    """
    out = ''
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if out and re.search(r'[0-9A-Za-z]$', out) and re.match(r'[0-9A-Za-z]', ln):
            out += ' '
        out += ln
    return out


def line_texts(block):
    """取出一个 layout 块的每行文字。

    注意 image/table 类块是嵌套的：它们有 `blocks`（image_body / image_caption）
    而不是直接的 `lines`。套用正文逻辑会静默漏掉图注。
    """
    out = []
    for ln in block.get('lines', []) or []:
        out.append(''.join(s.get('content') or '' for s in ln.get('spans', [])))
    for sub in block.get('blocks', []) or []:
        out.extend(line_texts(sub))
    return out


def block_text(block):
    return join_lines(line_texts(block))


def image_paths(block):
    """递归取出块里的图片路径。"""
    out = []
    for ln in block.get('lines', []) or []:
        for s in ln.get('spans', []):
            p = s.get('image_path') or s.get('img_path')
            if p:
                out.append(p.lstrip('/'))
    for sub in block.get('blocks', []) or []:
        out.extend(image_paths(sub))
    return out


def sub_blocks_by_type(block, wanted):
    out = []
    for sub in block.get('blocks', []) or []:
        if sub.get('type') in wanted:
            out.append(sub)
        out.extend(sub_blocks_by_type(sub, wanted))
    return out


def body_blocks(page):
    """一页里参与正文的块，按阅读顺序。"""
    return sorted(page.get('preproc_blocks', []), key=lambda b: b.get('index', 0))


def footnote_blocks(page):
    return [b for b in page.get('discarded_blocks', []) if b.get('type') == 'page_footnote']


def discarded_of_type(page, t):
    return [b for b in page.get('discarded_blocks', []) if b.get('type') == t]


def page_number_map(layout):
    """从 page_number 块推断每页的印刷页码。

    很多首页没有页码块，所以用「印刷页码 - page_idx」的众数当偏移量补齐。
    """
    seen = {}
    for pi, pg in enumerate(layout):
        for b in discarded_of_type(pg, 'page_number'):
            # layout.json 的 discarded 块没有 text 字段，只有 lines——必须回退到行级拼接
            m = re.search(r'\d+', strip_tags(b.get('text') or block_text(b)))
            if m:
                seen[pi] = int(m.group(0))
                break
    if not seen:
        return {pi: pi + 1 for pi in range(len(layout))}
    offsets = {}
    for pi, n in seen.items():
        offsets[n - pi] = offsets.get(n - pi, 0) + 1
    off = max(offsets, key=offsets.get)
    return {pi: seen.get(pi, pi + off) for pi in range(len(layout))}


def significant(s):
    """只保留实义字符，用于比对。"""
    return ''.join(KEEP_RE.findall(s))


NOTES_HEAD_RE = re.compile(r'^#{1,6}\s*注释\s*$')
HEAD_LINE_RE = re.compile(r'^#{1,6}\s+\S')


def split_notes(text):
    """把成品拆成 (正文, 注释)。注释区可能有多个：split_book.py 默认每章末尾一个「## 注释」，
    从它到下一个标题行之前都算注释；不能只按第一个「## 注释」一刀切，否则第一章以后的正文全被当成注释。"""
    body, notes = [], []
    in_notes = False
    for line in text.split('\n'):
        if NOTES_HEAD_RE.match(line.strip()):
            in_notes = True
            continue
        if in_notes and HEAD_LINE_RE.match(line):
            in_notes = False
        (notes if in_notes else body).append(line)
    # 注释区之前的「---」分隔线留在正文里也无妨（比对、计数都会剔掉）
    return '\n'.join(body), '\n'.join(notes)


def book_folder(path):
    """从书文件夹里任何位置（包括 _数据/过程报告/ 下面）找回书文件夹本身。"""
    folder = os.path.abspath(path)
    parts = folder.split(os.sep)
    if '_数据' in parts:
        folder = os.sep.join(parts[:len(parts) - 1 - parts[::-1].index('_数据')])
    return folder


def process_report(folder, name):
    """交付物之外的过程报告（校验、比对、整理报告）一律放进 <书文件夹>/_数据/过程报告/，不跟正文混在一起。
    folder 落在 _数据/ 里面时（如成品临时放在过程报告目录），回到书文件夹再放，不嵌套出 _数据/过程报告/_数据/…"""
    target = os.path.join(book_folder(folder), '_数据', '过程报告')
    if not os.path.isdir(target):
        os.makedirs(target)
    return os.path.join(target, name)


def report_path(mineru_dir, name):
    """probe.py、build.py 等对着 MinerU 输出目录跑的报告放哪：目录在书文件夹的 _数据/ 里面时，
    放进 <书>/_数据/过程报告/；拆卷的分卷（目录名带 pages-0001-0200）在文件名后加卷标，几卷不互相覆盖。
    目录不在书文件夹里（用户直接给的 MinerU 目录）时照旧写在目录里。"""
    folder = os.path.abspath(mineru_dir)
    if '_数据' not in folder.split(os.sep):
        return os.path.join(folder, name)
    vol = re.search(r'pages-\d+-\d+', os.path.basename(os.path.normpath(folder)))
    if vol:
        stem, ext = os.path.splitext(name)
        name = '%s_%s%s' % (stem, vol.group(0), ext)
    return process_report(folder, name)


def write_report(path, text):
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    print('report -> ' + ascii_path(os.path.basename(path)))


def ascii_path(path):
    """stdout 只打 ASCII：中文路径在 GBK 控制台上是乱码，换成 \\uXXXX 转义，至少看得出是哪个文件。"""
    return str(path).encode('ascii', 'backslashreplace').decode('ascii')


# 中文书里不该有的长段拉丁文：模型在空白页上「自言自语」（实测 MinerU 在空白页写出
# 「The Ground Truth image …」一整段）。连续 8 个以上英文单词、且前后是中文或段首段尾才算。
LATIN_RUN_RE = re.compile(r'(?:\b[A-Za-z][A-Za-z\'’-]*\b[ ,.;:]+){8,}')


def mostly_chinese(text):
    """全书是不是以中文为主（汉字多于拉丁字母）：外文书不查长段拉丁文。"""
    return len(re.findall(r'[一-鿿]', text)) > len(re.findall(r'[A-Za-z]', text))


def latin_runs(text):
    """文本里的长段拉丁文片段。调用方先用 mostly_chinese 判断全书是中文书再查。"""
    return [m.group(0).strip() for m in LATIN_RUN_RE.finditer(text or '')]
