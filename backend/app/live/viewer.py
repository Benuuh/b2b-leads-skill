"""Short-lived, loopback-only viewer for one committed live projection.

The server is a viewing process, not a worker: it never starts research, changes
batch state, runs maintenance or exports on its own. It binds 127.0.0.1, requires
a per-process random token on every request, rejects rebinding hosts and
cross-site requests, and exits on inactivity.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from app.errors import AppError
from app.live import projection as live

COOKIE_NAME = "b2b_live_view"
REQUEST_HEADER = "X-Live-Request"
MAX_BODY = 64 * 1024
ASSET_ROOT = Path(__file__).resolve().parent / "assets"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
SECURITY_HEADERS = (
    ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
)
FILTER_KEYS = {"q", "countries", "country", "channels", "channel"}
MAX_FILTER_ITEMS = 20


def _clean_filters(value):
    """Only the documented page filters reach the core; paths never do."""
    if not isinstance(value, dict):
        return {}, None
    cleaned = {}
    query = value.get("q")
    if isinstance(query, str) and query.strip():
        cleaned["q"] = query.strip()[:200]
    countries = value.get("countries", value.get("country"))
    if countries:
        supplied = [countries] if isinstance(countries, str) else countries
        chosen = [str(item).strip()[:4] for item in supplied if isinstance(item, (str, int))][:MAX_FILTER_ITEMS]
        if chosen:
            cleaned["countries"] = chosen
    channels = value.get("channels", value.get("channel"))
    if channels:
        supplied = [channels] if isinstance(channels, str) else channels
        chosen = [str(item).strip()[:40] for item in supplied if isinstance(item, str)][:3]
        if chosen:
            cleaned["channels"] = chosen
    return cleaned, cleaned.get("channels")


class ViewerConfig:
    def __init__(self, run_id, token, idle_timeout):
        self.run_id = run_id
        self.token = token
        self.idle_timeout = idle_timeout
        self.port = None
        self.last_activity = time.monotonic()
        self.lock = threading.Lock()

    def touch(self):
        with self.lock:
            self.last_activity = time.monotonic()

    def idle_seconds(self):
        with self.lock:
            return time.monotonic() - self.last_activity


def _handler(config):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "b2b-live"
        sys_version = ""

        def log_message(self, *args):  # never log paths, queries or tokens
            return

        def log_error(self, *args):
            return

        # -- security ----------------------------------------------------
        def _host_ok(self):
            host = (self.headers.get("Host") or "").strip().casefold()
            return host in {"127.0.0.1:%d" % config.port, "localhost:%d" % config.port}

        def _token_ok(self, token):
            return bool(token) and secrets.compare_digest(str(token), config.token)

        def _cookie_token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE_NAME:
                    return value
            return None

        def _authorized(self):
            if self._token_ok(self.headers.get("X-Live-Token")):
                return True
            return self._token_ok(self._cookie_token())

        def _origin_ok(self):
            origin = (self.headers.get("Origin") or "").strip()
            if not origin:
                return True
            allowed = {"http://127.0.0.1:%d" % config.port, "http://localhost:%d" % config.port}
            return origin.rstrip("/") in allowed

        def _send(self, status, body=b"", content_type="text/plain; charset=utf-8", extra=()):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in SECURITY_HEADERS:
                self.send_header(key, value)
            for key, value in extra:
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def _json(self, status, value, extra=()):
            body = json.dumps(value, ensure_ascii=False, default=str).encode()
            self._send(status, body, "application/json; charset=utf-8", extra)

        def _deny(self, message="访问被拒绝"):
            self._send(403, message.encode("utf-8"))

        def _prepare(self):
            config.touch()
            if not self._host_ok():
                self._deny("Host 校验失败")
                return False
            return True

        # -- verbs -------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._prepare():
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/" and parsed.query:
                query = parse_qs(parsed.query)
                supplied = (query.get("token") or [None])[0]
                if supplied is not None:
                    if not self._token_ok(supplied):
                        self._deny("访问链接无效或已过期")
                        return
                    self._send(303, b"", extra=(
                        ("Location", "/"),
                        ("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d"
                         % (COOKIE_NAME, config.token, int(config.idle_timeout))),
                    ))
                    return
            if not self._authorized():
                self._deny("需要有效的本机查看令牌；请重新运行查看命令")
                return
            if parsed.path in ASSETS:
                name, content_type = ASSETS[parsed.path]
                path = (ASSET_ROOT / name).resolve()
                if path.parent != ASSET_ROOT.resolve() or not path.is_file():
                    self._send(404, b"not found")
                    return
                body = path.read_bytes()
                self._send(200, body, content_type)
                return
            if parsed.path == "/favicon.ico":
                self._send(204)
                return
            if parsed.path == "/api/projection":
                self._projection()
                return
            self._send(404, b"not found")

        def do_POST(self):
            if not self._prepare():
                return
            if not self._authorized():
                self._deny("需要有效的本机查看令牌")
                return
            if not self._origin_ok():
                self._deny("Origin 校验失败")
                return
            if self.headers.get(REQUEST_HEADER) != "1":
                self._deny("缺少本机请求标记")
                return
            parsed = urlsplit(self.path)
            if parsed.path != "/api/export":
                self._send(404, b"not found")
                return
            if "application/json" not in str(self.headers.get("Content-Type") or "").casefold():
                self._send(415, b"application/json required")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self._send(413, b"request body too large or missing")
                return
            try:
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("object required")
            except (ValueError, UnicodeError):
                self._json(400, {"code": "INVALID_BODY", "message": "请求内容不是有效 JSON"})
                return
            self._export(body)

        def do_PUT(self):
            self._method_not_allowed()

        do_DELETE = do_PATCH = do_OPTIONS = do_PUT

        def _method_not_allowed(self):
            if not self._prepare():
                return
            self._send(405, b"method not allowed", extra=(("Allow", "GET, HEAD, POST"),))

        # -- endpoints ---------------------------------------------------
        def _projection(self):
            state = live.read_state(config.run_id)
            if not state["exists"]:
                self._json(404, {"code": "NO_PROJECTION",
                                 "message": "尚无已保存的成果页面；请先运行查看命令重新生成"})
                return
            page = state["projection"]
            view = live.status_view(page)
            pending = state["pending"] or {}
            payload = {
                "page": page,
                "server": {
                    "generated_at": page.get("generated_at"),
                    "age_seconds": state["age_seconds"],
                    "status_view": view,
                    "pending": pending or None,
                    "stale_after_seconds": page.get("stale_after_seconds"),
                    "poll_seconds": 2,
                    "notes": page.get("notes"),
                },
            }
            etag = '"%s"' % hashlib.sha256(
                ("%s|%s|%s|%s|%s" % (page.get("revision"), page.get("generated_at"),
                                      pending.get("at"), view["value"], view["stale"])).encode()
            ).hexdigest()[:32]
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                for key, value in SECURITY_HEADERS:
                    self.send_header(key, value)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self._json(200, payload, extra=(("ETag", etag),))

        def _export(self, body):
            from app import runs
            body_format = body.get("format") or "xlsx"
            if body_format not in {"xlsx", "csv"}:
                self._json(400, {"code": "INVALID_FORMAT", "message": "仅支持 xlsx 或 csv"})
                return
            filters, channels = _clean_filters(body.get("filters") or {})
            try:
                result = runs.export(config.run_id, format=body_format, filters=filters, channels=channels)
            except AppError as error:
                if error.code in {"PROCESS_BUSY", "RUN_PAUSED"}:
                    self._json(409, {"code": "BUSY", "message": "处理或导出正在进行，请稍后重试"})
                    return
                if error.code == "EXPORT_EMPTY":
                    self._json(409, {"code": "EXPORT_EMPTY", "message": "当前筛选下暂无可交付企业"})
                    return
                if error.code == "DEMO_EXPORT_BLOCKED":
                    self._json(409, {"code": "DEMO_EXPORT_BLOCKED", "message": "演示数据不能正式导出"})
                    return
                self._json(409, {"code": error.code, "message": error.message})
                return
            except Exception as error:  # lock timeout, disk or encoding failure
                code = getattr(error, "code", type(error).__name__)
                if "lock" in str(code).casefold() or "Operational" in type(error).__name__:
                    self._json(409, {"code": "BUSY", "message": "处理或导出正在进行，请稍后重试"})
                    return
                self._json(500, {"code": "EXPORT_FAILED", "message": "导出未完成；请重试"})
                return
            manifest = result.get("manifest") or {}
            delivery = manifest.get("delivery") or {}
            self._json(200, {
                "export_id": result.get("export_id"),
                "file": result.get("file"),
                "filename": result.get("filename"),
                "count": (manifest.get("row_counts") or {}).get("companies", 0),
                "snapshot_at": result.get("snapshot_at"),
                "filters": manifest.get("filters") or {},
                "channel_filter": delivery.get("channel_filter"),
                "delivery": {"eligible_count": delivery.get("eligible_count"),
                             "exported_count": delivery.get("exported_count")},
            })

    return Handler


def create_server(run_id, *, port=0, idle_timeout=1800):
    """Create the bound server without serving; used by serve() and tests."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise AppError("VALIDATION_ERROR", "端口必须为 0 至 65535 的整数")
    if isinstance(idle_timeout, bool) or not isinstance(idle_timeout, (int, float)) or not 1 <= idle_timeout <= 86400:
        raise AppError("VALIDATION_ERROR", "空闲超时必须在 1 至 86400 秒之间")
    state = live.read_state(run_id)
    if not state["exists"]:
        live.refresh(run_id, force=True)
        state = live.read_state(run_id)
    if not state["exists"]:
        raise AppError("NO_PROJECTION", "无法生成成果页面；请检查批次与本地目录", 409)
    token = secrets.token_urlsafe(32)
    config = ViewerConfig(run_id, token, int(idle_timeout))
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(config))
    server.daemon_threads = True
    server.timeout = 1
    config.port = server.server_address[1]
    return server, config, token, state


def serve(run_id, *, port=0, idle_timeout=1800, open_browser=False, announce=True):
    """Run until inactivity or Ctrl+C; returns a small shutdown record."""
    server, config, token, state = create_server(run_id, port=port, idle_timeout=idle_timeout)
    url = "http://127.0.0.1:%d/?token=%s" % (config.port, token)
    if announce:
        print(json.dumps({"run_id": run_id, "url": url, "host": "127.0.0.1", "port": config.port,
                          "expires_in_seconds": int(idle_timeout),
                          "projection_generated_at": state["generated_at"],
                          "export_formats": ["xlsx", "csv"]}, ensure_ascii=False), flush=True)
    if open_browser:
        import webbrowser
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        while True:
            server.handle_request()
            if config.idle_seconds() >= idle_timeout:
                break
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    if announce:
        print("查看服务已停止（无活动、断线或用户中断）；已保存的页面数据仍在本机。",
              file=sys.stderr, flush=True)
    return {"closed": True, "run_id": run_id, "port": config.port,
            "idle_seconds": round(config.idle_seconds(), 1)}
