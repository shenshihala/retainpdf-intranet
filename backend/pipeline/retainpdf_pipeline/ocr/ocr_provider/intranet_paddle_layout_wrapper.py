"""local_command wrapper: self-hosted PaddleOCR-VL sync POST /layout-parsing.

Reads the RetainPDF local_command environment contract, calls an intranet
PaddleOCR-VL serving endpoint, and writes a raw JSON payload that the built-in
paddle adapter can consume (layoutParsingResults + dataInfo).

This path is intentionally separate from the cloud paddle provider
(async /api/v2/ocr/jobs). Cloud paddle behavior is unchanged.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import http.client
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# --- Environment contract ----------------------------------------------------

ENV_SOURCE_PDF = "RETAIN_OCR_SOURCE_PDF"
ENV_RAW_PAYLOAD_JSON = "RETAIN_OCR_RAW_PAYLOAD_JSON"
ENV_OCR_CREDENTIAL = "RETAIN_OCR_CREDENTIAL"

ENV_BASE_URL = "RETAIN_INTRANET_PADDLE_BASE_URL"
ENV_TOKEN = "RETAIN_INTRANET_PADDLE_TOKEN"
ENV_TIMEOUT = "RETAIN_INTRANET_PADDLE_TIMEOUT_SECONDS"
ENV_AUTH_SCHEME = "RETAIN_INTRANET_PADDLE_AUTH_SCHEME"
ENV_OPTIONAL_JSON = "RETAIN_INTRANET_PADDLE_OPTIONAL_JSON"
ENV_DRY_RUN = "RETAIN_INTRANET_PADDLE_DRY_RUN"

DEFAULT_TIMEOUT_SECONDS = 1800.0
LAYOUT_PARSING_PATH = "/layout-parsing"

# Official self-hosted /layout-parsing optional fields (top-level JSON body).
# Keep a conservative whitelist so unknown keys cannot be injected blindly.
OPTIONAL_PAYLOAD_WHITELIST = frozenset(
    {
        "useDocOrientationClassify",
        "useDocUnwarping",
        "useLayoutDetection",
        "useChartRecognition",
        "useSealRecognition",
        "useOcrForImageBlock",
        "mergeTables",
        "relevelTitles",
        "layoutShapeMode",
        "promptLabel",
        "repetitionPenalty",
        "temperature",
        "topP",
        "minPixels",
        "maxPixels",
        "layoutThreshold",
        "layoutNms",
        "layoutUnclipRatio",
        "mergeLayoutBlocks",
        "markdownIgnoreLabels",
        "prettifyMarkdown",
        "visualize",
        "max_num_input_imgs",
        "restructurePages",
        "useTextlineOrientation",
        "useRegionDetection",
        "useFormulaRecognition",
        "useTableRecognition",
    }
)

DEFAULT_OPTIONAL_PAYLOAD: dict[str, Any] = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
    # Keep responses small; HPS often closes early on multi-MB visualize payloads.
    "visualize": False,
}


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def resolve_layout_parsing_url(base_url: str) -> str:
    trimmed = str(base_url or "").strip().rstrip("/")
    if not trimmed:
        raise RuntimeError(
            f"{ENV_BASE_URL} is required (e.g. http://ocr.intranet:8080)"
        )
    if trimmed.lower().endswith(LAYOUT_PARSING_PATH):
        return trimmed
    return f"{trimmed}{LAYOUT_PARSING_PATH}"


def resolve_auth_header(token: str, scheme: str) -> str | None:
    token = str(token or "").strip()
    if not token:
        return None
    normalized = str(scheme or "").strip().lower() or "bearer"
    if normalized in {"none", "off", "disabled"}:
        return None
    if normalized == "token":
        return f"token {token}"
    if normalized == "raw":
        return token
    # Default / "bearer"
    return f"Bearer {token}"


def load_optional_payload() -> dict[str, Any]:
    payload = dict(DEFAULT_OPTIONAL_PAYLOAD)
    raw = _env(ENV_OPTIONAL_JSON)
    if not raw:
        return payload
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{ENV_OPTIONAL_JSON} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{ENV_OPTIONAL_JSON} must be a JSON object")
    for key, value in parsed.items():
        name = str(key).strip()
        if name not in OPTIONAL_PAYLOAD_WHITELIST:
            print(
                f"[intranet_paddle] ignore non-whitelisted optional field: {name}",
                file=sys.stderr,
            )
            continue
        payload[name] = value
    return payload


def infer_file_type(path: Path) -> int:
    # Official contract: 0 = PDF, 1 = image.
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return 0
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}:
        return 1
    # Default to PDF for RetainPDF local uploads.
    return 0


def build_request_body(source_pdf: Path, optional_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    file_bytes = source_pdf.read_bytes()
    file_data = base64.b64encode(file_bytes).decode("ascii")
    body: dict[str, Any] = {
        "file": file_data,
        "fileType": infer_file_type(source_pdf),
    }
    for key, value in dict(optional_payload or {}).items():
        if key in OPTIONAL_PAYLOAD_WHITELIST:
            body[key] = value
    return body


def synthesize_data_info(layout_results: list[Any]) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for item in layout_results:
        if not isinstance(item, dict):
            pages.append({})
            continue
        pruned = item.get("prunedResult") if isinstance(item.get("prunedResult"), dict) else {}
        page: dict[str, Any] = {}
        width = pruned.get("width")
        height = pruned.get("height")
        if isinstance(width, (int, float)):
            page["width"] = width
        if isinstance(height, (int, float)):
            page["height"] = height
        pages.append(page)
    return {
        "type": "pdf",
        "numPages": len(layout_results),
        "pages": pages,
    }


def normalize_layout_payload(response_json: dict[str, Any]) -> dict[str, Any]:
    """Turn a /layout-parsing HTTP JSON envelope into paddle-adapter raw payload."""
    if not isinstance(response_json, dict):
        raise RuntimeError("layout-parsing response must be a JSON object")

    error_code = response_json.get("errorCode")
    if error_code is not None and int(error_code) != 0:
        raise RuntimeError(
            "layout-parsing failed: "
            f"errorCode={error_code} errorMsg={response_json.get('errorMsg', '')} "
            f"logId={response_json.get('logId', '')}"
        )

    result = response_json.get("result")
    if isinstance(result, dict):
        payload = dict(result)
    elif "layoutParsingResults" in response_json:
        # Some gateways may already unwrap result.
        payload = {
            key: value
            for key, value in response_json.items()
            if key not in {"errorCode", "errorMsg", "logId"}
        }
    else:
        raise RuntimeError(
            "layout-parsing response missing result.layoutParsingResults "
            f"(top-level keys={sorted(response_json.keys())})"
        )

    layout_results = payload.get("layoutParsingResults")
    if not isinstance(layout_results, list):
        raise RuntimeError("layout-parsing result.layoutParsingResults must be a list")

    data_info = payload.get("dataInfo")
    if not isinstance(data_info, dict):
        payload["dataInfo"] = synthesize_data_info(layout_results)
    else:
        # Ensure pages meta length is usable; keep provider data as-is otherwise.
        payload["dataInfo"] = dict(data_info)

    meta = dict(payload.get("_meta") or {})
    meta.setdefault("source", "intranet_paddle_layout_parsing")
    meta.setdefault("transport", "layout_parsing")
    payload["_meta"] = meta
    return payload


def _read_http_body(response) -> bytes:
    """Read body; tolerate Content-Length mismatches common with large HPS replies."""
    try:
        return response.read()
    except http.client.IncompleteRead as exc:
        partial = exc.partial or b""
        missing = getattr(exc, "expected", None)
        print(
            f"[intranet_paddle] IncompleteRead bytes={len(partial)} "
            f"expected_more={missing!r}; trying partial",
            flush=True,
        )
        if partial:
            return partial
        raise


def post_layout_parsing(
    *,
    url: str,
    body: dict[str, Any],
    token: str = "",
    auth_scheme: str = "bearer",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = 3,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    auth = resolve_auth_header(token, auth_scheme)
    if auth:
        headers["Authorization"] = auth

    payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            url,
            data=payload_bytes,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = _read_http_body(response)
                status = getattr(response, "status", None) or response.getcode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"layout-parsing HTTP {exc.code} for {url}: {detail[:2000]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"layout-parsing request failed for {url}: {exc}") from exc
        except http.client.IncompleteRead as exc:
            last_error = exc
            print(
                f"[intranet_paddle] attempt {attempt}/{max_attempts} IncompleteRead: {exc}",
                flush=True,
            )
            if attempt < max_attempts:
                time.sleep(min(2 * attempt, 5))
                continue
            raise RuntimeError(
                f"layout-parsing IncompleteRead after {max_attempts} attempts for {url}: {exc}"
            ) from exc

        if status and int(status) != 200:
            raise RuntimeError(f"layout-parsing HTTP {status} for {url}")

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            last_error = exc
            print(
                f"[intranet_paddle] attempt {attempt}/{max_attempts} non-JSON "
                f"({len(raw)} bytes): {exc}",
                flush=True,
            )
            if attempt < max_attempts:
                time.sleep(min(2 * attempt, 5))
                continue
            raise RuntimeError(
                f"layout-parsing returned non-JSON body after {max_attempts} attempts "
                f"({len(raw)} bytes): {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("layout-parsing JSON root must be an object")
        if attempt > 1:
            print(f"[intranet_paddle] succeeded on attempt {attempt}", flush=True)
        return parsed

    raise RuntimeError(f"layout-parsing failed for {url}: {last_error}")


def build_dry_run_payload(source_pdf: Path) -> dict[str, Any]:
    """Minimal paddle-shaped payload for offline wiring checks (not real OCR)."""
    return {
        "layoutParsingResults": [
            {
                "prunedResult": {
                    "width": 612,
                    "height": 792,
                    "parsing_res_list": [
                        {
                            "block_label": "text",
                            "block_content": f"[dry-run] {source_pdf.name}",
                            "block_bbox": [72, 72, 420, 120],
                        }
                    ],
                },
                "markdown": {
                    "text": f"[dry-run] {source_pdf.name}",
                    "images": {},
                },
                "outputImages": {},
            }
        ],
        "dataInfo": {
            "type": "pdf",
            "numPages": 1,
            "pages": [{"width": 612, "height": 792}],
        },
        "_meta": {
            "source": "intranet_paddle_layout_parsing",
            "transport": "layout_parsing",
            "dry_run": True,
        },
    }


def write_raw_payload(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def run_wrapper(
    *,
    source_pdf: Path | None = None,
    raw_payload_path: Path | None = None,
    base_url: str | None = None,
    token: str | None = None,
    timeout_seconds: float | None = None,
    auth_scheme: str | None = None,
    dry_run: bool | None = None,
) -> Path:
    source = Path(
        source_pdf
        if source_pdf is not None
        else _env(ENV_SOURCE_PDF)
    ).expanduser()
    if not str(source):
        raise RuntimeError(f"{ENV_SOURCE_PDF} is required")
    source = source.resolve()
    if not source.is_file():
        raise RuntimeError(f"source PDF not found: {source}")

    target = Path(
        raw_payload_path
        if raw_payload_path is not None
        else _env(ENV_RAW_PAYLOAD_JSON)
    ).expanduser()
    if not str(target):
        raise RuntimeError(f"{ENV_RAW_PAYLOAD_JSON} is required")
    target = target.resolve()

    use_dry_run = (
        bool(dry_run)
        if dry_run is not None
        else _env(ENV_DRY_RUN).lower() in {"1", "true", "yes", "on"}
    )
    if use_dry_run:
        payload = build_dry_run_payload(source)
        write_raw_payload(target, payload)
        print(f"[intranet_paddle] dry-run wrote {target}", flush=True)
        return target

    resolved_base = base_url if base_url is not None else _env(ENV_BASE_URL)
    url = resolve_layout_parsing_url(resolved_base)
    resolved_token = (
        token
        if token is not None
        else (_env(ENV_TOKEN) or _env(ENV_OCR_CREDENTIAL))
    )
    resolved_scheme = auth_scheme if auth_scheme is not None else (_env(ENV_AUTH_SCHEME) or "bearer")
    if timeout_seconds is None:
        raw_timeout = _env(ENV_TIMEOUT)
        timeout_seconds = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
    if timeout_seconds <= 0:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    optional_payload = load_optional_payload()
    body = build_request_body(source, optional_payload)
    print(
        f"[intranet_paddle] POST {url} file={source.name} "
        f"fileType={body.get('fileType')} timeout={timeout_seconds:.0f}s",
        flush=True,
    )
    response_json = post_layout_parsing(
        url=url,
        body=body,
        token=resolved_token,
        auth_scheme=resolved_scheme,
        timeout_seconds=timeout_seconds,
    )
    payload = normalize_layout_payload(response_json)
    write_raw_payload(target, payload)
    page_count = len(payload.get("layoutParsingResults") or [])
    print(
        f"[intranet_paddle] wrote raw payload pages={page_count} path={target}",
        flush=True,
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "RetainPDF local_command wrapper for intranet PaddleOCR-VL "
            "POST /layout-parsing"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write a synthetic paddle-shaped payload without calling HTTP.",
    )
    parser.add_argument(
        "--source-pdf",
        default="",
        help=f"Override {ENV_SOURCE_PDF}",
    )
    parser.add_argument(
        "--raw-payload-json",
        default="",
        help=f"Override {ENV_RAW_PAYLOAD_JSON}",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help=f"Override {ENV_BASE_URL}",
    )
    args = parser.parse_args(argv)
    try:
        run_wrapper(
            source_pdf=Path(args.source_pdf) if args.source_pdf.strip() else None,
            raw_payload_path=Path(args.raw_payload_json) if args.raw_payload_json.strip() else None,
            base_url=args.base_url.strip() or None,
            dry_run=True if args.dry_run else None,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary: surface any failure
        print(f"[intranet_paddle] error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_OPTIONAL_PAYLOAD",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENV_AUTH_SCHEME",
    "ENV_BASE_URL",
    "ENV_DRY_RUN",
    "ENV_OPTIONAL_JSON",
    "ENV_TIMEOUT",
    "ENV_TOKEN",
    "OPTIONAL_PAYLOAD_WHITELIST",
    "build_dry_run_payload",
    "build_request_body",
    "load_optional_payload",
    "main",
    "normalize_layout_payload",
    "post_layout_parsing",
    "resolve_auth_header",
    "resolve_layout_parsing_url",
    "run_wrapper",
    "synthesize_data_info",
    "write_raw_payload",
]
