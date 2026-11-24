import type { AvailableAgent } from "@/hooks/useAvailableAgents";

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

/** The catalog agent a deep link names, or undefined when it names none. */
export function findDeepLinkAgent(
  agents: readonly AvailableAgent[],
  name: string | null,
): AvailableAgent | undefined {
  if (name === null) return undefined;
  return agents.find((agent) => !agent.mine && agent.name === name);
}
