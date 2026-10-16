from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from collections import defaultdict
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pdf_preflight as preflight


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="strict", newline="") as handle:
        handle.write(value)


def test_text_evidence_and_decisions():
    normal = preflight.text_evidence("正常文字。Another sentence.")
    assert preflight.summarize([normal], "auto")["recommendation"] == "reuse_text_after_visual_check"
    broken = preflight.text_evidence("甲\ufffd\ue001")
    assert broken["suspiciousCount"] == 2
    assert preflight.summarize([broken], "auto")["awaitUserChoice"]
    assert not preflight.summarize([broken], "on")["awaitUserChoice"]
    assert preflight.summarize([broken], "on")["decision"] == "user_requested_ocr"
    blank = preflight.text_evidence("")
    assert preflight.summarize([blank], "auto")["recommendation"] == "ocr"
    assert preflight.summarize([blank, normal], "auto")["sampleStatus"] == "mixed_sample"
    assert preflight.summarize([{"error": "DecodeError"}], "auto")["awaitUserChoice"]
    ancient = preflight.text_evidence("古文" * 120)
    assert preflight.summarize([ancient], "auto")["recommendation"] == "review_original"


def test_sample_distribution():
    assert preflight.sample_indexes(1) == [0]
    samples = preflight.sample_indexes(340)
    assert len(samples) == 9 and samples[0] == 0 and samples[-1] == 339


def test_preflight_real_pdf_no_input_changes(tmp_path):
    pytest.importorskip("pypdf")
    fitz = pytest.importorskip("pymupdf")
    pdf = tmp_path / "scan.pdf"
    with fitz.open() as doc:
        doc.new_page()
        doc.new_page().insert_text((40, 40), "Readable sentence.")
        doc.save(pdf)
    original = pdf.read_bytes()
    report = preflight.inspect_pdf(pdf)
    assert report["sampleStatus"] == "mixed_sample"
    assert report["networkRequests"] == report["ocrRuns"] == 0
    assert pdf.read_bytes() == original
