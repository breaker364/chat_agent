// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ToastHost from "./ToastHost";
import { dismissToast, showToast, subscribeToasts } from "./toast";

describe("toast", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("pushes a toast to subscribers and dismisses it", () => {
    const events = [];
    const unsubscribe = subscribeToasts((event) => events.push(event));
    const id = showToast("导入完成", "success");
    expect(events).toHaveLength(1);
    expect(events[0].kind).toBe("push");
    expect(events[0].toast.message).toBe("导入完成");
    expect(events[0].toast.type).toBe("success");
    dismissToast(id);
    expect(events[1].kind).toBe("dismiss");
    expect(events[1].id).toBe(id);
    unsubscribe();
  });

  it("renders toasts and removes them after the auto-dismiss delay", () => {
    vi.useFakeTimers();
    render(<ToastHost />);
    act(() => {
      showToast("同步失败", "error");
    });
    expect(screen.getByText("同步失败")).toBeTruthy();
    expect(document.querySelector(".toast-error")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(3500);
    });
    expect(screen.queryByText("同步失败")).toBeNull();
  });

  it("supports closing a toast via its close button", () => {
    render(<ToastHost />);
    act(() => {
      showToast("提示", "info", 0);
    });
    fireEvent.click(screen.getByRole("button", { name: "关闭提示" }));
    expect(screen.queryByText("提示")).toBeNull();
  });
});
