"""Temporary native-iOS evidence driver for the real mobile File Viewer."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FILENAME = "ios-download-demo.mp4"


@pytest.fixture
def ios_download_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILENAME}"
    )
    response = httpx.put(
        file_url,
        json={"content": "native iOS download demo", "encoding": "utf-8"},
        timeout=10.0,
    )
    response.raise_for_status()
    yield base_url, session_id


@pytest.mark.timeout(600)
def test_real_ios_file_viewer_download(
    ios_download_session: tuple[str, str],
) -> None:
    base_url, session_id = ios_download_session
    device_id = os.environ["OMNIGENT_IOS_DEVICE_ID"]
    derived_data = os.environ["OMNIGENT_IOS_DERIVED_DATA"]
    method = os.environ["OMNIGENT_IOS_TEST_METHOD"]
    server_host = os.environ.get("OMNIGENT_IOS_SERVER_HOST", "127.0.0.1")
    server_port = urlsplit(base_url).port
    native_base_url = f"http://{server_host}:{server_port}"
    products = Path(derived_data) / "Build" / "Products" / "Debug-iphonesimulator"
    test_bundles = list(products.glob("OmnigentUITests-Runner.app/PlugIns/OmnigentUITests.xctest"))
    assert len(test_bundles) == 1, test_bundles
    (test_bundles[0] / "ios-e2e-config.json").write_text(
        json.dumps({"serverURL": f"{native_base_url}/c/{session_id}", "filename": _FILENAME})
    )
    subprocess.run(
        [
            "xcodebuild",
            "test-without-building",
            "-project",
            "web/ios/Omnigent.xcodeproj",
            "-scheme",
            "Omnigent",
            "-configuration",
            "Debug",
            "-destination",
            f"platform=iOS Simulator,id={device_id}",
            "-derivedDataPath",
            derived_data,
            f"-only-testing:OmnigentUITests/OmnigentUITests/{method}",
            "-parallel-testing-enabled",
            "NO",
            "CODE_SIGNING_ALLOWED=NO",
        ],
        cwd=_REPO_ROOT,
        env=os.environ,
        check=True,
        timeout=420,
    )
