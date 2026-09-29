from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visual_lab.server import QuantApiProxy, make_handler, serve_visual


class FakeResponse:
    def __init__(self, value: dict[str, object], status: int = 200):
        self.body = io.BytesIO(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self, length: int) -> bytes:
        return self.body.read(length)


def invoke(proxy: QuantApiProxy, method: str, path: str,
           payload: dict[str, object] | None = None,
           asset_dir: str | Path = "frontend/dist") -> tuple[int, dict[str, str], bytes]:
    """Exercise the HTTP handler with in-memory streams; no socket bind needed."""
    handler = object.__new__(make_handler(proxy, asset_dir))
    handler.path = path
    handler.wfile = io.BytesIO()
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    handler.rfile = io.BytesIO(body)
    handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
    received: dict[str, object] = {"headers": {}}
    handler.send_response = lambda code: received.update(status=int(code))
    handler.send_header = lambda key, value: received["headers"].update({key: value})
    handler.end_headers = lambda: None
    if method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    return int(received["status"]), received["headers"], handler.wfile.getvalue()


class VisualProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.proxy = QuantApiProxy("http://127.0.0.1:8766")
        self.assets = tempfile.TemporaryDirectory()
        self.addCleanup(self.assets.cleanup)
        root = Path(self.assets.name)
        (root / "assets").mkdir()
        (root / "index.html").write_text('<div id="root"></div><script src="/assets/app.js"></script>', encoding="utf-8")
        (root / "assets" / "app.js").write_text('document.getElementById("root")', encoding="utf-8")

    def test_serves_built_app_and_forwards_dashboard(self) -> None:
        status, headers, page = invoke(self.proxy, "GET", "/", asset_dir=self.assets.name)
        self.assertEqual(200, status)
        self.assertEqual("text/html; charset=utf-8", headers["Content-Type"])
        html = page.decode("utf-8")
        self.assertIn('<div id="root"></div>', html)
        status, headers, script = invoke(self.proxy, "GET", "/assets/app.js", asset_dir=self.assets.name)
        self.assertEqual(200, status)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn(b"document.getElementById", script)
        with patch("visual_lab.server.urlopen", return_value=FakeResponse({
            "market": {"asof": "2026-09-28"},
            "recommendations": {"status": "blocked_insufficient_evidence"},
        })) as opened:
            status, headers, body = invoke(self.proxy, "GET", "/api/dashboard")
        self.assertEqual(200, status)
        self.assertEqual("2026-09-28", json.loads(body)["market"]["asof"])
        self.assertEqual("http://127.0.0.1:8766/api/dashboard", opened.call_args.args[0].full_url)
        self.assertEqual("GET", opened.call_args.args[0].get_method())

    def test_forwards_stock_report_and_upstream_validation(self) -> None:
        response = {"status": "ready", "instrument_id": "stock:000338.SZ", "cost_price": 15.8}
        with patch("visual_lab.server.urlopen", return_value=FakeResponse(response)) as opened:
            status, _headers, body = invoke(
                self.proxy, "POST", "/api/stock-report", {"symbol": "000338", "cost_price": 15.8}
            )
        self.assertEqual(200, status)
        self.assertEqual(response, json.loads(body))
        upstream_request = opened.call_args.args[0]
        self.assertEqual("POST", upstream_request.get_method())
        self.assertEqual("http://127.0.0.1:8766/api/stock-report", upstream_request.full_url)
        self.assertEqual({"symbol": "000338", "cost_price": 15.8},
                         json.loads(upstream_request.data))

        error_body = io.BytesIO(b'{"status":"invalid_input","reason":"invalid symbol"}')
        error = HTTPError(upstream_request.full_url, HTTPStatus.BAD_REQUEST,
                          "bad input", {}, error_body)
        with patch("visual_lab.server.urlopen", side_effect=error):
            status, _headers, body = invoke(
                self.proxy, "POST", "/api/stock-report", {"symbol": "bad"}
            )
        self.assertEqual(400, status)
        self.assertEqual("invalid_input", json.loads(body)["status"])

    def test_console_status_is_read_only_and_forwarded(self) -> None:
        response = {"status": "ready", "strategies": [{"strategy_id": "sma-trend"}]}
        with patch("visual_lab.server.urlopen", return_value=FakeResponse(response)) as opened:
            status, _headers, body = invoke(self.proxy, "GET", "/api/console-status")
        self.assertEqual(200, status)
        self.assertEqual(response, json.loads(body))
        request = opened.call_args.args[0]
        self.assertEqual("GET", request.get_method())
        self.assertEqual("http://127.0.0.1:8766/api/console-status", request.full_url)
        with self.assertRaises(ValueError):
            self.proxy.request("/api/console-status", {"operation": "sync"})

    def test_feedback_case_is_forwarded_only_as_post(self) -> None:
        response = {"status": "saved", "case_id": "case-1"}
        payload = {"research_record_id": "record-1", "decision": "watch"}
        with patch("visual_lab.server.urlopen", return_value=FakeResponse(response)) as opened:
            status, _headers, body = invoke(self.proxy, "POST", "/api/feedback-case", payload)
        self.assertEqual(200, status)
        self.assertEqual(response, json.loads(body))
        self.assertEqual("POST", opened.call_args.args[0].get_method())
        self.assertEqual("http://127.0.0.1:8766/api/feedback-case", opened.call_args.args[0].full_url)
        with self.assertRaises(ValueError):
            self.proxy.request("/api/feedback-case")

    def test_feedback_requires_json_media_type(self) -> None:
        with patch("visual_lab.server.urlopen") as opened:
            handler = object.__new__(make_handler(self.proxy, self.assets.name))
            handler.path = "/api/feedback-case"
            handler.wfile = io.BytesIO()
            handler.rfile = io.BytesIO(b'{"decision":"watch"}')
            handler.headers = {"Content-Length": "20", "Content-Type": "text/plain"}
            response: dict[str, int] = {}
            handler.send_response = lambda code: response.update(status=int(code))
            handler.send_header = lambda *_args: None
            handler.end_headers = lambda: None
            handler.do_POST()
        self.assertEqual(415, response["status"])
        opened.assert_not_called()

    def test_bounded_console_actions_are_forwarded_as_post(self) -> None:
        for path, payload in (("/api/optimize-run", {"strategy": "all"}),
                              ("/api/review-cases", {})):
            with self.subTest(path=path):
                with patch("visual_lab.server.urlopen", return_value=FakeResponse({
                    "status": "accepted", "task_id": "test-task",
                }, status=202)) as opened:
                    status, _headers, body = invoke(self.proxy, "POST", path, payload)
                self.assertEqual(202, status)
                self.assertEqual("accepted", json.loads(body)["status"])
                self.assertEqual("POST", opened.call_args.args[0].get_method())
                self.assertEqual(f"http://127.0.0.1:8766{path}", opened.call_args.args[0].full_url)
                with self.assertRaises(ValueError):
                    self.proxy.request(path)

        busy = HTTPError("http://127.0.0.1:8766/api/optimize-run", HTTPStatus.CONFLICT,
                         "busy", {}, io.BytesIO(b'{"status":"busy","task_id":"existing-task","reason":"running"}'))
        with patch("visual_lab.server.urlopen", side_effect=busy):
            status, _headers, body = invoke(self.proxy, "POST", "/api/optimize-run",
                                            {"strategy": "all"})
        self.assertEqual(409, status)
        self.assertEqual("existing-task", json.loads(body)["task_id"])

    def test_unavailable_upstream_returns_clear_gateway_error(self) -> None:
        with patch("visual_lab.server.urlopen", side_effect=URLError("connection refused")):
            status, _headers, body = invoke(self.proxy, "GET", "/api/dashboard")
        self.assertEqual(502, status)
        self.assertEqual("quant_api_unavailable", json.loads(body)["status"])

    def test_nonlocal_bind_requires_container_marker(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                serve_visual(host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
