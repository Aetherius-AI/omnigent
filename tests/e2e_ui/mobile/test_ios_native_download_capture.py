"""Temporary native-iOS evidence driver for the real mobile File Viewer."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FILENAME = "ios-download-demo.mp4"


@pytest.fixture
def ios_download_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    workspace = _REPO_ROOT / session_id
    workspace.mkdir(parents=True, exist_ok=True)
    # Binary bytes force the same "Preview not available" File Viewer shown in the report.
    (workspace / _FILENAME).write_bytes(b"\x00\x00\x00\x18ftypmp42native-ios-download-demo")
    try:
        yield base_url, session_id
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_real_ios_file_viewer_download(
    ios_download_session: tuple[str, str],
) -> None:
    base_url, session_id = ios_download_session
    device_id = os.environ["OMNIGENT_IOS_DEVICE_ID"]
    derived_data = os.environ["OMNIGENT_IOS_DERIVED_DATA"]
    method = os.environ["OMNIGENT_IOS_TEST_METHOD"]
    env = {
        **os.environ,
        "OMNIGENT_IOS_E2E_SERVER_URL": f"{base_url}/c/{session_id}",
        "OMNIGENT_IOS_E2E_FILENAME": _FILENAME,
    }
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
        env=env,
        check=True,
    )
