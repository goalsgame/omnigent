import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RootGoogleCloudSessionAccess } from "./RootGoogleCloudSessionAccess";
import { authenticatedFetch } from "@/lib/identity";
import { getSessionSlim } from "@/lib/sessionsApi";
import { getSessionOwner } from "@/lib/permissionsApi";
import type { Session } from "@/lib/types";

const identity = vi.hoisted(() => ({ user: "owner@example.com" }));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
  getCurrentUserId: () => identity.user,
  resolveIdentity: async () => identity.user,
}));
vi.mock("@/lib/sessionsApi", () => ({ getSessionSlim: vi.fn() }));
vi.mock("@/lib/permissionsApi", () => ({ getSessionOwner: vi.fn() }));
afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  identity.user = "owner@example.com";
  vi.mocked(getSessionSlim).mockImplementation(
    async (id) =>
      ({
        id,
        parentSessionId: id === "grandchild" ? "child" : id === "child" ? "root" : null,
      }) as Session,
  );
  vi.mocked(getSessionOwner).mockResolvedValue(identity.user);
  vi.mocked(authenticatedFetch).mockImplementation(
    async (_url, options) =>
      ({
        ok: true,
        status: 200,
        json: async () => ({
          state: options?.method === "POST" ? "allowed" : "pending",
          generation: "g",
          email: "owner@example.com",
        }),
      }) as Response,
  );
});
function mount(id: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RootGoogleCloudSessionAccess sessionId={id} />
    </QueryClientProvider>,
  );
}
it.each(["root", "child", "grandchild"])(
  "reads and approves the root while viewing %s",
  async (id) => {
    mount(id);
    fireEvent.click(await screen.findByRole("button", { name: "Allow for this session" }));
    await screen.findByRole("button", { name: "Google Cloud: allowed" });
    expect(getSessionOwner).toHaveBeenCalledWith("root");
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    for (const [url] of vi.mocked(authenticatedFetch).mock.calls) {
      expect(url).toBe("/v1/connections/google_cloud/sessions/root/access");
    }
  },
);
it.each(["other@example.com", "local"])("does not expose approval to %s", async (viewer) => {
  identity.user = viewer;
  mount("child");
  await waitFor(() => expect(getSessionOwner).toHaveBeenCalledWith("root"));
  expect(authenticatedFetch).not.toHaveBeenCalled();
  expect(screen.queryByRole("button")).toBeNull();
});
