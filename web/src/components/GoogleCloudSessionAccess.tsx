import { useEffect, useRef, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";

interface Access {
  state: "off" | "allowed" | "pending" | "denied" | "not_connected";
  email: string | null;
  generation: string | null;
}

/** Owner-only consent for all processes sharing this session's sandbox. */
export function GoogleCloudSessionAccess({ sessionId }: { sessionId: string }) {
  const [access, setAccess] = useState<Access | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const revision = useRef(0);
  const endpoint = `/v1/connections/google_cloud/sessions/${encodeURIComponent(sessionId)}/access`;

  useEffect(() => {
    let active = true;
    let loading = false;
    const controller = new AbortController();
    const refresh = async () => {
      if (loading) return;
      loading = true;
      const observed = revision.current;
      try {
        const response = await authenticatedFetch(endpoint, { signal: controller.signal });
        if (response.status === 409 || response.status === 403) {
          if (active && observed === revision.current) setAccess(null);
          return;
        }
        if (!response.ok) throw new Error("unavailable");
        const result = (await response.json()) as Access;
        if (active && observed === revision.current) {
          setAccess(result);
          setError(null);
        }
      } catch {
        if (active) setError("Google Cloud access status is unavailable. Retry shortly.");
      } finally {
        loading = false;
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => {
      active = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [endpoint]);

  const decide = async (decision: "allowed" | "denied") => {
    if (!access?.generation || busy) return;
    revision.current += 1;
    setBusy(true);
    setError(null);
    try {
      const response = await authenticatedFetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, generation: access.generation }),
      });
      if (!response.ok) throw new Error("unavailable");
      setAccess((await response.json()) as Access);
    } catch {
      setError("Could not update Google Cloud access. Refresh the session and retry.");
    } finally {
      revision.current += 1;
      setBusy(false);
    }
  };

  if (!access && !error) return null;
  const pending = access?.state === "pending";
  const allowed = access?.state === "allowed";
  return (
    <section
      className="border-b border-border px-4 py-2 text-sm"
      aria-label="Session Google Cloud access"
    >
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded || pending}
      >
        {pending ? "Google Cloud access requested" : `Google Cloud: ${allowed ? "allowed" : "off"}`}
      </button>
      {(expanded || pending) && access && (
        <div className="mt-2 space-y-2">
          {access.state === "not_connected" ? (
            <p>Connect Google Cloud in Settings → Sandbox Integrations, then allow access here.</p>
          ) : (
            <>
              <p>
                Allow this session's sandbox, including its agents and subprocesses, to use{" "}
                {access.email}'s Google Cloud permissions? This includes any read or write access
                that account already has.
              </p>
              <p>Anyone who can run commands in this session can use those permissions.</p>
              <p>
                Revoking stops new tokens. Tokens already issued may remain valid for up to one
                hour.
              </p>
              {pending && (
                <p>The command was blocked. After approval, ask the agent to retry it.</p>
              )}
              <div className="flex gap-3">
                {!allowed && (
                  <button type="button" disabled={busy} onClick={() => void decide("allowed")}>
                    Allow for this session
                  </button>
                )}
                {(allowed || pending) && (
                  <button type="button" disabled={busy} onClick={() => void decide("denied")}>
                    {allowed ? "Revoke access" : "Deny"}
                  </button>
                )}
              </div>
            </>
          )}
        </div>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
