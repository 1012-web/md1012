import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_skeletons.py"


def test_volumes_are_joined_with_continuous_notes_and_seam(tmp_path: Path) -> None:
    one = tmp_path / "a.md"
    two = tmp_path / "b.md"
    one.write_text("<!--p.1-->\n\n甲段。[^1]\n\n乙段讲到一半[^2]，没有\n\n---\n\n## 注释\n\n[^1]: 注一\n\n[^2]: 注二\n",
                   encoding="utf-8")
    two.write_text("<!--p.3-->\n\n收尾的半句。[^1]\n\n丙段。\n\n---\n\n## 注释\n\n[^1]: 注三\n", encoding="utf-8")
    out = tmp_path / "whole.md"
    subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), str(one), str(two), "-o", str(out)], check=True)
    text = out.read_text(encoding="utf-8")
    assert "乙段讲到一半[^2]，没有<!--p.3-->收尾的半句。[^3]" in text
    assert "[^3]: 注三" in text and text.count("## 注释") == 1
