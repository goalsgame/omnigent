import { useSandboxModelOptions } from "@/hooks/useSandboxModelOptions";
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
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";

import { authenticatedFetch } from "@/lib/identity";
import type { Host } from "@/hooks/useHosts";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { fetchAgentCatalog, useAvailableAgents } from "@/hooks/useAvailableAgents";
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
  fetchAgentCatalog: vi.fn(),
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

const HELLO = agent();
const CLAUDE = agent({
  id: CLAUDE_AGENT_ID,
  name: "claude-native-ui",
  display_name: "Claude Code",
  harness: "claude-native",
});

const BASE_INFO: ServerInfo = {
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

function renderLanding(
  overrides: Partial<ServerInfo> = {},
  cachedCatalog?: AvailableAgent[],
): { rerender: () => void } {
  const info: ServerInfo = { ...BASE_INFO, ...overrides };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (cachedCatalog) client.setQueryData(["available-agents-catalog"], { agents: cachedCatalog });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <CapabilitiesProvider info={info}>
          <TooltipProvider>{children}</TooltipProvider>
        </CapabilitiesProvider>
      </QueryClientProvider>
    );
  }
  const view = render(<NewChatLandingScreen />, { wrapper: Wrapper });
  return { rerender: () => view.rerender(<NewChatLandingScreen />) };
}

function deepLink(params: Record<string, string>): URLSearchParams {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) query.set(key, value);
  return query;
}

/** The merged picker list: `merged` plus any pinned catalog row it lacks. */
function setAgents(
  merged: AvailableAgent[],
  { catalog = [HELLO, CLAUDE], placeholder = false } = {},
): void {
  vi.mocked(useAvailableAgents).mockImplementation((options) => {
    const pinned = (options?.pinnedAgentIds ?? [])
      .filter((id) => !merged.some((a) => a.id === id))
      .flatMap((id) => catalog.filter((a) => a.id === id));
    return {
      data: [...merged, ...pinned],
      isLoading: false,
      isError: false,
      isPlaceholderData: placeholder,
    } as unknown as ReturnType<typeof useAvailableAgents>;
  });
}

function setCatalog(agents: AvailableAgent[]): void {
  vi.mocked(fetchAgentCatalog).mockResolvedValue({ agents });
}

function selectAgent(agentId: string): void {
  fireEvent.pointerDown(agentSelect(), { button: 0 });
  if (screen.queryByTestId(`new-chat-landing-agent-${agentId}`) == null) {
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
  }
  fireEvent.click(screen.getByTestId(`new-chat-landing-agent-${agentId}`));
  const selectedRow = screen.queryByTestId(`new-chat-landing-agent-${agentId}`);
  if (selectedRow) fireEvent.keyDown(selectedRow, { key: "Escape" });
}

const SANDBOX_CATALOG = {
  configured: true,
  status: "ready" as const,
  models: [
    { id: "private/default", displayName: "Gateway default", isDefault: true },
    { id: "private/alternate", displayName: "Gateway alternate" },
  ],
  configuration_revision: "profile-revision-1",
  provider_label: "Gateway",
  default_model: "private/default",
};

const SANDBOX_INFO: Partial<ServerInfo> = {
  managed_sandboxes_enabled: true,
  sandbox_provider: "kubernetes",
  sandbox_provider_capabilities: { kubernetes: { inference_models: true } },
};

function fusionCombo(modelUid: string, effort: string) {
  return {
    modelUid,
    lead: "lead-1",
    leadLabel: "Lead 1",
    effort,
    fast: false,
    sidekick: "side-1",
    sidekickLabel: "Side 1",
    priority: false,
  };
}

function agentSelect(): HTMLElement {
  return screen.getByTestId("new-chat-landing-agent-select");
}

async function submitAndReadBody(): Promise<Record<string, unknown>> {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce({
    ok: true,
    json: () => Promise.resolve({ id: "conv_new" }),
  } as Response);
  await waitFor(() => expect(screen.getByTestId("new-chat-landing-submit")).toBeEnabled());
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
  setAgents([HELLO, CLAUDE]);
  setCatalog([HELLO, CLAUDE]);
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

    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Claude Code/));
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

    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Hello World/));
    expect(screen.getByTestId("new-chat-landing-input")).toHaveValue("Hello");
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_hello");
    expect(body.model_override).toBeUndefined();
  });

  it("ignores a model the agent does not offer", async () => {
    searchParams = deepLink({ agent: "claude-native-ui", model: "retired-model", prompt: "Hi" });
    renderLanding();

    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Claude Code/));
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

  it("blocks Send until the linked agent resolves", async () => {
    let resolveCatalog: (value: { agents: AvailableAgent[] }) => void = () => {};
    vi.mocked(fetchAgentCatalog).mockReturnValue(
      new Promise((resolve) => {
        resolveCatalog = resolve;
      }),
    );
    setAgents([HELLO, CLAUDE], { placeholder: true });
    searchParams = deepLink({ agent: "claude-native-ui", model: "opus", prompt: "Go" });
    const view = renderLanding();

    const submit = screen.getByTestId("new-chat-landing-submit");
    expect(submit).toBeDisabled();
    fireEvent.click(submit);
    await act(async () => resolveCatalog({ agents: [HELLO, CLAUDE] }));
    expect(screen.getByTestId("new-chat-landing-submit")).toBeDisabled();
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    expect(vi.mocked(authenticatedFetch)).not.toHaveBeenCalled();

    setAgents([HELLO, CLAUDE]);
    view.rerender();
    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Claude Code/));
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe(CLAUDE_AGENT_ID);
    expect(body.model_override).toBe("opus");
  });

  it("keeps the catalog agent when a newer session upload shares its name", async () => {
    const upload = agent({
      id: "ag_upload",
      name: "claude-native-ui",
      display_name: "Claude upload",
      harness: null,
      sessionId: "conv_upload",
      created_at: 2_000_000_000,
    });
    setAgents([HELLO, upload]);
    searchParams = deepLink({ agent: "claude-native-ui", prompt: "Go" });
    renderLanding();

    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Claude Code/));
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe(CLAUDE_AGENT_ID);
  });

  it("ignores a session upload whose name is not in the catalog", async () => {
    const upload = agent({
      id: "ag_upload",
      name: "uploaded-agent",
      display_name: "Uploaded",
      sessionId: "conv_upload",
    });
    setAgents([HELLO, CLAUDE, upload]);
    searchParams = deepLink({ agent: "uploaded-agent", prompt: "Go" });
    renderLanding();

    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Hello World/));
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_hello");
  });

  it("applies a new deep link while the landing stays mounted", async () => {
    searchParams = deepLink({ agent: "claude-native-ui", model: "opus", prompt: "First" });
    const view = renderLanding();
    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Claude Code/));

    searchParams = deepLink({ agent: "hello_world", prompt: "Second" });
    view.rerender();
    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Hello World/));
    expect(screen.getByTestId("new-chat-landing-input")).toHaveValue("Second");
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_hello");
  });

  it("drops the linked model after navigating to a link without one", async () => {
    searchParams = deepLink({ agent: "claude-native-ui", model: "opus", prompt: "First" });
    const view = renderLanding();
    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Claude Code/));

    searchParams = deepLink({ prompt: "Second" });
    view.rerender();
    await waitFor(() => expect(screen.getByTestId("new-chat-landing-input")).toHaveValue("Second"));
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe(CLAUDE_AGENT_ID);
    expect(body.model_override).toBe("sonnet");
  });

  it("keeps the user's edits across ordinary rerenders", async () => {
    searchParams = deepLink({ agent: "claude-native-ui", model: "opus", prompt: "First" });
    const view = renderLanding();
    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Claude Code/));

    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "Edited" },
    });
    searchParams = deepLink({ agent: "claude-native-ui", model: "opus", prompt: "First" });
    view.rerender();
    expect(screen.getByTestId("new-chat-landing-input")).toHaveValue("Edited");
  });

  it("seeds the linked model for Devin", async () => {
    const devin = agent({
      id: "ag_devin",
      name: "devin-native-ui",
      display_name: "Devin",
      harness: "devin-native",
    });
    setAgents([HELLO, devin], { catalog: [HELLO, devin] });
    setCatalog([HELLO, devin]);
    searchParams = deepLink({ agent: "devin-native-ui", model: "sonnet", prompt: "Go" });
    renderLanding();

    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Devin/));
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_devin");
    expect(body.model_override).toBe("sonnet");
  });

  it("seeds the linked model for a sandbox agent once its catalog resolves", async () => {
    const catalog = {
      configured: true,
      status: "ready" as const,
      models: [
        { id: "private/default", displayName: "Gateway default", isDefault: true },
        { id: "private/alternate", displayName: "Gateway alternate" },
      ],
      configuration_revision: "profile-revision-1",
      provider_label: "Gateway",
      default_model: "private/default",
    };
    vi.mocked(useSandboxModelOptions).mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    } as unknown as ReturnType<typeof useSandboxModelOptions>);
    vi.mocked(useHosts).mockReturnValue({ data: [] } as unknown as ReturnType<typeof useHosts>);
    const sdk = agent({ id: "ag_sdk", name: "sdk-agent", display_name: "SDK Agent" });
    setAgents([HELLO, sdk], { catalog: [HELLO, sdk] });
    setCatalog([HELLO, sdk]);
    searchParams = deepLink({ agent: "sdk-agent", model: "private/alternate", prompt: "Go" });
    const view = renderLanding({
      managed_sandboxes_enabled: true,
      sandbox_provider: "kubernetes",
      sandbox_provider_capabilities: { kubernetes: { inference_models: true } },
    });
    await waitFor(() => expect(vi.mocked(fetchAgentCatalog)).toHaveBeenCalled());
    expect(screen.getByTestId("new-chat-landing-submit")).toBeDisabled();

    vi.mocked(useSandboxModelOptions).mockReturnValue({
      data: catalog,
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSandboxModelOptions>);
    view.rerender();
    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/SDK Agent/));
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_sdk");
    expect(body.host_type).toBe("managed");
    expect(body.model_override).toBe("private/alternate");
  });

  it("keeps the linked agent when a cached project visit also configures one", async () => {
    localStorage.setItem("omnigent:last-harness-by-agent", JSON.stringify({ ag_hello: "auto" }));
    vi.mocked(useProjects).mockReturnValue({
      data: [{ id: "proj_alpha", name: "Alpha" }],
      isLoading: false,
    } as ReturnType<typeof useProjects>);
    vi.mocked(useProjectConfig).mockReturnValue({
      data: { host_id: "host_1", workspace: RECENT_WORKSPACE, agent_id: "ag_hello" },
      isLoading: false,
    } as ReturnType<typeof useProjectConfig>);
    searchParams = deepLink({ project: "Alpha", agent: "claude-native-ui", prompt: "Go" });
    renderLanding({}, [HELLO, CLAUDE]);

    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Claude Code/));
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe(CLAUDE_AGENT_ID);
    expect(body.harness_override).toBeUndefined();
  });

  it("prefills and sends a linked Devin Fusion combo", async () => {
    const devin = agent({
      id: "ag_devin",
      name: "devin-native-ui",
      display_name: "Devin",
      harness: "devin-native",
    });
    setAgents([HELLO, devin], { catalog: [HELLO, devin] });
    setCatalog([HELLO, devin]);
    const lowCombo = "fusion-lead-1-low-sidekick-side-1";
    const highCombo = "fusion-lead-1-high-sidekick-side-1";
    vi.mocked(useHostModelOptions).mockReturnValue({
      data: [
        { id: "swe-2", displayName: "SWE-2" },
        {
          id: "fusion",
          displayName: "Fusion",
          fusion: {
            combos: [fusionCombo(lowCombo, "low"), fusionCombo(highCombo, "high")],
            default: lowCombo,
          },
        },
      ],
    } as ReturnType<typeof useHostModelOptions>);
    searchParams = deepLink({ agent: "devin-native-ui", model: highCombo, prompt: "Go" });
    renderLanding();

    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Devin/));
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_devin");
    expect(body.model_override).toBe(highCombo);
  });

  it("drops the linked model when switching to another sandbox agent", async () => {
    vi.mocked(useSandboxModelOptions).mockReturnValue({
      data: SANDBOX_CATALOG,
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSandboxModelOptions>);
    vi.mocked(useHosts).mockReturnValue({ data: [] } as unknown as ReturnType<typeof useHosts>);
    const sdk = agent({ id: "ag_sdk", name: "sdk-agent", display_name: "SDK Agent" });
    const other = agent({ id: "ag_other", name: "other-agent", display_name: "Other Agent" });
    setAgents([HELLO, sdk, other], { catalog: [HELLO, sdk, other] });
    setCatalog([HELLO, sdk, other]);
    searchParams = deepLink({ agent: "sdk-agent", model: "private/alternate", prompt: "Go" });
    renderLanding(SANDBOX_INFO);
    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/SDK Agent/));
    await waitFor(() => expect(screen.getByTestId("new-chat-landing-submit")).toBeEnabled());

    selectAgent("ag_other");
    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Other Agent/));
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_other");
    expect(body.model_override).not.toBe("private/alternate");
  });

  it("keeps a model the user picks after the link seeded one", async () => {
    searchParams = deepLink({ agent: "claude-native-ui", model: "opus", prompt: "Go" });
    const view = renderLanding();
    await waitFor(() => expect(agentSelect()).toHaveAccessibleName(/Claude Code/));
    await waitFor(() => expect(screen.getByTestId("new-chat-landing-submit")).toBeEnabled());

    fireEvent.pointerDown(agentSelect(), { button: 0 });
    const edit = screen.getByTestId(`new-chat-landing-agent-config-${CLAUDE_AGENT_ID}`);
    fireEvent.pointerDown(edit, { button: 0 });
    fireEvent.click(edit);
    const menu = screen
      .getByTestId("new-chat-landing-agent-models")
      .closest<HTMLElement>('[role="menu"]')!;
    fireEvent.click(within(menu).getByRole("menuitemcheckbox", { name: "Sonnet" }));
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

    vi.mocked(useHostModelOptions).mockReturnValue({
      data: [
        { id: "opus", displayName: "Opus" },
        { id: "sonnet", displayName: "Sonnet" },
      ],
    } as ReturnType<typeof useHostModelOptions>);
    view.rerender();
    const body = await submitAndReadBody();
    expect(body.model_override).toBe("sonnet");
  });
});
