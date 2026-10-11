"""Items, child-session and sub-agent subtree routes."""

from __future__ import annotations

import asyncio

from fastapi import (
    APIRouter,
    Query,
    Request,
)

from omnigent.entities import Conversation
from omnigent.errors import StaleCursorError
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.runtime.policies.builder import load_session_tree
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_READ,
    AuthProvider,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import (
    STALE_CURSOR_RESPONSE,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import (
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.orchestration import (
    _child_session_summaries_from_conversations,
    _descendant_session_summaries_from_conversations,
)
from omnigent.server.schemas import (
    ChildSessionList,
    DescendantSessionList,
    PaginatedList,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore


def _descendant_rows(
    tree: list[Conversation],
    session_id: str,
    *,
    max_depth: int | None,
    include_archived: bool,
) -> list[tuple[Conversation, str, int]]:
    """
    Order the sub-agent subtree under *session_id* breadth-first.

    Children of each node come newest-first (``created_at`` desc, then id),
    matching ``GET …/child_sessions``. Archived rows are walked regardless
    of ``include_archived`` so live work under an archived node is still
    reached; without it, an archived row is kept only while a live
    descendant needs it to keep the listing connected.

    :param tree: Every conversation in the spawn tree, archived included.
    :param session_id: Subtree root; excluded from the result.
    :param max_depth: Deepest level to return, or ``None`` for all.
    :param include_archived: Also keep archived rows with no live descendant.
    :returns: ``(conversation, parent_id, depth)`` triples in listing order.
    """
    children_by_parent: dict[str, list[Conversation]] = {}
    for conv in tree:
        if conv.kind == "sub_agent" and conv.parent_conversation_id is not None:
            children_by_parent.setdefault(conv.parent_conversation_id, []).append(conv)
    for siblings in children_by_parent.values():
        siblings.sort(key=lambda conv: (conv.created_at, conv.id), reverse=True)

    rows: list[tuple[Conversation, str, int]] = []
    seen = {session_id}
    frontier = [session_id]
    depth = 0
    while frontier and (max_depth is None or depth < max_depth):
        depth += 1
        next_frontier: list[str] = []
        for parent_id in frontier:
            for conv in children_by_parent.get(parent_id, []):
                if conv.id in seen:
                    continue
                seen.add(conv.id)
                rows.append((conv, parent_id, depth))
                next_frontier.append(conv.id)
        frontier = next_frontier
    if include_archived:
        return rows
    # Reverse breadth-first order visits every child before its parent.
    live_below: set[str] = set()
    for conv, parent_id, _ in reversed(rows):
        if not conv.archived or conv.id in live_below:
            live_below.add(parent_id)
    return [row for row in rows if not row[0].archived or row[0].id in live_below]


def register_items_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the items routes on router."""

    @router.get(
        "/sessions/{session_id}/items",
        response_model=None,
        responses={200: {"model": PaginatedList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_session_items(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> PaginatedList:
        """
        List items in a session with cursor-based pagination.

        Delegates to the conversation items store — session_id is
        the conversation_id. Same pagination contract as
        ``GET /v1/conversations/{id}/items``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return
            (1-1000, default 100).
        :param after: Cursor — return items after this item ID,
            e.g. ``"msg_abc123"``.
        :param before: Cursor — return items before this item ID.
        :param order: Sort order, ``"asc"`` (chronological,
            default) or ``"desc"``.
        :returns: A :class:`PaginatedList` of conversation items.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        page = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [m.to_api_dict() for m in page.data]
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/child_sessions ────────────────

    @router.get(
        "/sessions/{session_id}/child_sessions",
        response_model=None,
        responses={200: {"model": ChildSessionList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_child_sessions(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        tool: str | None = Query(default=None),
        session_name: str | None = Query(default=None),
    ) -> PaginatedList:
        """
        List sub-agent (child) sessions under a parent session.

        Returns a page of :class:`ChildSessionSummary` objects
        derived from child conversations (``kind="sub_agent"``,
        ``parent_conversation_id=session_id``) plus each child's
        latest task. Powers the web / REPL debug surfaces' "child
        sessions" panel without parsing parent
        ``function_call_output`` JSON handles. Pagination contract
        matches :func:`list_session_items` so existing client code
        can reuse the same cursor logic.

        :param request: Inbound HTTP request; carries the caller
            identity used to authorize READ on the parent session.
        :param session_id: Parent session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of children to return
            (1-1000, default 20 — sub-agent fan-out is typically
            sparse compared to conversation items).
        :param after: Cursor — return children whose id appears
            after this one in sort order,
            e.g. ``"conv_child123"``.
        :param before: Cursor — return children before this one.
        :param order: Sort direction, ``"desc"`` (newest-first,
            default) or ``"asc"``. Sort column is ``created_at``.
        :param tool: When set, only return children whose title
            starts with this agent type (the segment before the
            ``":"``). Combined with ``session_name`` to form the
            exact title ``"{tool}:{session_name}"`` for server-side
            filtering.
        :param session_name: When set alongside ``tool``, only
            return children whose title matches
            ``"{tool}:{session_name}"`` exactly.
        :returns: A :class:`PaginatedList` of
            :class:`ChildSessionSummary` objects.
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there.
        """
        user_id = _get_user_id(request, auth_provider)
        # Require READ on the parent before listing its children (no cross-user enumeration).
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        parent = access.conversation
        if parent is None:
            parent = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if parent is None:
            raise _session_not_found()
        title_filter: str | None = None
        if tool and session_name:
            title_filter = f"{tool}:{session_name}"
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            kind="sub_agent",
            parent_conversation_id=session_id,
            order=order,
            sort_by="created_at",
            title=title_filter,
        )
        data = await _child_session_summaries_from_conversations(
            page.data,
            session_id,
            conversation_store,
        )
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/descendants ───────────────────

    @router.get(
        "/sessions/{session_id}/descendants",
        response_model=None,
        responses={200: {"model": DescendantSessionList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_descendant_sessions(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
        max_depth: int | None = Query(default=None, ge=1),
        include_archived: bool = Query(default=False),
    ) -> PaginatedList:
        """
        List every sub-agent session under a parent, at any depth.

        The whole subtree comes back as one flat, breadth-first listing of
        :class:`DescendantSessionSummary` rows, each naming its own parent
        and depth, so a client rebuilds the tree with one request per
        page instead of one ``GET …/child_sessions`` per node (the REPL
        navigator and the SDK ``child_sessions_tree`` / ``subtree_busy``
        helpers read this). The rows come from the spawn tree the store
        indexes by ``root_conversation_id``; children of each node are
        newest-first, matching :func:`list_child_sessions`.

        Archived nodes are walked regardless, so live work under an
        archived session stays visible. Without ``include_archived`` an
        archived row is returned only while a live descendant needs it to
        keep the listing connected.

        :param request: Inbound HTTP request; carries the caller identity
            used to authorize READ on ``session_id``.
        :param session_id: Session whose subtree to list, e.g.
            ``"conv_abc123"``. Not itself included in the result.
        :param limit: Maximum rows per page (1-1000, default 100).
        :param after: Cursor — return rows after this session id in the
            listing order, e.g. ``"conv_child123"``.
        :param max_depth: Deepest level to return (1 = direct children
            only). ``None`` returns the whole subtree.
        :param include_archived: Also return archived descendants that
            have no live descendant of their own.
        :returns: A :class:`PaginatedList` of
            :class:`DescendantSessionSummary` objects.
        :raises OmnigentError: 404 if no readable session exists at
            ``session_id``; 400 ``stale_cursor`` if ``after`` names a row
            that is no longer listed.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        parent = access.conversation
        if parent is None:
            parent = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if parent is None:
            raise _session_not_found()
        tree = await asyncio.to_thread(
            load_session_tree, session_id, conversation_store, parent.root_conversation_id
        )
        rows = _descendant_rows(
            tree, session_id, max_depth=max_depth, include_archived=include_archived
        )
        start = 0
        if after is not None:
            position = next((i for i, (conv, _, _) in enumerate(rows) if conv.id == after), None)
            if position is None:
                raise StaleCursorError(after)
            start = position + 1
        data = await _descendant_session_summaries_from_conversations(
            rows[start : start + limit], conversation_store
        )
        return PaginatedList(
            data=data,
            first_id=data[0].id if data else None,
            last_id=data[-1].id if data else None,
            has_more=start + limit < len(rows),
        )
