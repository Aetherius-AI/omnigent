import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  isClearAllNotificationsHotkey,
  useClearAllNotificationsHotkey,
} from "./useClearAllNotificationsHotkey";

afterEach(() => vi.restoreAllMocks());

describe("isClearAllNotificationsHotkey", () => {
  it("matches only unmodified Shift+Escape", () => {
    expect(
      isClearAllNotificationsHotkey(
        new KeyboardEvent("keydown", { key: "Escape", shiftKey: true }),
      ),
    ).toBe(true);
    expect(isClearAllNotificationsHotkey(new KeyboardEvent("keydown", { key: "Escape" }))).toBe(
      false,
    );
    expect(
      isClearAllNotificationsHotkey(
        new KeyboardEvent("keydown", { key: "Escape", shiftKey: true, metaKey: true }),
      ),
    ).toBe(false);
  });
});

describe("useClearAllNotificationsHotkey", () => {
  it("claims Shift+Escape before plain-Escape handlers and clears once", () => {
    const onClear = vi.fn();
    const descendantEscapeHandler = vi.fn();
    document.addEventListener("keydown", descendantEscapeHandler);
    renderHook(() => useClearAllNotificationsHotkey(onClear));

    const event = new KeyboardEvent("keydown", {
      key: "Escape",
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    document.body.dispatchEvent(event);

    expect(onClear).toHaveBeenCalledOnce();
    expect(descendantEscapeHandler).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(true);
    document.removeEventListener("keydown", descendantEscapeHandler);
  });

  it("leaves plain Escape available to existing handlers", () => {
    const onClear = vi.fn();
    renderHook(() => useClearAllNotificationsHotkey(onClear));

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));

    expect(onClear).not.toHaveBeenCalled();
  });
});
