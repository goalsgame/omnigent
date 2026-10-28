import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const shell = vi.hoisted(() => ({ electron: false }));
vi.mock("./nativeBridge", () => ({ isElectronShell: () => shell.electron }));

import { beginConnection, useRefreshOnFocus } from "./connectionFlow";

describe("beginConnection", () => {
  const path = "/v1/connections/google_drive/connect?return_to=%2Fsettings";
  let open: ReturnType<typeof vi.spyOn>;
  let location: Location;

  beforeEach(() => {
    open = vi.spyOn(window, "open").mockReturnValue(null);
    location = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...location, href: "https://omnigent.example/settings" },
    });
  });

  afterEach(() => {
    open.mockRestore();
    Object.defineProperty(window, "location", { configurable: true, value: location });
    shell.electron = false;
  });

  it("opens the flow in the system browser from the desktop app", () => {
    shell.electron = true;
    beginConnection(path);
    expect(open).toHaveBeenCalledWith(`https://omnigent.example${path}`, "_blank", "noopener");
    expect(window.location.href).toBe("https://omnigent.example/settings");
  });

  it("navigates the page in a browser", () => {
    beginConnection(path);
    expect(open).not.toHaveBeenCalled();
    expect(window.location.href).toBe(path);
  });
});

describe("useRefreshOnFocus", () => {
  afterEach(() => {
    shell.electron = false;
  });

  it("refreshes on focus in the desktop app", () => {
    shell.electron = true;
    const refresh = vi.fn();
    const { unmount } = renderHook(() => useRefreshOnFocus(refresh));
    act(() => void window.dispatchEvent(new Event("focus")));
    expect(refresh).toHaveBeenCalledTimes(1);
    unmount();
    window.dispatchEvent(new Event("focus"));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("does nothing in a browser", () => {
    const refresh = vi.fn();
    renderHook(() => useRefreshOnFocus(refresh));
    window.dispatchEvent(new Event("focus"));
    expect(refresh).not.toHaveBeenCalled();
  });
});
