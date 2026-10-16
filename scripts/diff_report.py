"""交付前必跑：逐字符比对源文本与成品，列出所有实质改动。

用法:
    python diff_report.py <mineru输出目录> <成品.md>
    python diff_report.py d1,d2,d3 "书/*.md"          # 一本书被切成几段解析、成品又分了章
    python diff_report.py --before "<书>/_数据/过程报告/vl稿/*.md" "书/0*.md"
                                                       # 竖排书（VL 底稿）：拿 vl_build.py 整理出的原稿比成品

竖排书没有 MinerU 底稿。VL → 原稿这一步的机械改动（拦下的编造块、删掉的页眉书口、字形表替换）
由 vl_build.py 记进 _vl_build_report.json；原稿 → 成品之间的一切改动由本脚本 --before 报出。
两份报告合起来，就是成品相对 VL 读出的文字改了什么。

剔除标点、空格、Markdown 语法后仍存在的差异 = 实质改动。
补标点不会出现在结果里；动了字一定会。

多目录/多文件时两侧各自按给定顺序拼接后比对——顺序必须与原书一致，
否则整篇会被判成「删了一大段又增了一大段」。

改动报告必须由这个脚本生成，不能靠回忆——凭印象列清单一定会漏，
而漏掉的往往正是最该报告的那条。
"""
import argparse
import difflib
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (CIRC, CIRC_RE, ascii_path, block_text, body_blocks, die, footnote_blocks,
                     join_lines, line_texts, load_layout, process_report, significant, split_notes,
                     strip_tags, write_report)

META_PREFIX = ('收稿日期', '作者简介', '基金项目', '通信作者', '引用格式', '作者单位', 'DOI')
CIRC_SET = set(CIRC)


def md_sections(text):
    """把成品拆成 (正文, 注释)，并去掉不属于原文的部分。"""
    text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.S)            # frontmatter
    # 注释区可能每章一个：只按第一个「## 注释」切，会把第一章以后的正文全当成注释来比
    body, notes = split_notes(text)
    # AI 校注 callout 是我们加的，不参与比对
    body = re.sub(r'>\s*\[!\w+\][^\n]*AI\s*校注[^\n]*\n(?:>[^\n]*\n?)*', '', body)
    notes = re.sub(r'>\s*\[!\w+\][^\n]*AI\s*校注[^\n]*\n(?:>[^\n]*\n?)*', '', notes)

    def clean(s):
        s = re.sub(r'<!--.*?-->', '', s, flags=re.S)                     # 页码锚点等注释
        s = re.sub(r'</?[A-Za-z][^<>]*>', '', s)                         # <small>、<sup class="item"> 等标签，字留下
        s = re.sub(r'\[\^\d+\]:?', '', s)                                # 脚注引用/定义标签
        s = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)                       # 图片
        s = re.sub(r'>\s*\[!\w+\]\s*', '', s)                            # callout 标记（保留标题文字）
        s = re.sub(r'^\s*[>#]+\s*', '', s, flags=re.M)                   # 引用/标题前缀
        return s.replace('**', '').replace('---', '')

    return clean(body), clean(notes)


def source_sections(layout):
    body = ''.join(strip_tags(block_text(b)) for pg in layout for b in body_blocks(pg))
    notes = []
    for pg in layout:
        for b in footnote_blocks(pg):
            t = strip_tags(b.get('text') or join_lines(line_texts(b))).strip()
            if not t or t.startswith(META_PREFIX):
                continue
            notes.append(CIRC_RE.sub('', t, count=1) if CIRC_RE.match(t) else t)
    return body, ''.join(notes)


def cut(s, n):
    """整段卷首/目录之类的大块差异会长到上万字符，原样印出来没人读得下去。"""
    return s if len(s) <= n else '%s……〔中略 %d 字〕……%s' % (s[:n // 2], len(s) - n, s[-n // 2:])


def compare(name, src, out, R, maxlen=160):
    a, b = significant(src), significant(out)
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        s, t = a[i1:i2], b[j1:j2]
        # 圈码被换成 [^n]（标签已在 clean 里去掉），属于预期转换，不算改动
        if tag == 'delete' and s and set(s) <= CIRC_SET:
            continue
        rows.append((tag, s, t, a[max(0, i1 - 22):i1], a[i2:i2 + 22]))

    R.append('\n## %s\n' % name)
    R.append('源 %d 实义字符 / 成品 %d，差异 %d 处\n' % (len(a), len(b), len(rows)))
    if not rows:
        R.append('-> 零改动。\n')
        return 0
    moved = sum(1 for tag, s, t, _, _ in rows if s and s in out) \
        + sum(1 for tag, s, t, _, _ in rows if t and t in src)
    if moved:
        R.append('其中 %d 处的文字在对侧仍然存在——多半是位置迁移（图注随段落接合而移位、\n'
                 '元信息搬进 frontmatter），不是内容增删。核对时先把这类配对消掉。\n' % moved)
    for tag, s, t, L, Rr in rows:
        label = {'replace': '改', 'insert': '增', 'delete': '删'}.get(tag, tag)
        R.append('\n[%s] 源「%s」-> 成品「%s」\n' % (label, cut(s, maxlen), cut(t, maxlen)))
        R.append('     …%s▶%s◀%s…\n' % (L, cut(s, 60), Rr))
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='+', help='<MinerU输出目录> <成品.md>；用 --before 时只给 <成品.md>')
    ap.add_argument('--before', default=None,
                    help='拿这几份 md（逗号分隔或通配符，按原书顺序）当源，代替 MinerU 目录：竖排书用 vl_build.py 原稿')
    ap.add_argument('--max-diff-chars', type=int, default=160, help='单条差异的打印上限')
    a = ap.parse_args()
    if a.before:
        if len(a.paths) != 1:
            die('用 --before 时只给成品 md 一个参数')
        a.dir, a.md = None, a.paths[0]
    else:
        if len(a.paths) != 2:
            die('要给两个参数：<MinerU输出目录> <成品.md>（竖排书用 --before <原稿.md> <成品.md>）')
        a.dir, a.md = a.paths

    def expand(spec, what):
        files = []
        for pat in spec.split(','):
            hit = sorted(glob.glob(pat)) if any(c in pat for c in '*?[') else [pat]
            if not hit or not all(os.path.isfile(h) for h in hit):
                die('没有匹配到%s: %s' % (what, ascii_path(pat)))
            files += hit
        return files

    mds = expand(a.md, '成品文件')
    src_body, src_notes = '', ''
    if a.before:
        dirs = expand(a.before, '原稿文件')
        for f in dirs:
            b, n = md_sections(open(f, encoding='utf-8').read())
            src_body += '\n' + b
            src_notes += '\n' + n
    else:
        dirs = [x for x in a.dir.split(',') if x.strip()]
        for d in dirs:
            if not os.path.isdir(d):
                die('目录不存在: ' + ascii_path(d))
        for d in dirs:
            b, n = source_sections(load_layout(d))
            src_body += b
            src_notes += n
    md_body, md_notes = '', ''
    for f in mds:
        b, n = md_sections(open(f, encoding='utf-8').read())
        md_body += '\n' + b
        md_notes += '\n' + n

    R = ['# 改动报告\n']
    R.append('源: %s\n' % '\n     '.join(os.path.abspath(d) for d in dirs))
    R.append('成品: %s\n' % '\n       '.join(os.path.abspath(f) for f in mds))
    R.append('\n比对口径：剔除标点、空格、Markdown 语法后逐字符比对。\n')
    R.append('补标点不会出现在下面；动了字一定会出现。\n')
    if len(dirs) > 1 or len(mds) > 1:
        R.append('\n两侧各自按上面的顺序拼接后比对。卷首、版权页、目录、书末书签这些\n')
        R.append('没进成品的部分会整块出现在「删」里，那是预期的——确认一遍就行。\n')

    n1 = compare('正文', src_body, md_body, R, a.max_diff_chars)
    n2 = compare('脚注', src_notes, md_notes, R, a.max_diff_chars)

    R.append('\n## 怎么用这份报告\n')
    R.append('  逐条判断每处差异属于哪类，然后写进给用户的报告，按重要度排序：\n')
    R.append('    1 内容改字   —— 逐条写明位置、原文、改后、依据。最需要用户知道的。\n')
    R.append('    2 内容补入   —— 脚注、元数据等从 JSON 捡回来的。\n')
    R.append('    3 结构判定   —— 标题降级、引用块认定。\n')
    R.append('    4 标点格式   —— 概括一句即可。\n')
    R.append('    5 位置迁移   —— 如刊号移入 frontmatter，字没变只是搬家。\n')
    R.append('  「删」类差异多半是第 5 类（搬进了 frontmatter），别误报成删除内容。\n')
    R.append('  凡是动了字的，都要进文末的 AI 校注 callout；需要用户拍板的加 ⚠️。\n')

    write_report(process_report(os.path.dirname(os.path.abspath(a.md)), '_diff_report.txt'), ''.join(R))
    print('body_diffs=%d note_diffs=%d' % (n1, n2))


if __name__ == '__main__':
    main()
