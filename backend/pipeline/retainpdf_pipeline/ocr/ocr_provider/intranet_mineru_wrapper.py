"""local_command wrapper: self-hosted MinerU sync parse → legacy middle.json.

Reads the RetainPDF local_command environment contract, calls an intranet
MinerU serving endpoint, and writes a raw JSON payload that the built-in
mineru adapter can consume (middle.json with pdf_info + para_blocks).

This path is intentionally separate from the cloud mineru provider
(async /api/v4/extract/task + zip bundle). Cloud mineru behavior is unchanged.

API modes (env RETAIN_INTRANET_MINERU_API = v1 | legacy | auto, default auto):
  - v1:     POST /v1/file_parse  (octet-stream body, Bearer auth, MinerU 4.0)
            Response may contain docvortex.middle 2.0; converted to legacy
            middle.json using source PDF page sizes (PyMuPDF / fitz).
  - legacy: POST /file_parse    (multipart/form-data, return_middle_json=true)
  - auto:   if BASE_URL path ends with /file_parse → legacy;
            if ends with /v1/file_parse → v1;
            else if RETAIN_INTRANET_MINERU_TOKEN is set → prefer /v1/file_parse;
            else → /file_parse.

V1 query params: filename, response_format=json, wait_timeout, tier
(RETAIN_INTRANET_MINERU_TIER, default basic), output_formats=middle_json
(+ markdown when RETAIN_INTRANET_MINERU_V1_MARKDOWN is truthy).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# --- Environment contract ----------------------------------------------------

ENV_SOURCE_PDF = "RETAIN_OCR_SOURCE_PDF"
ENV_RAW_PAYLOAD_JSON = "RETAIN_OCR_RAW_PAYLOAD_JSON"
ENV_OCR_CREDENTIAL = "RETAIN_OCR_CREDENTIAL"

ENV_BASE_URL = "RETAIN_INTRANET_MINERU_BASE_URL"
ENV_TOKEN = "RETAIN_INTRANET_MINERU_TOKEN"
ENV_TIMEOUT = "RETAIN_INTRANET_MINERU_TIMEOUT_SECONDS"
ENV_AUTH_SCHEME = "RETAIN_INTRANET_MINERU_AUTH_SCHEME"
ENV_BACKEND = "RETAIN_INTRANET_MINERU_BACKEND"
ENV_LANGUAGE = "RETAIN_INTRANET_MINERU_LANGUAGE"
ENV_DISABLE_FORMULA = "RETAIN_INTRANET_MINERU_DISABLE_FORMULA"
ENV_DISABLE_TABLE = "RETAIN_INTRANET_MINERU_DISABLE_TABLE"
ENV_OPTIONAL_JSON = "RETAIN_INTRANET_MINERU_OPTIONAL_JSON"
ENV_DRY_RUN = "RETAIN_INTRANET_MINERU_DRY_RUN"
ENV_API = "RETAIN_INTRANET_MINERU_API"
ENV_TIER = "RETAIN_INTRANET_MINERU_TIER"
ENV_V1_MARKDOWN = "RETAIN_INTRANET_MINERU_V1_MARKDOWN"

DEFAULT_TIMEOUT_SECONDS = 1800.0
FILE_PARSE_PATH = "/file_parse"
V1_FILE_PARSE_PATH = "/v1/file_parse"
DEFAULT_BACKEND = "pipeline"
DEFAULT_LANGUAGE = "ch"
DEFAULT_TIER = "basic"
DEFAULT_API_MODE = "auto"

# Cloud model_version "vlm" maps to local mineru-api backend names.
BACKEND_ALIASES = {
    "vlm": "vlm-auto-engine",
    "pipeline": "pipeline",
    "hybrid": "hybrid-auto-engine",
    "vlm-auto-engine": "vlm-auto-engine",
    "hybrid-auto-engine": "hybrid-auto-engine",
    "vlm-http-client": "vlm-http-client",
    "hybrid-http-client": "hybrid-http-client",
}

# Official mineru-api /file_parse optional form fields (conservative whitelist).
OPTIONAL_FORM_WHITELIST = frozenset(
    {
        "parse_method",
        "server_url",
        "start_page_id",
        "end_page_id",
        "output_dir",
        "return_md",
        "return_model_output",
        "return_content_list",
        "return_images",
        "response_format_zip",
    }
)

# docvortex block types that map to legacy discarded_blocks.
_DISCARDED_BLOCK_TYPES = frozenset(
    {"header", "footer", "page_number", "aside_text", "header_image", "footer_image"}
)

# Content node types treated as display/inline equations.
_EQUATION_CONTENT_TYPES = frozenset(
    {
        "equation",
        "interline_equation",
        "inline_equation",
        "equation_interline",
        "display_formula",
    }
)


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _env_truthy(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


def resolve_api_mode(
    base_url: str,
    *,
    api_mode: str | None = None,
    token: str | None = None,
) -> str:
    """Return 'v1' or 'legacy' after resolving auto / explicit mode."""
    raw = (api_mode if api_mode is not None else _env(ENV_API) or DEFAULT_API_MODE).strip().lower()
    if raw in {"v1", "legacy"}:
        return raw
    # auto
    trimmed = str(base_url or "").strip().rstrip("/")
    lower = trimmed.lower()
    if lower.endswith(FILE_PARSE_PATH) and not lower.endswith(V1_FILE_PARSE_PATH):
        return "legacy"
    if lower.endswith(V1_FILE_PARSE_PATH):
        return "v1"
    resolved_token = (
        token
        if token is not None
        else (_env(ENV_TOKEN) or _env(ENV_OCR_CREDENTIAL))
    )
    if str(resolved_token or "").strip():
        return "v1"
    return "legacy"


def resolve_file_parse_url(
    base_url: str,
    *,
    api_mode: str | None = None,
    token: str | None = None,
) -> str:
    """Resolve the POST URL for the selected API mode."""
    trimmed = str(base_url or "").strip().rstrip("/")
    if not trimmed:
        raise RuntimeError(
            f"{ENV_BASE_URL} is required (e.g. http://ocr.intranet:30000)"
        )
    lower = trimmed.lower()
    mode = resolve_api_mode(trimmed, api_mode=api_mode, token=token)
    if mode == "v1":
        if lower.endswith(V1_FILE_PARSE_PATH):
            return trimmed
        if lower.endswith(FILE_PARSE_PATH) and not lower.endswith(V1_FILE_PARSE_PATH):
            # Caller forced v1 but pointed at legacy path — rewrite.
            return trimmed[: -len(FILE_PARSE_PATH)] + V1_FILE_PARSE_PATH
        return f"{trimmed}{V1_FILE_PARSE_PATH}"
    # legacy
    if lower.endswith(V1_FILE_PARSE_PATH):
        return trimmed[: -len(V1_FILE_PARSE_PATH)] + FILE_PARSE_PATH
    if lower.endswith(FILE_PARSE_PATH):
        return trimmed
    return f"{trimmed}{FILE_PARSE_PATH}"


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
    return f"Bearer {token}"


def resolve_backend(raw: str) -> str:
    value = str(raw or "").strip() or DEFAULT_BACKEND
    return BACKEND_ALIASES.get(value.lower(), value)


def load_optional_form_fields() -> dict[str, Any]:
    raw = _env(ENV_OPTIONAL_JSON)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{ENV_OPTIONAL_JSON} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{ENV_OPTIONAL_JSON} must be a JSON object")
    out: dict[str, Any] = {}
    for key, value in parsed.items():
        name = str(key).strip()
        if name not in OPTIONAL_FORM_WHITELIST:
            print(
                f"[intranet_mineru] ignore non-whitelisted optional field: {name}",
                file=sys.stderr,
            )
            continue
        out[name] = value
    return out


def _coerce_middle_json(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def looks_like_legacy_middle(payload: dict[str, Any]) -> bool:
    pdf_info = payload.get("pdf_info")
    if not isinstance(pdf_info, list):
        return False
    if not pdf_info:
        return True
    first = pdf_info[0]
    return isinstance(first, dict) and "para_blocks" in first


def _is_docvortex_middle(payload: dict[str, Any]) -> bool:
    schema = str(payload.get("schema") or "").strip().lower()
    return schema in {"docvortex.middle", "docvortex.model"} or (
        "pages" in payload and "blocks" in (payload.get("pages") or [{}])[0]
        if isinstance(payload.get("pages"), list) and payload.get("pages")
        else False
    )


def _extract_from_file_parse_results(results: dict[str, Any]) -> dict[str, Any] | None:
    for _name, item in results.items():
        if not isinstance(item, dict):
            continue
        middle = _coerce_middle_json(item.get("middle_json"))
        if middle is not None:
            return middle
        for key in ("middle", "layout", "layout_json"):
            candidate = _coerce_middle_json(item.get(key))
            if candidate is not None:
                return candidate
    return None


def _extract_from_v1_files(files: list[Any]) -> dict[str, Any] | None:
    for item in files:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, dict):
            if looks_like_legacy_middle(content):
                return content
            for key in ("middle_json", "middle", "layout", "layout_json"):
                middle = _coerce_middle_json(content.get(key))
                if middle is not None:
                    return middle
        middle = _coerce_middle_json(content)
        if middle is not None:
            return middle
    return None


# --- docvortex.middle 2.0 → legacy mineru middle.json ------------------------


def _pdf_page_sizes(source_pdf_path: Path) -> list[list[float]]:
    try:
        import fitz  # PyMuPDF — present in the RetainPDF app image
    except ImportError as exc:  # pragma: no cover - box unit tests use page_sizes=
        raise RuntimeError(
            "PyMuPDF (fitz) is required to convert docvortex.middle using PDF page sizes"
        ) from exc
    doc = fitz.open(str(source_pdf_path))
    try:
        return [[float(page.rect.width), float(page.rect.height)] for page in doc]
    finally:
        doc.close()


def _denorm_bbox(
    bbox: Any,
    page_w: float,
    page_h: float,
) -> list[float]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return [0.0, 0.0, 0.0, 0.0]
    try:
        x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    except (TypeError, ValueError):
        return [0.0, 0.0, 0.0, 0.0]
    # Heuristic: already in PDF points if any coord > 1.5
    if max(abs(x0), abs(y0), abs(x1), abs(y1)) > 1.5:
        return [x0, y0, x1, y1]
    return [x0 * page_w, y0 * page_h, x1 * page_w, y1 * page_h]


def _join_text_nodes(nodes: list[Any]) -> str:
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            if node.strip():
                parts.append(node)
            continue
        if not isinstance(node, dict):
            continue
        ntype = str(node.get("type") or "").strip().lower()
        if ntype in {"text", "ref_text", ""} or not ntype:
            content = node.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
            elif isinstance(content, list):
                nested = _join_text_nodes(content)
                if nested:
                    parts.append(nested)
        elif ntype in _EQUATION_CONTENT_TYPES:
            content = node.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
    return "".join(parts) if len(parts) <= 1 else " ".join(p.strip() for p in parts if p.strip())


def _text_lines_from_content(
    content: Any,
    bbox: list[float],
    *,
    span_type: str = "text",
) -> list[dict[str, Any]]:
    """Build legacy lines/spans from a docvortex content array or string."""
    if isinstance(content, str):
        text = content
        if not text:
            return []
        return [
            {
                "bbox": list(bbox),
                "spans": [
                    {
                        "type": span_type,
                        "content": text,
                        "bbox": list(bbox),
                        "score": 1.0,
                    }
                ],
            }
        ]

    if not isinstance(content, list) or not content:
        return []

    # Prefer emitting one span per content node when they look like text/equation.
    spans: list[dict[str, Any]] = []
    for node in content:
        if isinstance(node, str):
            if node.strip():
                spans.append(
                    {
                        "type": "text",
                        "content": node,
                        "bbox": list(bbox),
                        "score": 1.0,
                    }
                )
            continue
        if not isinstance(node, dict):
            continue
        ntype = str(node.get("type") or "text").strip().lower() or "text"
        # Skip structural table/image carriers here — handled by dedicated builders.
        if ntype in {"table_body", "table_caption", "table_footnote", "image_body", "image_caption", "image_footnote"}:
            continue
        node_bbox = list(bbox)
        raw_bbox = node.get("bbox")
        if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) >= 4:
            # Caller already denormalized block bbox; content node bboxes may still
            # be normalized — leave as block bbox if we cannot know page size here.
            # (Block-level conversion denormalizes before calling this helper.)
            try:
                vals = [float(raw_bbox[i]) for i in range(4)]
                if max(abs(v) for v in vals) > 1.5:
                    node_bbox = vals
            except (TypeError, ValueError):
                pass
        if ntype in _EQUATION_CONTENT_TYPES:
            eq_content = node.get("content")
            if not isinstance(eq_content, str):
                eq_content = _join_text_nodes([node])
            span_t = (
                "inline_equation"
                if ntype == "inline_equation"
                else "interline_equation"
            )
            spans.append(
                {
                    "type": span_t,
                    "content": str(eq_content or ""),
                    "bbox": node_bbox,
                    "score": float(node.get("score") or 1.0),
                }
            )
            continue
        text = node.get("content")
        if isinstance(text, list):
            text = _join_text_nodes(text)
        if not isinstance(text, str):
            text = ""
        if not text and ntype not in {"text", "ref_text"}:
            continue
        spans.append(
            {
                "type": "text" if ntype in {"text", "ref_text", ""} else ntype,
                "content": text,
                "bbox": node_bbox,
                "score": float(node.get("score") or 1.0),
            }
        )

    if not spans:
        joined = _join_text_nodes(content)
        if not joined:
            return []
        spans = [
            {
                "type": span_type,
                "content": joined,
                "bbox": list(bbox),
                "score": 1.0,
            }
        ]
    return [{"bbox": list(bbox), "spans": spans}]


def _content_nodes_of_type(content: Any, *types: str) -> list[dict[str, Any]]:
    wanted = {t.lower() for t in types}
    out: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return out
    for node in content:
        if isinstance(node, dict) and str(node.get("type") or "").strip().lower() in wanted:
            out.append(node)
    return out


def _convert_table_block(
    block: dict[str, Any],
    *,
    page_w: float,
    page_h: float,
) -> dict[str, Any]:
    bbox = _denorm_bbox(block.get("bbox"), page_w, page_h)
    index = block.get("index", 0)
    nested: list[dict[str, Any]] = []
    content = block.get("content")

    for node in _content_nodes_of_type(content, "table_body"):
        nb = _denorm_bbox(node.get("bbox") or block.get("bbox"), page_w, page_h)
        html = node.get("content") if isinstance(node.get("content"), str) else ""
        nested.append(
            {
                "type": "table_body",
                "bbox": nb,
                "index": node.get("index", index),
                "lines": [
                    {
                        "bbox": nb,
                        "spans": [
                            {
                                "type": "table",
                                "html": html or "",
                                "bbox": nb,
                                "score": float(node.get("score") or 1.0),
                            }
                        ],
                    }
                ],
            }
        )

    for cap_type in ("table_caption", "table_footnote"):
        for node in _content_nodes_of_type(content, cap_type):
            nb = _denorm_bbox(node.get("bbox") or block.get("bbox"), page_w, page_h)
            text = node.get("content")
            if isinstance(text, list):
                text = _join_text_nodes(text)
            if not isinstance(text, str):
                text = str(text or "")
            nested.append(
                {
                    "type": cap_type,
                    "bbox": nb,
                    "index": node.get("index", index),
                    "lines": _text_lines_from_content(text, nb),
                }
            )

    if not nested:
        # Fallback: treat whole block content as HTML or text table_body.
        html = ""
        if isinstance(content, str):
            html = content
        elif isinstance(content, list):
            for node in content:
                if isinstance(node, dict) and isinstance(node.get("content"), str):
                    maybe = node["content"]
                    if "<table" in maybe.lower() or maybe.strip().startswith("<"):
                        html = maybe
                        break
            if not html:
                html = _join_text_nodes(content)
        nested.append(
            {
                "type": "table_body",
                "bbox": bbox,
                "index": index,
                "lines": [
                    {
                        "bbox": bbox,
                        "spans": [
                            {
                                "type": "table",
                                "html": html or "",
                                "bbox": bbox,
                                "score": 1.0,
                            }
                        ],
                    }
                ],
            }
        )

    return {
        "type": "table",
        "bbox": bbox,
        "index": index,
        "blocks": nested,
    }


def _convert_image_block(
    block: dict[str, Any],
    *,
    page_w: float,
    page_h: float,
) -> dict[str, Any]:
    bbox = _denorm_bbox(block.get("bbox"), page_w, page_h)
    index = block.get("index", 0)
    content = block.get("content")
    nested: list[dict[str, Any]] = []

    body_nodes = _content_nodes_of_type(content, "image_body", "figure_body")
    if not body_nodes:
        # Best-effort: empty image_body so adapter still sees an image group.
        nested.append(
            {
                "type": "image_body",
                "bbox": bbox,
                "index": index,
                "lines": [
                    {
                        "bbox": bbox,
                        "spans": [
                            {
                                "type": "image",
                                "image_path": str(
                                    block.get("image_path")
                                    or block.get("path")
                                    or ""
                                ),
                                "bbox": bbox,
                                "score": 1.0,
                            }
                        ],
                    }
                ],
            }
        )
    for node in body_nodes:
        nb = _denorm_bbox(node.get("bbox") or block.get("bbox"), page_w, page_h)
        nested.append(
            {
                "type": "image_body",
                "bbox": nb,
                "index": node.get("index", index),
                "lines": [
                    {
                        "bbox": nb,
                        "spans": [
                            {
                                "type": "image",
                                "image_path": str(
                                    node.get("image_path")
                                    or node.get("path")
                                    or block.get("image_path")
                                    or ""
                                ),
                                "bbox": nb,
                                "score": float(node.get("score") or 1.0),
                            }
                        ],
                    }
                ],
            }
        )

    for cap_type in ("image_caption", "image_footnote", "figure_caption"):
        mapped = (
            "image_caption"
            if cap_type in {"image_caption", "figure_caption"}
            else "image_footnote"
        )
        for node in _content_nodes_of_type(content, cap_type):
            nb = _denorm_bbox(node.get("bbox") or block.get("bbox"), page_w, page_h)
            text = node.get("content")
            if isinstance(text, list):
                text = _join_text_nodes(text)
            if not isinstance(text, str):
                text = str(text or "")
            nested.append(
                {
                    "type": mapped,
                    "bbox": nb,
                    "index": node.get("index", index),
                    "lines": _text_lines_from_content(text, nb),
                }
            )

    # If only text content and no image path, fall back to a text block.
    if (
        len(nested) == 1
        and nested[0]["type"] == "image_body"
        and not (nested[0]["lines"][0]["spans"][0].get("image_path") or "")
    ):
        joined = _join_text_nodes(content) if isinstance(content, list) else (
            content if isinstance(content, str) else ""
        )
        if joined:
            return {
                "type": "text",
                "bbox": bbox,
                "index": index,
                "lines": _text_lines_from_content(joined, bbox),
            }

    return {
        "type": "image",
        "bbox": bbox,
        "index": index,
        "blocks": nested,
    }


def _convert_simple_block(
    block: dict[str, Any],
    *,
    page_w: float,
    page_h: float,
    force_type: str | None = None,
) -> dict[str, Any]:
    bbox = _denorm_bbox(block.get("bbox"), page_w, page_h)
    raw_type = str(force_type or block.get("type") or "text").strip().lower() or "text"
    # Map a few docvortex aliases onto names the mineru adapter already knows.
    type_aliases = {
        "title": "title",
        "paragraph_title": "paragraph_title",
        "section_title": "paragraph_title",
        "doc_title": "doc_title",
        "list": "list",
        "index": "index",
        "ref_text": "ref_text",
        "abstract": "abstract",
        "code": "code",
        "equation": "interline_equation",
        "interline_equation": "interline_equation",
        "equation_interline": "interline_equation",
        "display_formula": "interline_equation",
        "inline_equation": "text",  # inline alone is rare as a block; keep as text+span
    }
    out_type = type_aliases.get(raw_type, raw_type)

    content = block.get("content")
    # list / index containers may nest item blocks in content.
    if out_type in {"list", "index"} and isinstance(content, list):
        item_blocks: list[dict[str, Any]] = []
        for node in content:
            if not isinstance(node, dict):
                continue
            ntype = str(node.get("type") or "text").strip().lower()
            if ntype in {"list_item", "text", "ref_text", ""} or not ntype:
                item_blocks.append(
                    _convert_simple_block(
                        {**node, "type": "text"},
                        page_w=page_w,
                        page_h=page_h,
                        force_type="text",
                    )
                )
        if item_blocks:
            result: dict[str, Any] = {
                "type": out_type,
                "bbox": bbox,
                "index": block.get("index", 0),
                "blocks": item_blocks,
            }
            if out_type == "list":
                result.setdefault("sub_type", "text")
            return result

    span_type = "text"
    if out_type == "interline_equation":
        span_type = "interline_equation"
    lines = _text_lines_from_content(content, bbox, span_type=span_type)
    if not lines and out_type == "interline_equation":
        # content may be a bare latex string under another key
        latex = block.get("latex") or block.get("text") or ""
        lines = _text_lines_from_content(str(latex), bbox, span_type="interline_equation")

    result = {
        "type": out_type,
        "bbox": bbox,
        "index": block.get("index", 0),
        "lines": lines,
    }
    if "level" in block:
        result["level"] = block.get("level")
    return result


def _convert_docvortex_block(
    block: dict[str, Any],
    *,
    page_w: float,
    page_h: float,
) -> tuple[str, dict[str, Any]]:
    """Return ('para'|'discarded', legacy_block)."""
    raw_type = str(block.get("type") or "text").strip().lower() or "text"
    if raw_type in _DISCARDED_BLOCK_TYPES:
        return "discarded", _convert_simple_block(
            block, page_w=page_w, page_h=page_h, force_type=raw_type
        )
    if raw_type == "table":
        return "para", _convert_table_block(block, page_w=page_w, page_h=page_h)
    if raw_type in {"image", "figure", "chart"}:
        return "para", _convert_image_block(block, page_w=page_w, page_h=page_h)
    return "para", _convert_simple_block(block, page_w=page_w, page_h=page_h)


def convert_docvortex_middle_to_legacy(
    middle: dict[str, Any],
    source_pdf_path: str | Path | None = None,
    *,
    page_sizes: list[list[float]] | None = None,
) -> dict[str, Any]:
    """Convert docvortex.middle 2.0 (normalized bboxes) into legacy mineru middle.json.

    Page sizes come from ``page_sizes`` when provided; otherwise from the source PDF
    via PyMuPDF (fitz). Each page_size is ``[width, height]`` in PDF points.
    """
    if not isinstance(middle, dict):
        raise RuntimeError("docvortex middle must be a JSON object")
    pages = middle.get("pages")
    if not isinstance(pages, list):
        raise RuntimeError("docvortex middle missing pages[]")

    sizes = list(page_sizes) if page_sizes is not None else None
    if sizes is None:
        if source_pdf_path is None or not str(source_pdf_path).strip():
            raise RuntimeError(
                "convert_docvortex_middle_to_legacy requires source_pdf_path "
                "or page_sizes to denormalize bboxes"
            )
        sizes = _pdf_page_sizes(Path(source_pdf_path).expanduser().resolve())

    pdf_info: list[dict[str, Any]] = []
    for i, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        if i < len(sizes):
            page_w, page_h = float(sizes[i][0]), float(sizes[i][1])
        elif sizes:
            page_w, page_h = float(sizes[-1][0]), float(sizes[-1][1])
        else:
            page_w, page_h = 612.0, 792.0
        page_idx = page.get("page_idx", i)
        para_blocks: list[dict[str, Any]] = []
        discarded_blocks: list[dict[str, Any]] = []
        for block in page.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            bucket, converted = _convert_docvortex_block(
                block, page_w=page_w, page_h=page_h
            )
            if bucket == "discarded":
                discarded_blocks.append(converted)
            else:
                para_blocks.append(converted)
        pdf_info.append(
            {
                "page_idx": page_idx,
                "page_size": [page_w, page_h],
                "para_blocks": para_blocks,
                "discarded_blocks": discarded_blocks,
            }
        )

    metadata = middle.get("metadata") if isinstance(middle.get("metadata"), dict) else {}
    producer = metadata.get("producer") if isinstance(metadata.get("producer"), dict) else {}
    backend = str(producer.get("name") or "intranet-mineru-v1")
    version_name = str(producer.get("version") or middle.get("schema_version") or "4.0")

    return {
        "_backend": backend,
        "_version_name": version_name,
        "pdf_info": pdf_info,
        "_meta": {
            "source": "intranet_mineru_v1",
            "transport": "v1_file_parse",
            "schema_in": str(middle.get("schema") or "docvortex.middle"),
            "schema_version_in": middle.get("schema_version"),
        },
    }


def normalize_mineru_payload(
    response_json: dict[str, Any],
    *,
    source_pdf_path: str | Path | None = None,
    page_sizes: list[list[float]] | None = None,
    transport: str | None = None,
) -> dict[str, Any]:
    """Turn a mineru-api JSON envelope into mineru raw (legacy middle.json).

    When the extracted middle is docvortex.middle, convert it using the source
    PDF page sizes (or explicit ``page_sizes``).
    """
    if not isinstance(response_json, dict):
        raise RuntimeError("mineru response must be a JSON object")

    used_conversion = False
    if looks_like_legacy_middle(response_json):
        payload = dict(response_json)
    else:
        middle: dict[str, Any] | None = None
        results = response_json.get("results")
        if isinstance(results, dict) and results:
            middle = _extract_from_file_parse_results(results)
        if middle is None:
            files = response_json.get("files")
            if isinstance(files, list) and files:
                middle = _extract_from_v1_files(files)
        if middle is None:
            middle = _coerce_middle_json(response_json.get("middle_json"))
        if middle is None and _is_docvortex_middle(response_json):
            middle = response_json
        if middle is None:
            raise RuntimeError(
                "mineru response missing middle_json / pdf_info "
                f"(top-level keys={sorted(response_json.keys())})"
            )
        if _is_docvortex_middle(middle) and not looks_like_legacy_middle(middle):
            payload = convert_docvortex_middle_to_legacy(
                middle,
                source_pdf_path,
                page_sizes=page_sizes,
            )
            used_conversion = True
        elif not looks_like_legacy_middle(middle):
            raise RuntimeError(
                "extracted middle_json is not legacy mineru middle "
                f"(keys={sorted(middle.keys())})"
            )
        else:
            payload = dict(middle)

    meta = dict(payload.get("_meta") or {})
    if used_conversion:
        meta.setdefault("source", "intranet_mineru_v1")
        meta.setdefault("transport", transport or "v1_file_parse")
    else:
        meta.setdefault("source", "intranet_mineru_file_parse")
        meta.setdefault("transport", transport or "file_parse")
    if "backend" in response_json and "backend" not in meta:
        meta["provider_backend"] = response_json.get("backend")
    if "version" in response_json and "provider_version" not in meta:
        meta["provider_version"] = response_json.get("version")
    if "tier" in response_json and "tier" not in meta:
        meta["tier"] = response_json.get("tier")
    payload["_meta"] = meta
    return payload


def _encode_multipart(
    fields: dict[str, Any],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    """Build multipart/form-data body. files: (field, filename, bytes, content_type)."""
    boundary = f"----RetainIntranetMinerU{uuid.uuid4().hex}"
    lines: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        lines.append(f"--{boundary}\r\n".encode("ascii"))
        lines.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        lines.append(text.encode("utf-8"))
        lines.append(b"\r\n")
    for field_name, filename, content, content_type in files:
        lines.append(f"--{boundary}\r\n".encode("ascii"))
        disposition = (
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"\r\n'
        )
        lines.append(disposition.encode("utf-8"))
        lines.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        lines.append(content)
        lines.append(b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode("ascii"))
    body = b"".join(lines)
    content_type_header = f"multipart/form-data; boundary={boundary}"
    return body, content_type_header


def build_file_parse_form(
    source_pdf: Path,
    *,
    backend: str,
    language: str,
    formula_enable: bool,
    table_enable: bool,
    optional_fields: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str, bytes, str]]]:
    fields: dict[str, Any] = {
        "return_middle_json": True,
        "return_md": False,
        "backend": resolve_backend(backend),
        "lang_list": language or DEFAULT_LANGUAGE,
        "formula_enable": formula_enable,
        "table_enable": table_enable,
    }
    for key, value in dict(optional_fields or {}).items():
        if key in OPTIONAL_FORM_WHITELIST:
            fields[key] = value
    if fields.get("response_format_zip") in {True, "true", "True", "1"}:
        print(
            "[intranet_mineru] forcing response_format_zip=false "
            "(local_command writes JSON payload)",
            file=sys.stderr,
        )
        fields["response_format_zip"] = False

    mime, _ = mimetypes.guess_type(source_pdf.name)
    content_type = mime or "application/pdf"
    file_bytes = source_pdf.read_bytes()
    files = [("files", source_pdf.name, file_bytes, content_type)]
    return fields, files


def post_file_parse(
    *,
    url: str,
    fields: dict[str, Any],
    files: list[tuple[str, str, bytes, str]],
    token: str = "",
    auth_scheme: str = "bearer",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    body, content_type = _encode_multipart(fields, files)
    headers = {"Content-Type": content_type, "Accept": "application/json"}
    auth = resolve_auth_header(token, auth_scheme)
    if auth:
        headers["Authorization"] = auth

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
            status = getattr(response, "status", None) or response.getcode()
            response_content_type = str(response.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"file_parse HTTP {exc.code} for {url}: {detail[:2000]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"file_parse request failed for {url}: {exc}") from exc

    if status and int(status) != 200:
        raise RuntimeError(f"file_parse HTTP {status} for {url}")

    if "application/zip" in response_content_type.lower() or raw[:2] == b"PK":
        raise RuntimeError(
            "file_parse returned a ZIP body; set return_middle_json=true and "
            "response_format_zip=false so intranet_mineru can write mineru raw JSON"
        )

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"file_parse returned non-JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("file_parse JSON root must be an object")
    if "error" in parsed and "results" not in parsed and "pdf_info" not in parsed:
        raise RuntimeError(f"file_parse error: {parsed.get('error')}")
    return parsed


def post_v1_file_parse(
    *,
    url: str,
    source_pdf: Path,
    token: str = "",
    auth_scheme: str = "bearer",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    tier: str = DEFAULT_TIER,
    include_markdown: bool = False,
) -> dict[str, Any]:
    """POST raw PDF bytes to MinerU 4.0 /v1/file_parse."""
    file_bytes = source_pdf.read_bytes()
    # Match the proven MinerU 4.0 V1 call. Do not send wait_timeout unless the
    # deployment documents it — some gateways close the connection on unknown
    # or oversized wait_timeout values.
    query: list[tuple[str, str]] = [
        ("filename", source_pdf.name),
        ("response_format", "json"),
        ("tier", str(tier or DEFAULT_TIER).strip() or DEFAULT_TIER),
        ("output_formats", "middle_json"),
    ]
    if include_markdown:
        query.append(("output_formats", "markdown"))
    # Optional wait_timeout only when explicitly requested via env.
    raw_wait = _env("RETAIN_INTRANET_MINERU_V1_WAIT_TIMEOUT")
    if raw_wait:
        query.append(("wait_timeout", str(int(float(raw_wait)))))
    elif timeout_seconds and timeout_seconds != DEFAULT_TIMEOUT_SECONDS:
        # Caller passed an explicit non-default timeout — forward as wait hint.
        query.append(("wait_timeout", str(int(max(1.0, timeout_seconds)))))
    # Preserve existing query on url if any.
    split = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(split.query, keep_blank_values=True)
    full_query = existing + query
    request_url = urllib.parse.urlunsplit(
        (
            split.scheme,
            split.netloc,
            split.path,
            urllib.parse.urlencode(full_query, doseq=True),
            split.fragment,
        )
    )

    # Let urllib set Content-Length from the body; duplicating it has caused
    # RemoteDisconnected against some MinerU gateways from Docker.
    headers = {
        "Content-Type": "application/octet-stream",
        "Accept": "application/json",
    }
    auth = resolve_auth_header(token, auth_scheme)
    if auth:
        headers["Authorization"] = auth

    request = urllib.request.Request(
        request_url, data=file_bytes, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
            status = getattr(response, "status", None) or response.getcode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        # Never echo Authorization / token material from error bodies beyond a trim.
        raise RuntimeError(
            f"v1 file_parse HTTP {exc.code} for {split.path}: {detail[:2000]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"v1 file_parse request failed for {split.path}: {exc}") from exc

    if status and int(status) != 200:
        raise RuntimeError(f"v1 file_parse HTTP {status} for {split.path}")

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"v1 file_parse returned non-JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("v1 file_parse JSON root must be an object")
    status_field = str(parsed.get("status") or "").strip().lower()
    if status_field and status_field not in {"ok", "success", "succeeded", "completed", "done"}:
        # Some gateways use status=failed with an error message.
        err = parsed.get("error") or parsed.get("message") or status_field
        if status_field in {"failed", "error", "cancelled", "canceled"}:
            raise RuntimeError(f"v1 file_parse status={status_field}: {err}")
    return parsed


def build_dry_run_payload(source_pdf: Path) -> dict[str, Any]:
    """Minimal mineru-shaped middle.json for offline wiring checks (not real OCR)."""
    return {
        "_backend": "intranet-mineru-dry-run",
        "_version_name": "dry-run",
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [612.0, 792.0],
                "para_blocks": [
                    {
                        "type": "text",
                        "bbox": [72, 72, 420, 120],
                        "index": 0,
                        "lines": [
                            {
                                "bbox": [72, 72, 420, 120],
                                "spans": [
                                    {
                                        "type": "text",
                                        "content": f"[dry-run] {source_pdf.name}",
                                        "bbox": [72, 72, 420, 120],
                                        "score": 1.0,
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "discarded_blocks": [],
            }
        ],
        "_meta": {
            "source": "intranet_mineru_file_parse",
            "transport": "file_parse",
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
    backend: str | None = None,
    language: str | None = None,
    formula_enable: bool | None = None,
    table_enable: bool | None = None,
    dry_run: bool | None = None,
    api_mode: str | None = None,
    tier: str | None = None,
) -> Path:
    source = Path(
        source_pdf if source_pdf is not None else _env(ENV_SOURCE_PDF)
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
        else _env_truthy(ENV_DRY_RUN)
    )
    if use_dry_run:
        payload = build_dry_run_payload(source)
        write_raw_payload(target, payload)
        print(f"[intranet_mineru] dry-run wrote {target}", flush=True)
        return target

    resolved_base = base_url if base_url is not None else _env(ENV_BASE_URL)
    resolved_token = (
        token
        if token is not None
        else (_env(ENV_TOKEN) or _env(ENV_OCR_CREDENTIAL))
    )
    resolved_mode = resolve_api_mode(
        resolved_base, api_mode=api_mode, token=resolved_token
    )
    url = resolve_file_parse_url(
        resolved_base, api_mode=resolved_mode, token=resolved_token
    )
    resolved_scheme = (
        auth_scheme if auth_scheme is not None else (_env(ENV_AUTH_SCHEME) or "bearer")
    )
    if timeout_seconds is None:
        raw_timeout = _env(ENV_TIMEOUT)
        timeout_seconds = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
    if timeout_seconds <= 0:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    if resolved_mode == "v1":
        resolved_tier = tier if tier is not None else (_env(ENV_TIER) or DEFAULT_TIER)
        include_md = _env_truthy(ENV_V1_MARKDOWN)
        print(
            f"[intranet_mineru] POST {url} (v1) file={source.name} "
            f"tier={resolved_tier} timeout={timeout_seconds:.0f}s "
            f"auth={'yes' if str(resolved_token or '').strip() else 'no'}",
            flush=True,
        )
        response_json = post_v1_file_parse(
            url=url,
            source_pdf=source,
            token=resolved_token,
            auth_scheme=resolved_scheme,
            timeout_seconds=timeout_seconds,
            tier=resolved_tier,
            include_markdown=include_md,
        )
        payload = normalize_mineru_payload(
            response_json,
            source_pdf_path=source,
            transport="v1_file_parse",
        )
    else:
        resolved_backend = (
            backend if backend is not None else (_env(ENV_BACKEND) or DEFAULT_BACKEND)
        )
        resolved_language = (
            language if language is not None else (_env(ENV_LANGUAGE) or DEFAULT_LANGUAGE)
        )
        if formula_enable is None:
            formula_enable = not _env_truthy(ENV_DISABLE_FORMULA)
        if table_enable is None:
            table_enable = not _env_truthy(ENV_DISABLE_TABLE)

        optional_fields = load_optional_form_fields()
        fields, files = build_file_parse_form(
            source,
            backend=resolved_backend,
            language=resolved_language,
            formula_enable=formula_enable,
            table_enable=table_enable,
            optional_fields=optional_fields,
        )
        print(
            f"[intranet_mineru] POST {url} (legacy) file={source.name} "
            f"backend={fields.get('backend')} lang={fields.get('lang_list')} "
            f"timeout={timeout_seconds:.0f}s",
            flush=True,
        )
        response_json = post_file_parse(
            url=url,
            fields=fields,
            files=files,
            token=resolved_token,
            auth_scheme=resolved_scheme,
            timeout_seconds=timeout_seconds,
        )
        payload = normalize_mineru_payload(
            response_json,
            source_pdf_path=source,
            transport="file_parse",
        )

    write_raw_payload(target, payload)
    page_count = len(payload.get("pdf_info") or [])
    print(
        f"[intranet_mineru] wrote raw payload pages={page_count} path={target}",
        flush=True,
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "RetainPDF local_command wrapper for intranet MinerU "
            "(legacy POST /file_parse or MinerU 4.0 POST /v1/file_parse)"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write a synthetic mineru-shaped payload without calling HTTP.",
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
    parser.add_argument(
        "--api",
        default="",
        choices=["", "v1", "legacy", "auto"],
        help=f"Override {ENV_API} (v1|legacy|auto)",
    )
    args = parser.parse_args(argv)
    try:
        run_wrapper(
            source_pdf=Path(args.source_pdf) if args.source_pdf.strip() else None,
            raw_payload_path=(
                Path(args.raw_payload_json) if args.raw_payload_json.strip() else None
            ),
            base_url=args.base_url.strip() or None,
            dry_run=True if args.dry_run else None,
            api_mode=args.api.strip() or None,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary: surface any failure
        print(f"[intranet_mineru] error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BACKEND_ALIASES",
    "DEFAULT_BACKEND",
    "DEFAULT_LANGUAGE",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TIER",
    "ENV_API",
    "ENV_AUTH_SCHEME",
    "ENV_BACKEND",
    "ENV_BASE_URL",
    "ENV_DISABLE_FORMULA",
    "ENV_DISABLE_TABLE",
    "ENV_DRY_RUN",
    "ENV_LANGUAGE",
    "ENV_OPTIONAL_JSON",
    "ENV_TIER",
    "ENV_TIMEOUT",
    "ENV_TOKEN",
    "ENV_V1_MARKDOWN",
    "OPTIONAL_FORM_WHITELIST",
    "V1_FILE_PARSE_PATH",
    "build_dry_run_payload",
    "build_file_parse_form",
    "convert_docvortex_middle_to_legacy",
    "load_optional_form_fields",
    "looks_like_legacy_middle",
    "main",
    "normalize_mineru_payload",
    "post_file_parse",
    "post_v1_file_parse",
    "resolve_api_mode",
    "resolve_auth_header",
    "resolve_backend",
    "resolve_file_parse_url",
    "run_wrapper",
    "write_raw_payload",
]
