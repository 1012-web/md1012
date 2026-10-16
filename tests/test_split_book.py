from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

BOOK = """---
title: 测试书
year: 2011
---

# 书名与出版信息

测试书，某出版社。

# 目录

第一章 …… 1

<!--p.1-->

# 第一章 甲

甲章正文[^1]，又一处[^2]。

<!--p.2-->

甲章第二页[^1]。

# 第二章 乙

<!--p.3-->

乙章正文[^3]。

# 后记

后记里也有注[^4]。

---

## 注释

[^1]: <!--p.1--> 注一

[^2]: <!--p.1--> 注二

[^3]: <!--p.3--> 注三

[^4]: 注四
"""


def run(tmp_path: Path, *extra: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "骨架.md"
    source.write_text(BOOK, "utf-8")
    out = tmp_path / "书"
    result = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPTS / "split_book.py"), str(source), "-o", str(out),
                             "--meta", "book=测试书", *extra], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return out


def test_body_is_one_file_with_continuous_notes_at_each_chapter_end(tmp_path: Path) -> None:
    out = run(tmp_path)
    names = sorted(p.name for p in out.glob("*.md"))
    assert names == ["01-书名与出版信息.md", "02-目录.md", "03-正文.md", "04-后记.md", "测试书.md"]
    body = (out / "03-正文.md").read_text("utf-8")
    # 全书连续编号：乙章的注接着甲章编，是 3 不是 1
    assert "甲章正文[^1]，又一处[^2]" in body and "甲章第二页[^1]" in body and "乙章正文[^3]" in body
    first, second = body.split("# 第二章 乙")
    assert "## 注释" in first and "[^1]: <!--p.1--> 注一" in first and "[^2]: <!--p.1--> 注二" in first
    assert "[^3]: <!--p.3--> 注三" in second and "[^1]:" not in second
    # 书末另存，注重新从 1 编
    assert "后记里也有注[^1]" in (out / "04-后记.md").read_text("utf-8")
    # 过程报告不混进书文件夹顶层
    assert not (out / "_split_report.txt").exists() and (out / "_数据" / "过程报告" / "_split_report.txt").exists()


def test_chapter_metadata_records_each_chapter_title_and_first_page(tmp_path: Path) -> None:
    out = run(tmp_path, "--start-page", "1")
    body = (out / "03-正文.md").read_text("utf-8")
    front = re.match(r"^---\n(.*?)\n---\n", body, re.S).group(1)
    assert "footnotes: 全书正文连续编号；注释在各章末尾" in front
    entries = re.findall(r"title: (.+)\n(?:.*\n)*?\s+pages: (\d+)", front.split("chapter_metadata:")[1])
    assert ("第一章 甲", "1") in entries and ("第二章 乙", "2") in entries


def test_index_links_body_chapters_by_heading(tmp_path: Path) -> None:
    out = run(tmp_path)
    index = (out / "测试书.md").read_text("utf-8")
    assert "[[03-正文#第一章 甲\\|03-正文]]" in index
    assert "[[04-后记\\|04-后记]]" in index


def test_body_range_can_be_given_explicitly_and_split_stays_available(tmp_path: Path) -> None:
    out = run(tmp_path, "--body", "3-5")  # 把后记也算进正文
    assert sorted(p.name for p in out.glob("[0-9]*.md")) == ["01-书名与出版信息.md", "02-目录.md", "03-正文.md"]
    split = run(tmp_path / "拆", "--split")
    assert "03-第一章 甲.md" in {p.name for p in split.glob("*.md")}
