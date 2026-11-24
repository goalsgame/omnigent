import type * as SandboxModelOptionsModule from "@/hooks/useSandboxModelOptions";

vi.mock("@/hooks/useSandboxModelOptions", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxModelOptionsModule>()),
  useSandboxModelOptions: vi.fn(() => ({
    data: {
      configured: false,
      status: "unconfigured",
      models: [],
      configuration_revision: null,
      provider_label: null,
      default_model: null,
    },
    isLoading: false,
    error: null,
  })),
}));
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useConversations as useTestConversations,
  useProjectConfig,
  useProjects,
} from "@/hooks/useConversations";

vi.mock("@/hooks/useSidebarData", () => ({ useLoadedConversations: () => useTestConversations() }));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({ skills: [], skillsStatus: "ready", refetch: vi.fn() }),
}));
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as HostWorktreesModule from "@/hooks/useHostWorktrees";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

import { authenticatedFetch } from "@/lib/identity";
import type { Host } from "@/hooks/useHosts";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { NewChatLandingScreen, resetLandingDraft } from "./NewChatDialog";
import { NEW_SESSION_DEEP_LINK_PROMPT_MAX_LENGTH } from "./newSessionDeepLink";
import { TooltipProvider } from "@/components/ui/tooltip";

const navigateMock = vi.fn();

const RECENT_KEY = "omnigent:recent-workspaces";
const RECENT_WORKSPACE = "/Users/corey/src/app";

let searchParams = new URLSearchParams();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigateMock,
  useSearchParams: () => [searchParams, vi.fn()],
}));

vi.mock("@/store/chatStore", () => ({
  setPendingInitialPrompt: vi.fn(),
}));

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
  getCurrentUserId: vi.fn(() => null),
  resolveIdentity: vi.fn(async () => null),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(() => ({ data: [] })),
  useInstallHarness: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useInstallingHarnesses: vi.fn(() => new Set<string>()),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: vi.fn(),
  // Opening the agent picker prefetches details per row; no-op here.
  prefetchAvailableAgentDetails: vi.fn(),
}));
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: () => ({ data: undefined }),
  useCreateHostDirectory: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/useHostWorktrees", async (importOriginal) => ({
  ...(await importOriginal<typeof HostWorktreesModule>()),
  useHostWorktrees: () => ({ data: [], isError: false }),
  hostWorktreesQueryOptions: (hostId: string, repoPath: string) => ({
    queryKey: ["host-worktrees", hostId, repoPath],
    queryFn: async () => [],
  }),
}));
vi.mock("@/hooks/useDirectorySessions", () => ({
  useDirectorySessions: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: () => new Map<string, boolean>(),
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useProjects: vi.fn(),
  useProjectConfig: vi.fn(),
  // The landing reads useConversations to decide hasNoSessions (the empty-state
  // import affordance); stub it so it doesn't fire an authenticatedFetch that
  // lands at mock.calls[0] and skews these create-POST call assertions.
  useConversations: () => ({ data: undefined }),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({}),
  // Stub so the setup dialog's hook doesn't fire its own /v1/harnesses fetch
  // (which would skew the create-flow call-count assertions here).
  useHarnessSetupSteps: () => ({}),
}));

const CLAUDE_AGENT_ID = "ag_claude";
const HARNESS_OPTIONS_KEY = "omnigent:last-mode-by-harness";
const LAST_AGENT_KEY = "omnigent:last-agent-id";

function host(overrides: Partial<Host> = {}): Host {
  return {
    host_id: "host_1",
    name: "corey-laptop",
    owner: "corey",
    status: "online",
    ...overrides,
  };
}

function agent(overrides: Partial<AvailableAgent> = {}): AvailableAgent {
  return {
    id: "ag_hello",
    name: "hello_world",
    display_name: "Hello World",
    description: null,
    harness: "claude-sdk",
    skills: [],
    ...overrides,
  };
}

function renderLanding(): void {
  const info: ServerInfo = {
    accounts_enabled: false,
    single_user: false,
    login_url: null,
    needs_setup: false,
    databricks_features: false,
    enabled_connections: [],
    managed_sandboxes_enabled: false,
    sandbox_provider: null,
    sharing_mode: "on",
    public_sharing_enabled: true,
    server_version: null,
    smart_routing_enabled: false,
    smart_routing_sources: { external: false, oss: false },
    features: { harness_install: false },
    harness_install_enabled: false,
    installable_harnesses: [],
    dictation_available: false,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <CapabilitiesProvider info={info}>
          <TooltipProvider>{children}</TooltipProvider>
        </CapabilitiesProvider>
      </QueryClientProvider>
    );
  }
  render(<NewChatLandingScreen />, { wrapper: Wrapper });
}

function deepLink(params: Record<string, string>): URLSearchParams {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) query.set(key, value);
  return query;
}

async function submitAndReadBody(): Promise<Record<string, unknown>> {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce({
    ok: true,
    json: () => Promise.resolve({ id: "conv_new" }),
  } as Response);
  fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
  await waitFor(() => expect(vi.mocked(authenticatedFetch)).toHaveBeenCalled());
  const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
  return JSON.parse(init.body as string) as Record<string, unknown>;
}

beforeEach(() => {
  navigateMock.mockReset();
  vi.mocked(authenticatedFetch).mockReset();
  resetLandingDraft();
  localStorage.clear();
  localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [RECENT_WORKSPACE] }));
  localStorage.setItem(LAST_AGENT_KEY, "ag_hello");
  localStorage.setItem(
    HARNESS_OPTIONS_KEY,
    JSON.stringify({ "claude-native": { model: "sonnet" } }),
  );
  searchParams = new URLSearchParams();
  vi.mocked(useHosts).mockReturnValue({ data: [host()] } as ReturnType<typeof useHosts>);
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [
      agent(),
      agent({
        id: CLAUDE_AGENT_ID,
        name: "claude-native-ui",
        display_name: "Claude Code",
        harness: "claude-native",
      }),
    ],
  } as ReturnType<typeof useAvailableAgents>);
  vi.mocked(useHostModelOptions).mockReturnValue({
    data: [
      { id: "opus", displayName: "Opus" },
      { id: "sonnet", displayName: "Sonnet" },
    ],
  } as ReturnType<typeof useHostModelOptions>);
  const projects: { id: string | null; name: string }[] = [];
  vi.mocked(useProjects).mockReturnValue({ data: projects, isLoading: false } as ReturnType<
    typeof useProjects
  >);
  vi.mocked(useProjectConfig).mockReturnValue({ data: undefined, isLoading: false } as ReturnType<
    typeof useProjectConfig
  >);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("NewChatLandingScreen new-session deep link", () => {
  it("prefills the prompt, agent and model without sending", async () => {
    searchParams = deepLink({
      agent: "claude-native-ui",
      model: "opus",
      prompt: "Fix the flaky test & open a PR.",
    });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select")).toHaveAccessibleName(
        /Claude Code/,
      ),
    );
    expect(screen.getByTestId("new-chat-landing-input")).toHaveValue(
      "Fix the flaky test & open a PR.",
    );
    expect(vi.mocked(authenticatedFetch)).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();

    const body = await submitAndReadBody();
    expect(body.agent_id).toBe(CLAUDE_AGENT_ID);
    expect(body.model_override).toBe("opus");
  });

  it("ignores an agent that is not in the catalog", async () => {
    searchParams = deepLink({ agent: "missing-agent", model: "opus", prompt: "Hello" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select")).toHaveAccessibleName(
        /Hello World/,
      ),
    );
    expect(screen.getByTestId("new-chat-landing-input")).toHaveValue("Hello");
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_hello");
    expect(body.model_override).toBeUndefined();
  });

  it("ignores a model the agent does not offer", async () => {
    searchParams = deepLink({ agent: "claude-native-ui", model: "retired-model", prompt: "Hi" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select")).toHaveAccessibleName(
        /Claude Code/,
      ),
    );
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe(CLAUDE_AGENT_ID);
    expect(body.model_override).toBe("sonnet");
  });

  it("caps a long prompt", async () => {
    searchParams = deepLink({ prompt: "y".repeat(NEW_SESSION_DEEP_LINK_PROMPT_MAX_LENGTH + 10) });
    renderLanding();

    await waitFor(() =>
      expect(
        (screen.getByTestId("new-chat-landing-input") as HTMLTextAreaElement).value,
      ).toHaveLength(NEW_SESSION_DEEP_LINK_PROMPT_MAX_LENGTH),
    );
    expect(vi.mocked(authenticatedFetch)).not.toHaveBeenCalled();
  });
});
