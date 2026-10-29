import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { SessionMemoryPanel } from "./SessionMemoryPanel";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const ROOT = "a".repeat(32);

function respond(status: number, body?: unknown) {
  vi.mocked(authenticatedFetch).mockResolvedValue(
    new Response(body === undefined ? null : JSON.stringify(body), { status }),
  );
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionMemoryPanel rootSessionId={ROOT} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SessionMemoryPanel", () => {
  beforeEach(() => vi.mocked(authenticatedFetch).mockReset());

  it("groups digest entries and links each to the messages it cites", async () => {
    respond(200, {
      session_id: ROOT,
      status: "ready",
      summary: "Moved sandboxes to gVisor.",
      topics: ["isolation"],
      entries: [
        {
          kind: "decision",
          title: "Use gVisor",
          text: "Sandboxes run under gVisor.",
          rationale: "Kernel isolation",
          alternatives: ["Kata"],
          item_ids: ["i1"],
          message_ids: ["resp_1"],
          created_at: 1,
        },
        {
          kind: "open_question",
          title: "Spot nodes",
          text: "Can sandboxes run on Spot?",
          item_ids: [],
          message_ids: ["m1", "m2"],
          created_at: 2,
        },
      ],
      model: "flash",
      updated_at: 3,
      error: null,
    });
    renderPanel();

    expect(await screen.findByText("Moved sandboxes to gVisor.")).toBeInTheDocument();
    expect(screen.getByText("isolation")).toBeInTheDocument();
    const decisions = screen.getByRole("region", { name: "Decisions" });
    expect(decisions).toHaveTextContent("Use gVisor");
    expect(decisions).toHaveTextContent("Why: Kernel isolation");
    expect(decisions).toHaveTextContent("Rejected: Kata");
    expect(screen.getByRole("link", { name: "View message" })).toHaveAttribute(
      "href",
      `/c/${ROOT}?message=resp_1`,
    );
    const questions = screen.getByRole("region", { name: "Open questions" });
    expect(questions).toHaveTextContent("Spot nodes");
    expect(screen.getByRole("link", { name: "Message 2" })).toHaveAttribute(
      "href",
      `/c/${ROOT}?message=m2`,
    );
    expect(screen.queryByRole("region", { name: "Tangents" })).toBeNull();
    expect(authenticatedFetch).toHaveBeenCalledWith(`/v1/sessions/${ROOT}/digest`);
  });

  it("explains a pending digest", async () => {
    respond(200, {
      session_id: ROOT,
      status: "pending",
      summary: "",
      topics: [],
      entries: [],
      model: null,
      updated_at: null,
      error: null,
    });
    renderPanel();
    expect(await screen.findByText("No memory yet")).toBeInTheDocument();
  });

  it("reports a failed first digest", async () => {
    respond(200, {
      session_id: ROOT,
      status: "failed",
      summary: "",
      topics: [],
      entries: [],
      model: null,
      updated_at: 3,
      error: "Vertex AI returned HTTP 503",
    });
    renderPanel();
    expect(await screen.findByText("Memory could not be written")).toBeInTheDocument();
    expect(screen.getByText("Vertex AI returned HTTP 503")).toBeInTheDocument();
  });

  it("says when the server has session memory off", async () => {
    respond(404);
    renderPanel();
    expect(await screen.findByText("Session memory is off")).toBeInTheDocument();
  });
});
