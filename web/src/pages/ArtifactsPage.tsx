import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { authenticatedFetch } from "@/lib/identity";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";

interface Grant {
  principal_type: "user" | "group";
  principal: string;
}
interface Artifact {
  id: string;
  title: string;
  filename: string;
  kind: "file" | "html";
  byte_size: number;
  expires_at: number;
  can_manage: boolean;
  grants?: Grant[];
}
// The iframe also enforces an opaque origin. Never add allow-same-origin or an API bridge.
const PREVIEW_POLICY =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";

export function ArtifactsPage() {
  const { artifactId } = useParams();
  return <ArtifactContents key={artifactId ?? "catalog"} artifactId={artifactId} />;
}

function ArtifactContents({ artifactId }: { artifactId?: string }) {
  const navigate = useNavigate();
  const alive = useRef(true);
  useLayoutEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [items, setItems] = useState<Artifact[]>([]);
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [principal, setPrincipal] = useState("");
  const [principalType, setPrincipalType] = useState<"user" | "group">("user");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    setArtifact(null);
    setPreview(null);
    async function load() {
      try {
        const response = await authenticatedFetch(
          artifactId ? `/v1/artifacts/${encodeURIComponent(artifactId)}` : "/v1/artifacts",
        );
        if (!response.ok)
          throw new Error(
            response.status === 404
              ? "Artifact unavailable, expired, or not shared with you."
              : "Could not load artifacts.",
          );
        const result = await response.json();
        if (!active) return;
        if (!artifactId) {
          setItems(result.data);
          return;
        }
        setArtifact(result);
        if (result.kind === "html") {
          const content = await authenticatedFetch(
            `/v1/artifacts/${encodeURIComponent(artifactId)}/preview`,
          );
          if (!content.ok) throw new Error("Could not load the HTML preview.");
          const html = await content.text();
          if (active)
            setPreview(
              `<meta http-equiv="Content-Security-Policy" content="${PREVIEW_POLICY}"><meta name="referrer" content="no-referrer">${html}`,
            );
        }
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : "Could not load artifacts.");
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, [artifactId]);

  async function download() {
    if (!artifact) return;
    try {
      const response = await authenticatedFetch(`/v1/artifacts/${artifact.id}/content`);
      if (!response.ok)
        throw new Error("Download unavailable. Access may have expired or been revoked.");
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifact.filename;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Download failed.");
    }
  }

  async function share(grants: Grant[]) {
    if (!artifact) return;
    setSaving(true);
    setError("");
    try {
      const response = await authenticatedFetch(`/v1/artifacts/${artifact.id}/permissions`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ grants }),
      });
      if (!response.ok)
        throw new Error("Could not update sharing. Use the exact user identity or group name.");
      const updated = await response.json();
      if (!alive.current) return;
      setArtifact(updated);
      setPrincipal("");
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : "Sharing failed.");
    } finally {
      if (alive.current) setSaving(false);
    }
  }

  async function remove() {
    if (!artifact || !window.confirm("Delete this published artifact and revoke its links?"))
      return;
    try {
      const response = await authenticatedFetch(`/v1/artifacts/${artifact.id}`, {
        method: "DELETE",
      });
      if (!alive.current) return;
      if (response.ok) navigate("/artifacts");
      else setError("Could not delete the artifact.");
    } catch {
      if (alive.current) setError("Could not delete the artifact.");
    }
  }

  return (
    <PageScroll
      maxWidthClassName="max-w-none"
      contentClassName="px-4"
      data-testid="artifacts-scroll"
    >
      <main className="flex min-h-0 flex-col gap-4">
        <header className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold">{artifact?.title ?? "Published artifacts"}</h1>
          {artifact && (
            <>
              <Button variant="outline" onClick={() => void download()}>
                Download
              </Button>
              <Button
                variant="outline"
                onClick={() =>
                  void navigator.clipboard
                    .writeText(window.location.href)
                    .catch(() => setError("Could not copy the link."))
                }
              >
                Copy link
              </Button>
              {artifact.can_manage && (
                <Button variant="outline" onClick={() => void remove()}>
                  Delete artifact
                </Button>
              )}
            </>
          )}
        </header>
        {loading && <p role="status">Loading artifact…</p>}
        {error && <p role="alert">{error}</p>}
        {!artifactId && !loading && !error && (
          <>
            <p className="text-sm text-muted-foreground">
              Your published files and self-contained HTML. Links require access; artifacts survive
              session deletion.
            </p>
            {items.length === 0 ? (
              <p>No published artifacts yet.</p>
            ) : (
              <ul>
                {items.map((item) => (
                  <li key={item.id}>
                    <Link className="underline" to={`/artifacts/${item.id}`}>
                      {item.title}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
        {artifact && (
          <p className="text-sm text-muted-foreground">
            {artifact.filename} · {artifact.byte_size.toLocaleString()} bytes · Expires{" "}
            {new Date(artifact.expires_at * 1000).toLocaleDateString()}
          </p>
        )}
        {artifact?.can_manage && (
          <details>
            <summary>Sharing</summary>
            <p className="my-2 text-sm">
              Only you and these people or groups can open this artifact. Copying a link grants no
              access.
            </p>
            <ul>
              {artifact.grants?.map((g, i) => (
                <li key={`${g.principal_type}:${g.principal}`}>
                  {g.principal_type}: {g.principal}{" "}
                  <Button
                    variant="ghost"
                    disabled={saving}
                    onClick={() => void share((artifact.grants ?? []).filter((_, n) => n !== i))}
                  >
                    Remove access
                  </Button>
                </li>
              ))}
            </ul>
            <form
              className="mt-2 flex flex-wrap gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (principal.trim())
                  void share([
                    ...(artifact.grants ?? []),
                    { principal_type: principalType, principal: principal.trim() },
                  ]);
              }}
            >
              <select
                aria-label="Principal type"
                value={principalType}
                onChange={(e) => setPrincipalType(e.target.value as "user" | "group")}
              >
                <option value="user">User</option>
                <option value="group">Group</option>
              </select>
              <input
                aria-label="User identity or group name"
                className="rounded border bg-background px-2"
                value={principal}
                onChange={(e) => setPrincipal(e.target.value)}
                required
              />
              <Button type="submit" disabled={saving}>
                Grant read access
              </Button>
            </form>
          </details>
        )}
        {preview !== null && (
          <iframe
            title="Published HTML artifact"
            sandbox="allow-scripts"
            referrerPolicy="no-referrer"
            srcDoc={preview}
            className="min-h-96 w-full flex-1 rounded border bg-white"
          />
        )}
      </main>
    </PageScroll>
  );
}
