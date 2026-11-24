// Catalog-only list backing the placeholder rows. Shared by the hook's
// catalog query and the merged queryFn (via fetchQuery), so a picker mount
// issues a single GET /v1/agents for both.
export const AGENT_CATALOG_QUERY_KEY = ["available-agents-catalog"] as const;
export const AVAILABLE_AGENTS_STALE_MS = 30_000;
