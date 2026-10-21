"""Persisted intranet OCR settings for the RetainPDF Docker overlay.

UI saves land in a JSON file under the data root (default
``/data/secrets/intranet-ocr-config.json``). ``local_command_driver`` injects
non-empty values into ``RETAIN_INTRANET_*`` before spawning a wrapper.
Empty stored fields leave the process environment alone, so ``app.env`` stays
the fallback. Secrets are never included in log lines or public views.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:  # POSIX is the production path.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


SCHEMA = "retainpdf_intranet_ocr_config_v1"
VIEW_SCHEMA = "retainpdf_intranet_ocr_config_view_v1"
FILE_NAME = "intranet-ocr-config.json"
MAX_FILE_BYTES = 64 * 1024
MAX_URL_LENGTH = 500
MAX_TOKEN_LENGTH = 512
MAX_TIER_LENGTH = 32

AUTH_SCHEMES = ("bearer", "token", "raw", "none")
API_MODES = ("auto", "v1", "legacy")

# Public field -> environment variable. Wrappers already read these names.
ENV_BY_PROVIDER: dict[str, dict[str, str]] = {
    "intranet_paddle": {
        "base_url": "RETAIN_INTRANET_PADDLE_BASE_URL",
        "token": "RETAIN_INTRANET_PADDLE_TOKEN",
        "auth_scheme": "RETAIN_INTRANET_PADDLE_AUTH_SCHEME",
    },
    "intranet_mineru": {
        "base_url": "RETAIN_INTRANET_MINERU_BASE_URL",
        "token": "RETAIN_INTRANET_MINERU_TOKEN",
        "auth_scheme": "RETAIN_INTRANET_MINERU_AUTH_SCHEME",
        "api": "RETAIN_INTRANET_MINERU_API",
        "tier": "RETAIN_INTRANET_MINERU_TIER",
    },
}

# Wrapper defaults used only when neither the UI nor the environment set a value.
# These match intranet_*_wrapper.py (auth falls back to bearer; MinerU api/tier
# fall back to auto/basic). They are display defaults, not stored overrides.
FIELD_DEFAULTS: dict[str, dict[str, str]] = {
    "intranet_paddle": {
        "auth_scheme": "bearer",
    },
    "intranet_mineru": {
        "auth_scheme": "bearer",
        "api": "auto",
        "tier": "basic",
    },
}

SECRET_FIELDS = frozenset({"token"})
NON_SECRET_FIELDS = {
    "intranet_paddle": ("base_url", "auth_scheme"),
    "intranet_mineru": ("base_url", "auth_scheme", "api", "tier"),
}

_WRITE_LOCK = threading.Lock()


class IntranetOcrConfigError(RuntimeError):
    """Safe configuration error. The message must never include a secret."""

    code = "BAD_REQUEST"
    status = 400


class IntranetOcrConfigConflict(IntranetOcrConfigError):
    code = "INTRANET_OCR_CONFIG_REVISION_CONFLICT"
    status = 409


class IntranetOcrConfigUnavailable(IntranetOcrConfigError):
    code = "INTRANET_OCR_CONFIG_UNAVAILABLE"
    status = 500


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    explicit = str(env.get("RETAIN_INTRANET_OCR_CONFIG_PATH", "") or "").strip()
    if explicit:
        return Path(explicit)
    data_root = str(env.get("RUST_API_DATA_ROOT", "") or "").strip() or "/data"
    return Path(data_root) / "secrets" / FILE_NAME


def masked_secret(value: str) -> str:
    """Same shape as RetainPDF AI runtime ``masked_secret``: never reversible."""
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if len(normalized) >= 8:
        return f"••••{normalized[-4:]}"
    return "••••"


def empty_record() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "revision": 0,
        "intranet_paddle": _empty_provider("intranet_paddle"),
        "intranet_mineru": _empty_provider("intranet_mineru"),
    }


def _empty_provider(provider: str) -> dict[str, str]:
    fields = ["token", *NON_SECRET_FIELDS[provider]]
    return {name: "" for name in fields}


def apply_persisted_intranet_env(
    env: dict[str, str],
    provider_name: str,
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Copy non-empty UI overrides into ``env`` for one intranet provider.

    Returns the applied field names (never values). Missing file and non-intranet
    providers are no-ops so cloud paddle/mineru and env-only intranet jobs keep
    working. A present but unreadable file raises a secret-free error.
    """
    provider = str(provider_name or "").strip().lower()
    mapping = ENV_BY_PROVIDER.get(provider)
    if not mapping:
        return []
    record = load_record(path, environ=environ)
    stored = record.get(provider) or {}
    applied: list[str] = []
    for field, env_name in mapping.items():
        value = str(stored.get(field, "") or "").strip()
        if not value:
            continue
        env[env_name] = value
        applied.append(field)
    return applied


def format_apply_log(provider_name: str, fields: list[str]) -> str:
    provider = str(provider_name or "").strip().lower()
    if not fields:
        return ""
    # Field names only. Callers must not append values.
    return (
        f"[intranet_ocr_config] {provider}: applied persisted fields "
        + ",".join(fields)
    )


def load_record(
    path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    target = Path(path) if path is not None else default_config_path(environ)
    if not target.exists():
        return empty_record()
    if target.is_symlink():
        raise IntranetOcrConfigUnavailable("内网 OCR 配置文件路径不安全")
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise IntranetOcrConfigUnavailable("内网 OCR 配置文件无法读取") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise IntranetOcrConfigUnavailable("内网 OCR 配置文件过大")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntranetOcrConfigUnavailable("内网 OCR 配置文件不是合法 JSON")
    try:
        return _validate_record(parsed)
    except IntranetOcrConfigError as exc:
        raise IntranetOcrConfigUnavailable(str(exc)) from exc


def public_view(
    record: dict[str, Any] | None = None,
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    stored = record if record is not None else load_record(path, environ=env)
    providers: dict[str, Any] = {}
    for provider, mapping in ENV_BY_PROVIDER.items():
        provider_stored = stored.get(provider) or _empty_provider(provider)
        view: dict[str, Any] = {}
        for field in (*NON_SECRET_FIELDS[provider],):
            effective, source = _effective(
                str(provider_stored.get(field, "") or ""),
                str(env.get(mapping[field], "") or ""),
                FIELD_DEFAULTS.get(provider, {}).get(field, ""),
            )
            view[field] = effective
            view[f"{field}_source"] = source
        token_stored = str(provider_stored.get("token", "") or "").strip()
        token_env = str(env.get(mapping["token"], "") or "").strip()
        if token_stored:
            token_source = "ui"
            token_value = token_stored
        elif token_env:
            token_source = "env"
            token_value = token_env
        else:
            token_source = "empty"
            token_value = ""
        view["token_configured"] = bool(token_value)
        view["token_source"] = token_source
        view["token_masked"] = masked_secret(token_value)
        providers[provider] = view
    return {
        "schema": VIEW_SCHEMA,
        "revision": int(stored.get("revision") or 0),
        "config_path_set": True,
        "providers": providers,
    }


def update_record(
    payload: Any,
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Merge a PUT body into the stored overrides and return the stored record."""
    if not isinstance(payload, dict):
        raise IntranetOcrConfigError("请求体必须是 JSON 对象")
    allowed = {"expected_revision", *ENV_BY_PROVIDER}
    unknown = set(payload) - allowed
    if unknown:
        raise IntranetOcrConfigError("请求包含无法识别的字段")
    expected = payload.get("expected_revision", None)
    if expected is not None and (
        isinstance(expected, bool) or not isinstance(expected, int) or expected < 0
    ):
        raise IntranetOcrConfigError("expected_revision 必须是非负整数")

    target = Path(path) if path is not None else default_config_path(environ)
    _ensure_parent(target.parent)
    with _WRITE_LOCK, _exclusive_file_lock(target.parent):
        current = load_record(target, environ=environ)
        current_revision = int(current.get("revision") or 0)
        if expected is not None and expected != current_revision:
            raise IntranetOcrConfigConflict("内网 OCR 配置已被其他请求更新，请刷新后重试。")
        merged = {
            "schema": SCHEMA,
            "revision": current_revision + 1,
            "intranet_paddle": dict(current["intranet_paddle"]),
            "intranet_mineru": dict(current["intranet_mineru"]),
        }
        for provider in ENV_BY_PROVIDER:
            if provider not in payload:
                continue
            incoming = payload[provider]
            if not isinstance(incoming, dict):
                raise IntranetOcrConfigError("提供商配置必须是 JSON 对象")
            merged[provider] = _merge_provider(
                provider,
                merged[provider],
                incoming,
            )
        _write_record(target, merged)
    return merged


def _merge_provider(
    provider: str,
    current: Mapping[str, str],
    incoming: Mapping[str, Any],
) -> dict[str, str]:
    allowed = set(NON_SECRET_FIELDS[provider]) | {"token", "clear_token"}
    unknown = set(incoming) - allowed
    if unknown:
        raise IntranetOcrConfigError("请求包含无法识别的字段")
    merged = {key: str(current.get(key, "") or "") for key in _empty_provider(provider)}
    for field in NON_SECRET_FIELDS[provider]:
        if field not in incoming:
            continue
        raw = incoming[field]
        if raw is None:
            merged[field] = ""
            continue
        if not isinstance(raw, str):
            raise IntranetOcrConfigError("配置项必须是字符串")
        merged[field] = _validate_non_secret(provider, field, raw)
    clear = bool(incoming.get("clear_token"))
    if "token" in incoming and incoming["token"] is not None and not isinstance(incoming["token"], str):
        raise IntranetOcrConfigError("密钥必须是字符串")
    requested = str(incoming.get("token", "") or "").strip() if "token" in incoming else ""
    token_sent = "token" in incoming and bool(requested)
    if clear and token_sent:
        raise IntranetOcrConfigError("密钥不能同时保存和清除")
    if clear:
        merged["token"] = ""
    elif token_sent:
        merged["token"] = _validate_token(requested)
    return merged


def _validate_non_secret(provider: str, field: str, raw: str) -> str:
    value = raw.strip()
    if any(ord(ch) < 32 for ch in value):
        raise IntranetOcrConfigError("配置项包含无法使用的字符")
    if not value:
        return ""
    if field == "base_url":
        return _validate_base_url(value)
    if field == "auth_scheme":
        normalized = value.lower()
        if normalized not in AUTH_SCHEMES:
            raise IntranetOcrConfigError("认证方式只能是 bearer、token、raw 或 none")
        return normalized
    if field == "api":
        normalized = value.lower()
        if normalized not in API_MODES:
            raise IntranetOcrConfigError("API 模式只能是 auto、v1 或 legacy")
        return normalized
    if field == "tier":
        if len(value) > MAX_TIER_LENGTH or not _is_tier(value):
            raise IntranetOcrConfigError("档位只能包含字母、数字、点、下划线或短横线")
        return value
    raise IntranetOcrConfigError("配置项无法识别")


def _validate_base_url(value: str) -> str:
    if len(value) > MAX_URL_LENGTH:
        raise IntranetOcrConfigError("服务地址过长")
    if any(ch.isspace() for ch in value):
        raise IntranetOcrConfigError("服务地址不能包含空白")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise IntranetOcrConfigError("服务地址必须是 http 或 https URL")
    if parsed.username or parsed.password:
        raise IntranetOcrConfigError("服务地址不要携带账号或密钥")
    if parsed.query or parsed.fragment:
        raise IntranetOcrConfigError("服务地址不要带查询参数或片段，密钥请填在密钥栏")
    return value.rstrip("/")


def _validate_token(value: str) -> str:
    if len(value) > MAX_TOKEN_LENGTH:
        raise IntranetOcrConfigError("密钥过长")
    if any(ord(ch) < 32 for ch in value):
        raise IntranetOcrConfigError("密钥包含无法使用的字符")
    if value.startswith("•") or value.startswith("*"):
        raise IntranetOcrConfigError("请填写完整密钥，不要提交掩码")
    return value


def _is_tier(value: str) -> bool:
    if not value or not (value[0].isalnum()):
        return False
    return all(ch.isalnum() or ch in "._-" for ch in value)


def _effective(stored: str, env_value: str, default: str) -> tuple[str, str]:
    if stored.strip():
        return stored.strip(), "ui"
    if env_value.strip():
        return env_value.strip(), "env"
    if default.strip():
        return default.strip(), "default"
    return "", "empty"


def _validate_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise IntranetOcrConfigError("内网 OCR 配置文件的 schema 不受支持")
    unknown = set(value) - {"schema", "revision", *ENV_BY_PROVIDER}
    if unknown:
        raise IntranetOcrConfigError("内网 OCR 配置文件包含无法识别的字段")
    revision = value.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise IntranetOcrConfigError("内网 OCR 配置文件的 revision 无效")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "revision": revision,
    }
    for provider in ENV_BY_PROVIDER:
        raw = value.get(provider, {})
        if not isinstance(raw, dict):
            raise IntranetOcrConfigError("内网 OCR 配置文件包含无效的提供商配置")
        allowed = set(_empty_provider(provider))
        if set(raw) - allowed:
            raise IntranetOcrConfigError("内网 OCR 配置文件包含无法识别的字段")
        normalized = _empty_provider(provider)
        for field in allowed:
            item = raw.get(field, "")
            if not isinstance(item, str):
                raise IntranetOcrConfigError("内网 OCR 配置文件包含无效的值")
            if field == "token":
                normalized[field] = item.strip()
            elif item.strip():
                normalized[field] = _validate_non_secret(provider, field, item)
            else:
                normalized[field] = ""
        result[provider] = normalized
    return result


def _ensure_parent(directory: Path) -> None:
    if directory.is_symlink():
        raise IntranetOcrConfigUnavailable("内网 OCR 配置目录不安全")
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise IntranetOcrConfigUnavailable("内网 OCR 配置目录无法创建") from exc
    if not directory.is_dir() or directory.is_symlink():
        raise IntranetOcrConfigUnavailable("内网 OCR 配置目录不安全")
    if os.name == "posix":
        directory.chmod(0o700)


def _write_record(path: Path, record: dict[str, Any]) -> None:
    if path.is_symlink():
        raise IntranetOcrConfigUnavailable("内网 OCR 配置文件路径不安全")
    normalized = _validate_record(record)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise IntranetOcrConfigError("内网 OCR 配置过大")
    directory = path.parent
    temporary = directory / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        if os.name == "posix":
            path.chmod(0o600)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


class _FileLock:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._handle: Any = None

    def __enter__(self) -> "_FileLock":
        lock_path = self._directory / ".intranet-ocr-config.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        self._handle = os.fdopen(descriptor, "r+", closefd=True)
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            if self._handle is not None and fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            if self._handle is not None:
                self._handle.close()
                self._handle = None


def _exclusive_file_lock(directory: Path) -> _FileLock:
    return _FileLock(directory)


__all__ = [
    "API_MODES",
    "AUTH_SCHEMES",
    "ENV_BY_PROVIDER",
    "FILE_NAME",
    "SCHEMA",
    "VIEW_SCHEMA",
    "IntranetOcrConfigConflict",
    "IntranetOcrConfigError",
    "IntranetOcrConfigUnavailable",
    "apply_persisted_intranet_env",
    "default_config_path",
    "format_apply_log",
    "load_record",
    "masked_secret",
    "public_view",
    "update_record",
]
