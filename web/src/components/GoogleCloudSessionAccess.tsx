import { useEffect, useRef, useState } from "react";
import { CloudIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
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
          if (active && observed === revision.current) {
            setAccess(null);
            setError(null);
            setExpanded(false);
          }
          return;
        }
        if (!response.ok) throw new Error("unavailable");
        const result = (await response.json()) as Access;
        if (active && observed === revision.current) {
          setAccess(result);
          setError(null);
        }
      } catch {
        if (active && observed === revision.current)
          setError("Google Cloud access status is unavailable. Retry shortly.");
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
      setExpanded(false);
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
    <Dialog open={expanded} onOpenChange={setExpanded}>
      <DialogTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="shrink-0 text-muted-foreground hover:text-foreground max-md:size-11"
          aria-label={`Google Cloud access: ${allowed ? "allowed" : pending ? "requested" : "off"}`}
          title="Google Cloud access"
        >
          <CloudIcon className="size-4 max-md:size-5" />
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            {pending ? "Allow Google Cloud access?" : "Google Cloud access"}
          </DialogTitle>
          <DialogDescription>
            {allowed
              ? "This session can use your connected Google account."
              : "Choose whether this session can use your connected Google account."}
          </DialogDescription>
        </DialogHeader>
        <div className="min-w-0 space-y-3 text-sm">
          {access?.state === "not_connected" ? (
            <p>Connect Google Cloud in Settings → Sandbox Integrations, then allow access here.</p>
          ) : access ? (
            <>
              <p className="break-words font-medium">{access.email}</p>
              <p>
                The sandbox's agents and subprocesses will have this account's existing Google Cloud
                read and write permissions. Anyone who can run commands in this session can use
                those permissions.
              </p>
              <p className="text-muted-foreground">
                Revoking stops new tokens. Tokens already issued may remain valid for up to one
                hour.
              </p>
              {pending && (
                <p>
                  Google Cloud access is awaiting owner approval in the session. Commands held at
                  the approval gate continue after approval; commands that already failed need
                  retrying.
                </p>
              )}
            </>
          ) : null}
          {error && (
            <p role="alert" className="text-destructive">
              {error}
            </p>
          )}
        </div>
        <DialogFooter>
          {access && access.state !== "not_connected" && (
            <>
              {(allowed || pending) && (
                <Button variant="outline" disabled={busy} onClick={() => void decide("denied")}>
                  {allowed ? "Revoke access" : "Deny"}
                </Button>
              )}
              {!allowed && (
                <Button
                  disabled={busy || !access.generation}
                  onClick={() => void decide("allowed")}
                >
                  Allow for this session
                </Button>
              )}
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
