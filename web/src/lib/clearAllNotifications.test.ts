import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import { getSeenCommentIds } from "@/hooks/useSeenComments";
import {
  isConversationUnseen,
  markConversationUnread,
  resetReadStateForTests,
  seedReadState,
} from "@/hooks/useUnseenConversations";
import { clearAllNotifications } from "./clearAllNotifications";

const toastMocks = vi.hoisted(() => ({ dismiss: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ toast: toastMocks }));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(async () => new Response(null, { status: 204 })),
}));

function conversation(id: string, pendingApprovals = 0): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 1_000,
    updated_at: 2_000,
    labels: {},
    permission_level: 4,
    status: "idle",
    pending_elicitations_count: pendingApprovals,
  };
}

beforeEach(() => {
  localStorage.clear();
  resetReadStateForTests();
  toastMocks.dismiss.mockReset();
});

describe("clearAllNotifications", () => {
  it("marks unread sessions and comments read, dismisses toasts, and preserves approvals", () => {
    const unread = conversation("unread");
    const unreadWithApproval = conversation("approval", 2);
    const alreadyRead = conversation("read");
    const conversations = [unread, unreadWithApproval, alreadyRead];
    seedReadState(conversations);
    markConversationUnread(unread.id, unread.updated_at);
    markConversationUnread(unreadWithApproval.id, unreadWithApproval.updated_at);

    clearAllNotifications(conversations, ["comment-1", "comment-2"]);

    expect(isConversationUnseen(unread.id, unread.updated_at, unread.status)).toBe(false);
    expect(
      isConversationUnseen(
        unreadWithApproval.id,
        unreadWithApproval.updated_at,
        unreadWithApproval.status,
      ),
    ).toBe(false);
    expect(isConversationUnseen(alreadyRead.id, alreadyRead.updated_at, alreadyRead.status)).toBe(
      false,
    );
    expect(getSeenCommentIds()).toEqual(new Set(["comment-1", "comment-2"]));
    expect(unreadWithApproval.pending_elicitations_count).toBe(2);
    expect(toastMocks.dismiss).toHaveBeenCalledExactlyOnceWith();
  });
});
