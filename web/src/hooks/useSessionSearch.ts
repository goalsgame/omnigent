import { useQuery } from "@tanstack/react-query";
import { isAbortTimeout, isSessionArchiving, isSessionDeleting } from "@/hooks/useConversations";
import { authenticatedFetch } from "@/lib/identity";
import { SESSION_SEARCH_KEY } from "@/lib/sessionListCache";

export interface SessionSearchMatch {
  item_id: string;
  conversation_id: string;
  created_at: number;
  snippet: string;
}

export interface SessionSearchHit {
  session_id: string;
  title: string;
  created_at: number;
  updated_at: number;
  score: number;
  title_match: boolean;
  matches: SessionSearchMatch[];
}

const SEARCH_TIMEOUT_MS = 10_000;
const MIN_QUERY_LENGTH = 2;

/** Relevance-ranked message search across the sessions the viewer can read. */
export function useSessionSearch(query: string, { enabled = true }: { enabled?: boolean } = {}) {
  const q = query.trim();
  return useQuery({
    queryKey: [...SESSION_SEARCH_KEY, q],
    enabled: enabled && q.length >= MIN_QUERY_LENGTH,
    retry: (failureCount, error) => !isAbortTimeout(error) && failureCount < 3,
    select: (hits: SessionSearchHit[]) =>
      hits.filter(
        (hit) => !isSessionDeleting(hit.session_id) && !isSessionArchiving(hit.session_id),
      ),
    queryFn: async () => {
      const params = new URLSearchParams({ q, limit: "8" });
      const response = await authenticatedFetch(`/v1/sessions/search?${params.toString()}`, {
        signal: AbortSignal.timeout(SEARCH_TIMEOUT_MS),
      });
      if (!response.ok) throw new Error(`Session search failed (${response.status})`);
      const body = (await response.json()) as { data: SessionSearchHit[] };
      return body.data;
    },
  });
}
