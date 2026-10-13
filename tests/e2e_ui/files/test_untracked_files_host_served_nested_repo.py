"""E2E: Files panel on an asleep host session whose workspace holds a large nested repo.

Journey from the report: a host session rooted at the user's home directory, with
a very large git checkout in a subdirectory and the runner asleep (files served
live from the host). The user creates an untracked directory with a file inside
the checkout, then looks for it in the Changes tab, the search box and the folder
tree.

The host is a real ``omnigent host`` daemon registered against the live server;
its runner is let go idle so the panel is served over the host tunnel exactly as
in production. The checkout is sized past the search scan budget, and a hidden
directory under the root holds more name matches than the search result cap.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Locator, Page, expect

from tests._helpers.compat import runner_executable
from tests._helpers.host import isolated_host
from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle, open_right_rail

# Tracked files under an alphabetically-first directory, more than the server's
# search scan budget (_SEARCH_SCAN_BUDGET = 50_000), so a filesystem walk runs
# out before reaching later directories of the checkout.
_FILLER_COUNT = 55_000
# Hidden-directory entries matching the untracked directory's name, more than
# the 500-result search cap.
_HIDDEN_MATCH_COUNT = 600
_REPO_DIR = "monorepo"
_UNTRACKED_DIR = "experimental"
_UNTRACKED_FILE = "FINDINGS.md"
_RUNNER_IDLE_TIMEOUT_S = 5
# Mirrors STARTING_GRACE_S in web/src/hooks/useSessionLiveness.ts.
_STARTING_GRACE_S = 45
_HOST_ONLINE_TIMEOUT_S = 180.0


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "e2e",
        "GIT_AUTHOR_EMAIL": "e2e@example.com",
        "GIT_COMMITTER_NAME": "e2e",
        "GIT_COMMITTER_EMAIL": "e2e@example.com",
    }


def seed_home_workspace(root: Path) -> Path:
    """Seed a home-like, non-git workspace root and return its nested repo.

    ``root/.cache`` holds ``_HIDDEN_MATCH_COUNT`` files named after the untracked
    directory; ``root/monorepo`` is a committed git repo with ``_FILLER_COUNT``
    tracked files plus the untracked ``experimental/FINDINGS.md``.
    """
    hidden = root / ".cache" / "pip"
    hidden.mkdir(parents=True)
    for i in range(_HIDDEN_MATCH_COUNT):
        (hidden / f"{_UNTRACKED_DIR}_{i:04d}.whl").touch()
    repo = root / _REPO_DIR
    filler = repo / "aaa_filler"
    filler.mkdir(parents=True)
    for i in range(_FILLER_COUNT):
        (filler / f"f{i:05d}.txt").touch()
    env = _git_env()
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True, env=env)
    untracked = repo / _UNTRACKED_DIR
    untracked.mkdir()
    (untracked / _UNTRACKED_FILE).write_text("# Findings\n")
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert f"?? {_UNTRACKED_DIR}/" in status, status
    return repo


def _wait_for(
    probe: Callable[[], object], *, timeout: float, interval: float = 1.0, what: str
) -> object:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:g}s waiting for {what}; last={last!r}")


def session_liveness(client: httpx.Client, session_id: str) -> dict[str, object]:
    payload = client.get("/health", params={"session_ids": session_id}, timeout=10).json()
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    row = sessions.get(session_id) if isinstance(sessions, dict) else None
    return row if isinstance(row, dict) else {}


def _bound_snapshot(client: httpx.Client, session_id: str) -> dict[str, object] | None:
    snapshot = client.get(f"/v1/sessions/{session_id}", timeout=10).json()
    return snapshot if snapshot.get("runner_id") else None


def _asleep_liveness(client: httpx.Client, session_id: str) -> dict[str, object] | None:
    live = session_liveness(client, session_id)
    if live.get("runner_online") is False and live.get("host_online") is True:
        return live
    return None


@contextlib.contextmanager
def asleep_host_session(
    base_url: str, root: Path, *, log: Callable[[str], None] = print
) -> Iterator[tuple[str, dict[str, object]]]:
    """Yield ``(session_id, snapshot)`` for a host-bound session whose runner went idle.

    Starts a real host daemon under ``root/host``, lets it launch the session's
    runner, waits for that runner's idle shutdown, then waits past the SPA's
    startup grace so the page reads the session as asleep rather than starting.
    """
    workspace = root / "home"
    workspace.mkdir()
    seed_home_workspace(workspace)
    base_env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    with (
        httpx.Client(base_url=base_url, trust_env=False) as client,
        isolated_host(
            root / "host",
            base_url,
            base_env=base_env,
            name=f"asleep-{root.name[:8]}",
            command=[
                runner_executable(),
                "-m",
                "omnigent",
                "host",
                "--server",
                base_url,
                "--non-interactive",
            ],
        ) as host,
    ):
        # The host-spawned runner reads runner.idle_timeout_s from the host's config home.
        config_path = host.home / ".omnigent" / "config.yaml"
        config = yaml.safe_load(config_path.read_text()) or {}
        config["runner"] = {"idle_timeout_s": _RUNNER_IDLE_TIMEOUT_S}
        config_path.write_text(yaml.safe_dump(config))
        host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=1)
        log(f"host {host.host_id} online")

        resp = post_session_bundle(
            client.post,
            "/v1/sessions",
            _build_hello_world_bundle(),
            metadata={"host_id": host.host_id, "workspace": str(workspace)},
            timeout=180.0,
        )
        resp.raise_for_status()
        session_id = resp.json()["session_id"]
        log(f"session {session_id} created: {resp.json()}")
        try:
            snapshot = _wait_for(
                lambda: _bound_snapshot(client, session_id),
                timeout=120,
                what="the host to bind a runner to the session",
            )
            assert isinstance(snapshot, dict)
            log(f"runner bound: runner_id={snapshot['runner_id']} host_id={snapshot['host_id']}")
            seen_online = _wait_for(
                lambda: session_liveness(client, session_id).get("runner_online") is True or None,
                timeout=90,
                what="the launched runner to come online",
            )
            log(f"runner online observed: {seen_online}")
            asleep = _wait_for(
                lambda: _asleep_liveness(client, session_id),
                timeout=_RUNNER_IDLE_TIMEOUT_S * 6 + 60,
                what="the runner to idle out while the host stays online",
            )
            log(f"session asleep: {asleep}")
            created_at = float(snapshot.get("created_at") or 0)
            remaining = created_at + _STARTING_GRACE_S + 2 - time.time()
            if remaining > 0:
                time.sleep(remaining)
            yield session_id, snapshot
        finally:
            with contextlib.suppress(httpx.HTTPError):
                client.delete(f"/v1/sessions/{session_id}", timeout=10)


@pytest.fixture(scope="module")
def asleep_session(
    live_server: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, str]]:
    root = tmp_path_factory.mktemp("asleep_host")
    with asleep_host_session(live_server, root) as (session_id, _snapshot):
        yield live_server, session_id


def _row(rail: Locator, name: str) -> Locator:
    return rail.get_by_role("button", name=re.compile(re.escape(name))).filter(has_text=name)


def _hold(page: Page) -> None:
    """Keep the asserted state on screen when the journey is being filmed."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(1_500)


def _open_asleep_files_panel(page: Page, base_url: str, session_id: str) -> Locator:
    page.goto(f"{base_url}/c/{session_id}?view=explore")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_test_id("files-host-served-badge")).to_be_visible(timeout=90_000)
    return rail


def _search_all_files(rail: Locator, query: str) -> Locator:
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    search = rail.get_by_role("searchbox", name="Search all files")
    expect(search).to_be_visible(timeout=30_000)
    search.fill(query)
    expect(search).to_have_value(query)
    expect(rail.get_by_text("Searching…")).to_have_count(0, timeout=60_000)
    return search


def test_changes_tab_lists_untracked_file_of_nested_repo(
    request: pytest.FixtureRequest, asleep_session: tuple[str, str]
) -> None:
    """The untracked file shows up as added in the Changes tab."""
    base_url, session_id = asleep_session
    page: Page = request.getfixturevalue("page")
    rail = _open_asleep_files_panel(page, base_url, session_id)
    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")
    expect(rail.get_by_text("Loading…")).to_have_count(0, timeout=60_000)
    expect(_row(rail, _UNTRACKED_FILE)).to_be_visible(timeout=30_000)
    _hold(page)


def test_search_finds_untracked_file_of_nested_repo(
    request: pytest.FixtureRequest, asleep_session: tuple[str, str]
) -> None:
    """Searching the untracked file's name finds it in the Files tab."""
    base_url, session_id = asleep_session
    page: Page = request.getfixturevalue("page")
    rail = _open_asleep_files_panel(page, base_url, session_id)
    _search_all_files(rail, _UNTRACKED_FILE)
    expect(rail.get_by_text(re.compile(r"No files match"))).to_have_count(0)
    expect(_row(rail, _UNTRACKED_FILE)).to_be_visible(timeout=30_000)
    _hold(page)


def test_search_surfaces_visible_directory_past_hidden_matches(
    request: pytest.FixtureRequest, asleep_session: tuple[str, str]
) -> None:
    """With hidden files hidden, searching the untracked directory's name lists it."""
    base_url, session_id = asleep_session
    page: Page = request.getfixturevalue("page")
    rail = _open_asleep_files_panel(page, base_url, session_id)
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    hide_hidden = rail.get_by_role("button", name="Hide hidden files")
    if hide_hidden.count() > 0:
        hide_hidden.click()
    expect(rail.get_by_role("button", name="Show hidden files")).to_be_visible()
    _search_all_files(rail, _UNTRACKED_DIR)
    expect(rail.get_by_text(re.compile(r"in hidden directories"))).to_have_count(0)
    # Search rows are labelled by workspace-relative path, unlike tree rows.
    expect(
        rail.get_by_role("button", name=f"{_REPO_DIR}/{_UNTRACKED_DIR}/", exact=True)
    ).to_be_visible(timeout=30_000)
    _hold(page)


def test_folder_tree_browses_to_untracked_directory(
    request: pytest.FixtureRequest, asleep_session: tuple[str, str]
) -> None:
    """Expanding the checkout in the folder tree reaches the untracked directory and file."""
    base_url, session_id = asleep_session
    page: Page = request.getfixturevalue("page")
    rail = _open_asleep_files_panel(page, base_url, session_id)
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    repo_row = rail.get_by_role("button", name=f"{_REPO_DIR}/", exact=True)
    expect(repo_row).to_be_visible(timeout=60_000)
    repo_row.click()
    dir_row = rail.get_by_role("button", name=f"{_UNTRACKED_DIR}/", exact=True)
    expect(dir_row).to_be_visible(timeout=60_000)
    dir_row.click()
    expect(_row(rail, _UNTRACKED_FILE)).to_be_visible(timeout=60_000)
    _hold(page)
