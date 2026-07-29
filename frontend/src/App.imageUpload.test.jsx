// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status || 200,
    headers: { "Content-Type": "application/json" },
  });
}

async function renderApp() {
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (url) => {
    if (url === "/feishu/session") {
      return jsonResponse({ logged_in: false, has_session: false, issued_at: null, metadata: {} });
    }
    if (url === "/sessions") {
      return jsonResponse({ sessions: [] });
    }
    return jsonResponse({ error: "unexpected request" }, { status: 404 });
  }));

  render(<App />);
  return screen.findByPlaceholderText(/send a message/i);
}

function imageTransfer(file) {
  return {
    types: ["Files"],
    files: [file],
    items: [
      {
        kind: "file",
        type: file.type,
        getAsFile: () => file,
      },
    ],
  };
}

describe("App image upload input", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("shows an image upload target and queues a dropped image", async () => {
    const textarea = await renderApp();
    const composer = textarea.closest("footer");
    const imageFile = new File(["image-bytes"], "dropped-image.png", {
      type: "image/png",
      lastModified: 1000,
    });

    fireEvent.dragEnter(composer, { dataTransfer: imageTransfer(imageFile) });

    expect(screen.getByText("Drop images to upload")).toBeTruthy();

    fireEvent.drop(composer, { dataTransfer: imageTransfer(imageFile) });

    await waitFor(() => expect(screen.getByText("dropped-image.png")).toBeTruthy());
  });

  it("queues an image pasted from the clipboard with a usable generated name", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1710000000000);
    const textarea = await renderApp();
    const pastedImage = new File(["image-bytes"], "", {
      type: "image/png",
      lastModified: 2000,
    });

    fireEvent.paste(textarea, { clipboardData: imageTransfer(pastedImage) });

    await waitFor(() => expect(screen.getByText("pasted-image-1710000000000.png")).toBeTruthy());
  });
});
