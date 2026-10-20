"""把 PDF 交给看典古籍 OCR API 解析，结果按页存盘。

看典古籍（kandianguji.com）：注册后到「古籍数字化」页申请 OCR API Token；
需要 token + Token 页面显示的账号（邮箱或手机号），额度以 status 返回为准。接口一次一页图片，返回 JSON；本脚本负责渲染、提交、重试与存盘。
接口与参数以官方文档为准：https://kandianguji.com/api_documentation

凭据按顺序取：--token-file 指的文件（两行：第一行 token，第二行账号）→ 环境变量
KANDIAN_OCR_TOKEN / KANDIAN_OCR_EMAIL → configure 存下的（Windows 用 DPAPI 加密，
与 mineru_api.py 同一个配置目录）。凭据不进聊天、不进命令行参数、不进仓库。

用法：
  python kandian_api.py configure                                    # 隐藏输入，存一次凭据
  python kandian_api.py submit 书.pdf -o <目录> [--pages 1-10] [--version v2] [--det-mode auto]

每页一个 pNNNN.json（按 PDF 内页序，1 起；存 API 原始返回），汇总写 <out>/_run.json。
只有已验证成功的页跳过，中断后重跑即续；失败返回非零退出码。stdout 只打 ASCII 行。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ocr_page_runner import parse_pages, run_pages, redact

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mineru_api import (  # noqa: E402  共用配置目录与 DPAPI
    ClientError,
    _dpapi_protect,
    _dpapi_unprotect,
    config_dir,
    read_json,
    write_json,
)

API_URL = "https://ocr.kandianguji.com/ocr_api"
TOKEN_ENV = "KANDIAN_OCR_TOKEN"
EMAIL_ENV = "KANDIAN_OCR_EMAIL"
DEFAULT_VERSION = "v2"     # default / beta / v2，不同版本算法结果有差异
DEFAULT_DET_MODE = "auto"  # auto / sp（竖排）/ hp（横排）
DEFAULT_IMAGE_SIZE = "2000"  # 0 为不调整；官方建议 1000-2000
RETRY = 2
TIMEOUT = 120


def settings_path() -> Path:
    return config_dir() / "kandian.json"


def store_credentials(token: str, email: str) -> None:
    token = token.strip()
    email = email.strip()
    if not token or not email:
        raise ClientError("Token and account (email or phone number) are both required")
    current = read_json(settings_path(), {})
    if os.name == "nt":
        current.update({"token_storage": "windows-dpapi", "token": _dpapi_protect(token), "email": email})
    else:
        current.update({"token_storage": "restricted-file", "token": token, "email": email})
    current["configured_at"] = dt.datetime.now().astimezone().isoformat()
    write_json(settings_path(), current)


def load_credentials(token_file: Path | None = None) -> tuple[str, str]:
    if token_file:
        lines = [ln.strip() for ln in token_file.expanduser().read_text(encoding="utf-8", errors="strict").splitlines() if ln.strip()]
        if len(lines) < 2:
            raise ClientError("Token file requires two lines: token and account (email or phone)")
        return lines[0], lines[1]
    env_token = os.environ.get(TOKEN_ENV, "").strip()
    env_email = os.environ.get(EMAIL_ENV, "").strip()
    if env_token:
        if not env_email:
            raise ClientError("KANDIAN_OCR_EMAIL must contain the account associated with KANDIAN_OCR_TOKEN")
        return env_token, env_email
    settings = read_json(settings_path(), {})
    token = settings.get("token")
    email = settings.get("email", "")
    if not token or not email:
        raise ClientError("Kandian credentials are not configured. Run 'python kandian_api.py configure' locally.")
    if settings.get("token_storage") == "windows-dpapi":
        token = _dpapi_unprotect(token)
    return token, email


def import_pymupdf():
    try:
        import pymupdf  # type: ignore
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz  # type: ignore
        return fitz
    except ImportError:
        raise ClientError("pymupdf is required. Install: python -m pip install pymupdf")


def render_jpeg(doc: Any, page_index: int, dpi: int, quality: int) -> bytes:
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes(output="jpeg", jpg_quality=quality)


def call_api(
    token: str,
    email: str,
    image_b64: str,
    version: str,
    det_mode: str,
    image_size: str,
    return_position: bool = False,
    return_choices: bool = False,
    det_layout: bool = False,
) -> tuple[int, str]:
    data = {
        "token": token,
        "email": email,
        "image": image_b64,
        "version": version,
        "det_mode": det_mode,
        "image_size": image_size,
        "return_position": str(return_position).lower(),
        "return_choices": str(return_choices).lower(),
        "det_layout": str(det_layout).lower(),
        "return_layout": str(return_position and det_layout).lower(),
        "only_plain_text": "false",
    }
    return post_form(API_URL, data)


def post_form(url: str, data: dict) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode("ascii")
    request = urllib.request.Request(url, data=body)
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("User-Agent", "md1012-kandian/0.1")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", errors="strict")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="strict")
    except urllib.error.URLError as exc:
        return 0, f"URLError: {exc.reason}"
    except (TimeoutError, ConnectionError) as exc:
        return 0, type(exc).__name__
    except UnicodeDecodeError:
        return 200, "Response is not valid UTF-8"


def extract_content(payload: Any) -> str:
    if not isinstance(payload, dict) or payload.get("message") != "success":
        return ""
    data = payload.get("data")
    if isinstance(data, dict):
        if "texts" in data:
            data = data["texts"]
        else:
            lines = data.get("text_lines", [])
            if not isinstance(lines, list):
                return ""
            data = [line.get("text", "") for line in lines if isinstance(line, dict)]
    if not isinstance(data, list) or not all(isinstance(line, str) for line in data):
        return ""
    return "\n".join(data)


def cmd_status(args: argparse.Namespace) -> int:
    token, email = load_credentials(Path(args.token_file) if args.token_file else None)
    status, text = post_form("https://ocr.kandianguji.com/get_token_status", {"token": token, "email": email})
    text = redact(text, (token, email))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"info": text}
    ok = status == 200 and isinstance(payload, dict) and payload.get("message") == "success"
    report = {"ok": ok, "status": status, "response": payload}
    if args.out:
        write_json(Path(args.out), report)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if ok else 2


def safe(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def cmd_configure(_: argparse.Namespace) -> int:
    print("Kandian Guji OCR API credential setup (stored encrypted on this machine)")
    token = getpass.getpass("token (hidden input): ").strip()
    email = input("account shown on Token page (email or phone): ").strip()
    store_credentials(token, email)
    print(f"saved to {settings_path()}")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    token, email = load_credentials(Path(args.token_file) if args.token_file else None)
    if args.return_choices and not args.return_position:
        raise ClientError("--return-choices requires --return-position")
    def call(image):
        return call_api(token, email, base64.b64encode(image).decode("ascii"), args.version,
                        args.det_mode, args.image_size, args.return_position,
                        args.return_choices, args.det_layout)
    metadata = dict(source="kandianguji", api_url=API_URL, version=args.version,
                    det_mode=args.det_mode, image_size=args.image_size,
                    return_position=args.return_position, return_choices=args.return_choices,
                    det_layout=args.det_layout)
    return run_pages(args, metadata, call, extract_content, render_jpeg, parse_pages,
                     import_pymupdf, (token, email), RETRY)


def main() -> int:
    parser = argparse.ArgumentParser(description="Kandian Guji (kandianguji.com) OCR API helper")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("configure", help="store token and email locally (hidden input)")
    p.set_defaults(func=cmd_configure)

    status = sub.add_parser("status", help="check token validity and quota without uploading images")
    status.add_argument("--token-file", default=None)
    status.add_argument("-o", "--out", default=None, help="save a redacted status report")
    status.set_defaults(func=cmd_status)

    s = sub.add_parser("submit", help="render a PDF and submit it page by page")
    s.add_argument("pdf")
    s.add_argument("-o", "--out", required=True, help="output directory (one pNNNN.json per page)")
    s.add_argument("--pages", default=None, help="e.g. 1-10 or 3,5,7 (1-based, default: all)")
    s.add_argument("--version", default=DEFAULT_VERSION, help="default / beta / v2")
    s.add_argument("--det-mode", dest="det_mode", default=DEFAULT_DET_MODE, help="auto / sp (vertical) / hp")
    s.add_argument("--image-size", dest="image_size", default=DEFAULT_IMAGE_SIZE, help="0 to keep original")
    s.add_argument("--return-position", action="store_true", help="return line and character coordinates")
    s.add_argument("--return-choices", action="store_true", help="return character candidates (requires positions)")
    s.add_argument("--det-layout", action="store_true", help="detect layout; retain headers and book gutters")
    s.add_argument("--dpi", type=int, default=300, help="render DPI (default 300)")
    s.add_argument("--quality", type=int, default=90, help="JPEG quality (default 90)")
    s.add_argument("--token-file", dest="token_file", default=None, help="file with two lines: token, email")
    s.add_argument("--force", action="store_true", help="redo pages that already have output")
    s.set_defaults(func=cmd_submit)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except ClientError as exc:
        print(f"ERROR: {safe(str(exc))}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
