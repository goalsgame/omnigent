import { useActiveRootSessionId } from "@/hooks/useSession";
import { useSessionOwner } from "@/hooks/usePermissions";
import { useViewerId } from "@/hooks/useViewerId";
import { GoogleCloudSessionAccess } from "./GoogleCloudSessionAccess";

/** Consent belongs to the root sandbox, including when viewing a sub-agent. */
export function RootGoogleCloudSessionAccess({ sessionId }: { sessionId: string }) {
  const rootId = useActiveRootSessionId(sessionId);
  const viewerId = useViewerId();
  const { data: owner } = useSessionOwner(rootId);
  if (!rootId || !viewerId || viewerId === "local" || owner !== viewerId) return null;
  return <GoogleCloudSessionAccess key={rootId} sessionId={rootId} />;
}
