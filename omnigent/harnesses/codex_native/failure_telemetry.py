"""Bounded provider evidence from Codex's explicit terminal error payloads."""

from __future__ import annotations

from omnigent.native.failure_telemetry import FailureContext, normalize_failure_context


def codex_error_info(info: object) -> tuple[str | None, int | None]:
    """Read both flat and externally tagged CodexErrorInfo enum representations."""
    variant = None
    status = None
    if isinstance(info, str):
        variant = info
    elif isinstance(info, dict):
        variant = next(
            (info[key] for key in ("type", "kind", "variant") if isinstance(info.get(key), str)),
            None,
        )
        status = info.get("httpStatusCode")
        if variant is None and len(info) == 1:
            key, value = next(iter(info.items()))
            if isinstance(key, str) and isinstance(value, dict):
                variant = key
                status = value.get("httpStatusCode")
    return variant, status if type(status) is int and 100 <= status <= 599 else None


def codex_failure_context(payload: dict[str, object], *, message: str) -> FailureContext:
    """Keep error-body request IDs separate from Omnigent's event transport ID."""
    variant, status = codex_error_info(payload.get("codexErrorInfo"))
    return normalize_failure_context(
        {
            "native_harness": "codex-native",
            "native_error_category": variant,
            "native_error_message": message,
            "http_status": status,
            "provider_error_type": payload.get("provider_error_type"),
            "provider_error_code": payload.get("provider_error_code"),
            "provider_error_param": payload.get("provider_error_param"),
            "native_error_request_id": payload.get("request_id"),
            "gateway_request_id": payload.get("gateway_request_id"),
            "provider_request_id": payload.get("provider_request_id"),
        }
    )
