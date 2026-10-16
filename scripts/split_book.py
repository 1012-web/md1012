"""把论文或整书骨架整理成交付文件：默认正文合成一份，卷首与书末另存；另出一份索引。

用法:
    python split_book.py <材料.md> -o <书文件夹> [--meta book=中国近现代史 --meta year=2009]
                         [--level 1|2] [--start-page 1] [--body 3-14] [--no-index] [--index-name 索引.md]
                         [--split]

默认（合并）:
    图书：正文各章合成一份 NN-正文.md；正文之前的篇章（书名页、目录、前言、总序……）与之后的
    （后记、附录、郑重声明……）各自一个文件。哪些算正文：默认从第一个到最后一个「第X章/编/篇/部分、
    绪论、导论、结束语……」标题；认错时用 --body 起-止（按本脚本列出的篇章序号）指定。
    正文脚注全书连续编号，注释放在各章末尾（「---」+「## 注释」），检索到某段时注就在同一章里。
    正文 frontmatter 的 chapter_metadata 记各章标题与印刷页：章首页的页码锚点在拼接时会落在上一章，
    靠它才知道每章从哪一页起。
    论文（--level 2）：整篇一份文件，注释在文末。

--split（只在用户要求按章拆分时用）:
    每个篇章一个文件，脚注按篇章重编号。

不要再叠一份全文/全书：同一段文字存两份，检索时同时命中、互相挤占名额，两份还会随编辑漂移。

输入要求:
    - 可有可无的 YAML frontmatter（其中的字段会被每一章继承）
    - 图书通常用 `# ` 一级标题分章；论文通常用 `## ` 二级标题分节，并传 --level 2
    - 文末的注释区形如 `\\n\\n---\\n\\n## 注释\\n\\n` 后跟 `[^n]: <!--p.N--> 正文`
"""
import argparse
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import die, process_report, write_report

SPLIT_RE = re.compile(r'\n+---\n+#+\s*注释\s*\n+')
NOTE_RE = re.compile(r'^\[\^(\d+)\]:\s*(?:<!--p\.(-?\d+)-->)?\s*(.*)$', re.S)
ANCHOR = re.compile(r'<!--p\.(-?\d+)-->')
FM_RE = re.compile(r'^---\n(.*?)\n---\n', re.S)
BAD = '\\/:*?"<>|'


def safe(name):
    for c in BAD:
        name = name.replace(c, '／' if c == '/' else '-')
    return name.strip()


def parse(path):
    text = io.open(path, encoding='utf-8', errors='strict').read()
    fm = ''
    m = FM_RE.match(text)
    if m:
        fm = m.group(1)
        text = text[m.end():]
    parts = SPLIT_RE.split(text, maxsplit=1)
    body = parts[0].strip()
    notes = {}
    if len(parts) > 1:
        for chunk in parts[1].strip().split('\n\n'):
            chunk = chunk.strip()
            if not chunk:
                continue
            mm = NOTE_RE.match(chunk)
            if not mm:
                die('脚注解析失败，这一条不是 `[^n]: 正文` 的形态:\n' + chunk[:80])
            notes[int(mm.group(1))] = (mm.group(2), mm.group(3).strip())
    return fm, body, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('md')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--meta', action='append', default=[],
                    help='附加到每章 frontmatter 的字段，形如 book=中国近现代史，可重复')
    ap.add_argument('--start-page', default=None,
                    help='第一章起始印刷页（首个页码锚点常在章标题之前，切分时已被留在卷首）')
    ap.add_argument('--level', type=int, choices=(1, 2), default=1,
                    help='按第几级标题切分：1=一级标题（默认，常用于图书），2=二级标题（常用于论文）')
    ap.add_argument('--index-name', default=None, help='索引文件名，默认取 --meta 里的 book')
    ap.add_argument('--no-index', action='store_true')
    ap.add_argument('--body', default=None,
                    help='合并时哪几个篇章算正文，形如 3-14（本脚本报告里的篇章序号）；默认按标题自动认')
    ap.add_argument('--split', action='store_true', help='按篇章拆成多个文件（旧做法，只在用户要求时用）')
    a = ap.parse_args()

    if not os.path.isdir(a.out):
        os.makedirs(a.out)
    extra = []
    for kv in a.meta:
        if '=' not in kv:
            die('--meta 要写成 键=值: ' + kv)
        extra.append(tuple(kv.split('=', 1)))
    meta = dict(extra)

    fm, body, notes = parse(a.md)
    blocks = body.split('\n\n')
    heading_re = re.compile(r'^' + re.escape('#' * a.level) + r'\s+(.+)$')
    first_split = next((i for i, b in enumerate(blocks) if heading_re.match(b)), None)
    if first_split is None:
        die('正文没有可供切分的 %d 级标题。先确认 build.py 的层级重建结果，或改用 --level 1/2。' % a.level)

    chapters, cur = [], None
    page = a.start_page
    preface = blocks[:first_split]
    for b in preface:
        for m in ANCHOR.finditer(b):
            page = m.group(1)
    for b in blocks[first_split:]:
        match = heading_re.match(b)
        if match:
            cur = {'title': match.group(1).strip(), 'blocks': list(preface) if not chapters else [], 'start': page}
            chapters.append(cur)
        for m in ANCHOR.finditer(b):
            page = m.group(1)
        cur['blocks'].append(b)
        cur['end'] = page

    if not a.split:
        merged(a, fm, extra, meta, notes, chapters)
        return

    rows = []
    for i, c in enumerate(chapters, 1):
        used, remap = [], {}

        def sub(m):
            n = int(m.group(1))
            if n not in remap:
                used.append(n)
                remap[n] = len(used)
            return '[^%d]' % remap[n]

        text = re.sub(r'\[\^(\d+)\]', sub, '\n\n'.join(c['blocks']))
        missing = [n for n in used if n not in notes]
        if missing:
            die('第 %d 章引用了没有定义的脚注: %s' % (i, missing[:8]))

        pages = '-'.join(x for x in (c['start'], c['end']) if x) or ''
        head = ['---', 'title: %s' % c['title']]
        head += [ln for ln in fm.split('\n')
                 if ln.strip() and not re.match(r'^title\s*:', ln)]
        head += ['%s: %s' % kv for kv in extra]
        if pages:
            head.append('pages: %s' % pages)
        head += ['---', '']

        out = '\n'.join(head) + '\n' + text
        if used:
            out += '\n\n---\n\n## 注释\n\n' + '\n\n'.join(
                '[^%d]: %s%s' % (remap[n],
                                 ('<!--p.%s--> ' % notes[n][0]) if notes[n][0] else '',
                                 notes[n][1]) for n in used)
        fname = '%02d-%s.md' % (i, safe(c['title']))
        io.open(os.path.join(a.out, fname), 'w', encoding='utf-8', errors='strict').write(out.rstrip() + '\n')
        rows.append((i, c['title'], fname, pages, len(used)))

    used_all = sum(r[4] for r in rows)

    if not a.no_index:
        name = a.index_name or (safe(meta.get('book', '')) + '.md' if meta.get('book') else '索引.md')
        L = ['---']
        L += ['%s: %s' % kv for kv in extra]
        L += ['---', '', '# %s' % meta.get('book', os.path.basename(a.md)[:-3]), '',
              '| 篇章 | 文件 | 印刷页 | 脚注 |', '|---|---|---|---|']
        for i, title, fname, pages, n in rows:
            L.append('| %s | [[%s]] | %s | %d |' % (title, fname[:-3], pages or '—', n))
        L += ['', '共 %d 篇章，%d 条脚注。' % (len(rows), used_all)]
        io.open(os.path.join(a.out, name), 'w', encoding='utf-8', errors='strict').write('\n'.join(L) + '\n')

    R = ['# 分章报告\n\n输出目录: %s\n\n' % os.path.abspath(a.out)]
    R.append('%-4s %-46s %-12s %s\n' % ('#', '标题', '印刷页', '脚注'))
    for i, title, fname, pages, n in rows:
        R.append('%-4d %-46s %-12s %d\n' % (i, title[:46], pages or '—', n))
    R.append('\n共 %d 篇章；脚注 源 %d 条 -> 分配 %d 条\n' % (len(rows), len(notes), used_all))
    if used_all != len(notes):
        R.append('\n** 有 %d 条脚注没有被任何一章引用。回到 verify.py / diff_report.py 查清楚。 **\n'
                 % (len(notes) - used_all))
    R.append('\n下一步: 对每个成品跑 verify.py，再跑 diff_report.py 出改动报告。\n')
    write_report(process_report(a.out, '_split_report.txt'), ''.join(R))
    print('chapters=%d notes_src=%d notes_placed=%d' % (len(rows), len(notes), used_all))


BODY_TITLE = re.compile(r'^(第[一二三四五六七八九十百零〇两\d]+[章编篇部讲]|绪论|导论|引论|导言|结束语|结语|余论|尾论)')


def page_range(c):
    return '-'.join(x for x in (c['start'], c['end']) if x) or ''


def note_line(number, note):
    page, text = note
    return '[^%d]: %s%s' % (number, ('<!--p.%s--> ' % page) if page else '', text)


def renumber(text, notes, counter, where):
    """把 text 里的 [^n] 按首次出现顺序接着 counter 编号；返回新文本与本段用到的注（新号, 旧注）。"""
    used = []
    remap = {}

    def sub(m):
        n = int(m.group(1))
        if n not in remap:
            counter[0] += 1
            remap[n] = counter[0]
            used.append(n)
        return '[^%d]' % remap[n]

    out = re.sub(r'\[\^(\d+)\]', sub, text)
    missing = [n for n in used if n not in notes]
    if missing:
        die('%s引用了没有定义的脚注: %s' % (where, missing[:8]))
    return out, [(remap[n], notes[n]) for n in used]


def frontmatter(title, fm, extra, pages, more=()):
    head = ['---', 'title: %s' % title]
    head += [ln for ln in fm.split('\n') if ln.strip() and not re.match(r'^title\s*:', ln)]
    head += ['%s: %s' % kv for kv in extra]
    if pages:
        head.append('pages: %s' % pages)
    head += list(more)
    head += ['---', '']
    return '\n'.join(head) + '\n'


def merged(a, fm, extra, meta, notes, chapters):
    book = meta.get('book') or os.path.basename(a.md)[:-3]
    if a.level == 2:
        # 论文：整篇一份，注释在文末
        counter = [0]
        text, used = renumber('\n\n'.join(b for c in chapters for b in c['blocks']), notes, counter, '全文')
        pages = '-'.join(x for x in (chapters[0]['start'], chapters[-1]['end']) if x)
        out = frontmatter(book, fm, extra, pages) + text
        if used:
            out += '\n\n---\n\n## 注释\n\n' + '\n\n'.join(note_line(n, note) for n, note in used)
        fname = safe(book) + '.md'
        io.open(os.path.join(a.out, fname), 'w', encoding='utf-8', errors='strict').write(out.rstrip() + '\n')
        R = ['# 整理报告\n\n输出: %s\n\n论文一份，%d 节，脚注 源 %d 条 -> 用到 %d 条\n'
             % (os.path.abspath(os.path.join(a.out, fname)), len(chapters), len(notes), len(used))]
        if len(used) != len(notes):
            R.append('\n** 有 %d 条脚注没有被引用。回到 verify.py / diff_report.py 查清楚。 **\n' % (len(notes) - len(used)))
        write_report(process_report(a.out, '_split_report.txt'), ''.join(R))
        print('paper=1 notes_src=%d notes_placed=%d' % (len(notes), len(used)))
        return

    if a.body:
        m = re.match(r'^(\d+)-(\d+)$', a.body)
        if not m or not 1 <= int(m.group(1)) <= int(m.group(2)) <= len(chapters):
            die('--body 要写成 起-止，且在 1-%d 之内: %s' % (len(chapters), a.body))
        first, last = int(m.group(1)) - 1, int(m.group(2)) - 1
    else:
        hits = [i for i, c in enumerate(chapters) if BODY_TITLE.match(c['title'])]
        if not hits:
            die('认不出哪些篇章是正文（没有「第X章」之类的标题）。看 --split 报告里的篇章序号，用 --body 起-止 指定。')
        first, last = hits[0], hits[-1]

    rows = []  # (序号, 篇章标题, 文件名, 链接目标, 印刷页, 脚注数)
    number = 0

    def write(fname, content):
        io.open(os.path.join(a.out, fname), 'w', encoding='utf-8', errors='strict').write(content.rstrip() + '\n')

    def single(c):
        nonlocal number
        number += 1
        text, used = renumber('\n\n'.join(c['blocks']), notes, [0], '「%s」' % c['title'])
        out = frontmatter(c['title'], fm, extra, page_range(c)) + text
        if used:
            out += '\n\n---\n\n## 注释\n\n' + '\n\n'.join(note_line(n, note) for n, note in used)
        fname = '%02d-%s.md' % (number, safe(c['title']))
        write(fname, out)
        rows.append((c['title'], fname, fname[:-3], page_range(c), len(used)))

    for c in chapters[:first]:
        single(c)

    number += 1
    body_name = '%02d-正文.md' % number
    counter = [0]
    parts = []
    body_rows = []
    for c in chapters[first:last + 1]:
        text, used = renumber('\n\n'.join(c['blocks']), notes, counter, '「%s」' % c['title'])
        if used:
            text += '\n\n---\n\n## 注释\n\n' + '\n\n'.join(note_line(n, note) for n, note in used)
        parts.append(text)
        body_rows.append((c['title'], body_name, '%s#%s' % (body_name[:-3], c['title']), page_range(c), len(used)))
    body_chapters = chapters[first:last + 1]
    metadata = ['chapter_metadata:']
    for c in body_chapters:
        metadata.append('- title: %s' % c['title'])
        if page_range(c):
            metadata.append('  pages: %s' % page_range(c))
    pages = '-'.join(x for x in (body_chapters[0]['start'], body_chapters[-1]['end']) if x)
    more = ['footnotes: 全书正文连续编号；注释在各章末尾'] + metadata
    write(body_name, frontmatter('%s · 正文' % book, fm, extra, pages, more) + '\n\n'.join(parts))
    rows.extend(body_rows)

    for c in chapters[last + 1:]:
        single(c)

    used_all = sum(r[4] for r in rows)
    if not a.no_index:
        name = a.index_name or safe(book) + '.md'
        L = ['---']
        L += ['%s: %s' % kv for kv in extra]
        L += ['---', '', '# %s' % book, '',
              '> 正文 %d 章合为一份 [[%s]]；卷首与书末另存。' % (len(body_chapters), body_name[:-3]), '',
              '| 篇章 | 文件 | 印刷页 | 脚注 |', '|---|---|---|---|']
        for title, fname, target, pages, n in rows:
            L.append('| %s | [[%s\\|%s]] | %s | %d |' % (title, target, fname[:-3], pages or '—', n))
        L += ['', '共 %d 篇章（正文 %d 章），%d 条脚注。' % (len(rows), len(body_chapters), used_all)]
        write(name, '\n'.join(L))

    R = ['# 整理报告\n\n书文件夹: %s\n\n' % os.path.abspath(a.out)]
    R.append('%-4s %-46s %-14s %-12s %s\n' % ('#', '篇章', '文件', '印刷页', '脚注'))
    for i, (title, fname, _, pages, n) in enumerate(rows, 1):
        R.append('%-4d %-46s %-14s %-12s %d\n' % (i, title[:46], fname[:14], pages or '—', n))
    R.append('\n正文：第 %d—%d 篇（%s）；如认错，用 --body 起-止 重跑。\n'
             % (first + 1, last + 1, '手动指定' if a.body else '按标题自动认'))
    R.append('\n共 %d 篇章；脚注 源 %d 条 -> 分配 %d 条\n' % (len(rows), len(notes), used_all))
    if used_all != len(notes):
        R.append('\n** 有 %d 条脚注没有被任何篇章引用。回到 verify.py / diff_report.py 查清楚。 **\n'
                 % (len(notes) - used_all))
    R.append('\n下一步: 对每个成品跑 verify.py，再跑 diff_report.py 出改动报告。\n')
    write_report(process_report(a.out, '_split_report.txt'), ''.join(R))
    print('files=%d body_chapters=%d notes_src=%d notes_placed=%d' % (
        len({r[1] for r in rows}), len(body_chapters), len(notes), used_all))


if __name__ == '__main__':
    main()
