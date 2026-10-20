from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_judgement.py"


def item(n: int, verdict: str = "A", confidence: str = "高", **extra) -> dict:
    return {"n": n, "page": f"p.{n}", "current": "人", "ocr": "入", "verdict": verdict,
            "confidence": confidence, "reason": "看图", **extra}


def setup(tmp_path: Path, batches: dict[int, list[dict]], count: int) -> None:
    manifest = [{"n": n, "context": f"前**人**后{n}", "pdfPage": n} for n in range(1, count + 1)]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
    for number, items in batches.items():
        (tmp_path / f"judgement-b{number}.json").write_text(json.dumps(items, ensure_ascii=False), "utf-8")


def run(tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), str(tmp_path)],
                          capture_output=True, text=True, encoding="utf-8")


def test_any_number_of_batches_are_merged_in_order(tmp_path: Path) -> None:
    # 第 10 批排在第 2 批后面（按数字、不按字符串排）；批数不是固定的 6
    setup(tmp_path, {1: [item(1), item(2, "C", correct="入")], 2: [item(3, "B")], 10: [item(4, "D", "低")]}, 4)
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    merged = json.loads((tmp_path / "judgement.json").read_text("utf-8"))
    assert [entry["n"] for entry in merged] == [1, 2, 3, 4]
    assert "batches 3 items 4" in result.stdout
    assert "人→入 ×2" in result.stdout  # C 与 B 改成同一个字，归为一组
    assert "把握低 1" in result.stdout


def test_missing_duplicate_and_malformed_entries_block_the_merge(tmp_path: Path) -> None:
    broken = {k: v for k, v in item(3).items() if k != "reason"}
    setup(tmp_path, {1: [item(1), item(1, "X")], 2: [broken, item(4, "C")]}, 5)
    result = run(tmp_path)
    assert result.returncode == 1
    assert not (tmp_path / "judgement.json").exists()
    out = result.stdout
    assert "#1 在各批里出现 2 次" in out
    assert "#2 没有判定" in out and "#5 没有判定" in out
    assert "#3 缺字段 ['reason']" in out
    assert "#1 verdict 应为 A/B/C/D" in out
    assert "#4 判 C 却没写 correct" in out


def test_no_batch_files_is_a_clear_error(tmp_path: Path) -> None:
    setup(tmp_path, {}, 2)
    result = run(tmp_path)
    assert result.returncode != 0
    assert "没有 judgement-bN.json" in result.stderr


def test_empty_correct_means_delete_and_is_accepted(tmp_path: Path) -> None:
    # 判 C、correct 为空串：原书该处没有字（如混进正文的书口页码），合法；不写 correct 才算缺
    setup(tmp_path, {1: [item(1, "C", correct=""), item(2, "C")]}, 2)
    result = run(tmp_path)
    assert result.returncode == 1
    assert "#2 判 C 却没写 correct" in result.stdout and "#1" not in result.stdout


def test_legacy_miner_u_field_is_still_accepted(tmp_path: Path) -> None:
    # 2026-10-20 前的判定文件用 minerU 记正文现字；合并时改名为 current
    legacy = {k: v for k, v in item(1, "B").items() if k != "current"} | {"minerU": "人"}
    setup(tmp_path, {1: [legacy]}, 1)
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    merged = json.loads((tmp_path / "judgement.json").read_text("utf-8"))
    assert merged[0]["current"] == "人" and "minerU" not in merged[0]
