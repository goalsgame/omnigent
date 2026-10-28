/**
 * Starts server-owned connection flows (``/v1/connections/<provider>/connect``).
 *
 * In the desktop app the flow opens in the system browser, so the provider's
 * sign-in never replaces the app window; elsewhere the page navigates to it.
 */

import { useEffect } from "react";
import { withBasePath } from "./basePath";
import { isElectronShell } from "./nativeBridge";

/** Begin the connect flow at ``path`` (relative to the server's base path). */
export function beginConnection(path: string): void {
  const url = withBasePath(path);
  if (isElectronShell()) {
    window.open(new URL(url, window.location.href).href, "_blank", "noopener");
    return;
  }
  window.location.href = url;
}

/** In the desktop app, call ``refresh`` when the window regains focus after a browser flow. */
export function useRefreshOnFocus(refresh: () => unknown): void {
  useEffect(() => {
    if (!isElectronShell()) return;
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);
}
