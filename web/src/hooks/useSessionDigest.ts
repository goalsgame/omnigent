import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export type SessionDigestKind = "decision" | "discussion" | "open_question" | "tangent";

export interface SessionDigestEntry {
  kind: SessionDigestKind;
  title: string;
  text: string;
  rationale?: string;
  alternatives?: string[];
  item_ids: string[];
  /** Transcript deep-link ids (user item ids, assistant response ids). */
  message_ids?: string[];
  created_at: number;
}

export interface SessionDigest {
  session_id: string;
  status: "pending" | "ready" | "failed" | "empty";
  summary: string;
  topics: string[];
  entries: SessionDigestEntry[];
  model: string | null;
  updated_at: number | null;
  error: string | null;
}

export const SESSION_DIGEST_KEY = ["session-digest"] as const;

/**
 * The session's memory digest, or ``null`` when the server has session
 * memory disabled (the endpoint answers 404).
 */
export function useSessionDigest(sessionId: string | null) {
  return useQuery({
    queryKey: [...SESSION_DIGEST_KEY, sessionId],
    enabled: !!sessionId,
    staleTime: 60_000,
    refetchInterval: (query) => (query.state.data?.status === "ready" ? false : 120_000),
    queryFn: async (): Promise<SessionDigest | null> => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId!)}/digest`,
      );
      if (response.status === 404) return null;
      if (!response.ok) throw new Error(`Session digest failed (${response.status})`);
      return (await response.json()) as SessionDigest;
    },
  });
}
