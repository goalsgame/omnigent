import { useSession } from "@/hooks/useSession";
import { useSessionOwner } from "@/hooks/usePermissions";
import { useViewerId } from "@/hooks/useViewerId";
import { GoogleCloudSessionAccess } from "./GoogleCloudSessionAccess";

/** Consent belongs to the root sandbox, including when viewing a sub-agent. */
export function RootGoogleCloudSessionAccess({ sessionId }: { sessionId: string }) {
  const { session } = useSession(sessionId);
  const rootId = session?.rootSessionId ?? null;
  const viewerId = useViewerId();
  const { data: owner } = useSessionOwner(rootId);
  const machine = owner?.startsWith("oidc-machine:") ?? false;
  if (!rootId || !viewerId || viewerId === "local" || (!machine && owner !== viewerId)) return null;
  return <GoogleCloudSessionAccess key={rootId} sessionId={rootId} machine={machine} />;
}
