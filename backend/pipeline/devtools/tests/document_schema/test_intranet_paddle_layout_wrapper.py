"""Mock HTTP coverage for intranet PaddleOCR-VL local_command wrapper."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from retainpdf_pipeline.foundation.shared.job_dirs import ensure_job_dirs
from retainpdf_pipeline.foundation.shared.job_dirs import resolve_job_dirs
from retainpdf_pipeline.ocr.document_schema.provider_adapters.paddle.adapter import (
    looks_like_paddle_layout,
)
from retainpdf_pipeline.ocr.ocr_provider import intranet_paddle_layout_wrapper as wrapper
from retainpdf_pipeline.ocr.ocr_provider.local_command_driver import run_local_command_ocr_to_job_dir
from retainpdf_pipeline.ocr.ocr_provider_config import OCR_PROVIDER_CONFIG_ENV
from retainpdf_pipeline.ocr.ocr_provider.drivers import list_registered_ocr_provider_drivers
from retainpdf_pipeline.ocr.ocr_provider.drivers import normalize_provider_name


class _LayoutParsingHandler(BaseHTTPRequestHandler):
    last_body: dict | None = None
    last_auth: str | None = None

    def log_message(self, format: str, *args) -> None:  # noqa: A003 - stdlib signature
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            body = {}
        type(self).last_body = body if isinstance(body, dict) else {}
        type(self).last_auth = self.headers.get("Authorization")
        if self.path.rstrip("/") != "/layout-parsing":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"errorCode":404,"errorMsg":"not found"}')
            return
        payload = {
            "logId": "mock-1",
            "errorCode": 0,
            "errorMsg": "Success",
            "result": {
                "layoutParsingResults": [
                    {
                        "prunedResult": {
                            "width": 320,
                            "height": 480,
                            "parsing_res_list": [
                                {
                                    "block_label": "text",
                                    "block_content": "intranet mock ocr",
                                    "block_bbox": [72.0, 60.0, 220.0, 90.0],
                                }
                            ],
                        },
                        "markdown": {"text": "intranet mock ocr", "images": {}},
                        "outputImages": {},
                    }
                ],
                "dataInfo": {
                    "type": "pdf",
                    "numPages": 1,
                    "pages": [{"width": 320, "height": 480}],
                },
            },
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture()
def mock_layout_server():
    _LayoutParsingHandler.last_body = None
    _LayoutParsingHandler.last_auth = None
    server = HTTPServer(("127.0.0.1", 0), _LayoutParsingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_resolve_layout_parsing_url_appends_path() -> None:
    assert (
        wrapper.resolve_layout_parsing_url("http://ocr.intranet:8080/")
        == "http://ocr.intranet:8080/layout-parsing"
    )
    assert (
        wrapper.resolve_layout_parsing_url("http://ocr.intranet:8080/layout-parsing")
        == "http://ocr.intranet:8080/layout-parsing"
    )


def test_normalize_layout_payload_synthesizes_data_info_when_missing() -> None:
    payload = wrapper.normalize_layout_payload(
        {
            "errorCode": 0,
            "result": {
                "layoutParsingResults": [
                    {
                        "prunedResult": {
                            "width": 100,
                            "height": 200,
                            "parsing_res_list": [],
                        }
                    }
                ]
            },
        }
    )
    assert looks_like_paddle_layout(payload)
    assert payload["dataInfo"]["numPages"] == 1
    assert payload["dataInfo"]["pages"][0]["width"] == 100


def test_wrapper_posts_layout_parsing_and_writes_paddle_raw(
    tmp_path: Path,
    mock_layout_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    target = tmp_path / "raw" / "payload.json"
    monkeypatch.setenv(wrapper.ENV_BASE_URL, mock_layout_server)
    monkeypatch.setenv(wrapper.ENV_TOKEN, "secret-token")
    monkeypatch.setenv(wrapper.ENV_AUTH_SCHEME, "bearer")
    monkeypatch.setenv(
        wrapper.ENV_OPTIONAL_JSON,
        json.dumps({"useDocOrientationClassify": False, "notWhitelisted": 1}),
    )

    written = wrapper.run_wrapper(source_pdf=source, raw_payload_path=target)
    assert written == target.resolve()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert looks_like_paddle_layout(payload)
    assert payload["layoutParsingResults"][0]["markdown"]["text"] == "intranet mock ocr"
    assert _LayoutParsingHandler.last_body is not None
    assert _LayoutParsingHandler.last_body.get("fileType") == 0
    assert "file" in _LayoutParsingHandler.last_body
    assert _LayoutParsingHandler.last_body.get("useDocOrientationClassify") is False
    assert "notWhitelisted" not in _LayoutParsingHandler.last_body
    assert _LayoutParsingHandler.last_auth == "Bearer secret-token"


def test_intranet_paddle_local_command_uses_paddle_adapter(
    tmp_path: Path,
    mock_layout_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_root = tmp_path / "20261019-intranet-paddle"
    job_dirs = resolve_job_dirs(job_root)
    ensure_job_dirs(job_dirs)
    source_pdf = job_dirs.source_dir / "book.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setenv(wrapper.ENV_BASE_URL, mock_layout_server)
    monkeypatch.delenv(wrapper.ENV_DRY_RUN, raising=False)

    result = run_local_command_ocr_to_job_dir(
        SimpleNamespace(
            provider="intranet_paddle",
            ocr_provider_kind="local_command",
            local_ocr_command=(
                f"{sys.executable} -m "
                "retainpdf_pipeline.ocr.ocr_provider.intranet_paddle_layout_wrapper"
            ),
            local_ocr_raw_provider="paddle",
            file_path=str(source_pdf),
            job_root=str(job_dirs.root),
            source_dir=str(job_dirs.source_dir),
            ocr_dir=str(job_dirs.ocr_dir),
            translated_dir=str(job_dirs.translated_dir),
            rendered_dir=str(job_dirs.rendered_dir),
            artifacts_dir=str(job_dirs.artifacts_dir),
            logs_dir=str(job_dirs.logs_dir),
        )
    )

    assert result.normalized_json_path.exists()
    normalized = json.loads(result.normalized_json_path.read_text(encoding="utf-8"))
    assert normalized["source"]["provider"] == "paddle"
    assert normalized["pages"][0]["blocks"][0]["text"] == "intranet mock ocr"
    raw = json.loads((job_dirs.ocr_dir / "local_raw" / "payload.json").read_text(encoding="utf-8"))
    assert looks_like_paddle_layout(raw)


def test_ocr_providers_json_registers_intranet_paddle() -> None:
    import importlib

    import retainpdf_pipeline.ocr.ocr_provider.drivers as drivers
    import retainpdf_pipeline.ocr.ocr_provider_config as provider_config

    # Force reload against the repo config (not a temp override).
    importlib.reload(provider_config)
    drivers = importlib.reload(drivers)
    assert "intranet_paddle" in provider_config.configured_local_command_providers()
    assert "intranet_paddle" in drivers.list_registered_ocr_provider_drivers()
    definition = provider_config.ocr_provider_definition("intranet_paddle")
    assert definition.get("kind") == "local_command"
    assert (
        provider_config.ocr_provider_options("intranet_paddle")
        .get("raw_provider", {})
        .get("default")
        == "paddle"
    )
    assert normalize_provider_name("intranet_paddle") == "intranet_paddle"
