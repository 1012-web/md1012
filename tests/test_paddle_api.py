from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pypdf import PdfWriter

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import paddle_api  # noqa: E402


def make_pdf(path: Path, pages: int) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=400)
    with path.open("wb") as handle:
        writer.write(handle)


def jsonl_for(pages: int) -> str:
    results = [{"prunedResult": {"width": 600, "height": 800, "parsing_res_list": [
        {"block_label": "text", "block_bbox": [10, 10, 590, 60], "block_content": f"第{n}页", "block_id": 0}]},
        "markdown": {}, "outputImages": {}} for n in range(pages)]
    # 官方把多页分在几行里返回
    return "\n".join(json.dumps({"result": {"layoutParsingResults": results[i:i + 2]}}) for i in range(0, pages, 2))


class MockPaddle(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    jobs: dict[str, int] = {}
    models: list[str] = []
    accept = {"PaddleOCR-VL-1.6", "PaddleOCR-VL-1.7"}

    def log_message(self, *_args):
        pass

    def send_json(self, value, status=200):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        assert self.headers["Authorization"] == "bearer test-token"
        body = self.rfile.read(int(self.headers["Content-Length"]))
        model = body.split(b'name="model"\r\n\r\n', 1)[1].split(b"\r\n", 1)[0].decode()
        type(self).models.append(model)
        if model not in self.accept:
            self.send_json({"code": 10007, "msg": "unknown model"}, status=400)
            return
        from io import BytesIO
        from pypdf import PdfReader
        pdf = body.split(b"Content-Type: application/pdf\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
        job = f"job{len(self.jobs)}"
        type(self).jobs[job] = len(PdfReader(BytesIO(pdf)).pages)
        self.send_json({"code": 0, "msg": "Success", "data": {"jobId": job}})

    def do_GET(self):
        port = self.server.server_port
        if self.path.startswith("/api/v2/ocr/jobs/"):
            job = self.path.rsplit("/", 1)[1]
            self.send_json({"code": 0, "data": {"state": "done", "resultUrl": {"jsonUrl": f"http://127.0.0.1:{port}/result/{job}"},
                                                 "extractProgress": {"totalPages": self.jobs[job]}}})
        elif self.path.startswith("/result/"):
            text = jsonl_for(self.jobs[self.path.rsplit("/", 1)[1]]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)


class PaddleApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockPaddle)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = {k: os.environ.get(k) for k in ("PADDLEOCR_API_BASE_URL", "MINERU_SKILL_CONFIG_DIR")}
        os.environ["PADDLEOCR_API_BASE_URL"] = f"http://127.0.0.1:{self.server.server_port}/api/v2/ocr"
        os.environ["MINERU_SKILL_CONFIG_DIR"] = str(self.root / "config")
        MockPaddle.jobs, MockPaddle.models = {}, []

    def tearDown(self):
        self.server.shutdown()
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_submit_writes_one_file_per_page_and_resumes(self):
        pdf = self.root / "book.pdf"
        make_pdf(pdf, 5)
        out = self.root / "VL"
        (out).mkdir()
        (out / "p0002.json").write_text("{}", encoding="utf-8")  # 已有的页跳过
        old_chunk = paddle_api.CHUNK_PAGES
        paddle_api.CHUNK_PAGES = 2
        try:
            summary = paddle_api.run_submit(pdf, out, "1-5", "PaddleOCR-VL-1.6", "test-token", interval=0)
        finally:
            paddle_api.CHUNK_PAGES = old_chunk
        self.assertEqual(summary["parsed"], 4)
        self.assertEqual(summary["skippedExisting"], 1)
        # 跳过 p2 后：[1]、[3,4]、[5]
        self.assertEqual([job["pages"] for job in summary["jobs"]], [[1, 1], [3, 4], [5, 5]])
        page = json.loads((out / "p0004.json").read_text(encoding="utf-8"))
        self.assertEqual(page["page"], 4)
        self.assertEqual(page["model"], "PaddleOCR-VL-1.6")
        self.assertEqual(page["width"], 600)
        self.assertEqual(page["parsing_res_list"][0]["block_label"], "text")
        self.assertNotIn("block_id", page["parsing_res_list"][0])
        self.assertEqual((out / "p0002.json").read_text(encoding="utf-8"), "{}")
        usage = json.loads(paddle_api.settings_path().read_text(encoding="utf-8"))["usage"]
        self.assertEqual(usage["pages"], 4)

    def test_check_models_probes_newer_versions_and_respects_interval(self):
        result = paddle_api.check_models("test-token", "PaddleOCR-VL-1.6")
        self.assertEqual(result["newer"], ["PaddleOCR-VL-1.7"])
        self.assertEqual(MockPaddle.models, ["PaddleOCR-VL-1.7", "PaddleOCR-VL-1.8", "PaddleOCR-VL-2.0"])
        again = paddle_api.check_models("test-token", "PaddleOCR-VL-1.6")
        self.assertTrue(again["skipped"])
        self.assertEqual(again["newer"], ["PaddleOCR-VL-1.7"])
        self.assertEqual(len(MockPaddle.models), 3)

    def test_token_sources(self):
        token_file = self.root / "token.txt"
        token_file.write_text("from-file\n", encoding="utf-8")
        self.assertEqual(paddle_api.load_token(token_file), "from-file")
        os.environ["PADDLEOCR_ACCESS_TOKEN"] = "from-env"
        try:
            self.assertEqual(paddle_api.load_token(), "from-env")
        finally:
            os.environ.pop("PADDLEOCR_ACCESS_TOKEN")
        paddle_api.store_token("stored")
        self.assertEqual(paddle_api.load_token(), "stored")
        self.assertNotIn("stored", paddle_api.settings_path().read_text(encoding="utf-8")) if os.name == "nt" else None

    def test_next_versions(self):
        self.assertEqual(paddle_api.next_versions("PaddleOCR-VL-1.6"),
                         ["PaddleOCR-VL-1.7", "PaddleOCR-VL-1.8", "PaddleOCR-VL-2.0"])
        self.assertEqual(paddle_api.next_versions("PaddleOCR-VL"), ["PaddleOCR-VL-1.0"])


if __name__ == "__main__":
    unittest.main()
