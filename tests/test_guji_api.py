from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gjcool_api as gj
import kandian_api as kd
import ocr_page_runner as runner
from mineru_api import ClientError


class GujiApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "book.pdf"
        with pymupdf.open() as doc:
            doc.new_page()
            doc.new_page()
            doc.save(self.pdf)
        self.args = argparse.Namespace(pdf=str(self.pdf), out=str(self.root / "out"),
                                       pages="1-2", dpi=72, quality=80, force=False)

    def run_pages(self, call, extract=kd.extract_content, **kwargs):
        with redirect_stdout(io.StringIO()):
            return runner.run_pages(self.args, {"model": "test"}, call, extract,
                                    kd.render_jpeg, runner.parse_pages, kd.import_pymupdf,
                                    ("secret-token",), **kwargs)

    def summary(self):
        return json.loads((self.root / "out/_run.json").read_text(encoding="utf-8", errors="strict"))

    def test_http_200_token_error_stops_without_success_file_or_secret(self):
        calls = []
        def call(image):
            calls.append(image)
            return 200, json.dumps({"message": "error", "info": "token无效 secret-token"})
        self.assertEqual(self.run_pages(call), 2)
        self.assertEqual(len(calls), 1)
        self.assertFalse((self.root / "out/p0001.json").exists())
        report = self.summary()
        self.assertFalse(report["pages"]["1"]["ok"])
        self.assertEqual(report["pending_pages"], [1, 2])
        body = (self.root / "out/p0001.failure.json").read_text(encoding="utf-8", errors="strict")
        self.assertNotIn("secret-token", body)
        self.assertIn("token无效", body)

    def test_failed_page_resumes_then_success_is_not_submitted_again(self):
        self.args.pages = "1"
        self.run_pages(lambda _: (200, '{"message":"error","info":"inactive"}'))
        calls = []
        def success(image):
            calls.append(image)
            return 200, '{"message":"success","data":["原文"]}'
        self.assertEqual(self.run_pages(success), 0)
        self.assertEqual(self.run_pages(success), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual((self.root / "out/p0001.txt").read_text(encoding="utf-8", errors="strict"), "原文")

    def test_http_401_is_not_retried(self):
        calls = []
        self.run_pages(lambda image: (calls.append(image) or 401, '{"error":"unauthorized"}'))
        self.assertEqual(len(calls), 1)

    def test_transient_failure_retries_and_checkpoints_success(self):
        self.args.pages = "1"
        replies = iter([(503, "busy"), (200, '{"message":"success","data":["甲"]}')])
        with patch.object(runner.time, "sleep"):
            self.assertEqual(self.run_pages(lambda _: next(replies)), 0)
        self.assertEqual(self.summary()["pages"]["1"]["attempts"], 2)

    def test_empty_chat_and_html_are_failures(self):
        for text in ['{"choices":[{"message":{"content":""}}]}', '<html>login</html>', '[]']:
            _, content, error = runner.decode_response(200, text, gj.extract_content)
            self.assertFalse(content)
            self.assertTrue(error)

    def test_success_extractors_preserve_line_order(self):
        self.assertEqual(kd.extract_content({"message": "success", "data": ["乙", "甲"]}), "乙\n甲")
        self.assertEqual(kd.extract_content({"message": "success", "data": {"texts": ["乙", "甲"]}}), "乙\n甲")
        self.assertEqual(kd.extract_content({"message": "success", "data": {"text_lines": [{"text": "乙"}, {"text": "甲"}]}}), "乙\n甲")
        self.assertEqual(gj.extract_content({"choices": [{"message": {"content": [{"type": "text", "text": "乙"}, {"text": "甲"}]}}]}), "乙甲")

    def test_changed_pdf_is_not_silently_skipped(self):
        self.args.pages = "1"
        self.run_pages(lambda _: (200, '{"message":"success","data":["甲"]}'))
        with self.pdf.open("ab") as f:
            f.write(b"\n% changed\n")
        with self.assertRaises(ClientError):
            self.run_pages(lambda _: self.fail("must not upload"))

    def test_malformed_page_ranges_are_rejected(self):
        for spec in ["3-1", "0", "3", "1,", "no", "-1"]:
            with self.assertRaises(ClientError):
                runner.parse_pages(spec, 2)
        self.assertEqual(runner.parse_pages("1-2,1", 2), [0, 1])

    def test_v1_base_does_not_append_v1_twice(self):
        self.assertEqual(gj.build_chat_urls("https://example.com/v1/"), ["https://example.com/v1/chat/completions"])

    def test_kandian_options_use_documented_field_names(self):
        with patch.object(kd, "post_form", return_value=(200, "{}")) as send:
            kd.call_api("secret", "account", "image", "v2", "sp", "2000", True, True, True)
        url, data = send.call_args.args
        self.assertEqual(url, kd.API_URL)
        self.assertEqual(data["return_position"], "true")
        self.assertEqual(data["return_choices"], "true")
        self.assertEqual(data["return_layout"], "true")
        self.assertEqual(data["only_plain_text"], "false")


if __name__ == "__main__":
    unittest.main()
