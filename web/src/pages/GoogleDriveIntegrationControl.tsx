import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  beginConnection,
  type ConnectionOutcome,
  finishConnectionWindow,
  useConnectionWindow,
} from "@/lib/connectionFlow";
import { authenticatedFetch } from "@/lib/identity";

interface ConnectionStatus {
  connected: boolean;
  email: string | null;
}

const STATUS_ERROR = "Google Drive connection status is unavailable. Please retry.";

export function GoogleDriveIntegrationControl() {
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connectedNotice, setConnectedNotice] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const response = await authenticatedFetch("/v1/connections/google_drive/status");
      if (!response.ok) throw new Error("status unavailable");
      setStatus((await response.json()) as ConnectionStatus);
      setError((current) => (current === STATUS_ERROR ? null : current));
    } catch {
      setStatus(null);
      setError(STATUS_ERROR);
    } finally {
      setLoading(false);
    }
  }, []);

  const showOutcome = useCallback((outcome: ConnectionOutcome) => {
    setConnectedNotice(outcome === "connected");
    if (outcome === "error")
      setError("Could not connect Google Drive. Try again and grant the requested permissions.");
  }, []);

  useConnectionWindow("google_drive", showOutcome, refresh);

  useEffect(() => {
    const url = new URL(window.location.href);
    const outcome = url.searchParams.get("google_drive");
    if (
      (outcome === "connected" || outcome === "error") &&
      finishConnectionWindow("google_drive", outcome)
    ) {
      return;
    }
    void refresh();
    if (outcome === "connected" || outcome === "error") {
      showOutcome(outcome);
      url.searchParams.delete("google_drive");
      window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    }
  }, [refresh, showOutcome]);

  async function disconnect() {
    setBusy(true);
    setError(null);
    setConnectedNotice(false);
    try {
      const response = await authenticatedFetch("/v1/connections/google_drive/disconnect", {
        method: "POST",
      });
      if (!response.ok) throw new Error("disconnect failed");
      await refresh();
    } catch {
      setError("Could not disconnect Google Drive. Please retry.");
    } finally {
      setBusy(false);
    }
  }

  function connect() {
    const returnTo = `${window.location.pathname}${window.location.search}`;
    beginConnection(
      `/v1/connections/google_drive/connect?return_to=${encodeURIComponent(returnTo)}`,
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {connectedNotice && (
        <p role="status" className="text-sm">
          Google Drive connected.
        </p>
      )}
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="text-sm font-medium">Google Drive</span>
          <span className="text-sm text-muted-foreground">
            {loading
              ? "Checking connection…"
              : status?.connected
                ? `Connected as ${status.email}. Nothing is shared until you add files or allow Drive access in a session.`
                : "Connect Google Drive so agents can read and edit Docs, Sheets and other files you share with a session."}
          </span>
        </div>
        {!loading && (
          <div className="flex shrink-0 items-center gap-2">
            {!status ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setError(null);
                  void refresh();
                }}
              >
                Retry
              </Button>
            ) : (
              <>
                <Button size="sm" disabled={busy} onClick={connect}>
                  {status.connected ? "Reconnect" : "Connect Google Drive"}
                </Button>
                {status.connected && (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => void disconnect()}
                  >
                    Disconnect
                  </Button>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
