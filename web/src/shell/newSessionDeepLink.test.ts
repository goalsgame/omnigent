import { describe, expect, it } from "vitest";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import {
  NEW_SESSION_DEEP_LINK_PROMPT_MAX_LENGTH,
  findDeepLinkAgent,
  parseNewSessionDeepLink,
} from "./newSessionDeepLink";

function agent(overrides: Partial<AvailableAgent> = {}): AvailableAgent {
  return {
    id: "ag_pi",
    name: "pi",
    display_name: "Pi",
    description: null,
    harness: "pi-native",
    skills: [],
    ...overrides,
  };
}

describe("parseNewSessionDeepLink", () => {
  it("reads agent, model and prompt", () => {
    const params = new URLSearchParams();
    params.set("agent", "pi");
    params.set("model", "provider/model-1");
    params.set("prompt", "Fix the flaky test\n\nThen open a PR & link it.");
    expect(parseNewSessionDeepLink(params)).toEqual({
      agent: "pi",
      model: "provider/model-1",
      prompt: "Fix the flaky test\n\nThen open a PR & link it.",
    });
  });

  it("treats missing and blank values as absent", () => {
    expect(parseNewSessionDeepLink(new URLSearchParams("agent=&model=%20&prompt=%20"))).toEqual({
      agent: null,
      model: null,
      prompt: null,
    });
    expect(parseNewSessionDeepLink(new URLSearchParams("project=Alpha"))).toEqual({
      agent: null,
      model: null,
      prompt: null,
    });
  });

  it("caps the prompt length", () => {
    const params = new URLSearchParams();
    params.set("prompt", "x".repeat(NEW_SESSION_DEEP_LINK_PROMPT_MAX_LENGTH + 500));
    expect(parseNewSessionDeepLink(params).prompt).toHaveLength(
      NEW_SESSION_DEEP_LINK_PROMPT_MAX_LENGTH,
    );
  });
});

describe("findDeepLinkAgent", () => {
  it("matches a catalog agent by name", () => {
    const pi = agent();
    expect(findDeepLinkAgent([agent({ id: "ag_other", name: "other" }), pi], "pi")).toBe(pi);
  });

  it("ignores unknown names", () => {
    expect(findDeepLinkAgent([agent()], "missing")).toBeUndefined();
    expect(findDeepLinkAgent([agent()], null)).toBeUndefined();
  });
});
