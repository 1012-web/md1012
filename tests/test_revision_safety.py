from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from content_state import text_hash
from doubt_sheets import caption, crops


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8", errors="strict")


def run(script, *args):
    return subprocess.run([sys.executable, "-X", "utf8", str(SCRIPTS / script), *map(str, args)],
                          capture_output=True, encoding="utf-8", errors="strict")


def correction_fixture(root):
    body = root / "body.md"
    body.write_bytes("甲乙\r\n".encode("utf-8"))
    folder = root / "judged"
    folder.mkdir()
    original = body.read_text(encoding="utf-8", errors="strict")
    manifest = [dict(n=n, file="body.md", sourceHash=text_hash(original), reader="Kandian",
                     pdfPage=1, page="p.1", context="甲乙", current="", **{"from": pos, "to": pos})
                for n, pos in [(1, 1), (2, 0)]]
    judged = [dict(n=1, verdict="C", confidence="高", ocr="丙", correct="丙", reason="核图"),
              dict(n=2, verdict="C", confidence="低", ocr="丁", correct="丁", reason="待核")]
    save(folder / "manifest.json", manifest)
    save(folder / "judgement-b1.json", judged)
    return body, folder, judged


def test_insertions_are_idempotent_and_pending_decisions_keep_original_offsets(tmp_path):
    body, folder, judged = correction_fixture(tmp_path)
    args = ("--book", tmp_path, "--dir", folder, "--variants", "keep")
    assert run("apply_doubts.py", *args).returncode == 0
    assert body.read_bytes() == "甲丙乙\r\n".encode("utf-8")
    assert run("apply_doubts.py", *args).returncode == 0
    assert body.read_bytes() == "甲丙乙\r\n".encode("utf-8")
    report = json.loads((folder / "apply-report.json").read_text(encoding="utf-8", errors="strict"))
    assert len(report["already_applied"]) == 1 and not report["changed"]
    judged[1]["confidence"] = "高"
    save(folder / "judgement-b1.json", judged)
    assert run("apply_doubts.py", *args).returncode == 0
    assert body.read_bytes() == "丁甲丙乙\r\n".encode("utf-8")
    assert (folder / "backup/body.md").read_bytes() == "甲乙\r\n".encode("utf-8")


def test_external_changes_and_revised_applied_decisions_are_rejected(tmp_path):
    body, folder, judged = correction_fixture(tmp_path)
    args = ("--book", tmp_path, "--dir", folder, "--variants", "keep")
    assert run("apply_doubts.py", *args).returncode == 0
    judged[0]["correct"] = "戊"
    save(folder / "judgement-b1.json", judged)
    assert run("apply_doubts.py", *args).returncode != 0
    assert body.read_text(encoding="utf-8", errors="strict") == "甲丙乙\n"
    body.write_text("用户增注：甲丙乙\n", encoding="utf-8", errors="strict")
    assert run("apply_doubts.py", *args).returncode != 0
    assert body.read_text(encoding="utf-8", errors="strict").startswith("用户增注")


def compare_fixture(root):
    body = root / "body.md"
    body.write_text("银行事业。\n第二页。", encoding="utf-8", errors="strict")
    raw = body.read_text(encoding="utf-8", errors="strict")
    boundary = raw.index("第二")
    mapping = {"writingMode": "vertical-rl", "files": {"body.md": {"hash": text_hash(raw), "lines": [
        {"from": 0, "to": boundary, "page": 21, "rect": [0.8, 0.1, 0.9, 0.8]},
        {"from": boundary, "to": len(raw), "page": 22, "rect": [0.8, 0.1, 0.9, 0.8]},
    ]}}}
    path = root / "line-map.json"
    save(path, mapping)
    reader = root / "reader"
    reader.mkdir()
    save(reader / "p0001.json", {"message": "success", "data": ["銀行事业。﹂"]})
    cfg = root / "_数据"
    cfg.mkdir()
    save(cfg / "config.json", {"variantEquivalents": ["银銀"]})
    return body, path, reader, mapping


def test_requested_scope_and_book_variants_are_used_without_extra_pages(tmp_path):
    body, mapping, reader, _ = compare_fixture(tmp_path)
    out = tmp_path / "report"
    args = ("--book", tmp_path, "--line-map", mapping, "--kandian", f"{reader}|21", "--out", out)
    assert run("vl_compare.py", *args).returncode == 2
    assert run("vl_compare.py", *args, "--pages", "21").returncode == 0
    coverage = json.loads((out / "_reader_coverage.json").read_text(encoding="utf-8", errors="strict"))
    assert coverage["complete"] and coverage["requested_physical_pages"] == [21]
    assert coverage["missing_physical_pages"] == [] and coverage["variant_groups"] == ["银銀"]
    assert json.loads((out / "doubts.json").read_text(encoding="utf-8", errors="strict")) == []
    assert run("vl_compare.py", *args, "--pages", "23").returncode == 2
    coverage = json.loads((out / "_reader_coverage.json").read_text(encoding="utf-8", errors="strict"))
    assert coverage["unmapped_requested_pages"] == [23]


def test_stale_mapping_fails_before_output_or_source_changes(tmp_path):
    body, mapping, reader, _ = compare_fixture(tmp_path)
    body.write_text("新增文字。银行事业。\n第二页。", encoding="utf-8", errors="strict")
    out = tmp_path / "report"
    result = run("vl_compare.py", "--book", tmp_path, "--line-map", mapping,
                 "--kandian", f"{reader}|21", "--out", out)
    assert result.returncode != 0 and "Stale line map" in result.stderr
    assert not out.exists()


def test_reader_caption_and_stale_crop_checks(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("甲", encoding="utf-8", errors="strict")
    item = dict(n=1, file="body.md", sourceHash=text_hash("乙"), reader="Kandian",
                pdfPage=1, page="p.1", rect=None, current="甲", ocr="乙", context="**甲**")
    assert "Kandian" in caption(item)[1] and "VL" not in caption(item)[1]
    doubts = tmp_path / "doubts.json"
    save(doubts, [item])
    with pytest.raises(ValueError, match="Stale"):
        crops(tmp_path / "not-needed.pdf", doubts, tmp_path / "crops", tmp_path)
    assert not (tmp_path / "crops").exists()
