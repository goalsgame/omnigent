import { useQuery } from "@tanstack/react-query";

import { AGENT_CATALOG_QUERY_KEY, AVAILABLE_AGENTS_STALE_MS } from "@/hooks/agentCatalogQuery";
import { fetchAgentCatalog, type AvailableAgent } from "@/hooks/useAvailableAgents";

export const NEW_SESSION_DEEP_LINK_PROMPT_MAX_LENGTH = 4000;

/**
 * Composer prefill carried by `/?agent=<name>&model=<id>&prompt=<text>`.
 * Each field is null when absent or empty. The link only prefills; the user
 * still sends the first message.
 */
export interface NewSessionDeepLink {
  agent: string | null;
  model: string | null;
  prompt: string | null;
}

function nonEmpty(value: string | null): string | null {
  const trimmed = value?.trim() ?? "";
  return trimmed === "" ? null : trimmed;
}

export function parseNewSessionDeepLink(params: URLSearchParams): NewSessionDeepLink {
  const prompt = params.get("prompt");
  return {
    agent: nonEmpty(params.get("agent")),
    model: nonEmpty(params.get("model")),
    prompt:
      prompt === null || prompt.trim() === ""
        ? null
        : prompt.slice(0, NEW_SESSION_DEEP_LINK_PROMPT_MAX_LENGTH),
  };
}

/** The `GET /v1/agents` catalog row a deep link names, or undefined when it names none. */
export function findDeepLinkAgent(
  catalog: readonly AvailableAgent[],
  name: string | null,
): AvailableAgent | undefined {
  if (name === null) return undefined;
  return catalog.find((agent) => agent.name === name);
}

/**
 * The catalog agent a deep link names: `undefined` while the catalog loads,
 * `null` when the link names no agent, the catalog has none by that name, or
 * the catalog fails to load.
 */
export function useDeepLinkCatalogAgent(name: string | null): AvailableAgent | null | undefined {
  const { data, isError } = useQuery({
    queryKey: AGENT_CATALOG_QUERY_KEY,
    queryFn: () => fetchAgentCatalog(),
    enabled: name !== null,
    staleTime: AVAILABLE_AGENTS_STALE_MS,
  });
  if (name === null || isError) return null;
  if (data === undefined) return undefined;
  return findDeepLinkAgent(data.agents, name) ?? null;
}
