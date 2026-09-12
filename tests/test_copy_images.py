from pathlib import Path
import importlib.util
import json
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "copy_images.py"
SPEC = importlib.util.spec_from_file_location("copy_images", SCRIPT)
COPY_IMAGES = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = COPY_IMAGES
SPEC.loader.exec_module(COPY_IMAGES)


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="strict")


def test_copies_only_referenced_images_and_reports_orphans(tmp_path: Path) -> None:
    source = tmp_path / "mineru"
    write(source / "images" / "a.png", b"A")
    write(source / "images" / "b.jpg", b"B")
    write(source / "images" / "orphan.png", b"O")
    write(source / "images" / "same.png", b"S")
    target = tmp_path / "book"
    write(target / "01-正文.md", "![](images/a.png)\n\n![[b.jpg|300]]\n\n<img src=\"images/same.png\">\n".encode("utf-8"))
    write(target / "images" / "same.png", b"S")
    report_path = tmp_path / "report.json"
    result = run(str(source), str(target), "--report", str(report_path))
    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["copied"] == ["a.png", "b.jpg"]
    assert report["kept"] == ["same.png"]
    assert report["orphansNotCopied"] == ["orphan.png"]
    assert (target / "images" / "a.png").read_bytes() == b"A"
    assert not (target / "images" / "orphan.png").exists()


def test_conflicts_and_missing_do_not_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "mineru"
    write(source / "images" / "a.png", b"NEW")
    target = tmp_path / "book"
    write(target / "01-正文.md", "![](images/a.png) ![](images/gone.png)".encode("utf-8"))
    write(target / "images" / "a.png", b"OLD")
    result = run(str(source), str(target), "--dry-run")
    assert result.returncode == 3
    assert "conflicts=1 missing=1" in result.stdout
    assert (target / "images" / "a.png").read_bytes() == b"OLD"


def test_reference_shapes_and_extensions(tmp_path: Path) -> None:
    text = "\n\n".join([
        '![标题](images/title.png "图一")',
        "![图 1] 说明](images/bracket.jpeg)",
        "![](<images/有 空格.png>)",
        "![](images/%E7%99%BE%20%E5%88%86.png)",
        "![[nested/deep.tif]]",
        "<img src=images/unquoted.avif width=200>",
        "<img src='images/single.svg'>",
        "[ref]: images/reference.tiff",
        "![](images/vector.svg)",
        "[^1]: 脚注正文，不是图片",
        "[普通链接](https://example.com/page.html)",
    ])
    target = tmp_path / "book"
    write(target / "01-正文.md", text.encode("utf-8"))

    assert COPY_IMAGES.referenced_names(str(target)) == {
        "title.png", "bracket.jpeg", "有 空格.png", "百 分.png", "deep.tif",
        "unquoted.avif", "single.svg", "reference.tiff", "vector.svg",
    }


def test_merges_every_images_dir_of_a_split_book(tmp_path: Path) -> None:
    source = tmp_path / "mineru"
    write(source / "part-01" / "images" / "a.png", b"A")
    write(source / "part-02" / "images" / "b.png", b"B")
    target = tmp_path / "book"
    write(target / "01-正文.md", "![](images/a.png)\n\n![](images/b.png)".encode("utf-8"))
    report_path = tmp_path / "report.json"

    result = run(str(source), str(target), "--report", str(report_path))

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(report["imagesDirs"]) == 2
    assert report["copied"] == ["a.png", "b.png"]
    assert (target / "images" / "b.png").read_bytes() == b"B"


def test_same_name_different_bytes_across_parts_is_reported_not_guessed(tmp_path: Path) -> None:
    source = tmp_path / "mineru"
    write(source / "part-01" / "images" / "clash.png", b"ONE")
    write(source / "part-02" / "images" / "clash.png", b"TWO")
    write(source / "part-01" / "images" / "agree.png", b"SAME")
    write(source / "part-02" / "images" / "agree.png", b"SAME")
    target = tmp_path / "book"
    write(target / "01-正文.md", "![](images/clash.png)\n\n![](images/agree.png)".encode("utf-8"))
    report_path = tmp_path / "report.json"

    result = run(str(source), str(target), "--report", str(report_path))

    assert result.returncode == 3
    assert "dups=1" in result.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert [entry["name"] for entry in report["sourceDuplicates"]] == ["clash.png"]
    assert report["copied"] == ["agree.png"]
    assert not (target / "images" / "clash.png").exists()
