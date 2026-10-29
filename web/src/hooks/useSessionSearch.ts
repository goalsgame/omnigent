import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

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
    queryKey: ["session-search", q],
    enabled: enabled && q.length >= MIN_QUERY_LENGTH,
    staleTime: 30_000,
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
