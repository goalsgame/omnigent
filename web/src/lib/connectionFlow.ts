/**
 * Starts server-owned connection flows (``/v1/connections/<provider>/connect``).
 *
 * In the desktop app the flow runs in the shell's sign-in window, which shares
 * the app's session, so the provider's sign-in never replaces the app window.
 * When the provider sends the user back, that window hands the outcome to the
 * app and closes. Elsewhere the page navigates to the flow.
 */

import { useEffect, useRef } from "react";
import { withBasePath } from "./basePath";
import { isElectronShell } from "./nativeBridge";

/** Name of the desktop sign-in window. */
export const CONNECTION_WINDOW = "omnigent-connect";
const MESSAGE_TYPE = "omnigent:connection";

export type ConnectionOutcome = "connected" | "error";

/** Begin the connect flow at ``path`` (relative to the server's base path). */
export function beginConnection(path: string): void {
  const url = withBasePath(path);
  if (isElectronShell()) {
    window.open(
      new URL(url, window.location.href).href,
      CONNECTION_WINDOW,
      "popup,width=520,height=720",
    );
    return;
  }
  window.location.href = url;
}

/**
 * In the desktop sign-in window, hand ``outcome`` to the app window and close.
 * Returns false anywhere else, where the caller shows the outcome itself.
 */
export function finishConnectionWindow(provider: string, outcome: ConnectionOutcome): boolean {
  const opener = window.opener as Window | null;
  if (window.name !== CONNECTION_WINDOW || !opener) return false;
  opener.postMessage({ type: MESSAGE_TYPE, provider, outcome }, window.location.origin);
  window.close();
  return true;
}

/**
 * In the desktop app, call ``onOutcome`` when the sign-in window reports
 * ``provider``'s outcome, and ``refresh`` when the app window regains focus.
 */
export function useConnectionWindow(
  provider: string,
  onOutcome: (outcome: ConnectionOutcome) => void,
  refresh: () => unknown,
): void {
  const handlers = useRef({ onOutcome, refresh });
  handlers.current = { onOutcome, refresh };
  useEffect(() => {
    if (!isElectronShell()) return;
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; provider?: unknown; outcome?: unknown } | null;
      if (
        event.origin !== window.location.origin ||
        data?.type !== MESSAGE_TYPE ||
        data.provider !== provider ||
        (data.outcome !== "connected" && data.outcome !== "error")
      ) {
        return;
      }
      handlers.current.onOutcome(data.outcome);
      void handlers.current.refresh();
    };
    const onFocus = () => void handlers.current.refresh();
    window.addEventListener("message", onMessage);
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("message", onMessage);
      window.removeEventListener("focus", onFocus);
    };
  }, [provider]);
}
