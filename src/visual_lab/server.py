"""Serve the built visual application and proxy its narrow quant API contract."""
from __future__ import annotations

import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_REQUEST_BYTES = 8192
MAX_UPSTREAM_BYTES = 2_000_000
DEFAULT_ASSET_DIR = Path("frontend/dist")


class QuantApiProxy:
    """Forward only the allowlisted read and report endpoints."""

    def __init__(self, base_url: str, timeout_seconds: float = 5.0):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("quant API base must be an http(s) URL without query or fragment")
        if parsed.username or parsed.password:
            raise ValueError("quant API base must not contain credentials")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        if len(raw) > MAX_UPSTREAM_BYTES:
            raise ValueError("upstream response too large")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("upstream response is not a JSON object")
        return value

    def request(self, path: str, payload: dict[str, Any] | None = None) -> tuple[HTTPStatus, dict[str, Any]]:
        if path not in ("/api/dashboard", "/api/stock-report", "/api/console-status",
                        "/api/feedback-case", "/api/optimize-run", "/api/review-cases"):
            raise ValueError("unsupported quant API path")
        expects_payload = path in ("/api/stock-report", "/api/feedback-case",
                                   "/api/optimize-run", "/api/review-cases")
        if expects_payload == (payload is None):
            raise ValueError("unexpected request method for quant API path")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False,
                                                        allow_nan=False).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=body,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {})},
            method="GET" if body is None else "POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_UPSTREAM_BYTES + 1)
                value = self._decode(raw)
                status = HTTPStatus(response.status)
                if status not in (HTTPStatus.OK, HTTPStatus.ACCEPTED):
                    raise ValueError("unexpected upstream success status")
                return status, value
        except HTTPError as exc:
            try:
                upstream = self._decode(exc.read(MAX_UPSTREAM_BYTES + 1))
            except (ValueError, UnicodeError, json.JSONDecodeError):
                upstream = {}
            if (exc.code in (HTTPStatus.BAD_REQUEST, HTTPStatus.CONFLICT,
                             HTTPStatus.UNSUPPORTED_MEDIA_TYPE, HTTPStatus.SERVICE_UNAVAILABLE)
                    and upstream.get("status") in ("invalid_input", "busy",
                                                   "stock_data_unavailable", "storage_unavailable")
                    and isinstance(upstream.get("reason"), str)):
                return HTTPStatus(exc.code), upstream
            return HTTPStatus.BAD_GATEWAY, {
                "status": "quant_api_error",
                "reason": f"量化服务返回 {exc.code}；请稍后重试或检查量化服务。",
            }
        except (URLError, TimeoutError, OSError):
            return HTTPStatus.BAD_GATEWAY, {
                "status": "quant_api_unavailable",
                "reason": "量化服务暂时不可用；请确认其已启动并重试。",
            }
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return HTTPStatus.BAD_GATEWAY, {
                "status": "invalid_quant_response",
                "reason": "量化服务返回的数据格式不正确。",
            }


def make_handler(proxy: QuantApiProxy,
                 asset_dir: str | Path = DEFAULT_ASSET_DIR) -> type[BaseHTTPRequestHandler]:
    asset_root = Path(asset_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False,
                                          allow_nan=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _asset(self, path: str) -> None:
            if path in ("/", "/index.html"):
                file = asset_root / "index.html"
            elif path.startswith("/assets/"):
                file = (asset_root / path.lstrip("/")).resolve()
                if asset_root not in file.parents:
                    self._json(HTTPStatus.NOT_FOUND,
                               {"status": "not_found", "reason": "资源不存在。"})
                    return
            else:
                self._json(HTTPStatus.NOT_FOUND,
                           {"status": "not_found", "reason": "页面不存在。"})
                return
            try:
                body = file.read_bytes()
            except OSError:
                self._json(HTTPStatus.NOT_FOUND,
                           {"status": "not_found", "reason": "资源不存在。"})
                return
            content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in ("application/javascript", "image/svg+xml"):
                content_type += "; charset=utf-8"
            self._send(HTTPStatus.OK, body, content_type)

        def do_GET(self) -> None:  # noqa: N802 - standard library naming
            if self.path in ("/api/dashboard", "/api/console-status"):
                status, value = proxy.request(self.path)
                self._json(status, value)
            elif self.path == "/healthz":
                self._json(HTTPStatus.OK, {"status": "up"})
            else:
                self._asset(self.path)

        def do_POST(self) -> None:  # noqa: N802 - standard library naming
            if self.path not in ("/api/stock-report", "/api/feedback-case",
                                 "/api/optimize-run", "/api/review-cases"):
                self._json(HTTPStatus.NOT_FOUND,
                           {"status": "not_found", "reason": "接口不存在。"})
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                           {"status": "invalid_input", "reason": "请求须使用 application/json。"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length < 1 or length > MAX_REQUEST_BYTES:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "invalid_input", "reason": "请求内容长度无效。"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (UnicodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "invalid_input", "reason": "请求必须是 JSON。"})
                return
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "invalid_input", "reason": "请求必须是 JSON 对象。"})
                return
            try:
                status, result = proxy.request(self.path, payload)
            except (TypeError, ValueError):
                self._json(HTTPStatus.BAD_REQUEST,
                           {"status": "invalid_input", "reason": "请求包含无效数值。"})
                return
            self._json(status, result)

    return Handler


def serve_visual(base_url: str = "http://127.0.0.1:8766", host: str = "127.0.0.1",
                 port: int = 8765, asset_dir: str | Path = DEFAULT_ASSET_DIR) -> None:
    """Serve the visual system; configure ``base_url`` to the separate quant API."""
    container_bind = host == "0.0.0.0" and os.getenv("VISUAL_DASHBOARD_CONTAINER") == "1"
    if host not in ("127.0.0.1", "localhost", "::1") and not container_bind:
        raise ValueError("visual service only binds locally unless run inside its container")
    if not (Path(asset_dir) / "index.html").is_file():
        raise FileNotFoundError(f"visual application is not built: {Path(asset_dir) / 'index.html'}")
    proxy = QuantApiProxy(base_url)
    server = ThreadingHTTPServer((host, port), make_handler(proxy, asset_dir))
    server.daemon_threads = True
    print(f"可视化应用：http://{host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
