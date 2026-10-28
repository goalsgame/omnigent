import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const shell = vi.hoisted(() => ({ electron: false }));
vi.mock("./nativeBridge", () => ({ isElectronShell: () => shell.electron }));

import {
  beginConnection,
  CONNECTION_WINDOW,
  finishConnectionWindow,
  useConnectionWindow,
} from "./connectionFlow";

const origin = window.location.origin;

const setOpener = (value: unknown) =>
  Object.defineProperty(window, "opener", { configurable: true, writable: true, value });

afterEach(() => {
  shell.electron = false;
  window.name = "";
  setOpener(null);
  vi.restoreAllMocks();
});

describe("beginConnection", () => {
  const path = "/v1/connections/google_drive/connect?return_to=%2Fsettings";
  let location: Location;

  const at = (href: string) =>
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...location, href, protocol: new URL(href).protocol },
    });

  beforeEach(() => {
    location = window.location;
    at("https://omnigent.example/settings");
  });

  afterEach(() => {
    Object.defineProperty(window, "location", { configurable: true, value: location });
  });

  it("opens the flow in a sized sign-in window from the desktop app", () => {
    shell.electron = true;
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    beginConnection(path);
    expect(open).toHaveBeenCalledWith(
      `https://omnigent.example${path}`,
      CONNECTION_WINDOW,
      expect.stringMatching(/width=/),
    );
    expect(window.location.href).toBe("https://omnigent.example/settings");
  });

  it("navigates the page for a plain-HTTP server in the desktop app", () => {
    shell.electron = true;
    at("http://localhost:8000/settings");
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    beginConnection(path);
    expect(open).not.toHaveBeenCalled();
    expect(window.location.href).toBe(path);
  });

  it("navigates the page in a browser", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    beginConnection(path);
    expect(open).not.toHaveBeenCalled();
    expect(window.location.href).toBe(path);
  });
});

describe("finishConnectionWindow", () => {
  it("hands the outcome to the app window and closes the sign-in window", () => {
    const postMessage = vi.fn();
    setOpener({ postMessage });
    const close = vi.spyOn(window, "close").mockImplementation(() => {});
    window.name = CONNECTION_WINDOW;
    expect(finishConnectionWindow("google_drive", "connected")).toBe(true);
    expect(postMessage).toHaveBeenCalledWith(
      { type: "omnigent:connection", provider: "google_drive", outcome: "connected" },
      origin,
    );
    expect(close).toHaveBeenCalled();
  });

  it("does nothing outside the sign-in window", () => {
    const close = vi.spyOn(window, "close").mockImplementation(() => {});
    expect(finishConnectionWindow("google_drive", "connected")).toBe(false);
    window.name = "other";
    setOpener({ postMessage: vi.fn() });
    expect(finishConnectionWindow("google_drive", "connected")).toBe(false);
    expect(close).not.toHaveBeenCalled();
  });
});

describe("useConnectionWindow", () => {
  const message = (data: unknown, from = origin) =>
    act(() => void window.dispatchEvent(new MessageEvent("message", { data, origin: from })));

  it("reports the provider's outcome and refreshes in the desktop app", () => {
    shell.electron = true;
    const onOutcome = vi.fn();
    const refresh = vi.fn();
    const { unmount } = renderHook(() => useConnectionWindow("google_drive", onOutcome, refresh));
    message({ type: "omnigent:connection", provider: "google_drive", outcome: "connected" });
    expect(onOutcome).toHaveBeenCalledWith("connected");
    expect(refresh).toHaveBeenCalledTimes(1);
    act(() => void window.dispatchEvent(new Event("focus")));
    expect(refresh).toHaveBeenCalledTimes(2);
    unmount();
    act(() => void window.dispatchEvent(new Event("focus")));
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("ignores other providers, other origins and malformed messages", () => {
    shell.electron = true;
    const onOutcome = vi.fn();
    renderHook(() => useConnectionWindow("google_drive", onOutcome, vi.fn()));
    message({ type: "omnigent:connection", provider: "github", outcome: "connected" });
    message(
      { type: "omnigent:connection", provider: "google_drive", outcome: "connected" },
      "https://evil.example",
    );
    message({ type: "omnigent:connection", provider: "google_drive", outcome: "nope" });
    message("connected");
    expect(onOutcome).not.toHaveBeenCalled();
  });

  it("does nothing in a browser", () => {
    const onOutcome = vi.fn();
    const refresh = vi.fn();
    renderHook(() => useConnectionWindow("google_drive", onOutcome, refresh));
    message({ type: "omnigent:connection", provider: "google_drive", outcome: "connected" });
    act(() => void window.dispatchEvent(new Event("focus")));
    expect(onOutcome).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });
});
