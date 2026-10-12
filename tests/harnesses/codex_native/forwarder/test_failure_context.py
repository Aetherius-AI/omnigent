"""Codex provider evidence survives the forwarder's status transport."""

import json

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["turn", "notification", "error_item"])
async def test_failed_status_preserves_provider_evidence(source: str) -> None:
    payload = {
        "message": "The upstream request failed.",
        "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 503}},
        "request_id": "opaque-error-request",
        "gateway_request_id": "gateway-request",
        "provider_request_id": "provider-request",
        "provider_error_code": "upstream_unavailable",
        "code": "unqualified-native-code",
        "failure_source": "forged",
        "runner_version": "forged",
    }
    if source == "notification":
        error = forwarder._terminal_error_from_notification({"error": payload})
    else:
        turn = (
            {"error": payload} if source == "turn" else {"items": [{"type": "error", **payload}]}
        )
        error = forwarder._terminal_error_from_turn({"turn": turn})
    assert error is not None
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(204)

    edge = forwarder._CodexTurnStatusEdge(
        status="failed", turn_id="native-turn", source="turn/completed:turn-error", error=error
    )
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as client:
        await forwarder._post_turn_status_edge(client, "omnigent-session", edge)
        await forwarder._post_turn_status_edge(client, "omnigent-session", edge)
    first, second = [r["data"] for r in requests]
    assert first == second
    assert first["status"] == "failed"
    assert first["output"] == payload["message"]
    assert first["response_id"] == "codex_native-turn"
    assert "reauth_required" not in first
    context = first["failure_context"]
    assert context["failure_source"] == "codex_terminal_turn"
    assert context["native_harness"] == "codex-native"
    assert context["native_error_category"] == "responseTooManyFailedAttempts"
    assert context["http_status"] == 503
    assert context["provider_error_code"] == "upstream_unavailable"
    assert context["native_error_request_id"] == "opaque-error-request"
    assert context["gateway_request_id"] == "gateway-request"
    assert context["provider_request_id"] == "provider-request"
    assert context["failure_id"]
    assert context["runner_version"] != "forged"


@pytest.mark.asyncio
async def test_evidence_is_bounded_and_does_not_override_lifecycle_fields() -> None:
    error = forwarder._terminal_error_from_turn(
        {
            "turn": {
                "error": {
                    "message": 'password="synthetic secret" ' + "x" * 3000,
                    "codexErrorInfo": {"type": "Other", "httpStatusCode": True},
                    "gateway_request_id": "g" * 1000,
                    "status": "idle",
                    "session_id": "another-session",
                    "request_id": {"secret": "synthetic secret"},
                }
            }
        }
    )
    assert error is not None
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(204)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as client:
        await forwarder._post_turn_status_edge(
            client,
            "omnigent-session",
            forwarder._CodexTurnStatusEdge(
                status="failed",
                turn_id="native-turn",
                source="turn/completed:turn-error",
                error=error,
            ),
        )
    data = requests[0]["data"]
    assert data["status"] == "failed"
    context = data["failure_context"]
    assert "synthetic secret" not in json.dumps(context)
    assert len(context["native_error_message"]) <= 1024
    assert len(context["gateway_request_id"]) == 256
    assert "http_status" not in context
    assert "session_id" not in context
    assert "native_error_request_id" not in context


@pytest.mark.parametrize(
    ("info", "message", "auth"),
    [
        ({"httpConnectionFailed": {"httpStatusCode": 401}}, "Denied", True),
        ({"responseStreamDisconnected": {"httpStatusCode": 403}}, "Denied", True),
        (
            {"httpConnectionFailed": {"httpStatusCode": 403}},
            'Budget "test" has reached its limit',
            False,
        ),
        ({"type": 123, "httpStatusCode": 401}, "Denied", True),
        ({"type": ["unauthorized"]}, "Denied", False),
    ],
)
def test_wire_error_variants_do_not_lose_auth_or_crash(
    info: object, message: str, auth: bool
) -> None:
    error = forwarder._terminal_error_from_notification(
        {"error": {"message": message, "codexErrorInfo": info}}
    )
    assert error is not None
    assert error.is_auth is auth
