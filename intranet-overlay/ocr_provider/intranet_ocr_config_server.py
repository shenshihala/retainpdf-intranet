"""Tiny stdlib HTTP service for intranet OCR settings.

GET/PUT ``/api/v1/intranet/ocr-config`` persist under the data root and return
masked tokens. The RetainPDF web nginx proxies this path; wrappers never call
the HTTP service themselves — ``local_command_driver`` reads the same file.

Run inside the overlay sidecar (do not enable insecure mode in Docker):

    python3 intranet_ocr_config_server.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from intranet_ocr_config import (
    IntranetOcrConfigConflict,
    IntranetOcrConfigError,
    IntranetOcrConfigUnavailable,
    default_config_path,
    public_view,
    update_record,
)

CONFIG_PATH = "/api/v1/intranet/ocr-config"
MAX_BODY_BYTES = 64 * 1024


def load_api_keys(
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    env = environ if environ is not None else os.environ
    keys: list[str] = []
    raw = str(env.get("RETAIN_INTRANET_OCR_CONFIG_API_KEYS", "") or "")
    keys.extend(part.strip() for part in raw.split(",") if part.strip())
    auth_file = str(env.get("RETAIN_INTRANET_OCR_CONFIG_AUTH_FILE", "") or "").strip()
    if auth_file:
        path = Path(auth_file)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IntranetOcrConfigUnavailable("内网 OCR 配置接口无法读取访问密钥") from exc
        listed = parsed.get("api_keys") if isinstance(parsed, dict) else None
        if not isinstance(listed, list):
            raise IntranetOcrConfigUnavailable("内网 OCR 配置接口的访问密钥格式无效")
        for item in listed:
            if isinstance(item, str) and item.strip():
                keys.append(item.strip())
    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def auth_allows(presented: str, configured: list[str]) -> bool:
    candidate = str(presented or "")
    if not candidate or not configured:
        return False
    for expected in configured:
        try:
            if secrets.compare_digest(candidate, expected):
                return True
        except (TypeError, ValueError):
            continue
    return False


def insecure_auth_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return str(env.get("RETAIN_INTRANET_OCR_CONFIG_ALLOW_INSECURE", "") or "").strip() == "1"


def _error_body(code: str, message: str, status: int) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "error": {
            "code": code,
            "http_status": status,
            "details": {},
        },
    }


class IntranetOcrConfigHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "intranet-ocr-config"
    # BaseHTTPRequestHandler writes this onto stderr; keep it free of bodies.
    sys_version = ""

    config_path: Path = Path("/data/secrets/intranet-ocr-config.json")
    api_keys: list[str] = []
    allow_insecure: bool = False

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, {"code": "OK", "message": "ok", "data": {"ok": True}})
            return
        if path != CONFIG_PATH:
            self._send(404, _error_body("NOT_FOUND", "路径不存在", 404))
            return
        if not self._authorized():
            return
        try:
            view = public_view(path=self.config_path)
        except IntranetOcrConfigError as exc:
            self._send(exc.status, _error_body(exc.code, str(exc), exc.status))
            return
        self._send(200, {"code": "OK", "message": "ok", "data": view})

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler name
        path = urlsplit(self.path).path
        if path != CONFIG_PATH:
            self._send(404, _error_body("NOT_FOUND", "路径不存在", 404))
            return
        if not self._authorized():
            return
        try:
            payload = self._read_json()
            record = update_record(payload, path=self.config_path)
            view = public_view(record, path=self.config_path)
        except IntranetOcrConfigError as exc:
            self._send(exc.status, _error_body(exc.code, str(exc), exc.status))
            return
        self._send(200, {"code": "OK", "message": "ok", "data": view})

    def do_POST(self) -> None:  # noqa: N802
        self._send(405, _error_body("METHOD_NOT_ALLOWED", "请使用 GET 或 PUT", 405))

    def log_message(self, fmt: str, *args: Any) -> None:
        # Default logging includes the request line only; still avoid passing
        # through arbitrary format values that might be a body or a header.
        try:
            status = args[1] if len(args) > 1 else ""
            sys.stderr.write(
                f"[intranet_ocr_config] {self.command} {urlsplit(self.path).path} {status}\n"
            )
        except Exception:
            sys.stderr.write("[intranet_ocr_config] request\n")

    def _authorized(self) -> bool:
        if self.allow_insecure:
            return True
        presented = str(self.headers.get("X-API-Key", "") or "")
        try:
            keys = list(self.api_keys)
        except Exception:
            keys = []
        if not keys:
            self._send(
                401,
                _error_body("UNAUTHORIZED", "内网 OCR 配置接口未配置访问密钥", 401),
            )
            return False
        if not auth_allows(presented, keys):
            self._send(401, _error_body("UNAUTHORIZED", "访问密钥无效", 401))
            return False
        return True

    def _read_json(self) -> Any:
        raw_length = str(self.headers.get("Content-Length", "") or "").strip()
        if not raw_length:
            raise IntranetOcrConfigError("缺少 Content-Length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise IntranetOcrConfigError("Content-Length 无效") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise IntranetOcrConfigError("请求体过大")
        body = self.rfile.read(length)
        if not body:
            raise IntranetOcrConfigError("请求体不能为空")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise IntranetOcrConfigError("请求体必须是 JSON") from exc

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            return


def build_server(
    *,
    bind: str,
    port: int,
    config_path: Path,
    api_keys: list[str],
    allow_insecure: bool,
) -> ThreadingHTTPServer:
    class BoundHandler(IntranetOcrConfigHandler):
        pass

    BoundHandler.config_path = config_path
    BoundHandler.api_keys = list(api_keys)
    BoundHandler.allow_insecure = allow_insecure
    return ThreadingHTTPServer((bind, port), BoundHandler)


def main(argv: list[str] | None = None) -> int:
    del argv
    bind = str(os.environ.get("RETAIN_INTRANET_OCR_CONFIG_BIND", "0.0.0.0") or "").strip() or "0.0.0.0"
    raw_port = str(os.environ.get("RETAIN_INTRANET_OCR_CONFIG_PORT", "8080") or "").strip()
    try:
        port = int(raw_port)
    except ValueError:
        print("[intranet_ocr_config] invalid port", file=sys.stderr)
        return 2
    config_path = default_config_path()
    try:
        api_keys = load_api_keys()
    except IntranetOcrConfigError as exc:
        print(f"[intranet_ocr_config] {exc}", file=sys.stderr)
        return 2
    allow_insecure = insecure_auth_enabled()
    if allow_insecure:
        print("[intranet_ocr_config] WARNING: API key check is disabled", flush=True)
    elif not api_keys:
        print("[intranet_ocr_config] no API keys configured; requests will be rejected", flush=True)
    try:
        server = build_server(
            bind=bind,
            port=port,
            config_path=config_path,
            api_keys=api_keys,
            allow_insecure=allow_insecure,
        )
    except OSError as exc:
        print(f"[intranet_ocr_config] bind failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"[intranet_ocr_config] listening {bind}:{port} file_present={config_path.is_file()}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
