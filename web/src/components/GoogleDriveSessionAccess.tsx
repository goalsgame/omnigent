import { useCallback, useEffect, useRef, useState } from "react";
import { HardDriveIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
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
import { Input } from "@/components/ui/input";
import { authenticatedFetch } from "@/lib/identity";

type Access = "read" | "write";

interface Member {
  principal: string;
  label: string;
  level: number;
  can_use_agent: boolean;
}

interface Grant {
  id: string;
  kind: "file" | "drive";
  file_id: string | null;
  name: string | null;
  access: Access;
  account: string | null;
  mine: boolean;
  state: "active" | "suspended" | "disconnected" | "inactive";
  new_audience: Member[];
}

interface Snapshot {
  session_id: string;
  viewer: {
    connected: boolean;
    email: string | null;
    generation: string | null;
    can_grant: boolean;
    can_manage: boolean;
  };
  audience?: Member[];
  grants: Grant[];
}

interface DriveFile {
  id: string;
  name: string;
  mime_type: string;
  can_edit: boolean;
}

interface Pending {
  kind: "file" | "drive" | "confirm";
  access: Access;
  file?: string;
  label: string;
  grantId?: string;
  audience: Member[];
}

const STATE_LABELS: Record<Grant["state"], string> = {
  active: "Active",
  suspended: "Paused",
  disconnected: "Account disconnected",
  inactive: "Owner left session",
};

function audienceMap(members: Member[]): Record<string, number> {
  return Object.fromEntries(members.map((m) => [m.principal, m.level]));
}

async function errorDetail(response: Response): Promise<{ message: string; audience?: Member[] }> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return { message: body.detail };
    if (body.detail && typeof body.detail === "object") {
      const detail = body.detail as { message?: string; audience?: Member[] };
      return { message: detail.message ?? "Request failed", audience: detail.audience };
    }
  } catch {
    // Fall through to the generic message.
  }
  return { message: "Google Drive access could not be updated. Retry shortly." };
}

function grantTitle(grant: Grant): string {
  if (grant.kind === "drive") return "Whole Drive";
  return grant.name ?? grant.file_id ?? "File";
}

/** Session-scoped Google Drive grants: who granted what, and how to add or revoke access. */
export function GoogleDriveSessionAccess({ sessionId }: { sessionId: string }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<DriveFile[] | null>(null);
  const [link, setLink] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const revision = useRef(0);
  const endpoint = `/v1/sessions/${encodeURIComponent(sessionId)}/google-drive`;

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      const observed = revision.current;
      try {
        const response = await authenticatedFetch(endpoint, { signal });
        if (response.status === 403 || response.status === 404) {
          if (observed === revision.current) setSnapshot(null);
          return;
        }
        if (!response.ok) throw new Error("unavailable");
        const body = (await response.json()) as Snapshot;
        if (observed === revision.current) setSnapshot(body);
      } catch (exc) {
        if ((exc as Error).name !== "AbortError" && observed === revision.current)
          setError("Google Drive access status is unavailable. Retry shortly.");
      }
    },
    [endpoint],
  );

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setInterval(() => void refresh(controller.signal), open ? 5000 : 30000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [refresh, open]);

  const mutate = async (path: string, init: RequestInit): Promise<boolean> => {
    revision.current += 1;
    setBusy(true);
    setError(null);
    try {
      const response = await authenticatedFetch(`${endpoint}${path}`, init);
      if (response.status === 409) {
        const detail = await errorDetail(response);
        if (detail.audience && pending) {
          setPending({ ...pending, audience: detail.audience });
          setError("Sharing changed. Review who can use this access before confirming.");
          return false;
        }
        throw new Error(detail.message);
      }
      if (!response.ok) throw new Error((await errorDetail(response)).message);
      setSnapshot((await response.json()) as Snapshot);
      return true;
    } catch (exc) {
      setError((exc as Error).message);
      return false;
    } finally {
      revision.current += 1;
      setBusy(false);
    }
  };

  const submit = async (request: Pending) => {
    if (!snapshot?.viewer.generation && request.kind !== "confirm") return;
    const json = { "Content-Type": "application/json" };
    const audience = audienceMap(request.audience);
    const ok =
      request.kind === "confirm"
        ? await mutate(`/grants/${encodeURIComponent(request.grantId ?? "")}/confirm`, {
            method: "POST",
            headers: json,
            body: JSON.stringify({ audience }),
          })
        : await mutate("/grants", {
            method: "POST",
            headers: json,
            body: JSON.stringify({
              kind: request.kind,
              file: request.file,
              access: request.access,
              generation: snapshot?.viewer.generation,
              audience,
            }),
          });
    if (ok) {
      setPending(null);
      setLink("");
    }
  };

  const request = (next: Omit<Pending, "audience">) => {
    const audience = snapshot?.audience ?? [];
    const pendingRequest = { ...next, audience };
    if (audience.length > 0) setPending(pendingRequest);
    else void submit(pendingRequest);
  };

  const search = async () => {
    setError(null);
    try {
      const response = await authenticatedFetch(
        `/v1/connections/google_drive/files?q=${encodeURIComponent(query)}`,
      );
      if (!response.ok) throw new Error((await errorDetail(response)).message);
      setResults(((await response.json()) as { files: DriveFile[] }).files);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  if (!snapshot && !error) return null;
  const grants = snapshot?.grants ?? [];
  const active = grants.filter((g) => g.state === "active").length;
  const paused = grants.some((g) => g.mine && g.state === "suspended");
  const viewer = snapshot?.viewer;
  const shared = (snapshot?.audience?.length ?? 0) > 0;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setPending(null);
      }}
    >
      <DialogTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="relative shrink-0 text-muted-foreground hover:text-foreground max-md:size-11"
          aria-label={`Google Drive access: ${
            paused ? "needs confirmation" : active > 0 ? `${active} active` : "off"
          }`}
          title="Google Drive access"
        >
          <HardDriveIcon className="size-4 max-md:size-5" />
          {(active > 0 || paused) && (
            <span
              aria-hidden
              className={`absolute top-1.5 right-1.5 size-1.5 rounded-full ${
                paused ? "bg-destructive" : "bg-primary"
              }`}
            />
          )}
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {pending ? "Share Google Drive access?" : "Google Drive access"}
          </DialogTitle>
          <DialogDescription>
            {pending
              ? "This session is shared. Everyone below gets this access, not just you."
              : "Files and Drives that this session's agents can use."}
          </DialogDescription>
        </DialogHeader>
        <div className="min-w-0 space-y-4 text-sm">
          {pending ? (
            <>
              <p className="font-medium">
                {pending.label} with {pending.access === "write" ? "view and edit" : "view-only"}{" "}
                access as {viewer?.email}
              </p>
              <ul className="space-y-1 rounded-lg border p-3" aria-label="Session participants">
                {pending.audience.map((member) => (
                  <li key={member.principal} className="flex justify-between gap-3">
                    <span className="min-w-0 break-words">{member.label}</span>
                    <span className="shrink-0 text-muted-foreground">
                      {member.can_use_agent ? "Can use the agent" : "Can read the session"}
                    </span>
                  </li>
                ))}
              </ul>
              <p>
                People who can use the agent can make it read
                {pending.access === "write" ? " and change" : ""}{" "}
                {pending.kind === "drive" ? "anything in your Drive" : "this file"} with your Google
                permissions. Anyone who can read the session can see what the agent reads. People
                added to the session later pause this access until you confirm again.
              </p>
            </>
          ) : (
            <>
              {grants.length === 0 ? (
                <p className="text-muted-foreground">No Google Drive access in this session.</p>
              ) : (
                <ul className="space-y-2">
                  {grants.map((grant) => (
                    <li key={grant.id} className="rounded-lg border p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="break-words font-medium">{grantTitle(grant)}</p>
                          <p className="break-words text-muted-foreground">
                            {grant.account ?? "Unknown account"} ·{" "}
                            {grant.access === "write" ? "View and edit" : "View only"}
                          </p>
                        </div>
                        <Badge variant={grant.state === "active" ? "secondary" : "destructive"}>
                          {STATE_LABELS[grant.state]}
                        </Badge>
                      </div>
                      {grant.state === "suspended" && grant.mine && (
                        <div className="mt-2 space-y-2">
                          <p>
                            Paused because people were added to this session:{" "}
                            {grant.new_audience.map((m) => m.label).join(", ")}.
                          </p>
                          <Button
                            size="sm"
                            disabled={busy}
                            onClick={() =>
                              setPending({
                                kind: "confirm",
                                access: grant.access,
                                label: grantTitle(grant),
                                grantId: grant.id,
                                audience: snapshot?.audience ?? [],
                              })
                            }
                          >
                            Review and resume
                          </Button>
                        </div>
                      )}
                      {(grant.mine || viewer?.can_manage) && (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="mt-2"
                          disabled={busy}
                          onClick={() =>
                            void mutate(`/grants/${encodeURIComponent(grant.id)}`, {
                              method: "DELETE",
                            })
                          }
                        >
                          Revoke
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {viewer?.can_grant && !viewer.connected && (
                <p>
                  Connect Google Drive in Settings → Sandbox Integrations to share your files with
                  this session.
                </p>
              )}
              {viewer?.can_grant && viewer.connected && (
                <div className="space-y-3 border-t pt-3">
                  <p className="font-medium">Add your files</p>
                  <form
                    className="flex gap-2"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void search();
                    }}
                  >
                    <Input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="Search your Drive"
                      aria-label="Search your Drive"
                    />
                    <Button type="submit" size="sm" variant="outline">
                      Search
                    </Button>
                  </form>
                  {results && (
                    <ul className="max-h-48 space-y-1 overflow-y-auto" aria-label="Drive files">
                      {results.length === 0 && (
                        <li className="text-muted-foreground">No matching files.</li>
                      )}
                      {results.map((file) => (
                        <li key={file.id} className="flex items-center justify-between gap-2">
                          <span className="min-w-0 truncate">{file.name}</span>
                          <span className="flex shrink-0 gap-1">
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busy}
                              aria-label={`Add ${file.name} view-only`}
                              onClick={() =>
                                request({
                                  kind: "file",
                                  access: "read",
                                  file: file.id,
                                  label: file.name,
                                })
                              }
                            >
                              View
                            </Button>
                            {file.can_edit && (
                              <Button
                                size="sm"
                                variant="outline"
                                disabled={busy}
                                aria-label={`Add ${file.name} with editing`}
                                onClick={() =>
                                  request({
                                    kind: "file",
                                    access: "write",
                                    file: file.id,
                                    label: file.name,
                                  })
                                }
                              >
                                Edit
                              </Button>
                            )}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <form
                    className="flex gap-2"
                    onSubmit={(event) => {
                      event.preventDefault();
                      if (link.trim())
                        request({ kind: "file", access: "read", file: link, label: "This file" });
                    }}
                  >
                    <Input
                      value={link}
                      onChange={(event) => setLink(event.target.value)}
                      placeholder="Or paste a Docs, Sheets or Drive link"
                      aria-label="Google Drive link"
                    />
                    <Button type="submit" size="sm" variant="outline" disabled={busy}>
                      Add
                    </Button>
                  </form>
                  <div className="space-y-2 border-t pt-3">
                    <p className="font-medium">Whole Drive</p>
                    <p className="text-muted-foreground">
                      Lets agents in this session search and open anything you can see in Google
                      Drive{shared ? ", on behalf of everyone in this shared session" : ""}.
                    </p>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() =>
                          request({ kind: "drive", access: "read", label: "Your whole Drive" })
                        }
                      >
                        Allow view-only
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() =>
                          request({ kind: "drive", access: "write", label: "Your whole Drive" })
                        }
                      >
                        Allow view and edit
                      </Button>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
          {error && (
            <p role="alert" className="text-destructive">
              {error}
            </p>
          )}
        </div>
        {pending && (
          <DialogFooter>
            <Button variant="outline" disabled={busy} onClick={() => setPending(null)}>
              Cancel
            </Button>
            <Button disabled={busy} onClick={() => void submit(pending)}>
              {pending.kind === "confirm" ? "Resume for everyone" : "Share with everyone"}
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** Warns people changing sharing that the session can use someone's Google Drive. */
export function GoogleDriveSharingNotice({ sessionId }: { sessionId: string }) {
  const [accounts, setAccounts] = useState<string[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await authenticatedFetch(
          `/v1/sessions/${encodeURIComponent(sessionId)}/google-drive`,
          { signal: controller.signal },
        );
        if (!response.ok) return;
        const body = (await response.json()) as Snapshot;
        setAccounts([...new Set(body.grants.map((g) => g.account ?? "a participant"))]);
      } catch {
        // The notice is advisory; sharing still works without it.
      }
    })();
    return () => controller.abort();
  }, [sessionId]);

  if (accounts.length === 0) return null;
  return (
    <div role="note" className="rounded-lg border border-destructive/40 px-3 py-2 text-sm">
      <p className="font-medium">This session can use Google Drive</p>
      <p className="text-muted-foreground">
        Access from {accounts.join(", ")} applies to everyone in this session. People you add can
        use it through the agent once its owner confirms them; until then it is paused.
      </p>
    </div>
  );
}
