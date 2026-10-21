"""Mock HTTP coverage for intranet MinerU local_command wrapper."""

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
from retainpdf_pipeline.ocr.document_schema.provider_adapters.mineru import (
    looks_like_mineru_layout,
)
from retainpdf_pipeline.ocr.ocr_provider import intranet_mineru_wrapper as wrapper
from retainpdf_pipeline.ocr.ocr_provider.local_command_driver import run_local_command_ocr_to_job_dir
from retainpdf_pipeline.ocr.ocr_provider.drivers import normalize_provider_name


def _legacy_middle_json(*, text: str = "intranet mock mineru") -> dict:
    return {
        "_backend": "pipeline",
        "_version_name": "2.0-mock",
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [320.0, 480.0],
                "para_blocks": [
                    {
                        "type": "text",
                        "bbox": [72.0, 60.0, 220.0, 90.0],
                        "index": 0,
                        "lines": [
                            {
                                "bbox": [72.0, 60.0, 220.0, 90.0],
                                "spans": [
                                    {
                                        "type": "text",
                                        "content": text,
                                        "bbox": [72.0, 60.0, 220.0, 90.0],
                                        "score": 0.99,
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "discarded_blocks": [],
            }
        ],
    }


class _FileParseHandler(BaseHTTPRequestHandler):
    last_content_type: str | None = None
    last_auth: str | None = None
    last_body: bytes | None = None
    mode: str = "legacy_string"  # legacy_string | legacy_object | docvortex | error

    def log_message(self, format: str, *args) -> None:  # noqa: A003 - stdlib signature
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        type(self).last_body = raw
        type(self).last_content_type = self.headers.get("Content-Type")
        type(self).last_auth = self.headers.get("Authorization")
        if self.path.rstrip("/") != "/file_parse":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')
            return

        mode = type(self).mode
        if mode == "error":
            encoded = json.dumps({"error": "boom"}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        middle = _legacy_middle_json()
        if mode == "docvortex":
            middle = {
                "schema": "docvortex.middle",
                "schema_version": "2.0",
                "pages": [{"page_idx": 0, "blocks": [{"type": "text", "content": [{"type": "text", "content": "x"}]}]}],
                "is_full_document": True,
            }
            results_middle = middle
        elif mode == "legacy_object":
            results_middle = middle
        else:
            results_middle = json.dumps(middle)

        payload = {
            "backend": "pipeline",
            "version": "2.0-mock",
            "results": {
                "sample": {
                    "middle_json": results_middle,
                }
            },
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture()
def mock_file_parse_server():
    _FileParseHandler.last_body = None
    _FileParseHandler.last_auth = None
    _FileParseHandler.last_content_type = None
    _FileParseHandler.mode = "legacy_string"
    server = HTTPServer(("127.0.0.1", 0), _FileParseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_resolve_file_parse_url_appends_path() -> None:
    assert (
        wrapper.resolve_file_parse_url("http://ocr.intranet:30000/")
        == "http://ocr.intranet:30000/file_parse"
    )
    assert (
        wrapper.resolve_file_parse_url("http://ocr.intranet:30000/file_parse")
        == "http://ocr.intranet:30000/file_parse"
    )


def test_resolve_backend_aliases() -> None:
    assert wrapper.resolve_backend("vlm") == "vlm-auto-engine"
    assert wrapper.resolve_backend("pipeline") == "pipeline"
    assert wrapper.resolve_backend("custom-backend") == "custom-backend"


def test_normalize_file_parse_string_middle_json() -> None:
    middle = _legacy_middle_json(text="normalized")
    payload = wrapper.normalize_mineru_payload(
        {
            "backend": "pipeline",
            "version": "x",
            "results": {"doc": {"middle_json": json.dumps(middle)}},
        }
    )
    assert looks_like_mineru_layout(payload)
    assert payload["pdf_info"][0]["para_blocks"][0]["lines"][0]["spans"][0]["content"] == "normalized"
    assert payload["_meta"]["source"] == "intranet_mineru_file_parse"


def test_normalize_converts_docvortex_middle_with_page_sizes() -> None:
    payload = wrapper.normalize_mineru_payload(
        {
            "files": [
                {
                    "content": {
                        "middle_json": {
                            "schema": "docvortex.middle",
                            "schema_version": "2.0",
                            "pages": [
                                {
                                    "page_idx": 0,
                                    "blocks": [
                                        {
                                            "type": "doc_title",
                                            "bbox": [0.1, 0.2, 0.9, 0.3],
                                            "index": 0,
                                            "content": [{"type": "text", "content": "Hello"}],
                                        },
                                        {
                                            "type": "header",
                                            "bbox": [0.1, 0.01, 0.5, 0.05],
                                            "index": 1,
                                            "content": [{"type": "text", "content": "hdr"}],
                                        },
                                    ],
                                }
                            ],
                        }
                    }
                }
            ]
        },
        page_sizes=[[612.0, 792.0]],
    )
    assert wrapper.looks_like_legacy_middle(payload)
    assert payload["_meta"]["source"] == "intranet_mineru_v1"
    assert payload["_meta"]["schema_in"] == "docvortex.middle"
    title = payload["pdf_info"][0]["para_blocks"][0]
    assert title["type"] == "doc_title"
    assert abs(title["bbox"][0] - 0.1 * 612) < 0.01
    assert payload["pdf_info"][0]["discarded_blocks"][0]["type"] == "header"


def test_normalize_docvortex_requires_page_geometry() -> None:
    with pytest.raises(RuntimeError, match="source_pdf_path|page_sizes"):
        wrapper.normalize_mineru_payload(
            {
                "results": {
                    "doc": {
                        "middle_json": {
                            "schema": "docvortex.middle",
                            "schema_version": "2.0",
                            "pages": [{"page_idx": 0, "blocks": []}],
                        }
                    }
                }
            }
        )


def test_wrapper_posts_file_parse_and_writes_mineru_raw(
    tmp_path: Path,
    mock_file_parse_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    target = tmp_path / "raw" / "payload.json"
    monkeypatch.setenv(wrapper.ENV_BASE_URL, mock_file_parse_server)
    monkeypatch.setenv(wrapper.ENV_TOKEN, "secret-token")
    monkeypatch.setenv(wrapper.ENV_AUTH_SCHEME, "bearer")
    monkeypatch.setenv(wrapper.ENV_BACKEND, "vlm")
    monkeypatch.setenv(wrapper.ENV_LANGUAGE, "en")
    monkeypatch.setenv(
        wrapper.ENV_OPTIONAL_JSON,
        json.dumps({"parse_method": "ocr", "notWhitelisted": 1}),
    )

    written = wrapper.run_wrapper(source_pdf=source, raw_payload_path=target)
    assert written == target.resolve()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert looks_like_mineru_layout(payload)
    assert (
        payload["pdf_info"][0]["para_blocks"][0]["lines"][0]["spans"][0]["content"]
        == "intranet mock mineru"
    )
    assert _FileParseHandler.last_body is not None
    body_text = _FileParseHandler.last_body.decode("utf-8", errors="replace")
    assert "return_middle_json" in body_text
    assert "vlm-auto-engine" in body_text
    assert "parse_method" in body_text
    assert "notWhitelisted" not in body_text
    assert "multipart/form-data" in str(_FileParseHandler.last_content_type or "")
    assert _FileParseHandler.last_auth == "Bearer secret-token"


def test_wrapper_dry_run_writes_mineru_shaped_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    target = tmp_path / "payload.json"
    monkeypatch.delenv(wrapper.ENV_BASE_URL, raising=False)
    written = wrapper.run_wrapper(source_pdf=source, raw_payload_path=target, dry_run=True)
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert looks_like_mineru_layout(payload)
    assert payload["_meta"]["dry_run"] is True


def test_intranet_mineru_local_command_uses_mineru_adapter(
    tmp_path: Path,
    mock_file_parse_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_root = tmp_path / "20261021-intranet-mineru"
    job_dirs = resolve_job_dirs(job_root)
    ensure_job_dirs(job_dirs)
    source_pdf = job_dirs.source_dir / "book.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setenv(wrapper.ENV_BASE_URL, mock_file_parse_server)
    monkeypatch.delenv(wrapper.ENV_DRY_RUN, raising=False)

    result = run_local_command_ocr_to_job_dir(
        SimpleNamespace(
            provider="intranet_mineru",
            ocr_provider_kind="local_command",
            local_ocr_command=(
                f"{sys.executable} -m "
                "retainpdf_pipeline.ocr.ocr_provider.intranet_mineru_wrapper"
            ),
            local_ocr_raw_provider="mineru",
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
    assert normalized["source"]["provider"] == "mineru"
    assert normalized["pages"][0]["blocks"][0]["text"] == "intranet mock mineru"
    raw = json.loads((job_dirs.ocr_dir / "local_raw" / "payload.json").read_text(encoding="utf-8"))
    assert looks_like_mineru_layout(raw)


def test_ocr_providers_json_registers_intranet_mineru() -> None:
    import importlib

    import retainpdf_pipeline.ocr.ocr_provider.drivers as drivers
    import retainpdf_pipeline.ocr.ocr_provider_config as provider_config

    importlib.reload(provider_config)
    drivers = importlib.reload(drivers)
    assert "intranet_mineru" in provider_config.configured_local_command_providers()
    assert "intranet_mineru" in drivers.list_registered_ocr_provider_drivers()
    definition = provider_config.ocr_provider_definition("intranet_mineru")
    assert definition.get("kind") == "local_command"
    assert (
        provider_config.ocr_provider_options("intranet_mineru")
        .get("raw_provider", {})
        .get("default")
        == "mineru"
    )
    assert normalize_provider_name("intranet_mineru") == "intranet_mineru"


def test_convert_docvortex_sample_m_mid_fake_page_size() -> None:
    sample = Path("/workspace/agent-tools/m-mid.json")
    if not sample.is_file():
        pytest.skip("m-mid.json sample not present on box")
    envelope = json.loads(sample.read_text(encoding="utf-8"))
    middle = envelope["files"][0]["content"]["middle_json"]
    if isinstance(middle, str):
        middle = json.loads(middle)
    legacy = wrapper.convert_docvortex_middle_to_legacy(
        middle, page_sizes=[[612.0, 792.0]] * 4
    )
    assert wrapper.looks_like_legacy_middle(legacy)
    assert len(legacy["pdf_info"]) == 4
    titles = [b for b in legacy["pdf_info"][0]["para_blocks"] if b.get("type") == "doc_title"]
    assert titles
    bbox = titles[0]["bbox"]
    assert abs(bbox[0] - 0.175 * 612) < 0.5
    assert abs(bbox[1] - 0.199 * 792) < 0.5
