"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
SUPPLEMENT_SINGLE_RE = re.compile(r"^/api/supplements/(\d+)$")
SUPPLEMENT_ACTION_RE = re.compile(r"^/api/supplements/(\d+)/([a-z_]+)$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "immigration-deadline/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _idem_key(self) -> str:
            return self.headers.get("Idempotency-Key", "").strip()

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "immigration-deadline", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(self._actor(), state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                if parsed.path == "/api/policies":
                    self._send(200, {"items": service.list_policies(self._actor())})
                    return
                if parsed.path == "/api/capacity":
                    query = parse_qs(parsed.query)
                    officer_id = query.get("officer_id", [""])[0]
                    day = query.get("day", ["0"])[0]
                    self._send(200, service.capacity_view(self._actor(), officer_id, int(day)))
                    return
                if parsed.path == "/api/supplements":
                    query = parse_qs(parsed.query)
                    self._send(200, {"items": service.list_supplements(
                        self._actor(),
                        record_id=int(query["record_id"][0]) if "record_id" in query else None,
                        status=query.get("status", [None])[0],
                        officer_id=query.get("officer_id", [None])[0],
                        day=int(query["day"][0]) if "day" in query else None)})
                    return
                match = SUPPLEMENT_SINGLE_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_supplement(self._actor(), int(match.group(1))))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                if parsed.path == "/api/policies":
                    self._send(201, service.publish_policy(self._actor(), body.get("data", body)))
                    return
                if parsed.path == "/api/policies/backfill":
                    self._send(200, {"backfilled": service.backfill_policy_versions(self._actor())})
                    return
                if parsed.path == "/api/capacity":
                    self._send(200, service.set_capacity(self._actor(), body.get("data", body)))
                    return
                supplement_issue = re.compile(r"^/api/records/(\d+)/supplements$").match(parsed.path)
                if supplement_issue:
                    data = body.get("data", body)
                    if self._idem_key() and "idem_key" not in data:
                        data["idem_key"] = self._idem_key()
                    self._send(201, service.issue_supplement(
                        self._actor(), int(supplement_issue.group(1)), data))
                    return
                if parsed.path == "/api/supplements/batch":
                    self._send(200, service.issue_supplement_batch(self._actor(), body.get("data", body)))
                    return
                match = SUPPLEMENT_ACTION_RE.match(parsed.path)
                if match:
                    task_id = int(match.group(1))
                    sub = match.group(2)
                    if sub == "confirm":
                        self._send(200, service.confirm_quota(self._actor(), task_id, self._idem_key()))
                        return
                    if sub == "respond":
                        self._send(200, service.respond_supplement(self._actor(), task_id, body.get("data", body)))
                        return
                    if sub == "release":
                        self._send(200, service.release_quota(self._actor(), task_id))
                        return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
