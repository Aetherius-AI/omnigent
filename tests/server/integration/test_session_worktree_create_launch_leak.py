"""Regression: a worktree this inline ``POST /v1/sessions`` create made is
removed and its binding cleared on launch failure, while a user's existing
worktree is kept for the lenient first-message relaunch. When the host does
not confirm the removal, the binding and root label stay so an opt-in delete
can retry the cleanup later."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HostCreateWorktreeFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostListWorktreesFrame,
    HostRemoveWorktreeFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection
from omnigent.server.routes._host_worktree import (
    WORKTREE_ROOT_LABEL_KEY,
    worktree_root_fingerprint,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "7f0c2a9d8e4b41f6a1c3d5e7b9a0c2d4"
_SOURCE_REPO = "/Users/alice/myrepo"


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App wired WITH ``host_store`` so the inline create path attempts a
    host launch (the shared ``app`` fixture passes ``host_store=None``)."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
    )


class _FakeWebSocket:
    """Minimal WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        """No-op send; frames flow through the outbound queue."""


@dataclass
class _HostCapture:
    """Control frames a fake host received during one request, plus the
    worktrees it reports so a cleanup-time ``host.list_worktrees`` has
    something to list. ``unexpected`` records frames the drain does not
    answer, so a request stuck on a new frame type is diagnosable."""

    create: list[HostCreateWorktreeFrame] = field(default_factory=list)
    launch: list[HostLaunchRunnerFrame] = field(default_factory=list)
    remove: list[HostRemoveWorktreeFrame] = field(default_factory=list)
    worktrees: list[dict[str, Any]] = field(default_factory=list)
    unexpected: list[Any] = field(default_factory=list)


RegisterHost = Callable[..., _HostCapture]


def _fail_all_pending(conn: HostConnection, reason: str) -> None:
    """Reject every outstanding RPC this fake host answers so a request stuck
    on a frame the drain does not recognize fails fast with ``reason`` instead
    of hanging to the 30s launch timeout."""
    for pending in (
        conn.pending_stats,
        conn.pending_create_worktrees,
        conn.pending_launches,
        conn.pending_list_worktrees,
        conn.pending_remove_worktrees,
    ):
        while pending:
            _request_id, fut = pending.popitem()
            if not fut.done():
                fut.set_exception(RuntimeError(reason))


def _install_fake_host(
    app: FastAPI,
    db_uri: str,
    *,
    owner: str = RESERVED_USER_LOCAL,
    launch_status: str = "launched",
    workspace_subdir: str | None = None,
    remove_status: str = "ok",
    disconnect_after_launch: bool = False,
) -> tuple[_HostCapture, HostConnection]:
    """Register a fake host owned by ``owner`` that answers and captures
    stat/create/launch/list/remove frames. ``launch_status`` models the host's
    launch verdict (``"timeout"`` fails the launch future the way the launch
    wait does); ``remove_status`` models the removal verdict;
    ``workspace_subdir`` relocates the picked subdir into the worktree;
    ``disconnect_after_launch`` drops the host right after it answers the
    launch frame. Retire the connection with :func:`_retire_fake_host`."""
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", owner)
    conn = app.state.host_registry.register(
        host_id=_HOST_ID,
        ws=_FakeWebSocket(),  # type: ignore[arg-type]
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="wt-host"),
        owner=owner,
    )
    cap = _HostCapture()

    async def _drain() -> None:
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            try:
                frame = decode_host_frame(frame_text)
            except Exception as exc:  # surface decode errors instead of stalling
                cap.unexpected.append(exc)
                _fail_all_pending(conn, f"undecodable host frame: {exc!r}")
                continue
            if isinstance(frame, HostStatFrame):
                fut = conn.pending_stats.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(
                        {
                            "status": "ok",
                            "exists": True,
                            "type": "directory",
                            "canonical_path": frame.path,
                            "error": None,
                        }
                    )
            elif isinstance(frame, HostCreateWorktreeFrame):
                cap.create.append(frame)
                branch_dir = frame.branch_name.replace("/", "-")
                worktree_path = f"{_SOURCE_REPO}-worktrees/{branch_dir}"
                cap.worktrees.append(
                    {"path": worktree_path, "branch": frame.branch_name, "is_main": False}
                )
                workspace = f"{worktree_path}/{workspace_subdir}" if workspace_subdir else None
                fut = conn.pending_create_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(
                        {
                            "status": "ok",
                            "worktree_path": worktree_path,
                            "workspace": workspace,
                            "branch": frame.branch_name,
                            "error": None,
                        }
                    )
            elif isinstance(frame, HostLaunchRunnerFrame):
                cap.launch.append(frame)
                fut = conn.pending_launches.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    if launch_status == "timeout":
                        # Model the 30s launch wait expiring: the create's wait
                        # sees the same error its timeout branch raises.
                        fut.set_exception(asyncio.TimeoutError("host launch timed out"))
                    else:
                        launched = launch_status == "launched"
                        fut.set_result(
                            {
                                "status": launch_status,
                                "runner_id": "runner_from_host" if launched else None,
                                "error": None if launched else "boom",
                            }
                        )
                if disconnect_after_launch:
                    app.state.host_registry.deregister(
                        conn.host_id, workspace_id=conn.workspace_id, conn=conn
                    )
            elif isinstance(frame, HostListWorktreesFrame):
                fut = conn.pending_list_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result({"status": "ok", "worktrees": list(cap.worktrees)})
            elif isinstance(frame, HostRemoveWorktreeFrame):
                cap.remove.append(frame)
                fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    removed = remove_status == "ok"
                    fut.set_result(
                        {
                            "status": remove_status,
                            "error": None if removed else "worktree is locked",
                        }
                    )
            else:
                cap.unexpected.append(frame)
                _fail_all_pending(conn, f"unexpected host frame: {type(frame).__name__}")

    conn._drain_task_for_test = asyncio.create_task(_drain())  # type: ignore[attr-defined]
    return cap, conn


async def _retire_fake_host(conn: HostConnection) -> None:
    """Stop a fake host's drain task once its test is done."""
    conn.outbound_queue.put_nowait(None)
    task = conn._drain_task_for_test  # type: ignore[attr-defined]
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest_asyncio.fixture()
async def register_worktree_launch_host(
    app: FastAPI,
    db_uri: str,
) -> AsyncIterator[RegisterHost]:
    """Yield a factory installing a fake host on ``app`` (see
    :func:`_install_fake_host` for the controls) and retire every host it
    registered on teardown."""
    conns: list[HostConnection] = []

    def _register(**controls: Any) -> _HostCapture:
        cap, conn = _install_fake_host(app, db_uri, **controls)
        conns.append(conn)
        return cap

    yield _register

    for conn in conns:
        await _retire_fake_host(conn)


async def _create_git_session(
    client: httpx.AsyncClient,
    agent_id: str,
    git: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST a JSON session-create with a ``git`` block on the target host."""
    return await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "host_id": _HOST_ID,
            "workspace": _SOURCE_REPO,
            "git": git,
        },
        headers=headers,
    )


@pytest.mark.parametrize("launch_status", ["failed", "timeout"])
async def test_inline_create_launch_failure_cleans_up_worktree(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
    launch_status: str,
) -> None:
    """A launch the host reports as failed, or whose result wait expires, sends
    one ``host.remove_worktree`` for the worktree this create made and clears
    the host binding, matching the dedicated bind endpoint; otherwise the
    worktree leaks until storage fills."""
    cap = register_worktree_launch_host(launch_status=launch_status)
    agent = await create_test_agent(client, name=f"wt-leak-{launch_status}-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/leak", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(resp.json()["id"])
    assert conv is not None

    assert len(cap.create) == 1, f"expected one create_worktree frame, got {len(cap.create)}"
    assert len(cap.launch) == 1, f"expected one launch_runner frame, got {len(cap.launch)}"

    worktree_path = f"{_SOURCE_REPO}-worktrees/feature-leak"
    observed = {
        "create_status": resp.status_code,
        "create_frames": len(cap.create),
        "launch_frames": len(cap.launch),
        "remove_frames": len(cap.remove),
        "session_workspace": conv.workspace,
        "session_git_branch": conv.git_branch,
        "session_host_id": conv.host_id,
        "session_runner_id_set": conv.runner_id is not None,
        "unexpected_frames": cap.unexpected,
    }
    assert len(cap.remove) == 1, (
        "ABANDONED WORKTREE (leak): the inline create path sent no host.remove_worktree "
        f"on launch failure, so {worktree_path} is left on the host. observed={observed}"
    )
    assert cap.remove[0].worktree_path == worktree_path
    assert cap.remove[0].delete_branch is True

    # Binding is cleared so no later cleanup acts on a stale workspace/branch.
    assert conv.host_id is None, observed
    assert conv.workspace is None, observed
    assert conv.git_branch is None, observed
    assert conv.runner_id is None, observed
    assert WORKTREE_ROOT_LABEL_KEY not in conv.labels, observed

    # The create response reports the cleared binding, not the removed worktree.
    body = resp.json()
    assert body["host_id"] is None and body["runner_id"] is None, body
    assert body["workspace"] is None and body["git_branch"] is None, body
    assert WORKTREE_ROOT_LABEL_KEY not in body["labels"], body


async def test_inline_create_launch_failure_removes_relocated_worktree_root(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A picked repo subdirectory is relocated inside the new worktree, so
    the rollback must remove the worktree root, not that subdirectory."""
    cap = register_worktree_launch_host(launch_status="failed", workspace_subdir="packages/app")
    agent = await create_test_agent(client, name="wt-subdir-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_id": _HOST_ID,
            "workspace": f"{_SOURCE_REPO}/packages/app",
            "git": {"branch_name": "feature/sub", "base_branch": "main"},
        },
    )
    assert resp.status_code == 201, resp.text
    assert len(cap.create) == 1 and cap.create[0].repo_path == f"{_SOURCE_REPO}/packages/app"
    assert len(cap.launch) == 1, (
        f"expected one launch_runner frame, got {len(cap.launch)}; unexpected={cap.unexpected}"
    )
    assert len(cap.remove) == 1, (
        f"expected one remove_worktree frame, got {cap.remove}; unexpected={cap.unexpected}"
    )
    assert cap.remove[0].worktree_path == f"{_SOURCE_REPO}-worktrees/feature-sub"
    assert cap.remove[0].delete_branch is True

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(resp.json()["id"])
    assert conv is not None
    assert conv.host_id is None and conv.workspace is None and conv.git_branch is None
    assert WORKTREE_ROOT_LABEL_KEY not in conv.labels


async def test_inline_create_existing_worktree_launch_failure_keeps_worktree(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Binding to a PRE-EXISTING worktree then failing to launch must NOT
    remove it (the user's, not an orphan); the session stays bound so the
    first message can retry the launch."""
    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name="wt-bind-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/mine", "existing_worktree": True}
    )
    assert resp.status_code == 201, resp.text
    assert len(cap.launch) == 1, f"expected one launch_runner frame, got {len(cap.launch)}"
    assert cap.create == [], "existing_worktree must not create a worktree"
    assert cap.remove == [], (
        f"a user's existing worktree must never be removed: {cap.remove}; "
        f"unexpected={cap.unexpected}"
    )

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(resp.json()["id"])
    assert conv is not None
    assert conv.host_id == _HOST_ID
    assert conv.workspace == _SOURCE_REPO
    assert conv.git_branch == "feature/mine"
    assert conv.runner_id is not None, "the lenient path keeps the runner binding for relaunch"
    assert resp.json()["runner_id"] == conv.runner_id


async def test_inline_create_launch_failure_keeps_existing_branch(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Rolling back a worktree that checked out a pre-existing branch removes
    the directory but never force-deletes the user's branch."""
    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name="wt-existing-branch-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/existing", "existing_branch": True}
    )
    assert resp.status_code == 201, resp.text
    assert len(cap.create) == 1 and cap.create[0].existing_branch is True
    assert len(cap.remove) == 1, (
        f"expected one remove_worktree frame, got {cap.remove}; unexpected={cap.unexpected}"
    )
    assert cap.remove[0].worktree_path == f"{_SOURCE_REPO}-worktrees/feature-existing"
    assert cap.remove[0].delete_branch is False, "a pre-existing branch must survive the rollback"

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(resp.json()["id"])
    assert conv is not None
    assert conv.host_id is None and conv.workspace is None and conv.git_branch is None


@pytest.mark.parametrize("failure", ["rejected", "offline"])
async def test_inline_create_launch_failure_keeps_cleanup_metadata_until_removed(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
    failure: str,
) -> None:
    """When the host rejects the removal or has disconnected, the session keeps
    its host binding and root label instead of discarding the only record of
    the directory, and ``DELETE ...?delete_branch=true`` removes the worktree
    once the host is back."""
    cap = register_worktree_launch_host(
        launch_status="failed",
        remove_status="failed" if failure == "rejected" else "ok",
        disconnect_after_launch=failure == "offline",
    )
    agent = await create_test_agent(client, name=f"wt-retry-{failure}-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/retry", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    worktree_path = f"{_SOURCE_REPO}-worktrees/feature-retry"
    assert len(cap.create) == 1 and len(cap.launch) == 1
    # A reachable host is asked once and refuses; an offline host is never asked.
    assert len(cap.remove) == (1 if failure == "rejected" else 0), (
        f"got {cap.remove}; unexpected={cap.unexpected}"
    )

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.get_conversation(body["id"])
    assert conv is not None
    # An unconfirmed removal keeps the whole binding, including the bound
    # runner_id, so a later delete/archive has everything it needs to retry.
    assert body["runner_id"] is not None, body
    expected_binding = {
        "host_id": _HOST_ID,
        "workspace": worktree_path,
        "git_branch": "feature/retry",
        "runner_id": body["runner_id"],
        "root_label": worktree_root_fingerprint(worktree_path),
    }
    retained = {
        "host_id": conv.host_id,
        "workspace": conv.workspace,
        "git_branch": conv.git_branch,
        "runner_id": conv.runner_id,
        "root_label": conv.labels.get(WORKTREE_ROOT_LABEL_KEY),
    }
    assert retained == expected_binding, (
        "an unconfirmed removal must keep the cleanup metadata; "
        f"retained={retained} unexpected_frames={cap.unexpected}"
    )
    assert {
        "host_id": body["host_id"],
        "workspace": body["workspace"],
        "git_branch": body["git_branch"],
        "runner_id": body["runner_id"],
        "root_label": body["labels"].get(WORKTREE_ROOT_LABEL_KEY),
    } == expected_binding, body

    # The host comes back (newest connection wins) and still has the worktree.
    cap_retry = register_worktree_launch_host()
    cap_retry.worktrees.extend(cap.worktrees)
    del_resp = await client.delete(f"/v1/sessions/{body['id']}?delete_branch=true")
    assert del_resp.status_code == 200, del_resp.text
    assert len(cap_retry.remove) == 1, (
        f"the retained binding should let delete retry the removal; got {cap_retry.remove}"
    )
    assert cap_retry.remove[0].worktree_path == worktree_path
    assert cap_retry.remove[0].delete_branch is True
    assert store.get_conversation(body["id"]) is None


async def test_inline_create_successful_launch_fires_no_cleanup(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A successfully-started git session keeps its worktree: the create
    path rolls back only on launch failure, so no host.remove_worktree
    frame is sent. Opt-in delete removal is covered by the delete suite's
    ``test_delete_with_flag_sends_remove_worktree``."""
    cap = register_worktree_launch_host(launch_status="launched")
    agent = await create_test_agent(client, name="wt-del-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/keep", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text

    assert cap.remove == [], (
        f"a successful launch must not trigger create-time cleanup: {cap.remove}; "
        f"unexpected={cap.unexpected}"
    )


async def test_inline_create_launch_failure_skips_rollback_when_runner_rebound(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent first-message relaunch can rebind a new runner between the
    failed launch and the rollback. The create must then leave the worktree and
    the fresh binding alone instead of removing them under the live runner."""
    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name="wt-race-agent")

    original_get = SqlAlchemyConversationStore.get_conversation
    rebound = {"done": False}

    def _racing_get(self: SqlAlchemyConversationStore, conversation_id: str) -> Any:
        conv = original_get(self, conversation_id)
        # Rebind once, on the rollback's re-read after the failed launch, to
        # mimic post_event relaunching a new runner on this same session.
        if not rebound["done"] and cap.launch and conv is not None and conv.runner_id is not None:
            rebound["done"] = True
            self.replace_runner_id(conversation_id, "runner_relaunched")
            return original_get(self, conversation_id)
        return conv

    monkeypatch.setattr(SqlAlchemyConversationStore, "get_conversation", _racing_get)

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/race", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text
    assert rebound["done"], "the rollback never re-read the row, so the race was not exercised"
    assert len(cap.launch) == 1, f"expected one launch_runner frame; unexpected={cap.unexpected}"
    assert cap.remove == [], (
        "rollback must be skipped when a newer runner is bound, so no worktree is "
        f"removed under the live runner: {cap.remove}; unexpected={cap.unexpected}"
    )

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(resp.json()["id"])
    assert conv is not None
    assert conv.runner_id == "runner_relaunched", conv.runner_id
    assert conv.workspace is not None and conv.git_branch is not None, conv
    assert WORKTREE_ROOT_LABEL_KEY in conv.labels, conv.labels


async def test_inline_create_launch_failure_tolerates_binding_cleanup_error(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient store error while clearing the binding after a confirmed
    worktree removal must not 500 the create. The worktree is already gone, so
    the response and the persisted row keep reporting the binding instead of a
    false unbound result, and retain the root label for a later retry."""
    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name="wt-clearerr-agent")

    def _boom(self: SqlAlchemyConversationStore, conversation_id: str) -> Any:
        raise RuntimeError("transient store error")

    monkeypatch.setattr(SqlAlchemyConversationStore, "clear_host_binding", _boom)

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/clearerr", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text
    assert len(cap.remove) == 1, (
        f"the worktree must still be removed before the clear failed: {cap.remove}; "
        f"unexpected={cap.unexpected}"
    )
    worktree_path = f"{_SOURCE_REPO}-worktrees/feature-clearerr"
    body = resp.json()
    # The clear failed, so the still-persisted binding must be reported, not a
    # misleading unbound result, and the root label kept for a later retry.
    assert body["host_id"] == _HOST_ID and body["runner_id"] is not None, body
    assert body["workspace"] == worktree_path and body["git_branch"] == "feature/clearerr", body
    assert body["labels"].get(WORKTREE_ROOT_LABEL_KEY) == worktree_root_fingerprint(
        worktree_path
    ), body

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(body["id"])
    assert conv is not None
    assert conv.host_id == _HOST_ID and conv.runner_id is not None
    assert conv.workspace == worktree_path and conv.git_branch == "feature/clearerr"
    assert conv.labels.get(WORKTREE_ROOT_LABEL_KEY) == worktree_root_fingerprint(worktree_path)


async def test_inline_create_launch_failure_reports_unbound_when_label_delete_fails(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unbinding is the success criterion: once ``clear_host_binding`` commits,
    a later failure to drop the root label must not revert the response to the
    old binding. The POST, a follow-up GET, and the stored row then agree the
    session is unbound, leaving only the orphaned label for a later retry."""
    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name="wt-labelerr-agent")

    original_delete_label = SqlAlchemyConversationStore.delete_label

    def _label_boom(self: SqlAlchemyConversationStore, conversation_id: str, key: str) -> None:
        # Fail only the root-label delete; every other label keeps its real behavior.
        if key == WORKTREE_ROOT_LABEL_KEY:
            raise RuntimeError("transient store error")
        original_delete_label(self, conversation_id, key)

    monkeypatch.setattr(SqlAlchemyConversationStore, "delete_label", _label_boom)

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/labelerr", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text
    assert len(cap.remove) == 1, (
        f"the worktree must still be removed before the label delete failed: "
        f"{cap.remove}; unexpected={cap.unexpected}"
    )
    worktree_path = f"{_SOURCE_REPO}-worktrees/feature-labelerr"
    body = resp.json()
    # clear_host_binding committed, so the response reports the session unbound
    # even though the label delete failed; the stale label stays visible.
    assert body["host_id"] is None and body["runner_id"] is None, body
    assert body["workspace"] is None and body["git_branch"] is None, body
    assert body["labels"].get(WORKTREE_ROOT_LABEL_KEY) == worktree_root_fingerprint(
        worktree_path
    ), body

    snapshot = (
        await client.get(
            f"/v1/sessions/{body['id']}",
            params={
                "include_items": "false",
                "include_liveness": "false",
                "include_usage": "false",
            },
        )
    ).json()
    assert snapshot["host_id"] is None and snapshot["runner_id"] is None, snapshot
    assert snapshot["workspace"] is None and snapshot["git_branch"] is None, snapshot

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(body["id"])
    assert conv is not None
    assert conv.host_id is None and conv.runner_id is None
    assert conv.workspace is None and conv.git_branch is None
    # The label delete failed, so the orphaned root label persists for a retry.
    assert conv.labels.get(WORKTREE_ROOT_LABEL_KEY) == worktree_root_fingerprint(worktree_path)


async def test_inline_create_launch_failure_skips_grant_when_session_vanished(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    mock_llm: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the rollback's re-read finds the row gone, the create must still
    remove the worktree it made (nothing else records it) and must suppress the
    default-public grant it would otherwise write, so a concurrently deleted
    session is never left publicly shared. Runs with header auth so the grant
    branch is reachable: the first, ordinary failed launch is the positive
    control that proves the grant spy fires; the second create stubs the
    re-read to return ``None`` once."""
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.routes.sessions import routes_core
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from tests.server.conftest import _app_client

    user = "alice@example.com"
    headers = {"X-Forwarded-Email": user}
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    auth_app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )
    cap, conn = _install_fake_host(auth_app, db_uri, owner=user, launch_status="failed")

    granted: list[str] = []

    async def _spy_grant(
        app_state: Any,
        permission_store: Any,
        session_id: str,
        **kwargs: Any,
    ) -> None:
        granted.append(session_id)

    try:
        async with _app_client(auth_app, mock_llm, tmp_path) as client:
            agent = await create_test_agent(client, name="wt-vanish-agent", user=user)
            # Spy only on the git sessions below, not on the agent's owning session.
            monkeypatch.setattr(routes_core, "_grant_default_public", _spy_grant)

            # Positive control: a plain failed launch rolls back and still grants.
            control = await _create_git_session(
                client,
                agent["id"],
                {"branch_name": "feature/control", "base_branch": "main"},
                headers=headers,
            )
            assert control.status_code == 201, control.text
            assert granted == [control.json()["id"]], granted
            assert len(cap.remove) == 1, f"got {cap.remove}; unexpected={cap.unexpected}"

            original_get = SqlAlchemyConversationStore.get_conversation
            vanished = {"done": False}

            def _vanishing_get(self: SqlAlchemyConversationStore, conversation_id: str) -> Any:
                conv = original_get(self, conversation_id)
                # Return None once, on the rollback's re-read after the failed
                # launch, standing in for a concurrent delete that dropped the row.
                if (
                    not vanished["done"]
                    and len(cap.launch) == 2
                    and conv is not None
                    and conv.runner_id is not None
                ):
                    vanished["done"] = True
                    return None
                return conv

            monkeypatch.setattr(SqlAlchemyConversationStore, "get_conversation", _vanishing_get)

            resp = await _create_git_session(
                client,
                agent["id"],
                {"branch_name": "feature/vanish", "base_branch": "main"},
                headers=headers,
            )
            assert resp.status_code == 201, resp.text
            assert vanished["done"], (
                "the rollback never re-read the row, so the vanish was not hit"
            )
            # The row is gone, so the worktree is removed from the pre-launch
            # snapshot: it is the only record left of the directory.
            assert len(cap.remove) == 2, (
                f"a vanished row must still have its new worktree removed: {cap.remove}; "
                f"unexpected={cap.unexpected}"
            )
            assert cap.remove[1].worktree_path == f"{_SOURCE_REPO}-worktrees/feature-vanish"
            assert granted == [control.json()["id"]], (
                f"no default-public grant may be written for a vanished session: {granted}"
            )
    finally:
        await _retire_fake_host(conn)


async def test_inline_create_launch_failure_holds_relaunch_lock_across_rollback(
    register_worktree_launch_host: RegisterHost,
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rollback runs under the per-conversation relaunch lock across the
    ownership check, worktree removal, and binding clear. A production relaunch
    that raced the failed launch therefore waits, re-reads the row the rollback
    has just unbound, and is refused by host admission: no runner is launched
    into the removed worktree and the persisted binding stays cleared."""
    from omnigent.server.routes._sessions import helpers as sessions_helpers
    from omnigent.server.routes._sessions.helpers import relaunch_lock_for

    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name="wt-lock-agent")
    store = SqlAlchemyConversationStore(db_uri)

    in_removal = asyncio.Event()
    proceed = asyncio.Event()
    observed: dict[str, Any] = {}

    async def _barrier_remove(*args: Any, **kwargs: Any) -> bool:
        # Past the ownership check now; a concurrent relaunch must stay blocked.
        conv_id = kwargs["exclude_conversation_id"]
        observed["conv_id"] = conv_id
        observed["locked_at_removal"] = relaunch_lock_for(conv_id).locked()
        # The row as a racing first-message relaunch would have snapshotted it:
        # still bound to the failed launch's runner and worktree.
        observed["snapshot"] = store.get_conversation(conv_id)
        in_removal.set()
        await proceed.wait()
        return True

    monkeypatch.setattr(sessions_helpers, "_remove_session_worktree_best_effort", _barrier_remove)

    create = asyncio.create_task(
        _create_git_session(
            client, agent["id"], {"branch_name": "feature/lock", "base_branch": "main"}
        )
    )
    relaunch_started = asyncio.Event()
    relaunch_outcome: dict[str, Any] = {}
    relaunch: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(in_removal.wait(), timeout=5)
        conv_id = observed["conv_id"]
        snapshot = observed["snapshot"]
        assert observed["locked_at_removal"] is True, "rollback must hold the relaunch lock"
        assert relaunch_lock_for(conv_id).locked() is True
        assert snapshot is not None and snapshot.host_id == _HOST_ID, snapshot
        host_conn = app.state.host_registry.get(_HOST_ID)
        assert host_conn is not None

        async def _production_relaunch() -> None:
            # The real single-flight relaunch: it must wait on the lock, then
            # re-read the row instead of launching from the stale snapshot.
            relaunch_started.set()
            try:
                relaunch_outcome["attempt"] = await sessions_helpers._launch_runner_on_host_impl(
                    snapshot, store, app.state.host_registry, host_conn
                )
            except OmnigentError as exc:
                relaunch_outcome["error"] = exc

        relaunch = asyncio.create_task(_production_relaunch())
        await asyncio.wait_for(relaunch_started.wait(), timeout=5)
        await asyncio.sleep(0.05)
        # The rollback still holds the lock here, so the relaunch is deterministically
        # excluded; the sleep only gives a buggy, lock-free rollback time to slip.
        assert relaunch_lock_for(conv_id).locked(), (
            "rollback released the relaunch lock mid-removal"
        )
        assert not relaunch.done(), "a concurrent relaunch ran during the rollback"
        assert len(cap.launch) == 1, (
            f"a relaunch sent a launch frame mid-rollback; unexpected={cap.unexpected}"
        )

        proceed.set()
        resp = await asyncio.wait_for(create, timeout=5)
        await asyncio.wait_for(relaunch, timeout=5)

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["host_id"] is None and body["runner_id"] is None, body
        # The relaunch re-read the unbound row and host admission refused it, so
        # no runner was bound or launched into the removed worktree.
        assert "attempt" not in relaunch_outcome, relaunch_outcome
        error = relaunch_outcome.get("error")
        assert isinstance(error, OmnigentError) and error.code == ErrorCode.UNAUTHORIZED, error
        assert len(cap.launch) == 1, (
            f"expected only the failed launch frame; unexpected={cap.unexpected}"
        )
        assert cap.remove == [], f"the barrier stands in for real removal: {cap.remove}"

        conv = store.get_conversation(body["id"])
        assert conv is not None
        assert conv.host_id is None and conv.workspace is None and conv.git_branch is None
        assert conv.runner_id is None, conv.runner_id
        assert WORKTREE_ROOT_LABEL_KEY not in conv.labels
    finally:
        # Unblock and drain the background tasks so a failed assertion above
        # surfaces cleanly, not as a destroyed-pending-task warning.
        proceed.set()
        for task in (create, relaunch):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


async def test_inline_create_launch_failure_serializes_runner_patch_with_rollback(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PATCH /v1/sessions/{id}`` rebinding the runner takes the relaunch lock
    the rollback holds, so a rebind racing a failed launch waits until the
    rollback has finished instead of landing between its ownership check and
    its unbind. The rebind then survives the unbind, and the runner is never
    initialized into the directory being removed."""
    from omnigent.runner.routing import RunnerRouter
    from omnigent.server.routes._sessions import helpers as sessions_helpers
    from omnigent.server.routes.sessions import routes_core

    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name="wt-patch-agent")
    store = SqlAlchemyConversationStore(db_uri)

    in_removal = asyncio.Event()
    proceed = asyncio.Event()
    observed: dict[str, Any] = {}

    async def _barrier_remove(*args: Any, **kwargs: Any) -> bool:
        observed["conv_id"] = kwargs["exclude_conversation_id"]
        in_removal.set()
        await proceed.wait()
        return True

    monkeypatch.setattr(sessions_helpers, "_remove_session_worktree_best_effort", _barrier_remove)

    # The replacement runner only has to look registered: the binding write is
    # what is under test, so no relay or runner init is started for it.
    monkeypatch.setattr(RunnerRouter, "runner_is_online", lambda self, rid: rid == "runner_b")
    monkeypatch.setattr(RunnerRouter, "runner_owner", lambda self, rid: None)

    async def _no_runner_client(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(routes_core, "_get_runner_client", _no_runner_client)

    patch_at_lock = asyncio.Event()
    real_lock_for = routes_core.relaunch_lock_for

    def _observed_lock_for(conversation_id: str) -> asyncio.Lock:
        patch_at_lock.set()
        return real_lock_for(conversation_id)

    monkeypatch.setattr(routes_core, "relaunch_lock_for", _observed_lock_for)

    create = asyncio.create_task(
        _create_git_session(
            client, agent["id"], {"branch_name": "feature/patch", "base_branch": "main"}
        )
    )
    patch: asyncio.Task[httpx.Response] | None = None
    try:
        await asyncio.wait_for(in_removal.wait(), timeout=5)
        conv_id = observed["conv_id"]
        failed_runner_id = store.get_conversation(conv_id).runner_id  # type: ignore[union-attr]
        assert failed_runner_id is not None

        patch = asyncio.create_task(
            client.patch(f"/v1/sessions/{conv_id}", json={"runner_id": "runner_b"})
        )
        # The PATCH reaches the lock the rollback holds, so it cannot finish, and
        # the row still carries the failed launch's runner until the unbind.
        await asyncio.wait_for(patch_at_lock.wait(), timeout=5)
        assert not patch.done(), "the rebind must wait for the rollback to finish"
        assert store.get_conversation(conv_id).runner_id == failed_runner_id  # type: ignore[union-attr]

        proceed.set()
        resp = await asyncio.wait_for(create, timeout=5)
        patch_resp = await asyncio.wait_for(patch, timeout=10)

        assert resp.status_code == 201, resp.text
        assert patch_resp.status_code == 200, patch_resp.text
        assert len(cap.launch) == 1, (
            f"expected only the failed launch frame; unexpected={cap.unexpected}"
        )
        assert cap.remove == [], f"the barrier stands in for real removal: {cap.remove}"

        # The rollback unbound the removed worktree first; the rebind that
        # waited on the lock then landed and was not erased.
        conv = store.get_conversation(conv_id)
        assert conv is not None
        assert conv.runner_id == "runner_b", conv.runner_id
        assert conv.host_id is None and conv.workspace is None and conv.git_branch is None
        assert WORKTREE_ROOT_LABEL_KEY not in conv.labels
    finally:
        proceed.set()
        for task in (create, patch):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@pytest.mark.parametrize("fault", ["reread", "shared_check"])
async def test_inline_create_launch_failure_tolerates_pre_removal_store_error(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    """A transient store error before the host round-trip, on the rollback's
    re-read or in the shared-worktree guard inside the removal helper, must not
    500 the create. No removal runs, so the response and the persisted row keep
    reporting the binding and the root label for a later delete or archive."""
    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name=f"wt-{fault}-err-agent")
    raised = {"done": False}

    if fault == "reread":
        original_get = SqlAlchemyConversationStore.get_conversation

        def _raising_get(self: SqlAlchemyConversationStore, conversation_id: str) -> Any:
            conv = original_get(self, conversation_id)
            # Raise once, on the rollback's re-read after the failed launch.
            if (
                not raised["done"]
                and cap.launch
                and conv is not None
                and conv.runner_id is not None
            ):
                raised["done"] = True
                raise RuntimeError("transient store error")
            return conv

        monkeypatch.setattr(SqlAlchemyConversationStore, "get_conversation", _raising_get)
    else:

        def _shared_boom(self: SqlAlchemyConversationStore, **kwargs: Any) -> bool:
            raised["done"] = True
            raise RuntimeError("transient store error")

        monkeypatch.setattr(
            SqlAlchemyConversationStore, "has_other_live_session_in_workspace", _shared_boom
        )

    branch = f"feature/{fault}-err"
    resp = await _create_git_session(
        client, agent["id"], {"branch_name": branch, "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text
    assert raised["done"], f"the {fault} fault was never hit, so the failure was not exercised"
    assert cap.remove == [], (
        f"the {fault} fault precedes the host round-trip, so no removal must be sent: "
        f"{cap.remove}; unexpected={cap.unexpected}"
    )
    worktree_path = f"{_SOURCE_REPO}-worktrees/feature-{fault}-err"
    body = resp.json()
    # Removal is unconfirmed, so the binding and root label stay for a later retry.
    assert body["host_id"] == _HOST_ID and body["runner_id"] is not None, body
    assert body["workspace"] == worktree_path and body["git_branch"] == branch, body
    assert body["labels"].get(WORKTREE_ROOT_LABEL_KEY) == worktree_root_fingerprint(
        worktree_path
    ), body

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(body["id"])
    assert conv is not None
    assert conv.host_id == _HOST_ID and conv.runner_id is not None
    assert conv.workspace == worktree_path and conv.git_branch == branch
    assert conv.labels.get(WORKTREE_ROOT_LABEL_KEY) == worktree_root_fingerprint(worktree_path)


async def test_inline_create_launch_failure_reconciles_binding_when_cleanup_readback_fails(
    register_worktree_launch_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``clear_host_binding`` commits the unbind before reading the row back, so a
    failure after that commit must not report a stale bound session. The create
    re-reads the row and returns the unbound result that was actually persisted."""
    cap = register_worktree_launch_host(launch_status="failed")
    agent = await create_test_agent(client, name="wt-readbackerr-agent")

    original_clear = SqlAlchemyConversationStore.clear_host_binding

    def _clear_then_boom(self: SqlAlchemyConversationStore, conversation_id: str) -> Any:
        # Let the real unbind commit, then fail as a post-commit readback would.
        original_clear(self, conversation_id)
        raise RuntimeError("post-commit readback error")

    monkeypatch.setattr(SqlAlchemyConversationStore, "clear_host_binding", _clear_then_boom)

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/readbackerr", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text
    assert len(cap.remove) == 1, (
        f"the worktree must still be removed before the clear readback failed: "
        f"{cap.remove}; unexpected={cap.unexpected}"
    )
    body = resp.json()
    # The unbind committed, so the reconciled response reports the session
    # unbound, matching persisted state, despite the clear call raising.
    assert body["host_id"] is None and body["runner_id"] is None, body
    assert body["workspace"] is None and body["git_branch"] is None, body
    assert WORKTREE_ROOT_LABEL_KEY not in body["labels"], body

    conv = SqlAlchemyConversationStore(db_uri).get_conversation(body["id"])
    assert conv is not None
    assert conv.host_id is None and conv.runner_id is None
    assert conv.workspace is None and conv.git_branch is None
    assert WORKTREE_ROOT_LABEL_KEY not in conv.labels
