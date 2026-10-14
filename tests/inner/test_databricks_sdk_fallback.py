"""The SDK mint fallback must not repeat the caller's failed CLI authentication."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from omnigent.inner.databricks_token import _sdk_bearer


@pytest.fixture(autouse=True)
def local_workspace_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from databricks.sdk.config import Config

    monkeypatch.setattr(Config, "_resolve_host_metadata", lambda self: None)


def test_cli_profile_is_not_force_refreshed_again_by_the_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "cli-calls"
    cli = tmp_path / "databricks"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "if sys.argv[1:] == ['-v']:\n"
        " print('Databricks CLI v0.999.0')\n"
        "elif sys.argv[1:] == ['--version']:\n"
        " print('Databricks CLI v0.999.0')\n"
        "else:\n"
        f" pathlib.Path({str(record)!r}).write_text(' '.join(sys.argv[1:]))\n"
        " raise SystemExit(1)\n",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    config = tmp_path / "databrickscfg"
    config.write_text(
        "[owner]\nhost = https://example.databricks.com\nauth_type = databricks-cli\n"
        f"databricks_cli_path = {cli}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    with pytest.raises(ValueError):
        _sdk_bearer("owner", "https://example.databricks.com")
    assert not record.exists(), "SDK fallback invoked CLI authentication a second time"


def test_sdk_fallback_keeps_profile_pinned_pat_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "databrickscfg"
    config.write_text(
        "[owner]\nhost = https://example.databricks.com\nauth_type = pat\ntoken = fixture-pat\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    monkeypatch.setenv("DATABRICKS_TOKEN", "ambient-other-identity")
    assert _sdk_bearer("owner", "https://example.databricks.com") == (
        "https://example.databricks.com",
        "fixture-pat",
    )


def test_sdk_fallback_keeps_real_m2m_token_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from databricks.sdk.config import Config
    from databricks.sdk.oauth import OidcEndpoints

    grants: list[dict[str, list[str]]] = []

    class Issuer(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            grants.append(parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            payload = json.dumps(
                {
                    "access_token": "fixture-service-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    issuer = ThreadingHTTPServer(("127.0.0.1", 0), Issuer)
    thread = threading.Thread(target=issuer.serve_forever, daemon=True)
    thread.start()
    issuer_url = f"http://127.0.0.1:{issuer.server_port}"
    # Only metadata discovery and the external IdP are controlled; SDK minting stays real.
    monkeypatch.setattr(
        Config,
        "databricks_oidc_endpoints",
        property(
            lambda self: OidcEndpoints(
                authorization_endpoint=issuer_url + "/authorize",
                token_endpoint=issuer_url + "/token",
            )
        ),
    )
    config = tmp_path / "databrickscfg"
    config.write_text(
        "[service]\nhost = https://example.databricks.com\nauth_type = oauth-m2m\n"
        "client_id = fixture-client\nclient_secret = fixture-secret\n"
        "disable_async_token_refresh = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    try:
        assert _sdk_bearer("service", "https://example.databricks.com") == (
            "https://example.databricks.com",
            "fixture-service-token",
        )
        assert len(grants) == 1
        assert grants[0]["grant_type"] == ["client_credentials"]
    finally:
        issuer.shutdown()
        issuer.server_close()
        thread.join(timeout=2)
