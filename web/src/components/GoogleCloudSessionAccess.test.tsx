import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GoogleCloudSessionAccess } from "./GoogleCloudSessionAccess";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
const fetchMock = vi.mocked(authenticatedFetch);
const status = (state: string) => ({
  state,
  email: "user@example.com",
  generation: "connection-one",
});
const response = (body: unknown, code = 200) =>
  ({ ok: code === 200, status: code, json: async () => body }) as Response;
beforeEach(() => vi.resetAllMocks());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("defaults to off and sends an explicit session-bound decision", async () => {
  fetchMock
    .mockResolvedValueOnce(response(status("off")))
    .mockResolvedValueOnce(response(status("allowed")));
  render(<GoogleCloudSessionAccess sessionId="session-one" />);
  fireEvent.click(await screen.findByRole("button", { name: "Google Cloud access: off" }));
  expect(screen.getByText(/user@example.com/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Allow for this session" }));
  await screen.findByRole("button", { name: "Google Cloud access: allowed" });
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/v1/connections/google_cloud/sessions/session-one/access",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: "allowed", generation: "connection-one" }),
    },
  );
});

it("opens the approval prompt when a command requested credentials", async () => {
  fetchMock.mockResolvedValue(response(status("pending")));
  render(<GoogleCloudSessionAccess sessionId="session-one" />);
  expect(await screen.findByRole("button", { name: "Allow for this session" })).toBeTruthy();
  expect(screen.getByText(/command was blocked/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Deny" })).toBeTruthy();
});

it("persists denial and lets the owner revoke an existing grant", async () => {
  fetchMock
    .mockResolvedValueOnce(response(status("allowed")))
    .mockResolvedValueOnce(response(status("denied")));
  render(<GoogleCloudSessionAccess sessionId="session-one" />);
  fireEvent.click(await screen.findByRole("button", { name: "Google Cloud access: allowed" }));
  fireEvent.click(screen.getByRole("button", { name: "Revoke access" }));
  await screen.findByRole("button", { name: "Google Cloud access: off" });
  expect(fetchMock.mock.calls[1][1]?.body).toContain('"decision":"denied"');
});

it("does not treat a failed grant as approval", async () => {
  fetchMock
    .mockResolvedValueOnce(response(status("pending")))
    .mockResolvedValueOnce(response({}, 409));
  render(<GoogleCloudSessionAccess sessionId="session-one" />);
  fireEvent.click(await screen.findByRole("button", { name: "Allow for this session" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not update");
  expect(screen.queryByRole("button", { name: "Google Cloud access: allowed" })).toBeNull();
});

it("hides controls for a nonowner or an unsupported shared host", async () => {
  fetchMock.mockResolvedValue(response({}, 403));
  render(<GoogleCloudSessionAccess sessionId="session-one" />);
  await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  expect(screen.queryByRole("button")).toBeNull();
});

it.each(["allowed", "denied"])("ignores a stale polling failure after %s", async (decision) => {
  vi.useFakeTimers();
  let rejectPoll!: (error: Error) => void;
  const pendingPoll = new Promise<Response>((_resolve, reject) => {
    rejectPoll = reject;
  });
  fetchMock
    .mockResolvedValueOnce(response(status("pending")))
    .mockReturnValueOnce(pendingPoll)
    .mockResolvedValueOnce(response(status(decision)));
  await act(async () => {
    render(<GoogleCloudSessionAccess sessionId="session-one" />);
  });
  act(() => vi.advanceTimersByTime(3000));
  await act(async () => {
    fireEvent.click(
      screen.getByRole("button", {
        name: decision === "allowed" ? "Allow for this session" : "Deny",
      }),
    );
  });
  expect(
    screen.getByRole("button", {
      name: decision === "allowed" ? "Google Cloud access: allowed" : "Google Cloud access: off",
    }),
  ).toBeTruthy();
  await act(async () => {
    rejectPoll(new Error("offline"));
  });
  expect(screen.queryByRole("alert")).toBeNull();
});

it.each([403, 409])("clears a polling error when access becomes unavailable (%s)", async (code) => {
  vi.useFakeTimers();
  fetchMock
    .mockResolvedValueOnce(response(status("pending")))
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValue(response({}, code));
  await act(async () => {
    render(<GoogleCloudSessionAccess sessionId="session-one" />);
  });
  expect(screen.getByRole("button", { name: "Allow for this session" })).toBeTruthy();
  await act(async () => {
    vi.advanceTimersByTime(3000);
  });
  expect(screen.getByRole("alert")).toHaveTextContent("status is unavailable");
  await act(async () => {
    vi.advanceTimersByTime(3000);
  });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
  await act(async () => {
    vi.advanceTimersByTime(3000);
  });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("button")).toBeNull();
});

it("uses a portal dialog and leaves no status text in the chat layout", async () => {
  fetchMock.mockResolvedValue(response(status("pending")));
  const { container } = render(<GoogleCloudSessionAccess sessionId="session-one" />);
  const dialog = await screen.findByRole("dialog", { name: "Allow Google Cloud access?" });
  expect(container.contains(dialog)).toBe(false);
  expect(document.querySelector('[data-slot="dialog-overlay"]')).toBeTruthy();
  expect(container.textContent).toBe("");
});

it("lets the owner dismiss a request without polling immediately reopening it", async () => {
  vi.useFakeTimers();
  fetchMock.mockResolvedValue(response(status("pending")));
  await act(async () => {
    render(<GoogleCloudSessionAccess sessionId="session-one" />);
  });
  expect(screen.getByRole("dialog")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => {
    vi.advanceTimersByTime(6000);
  });
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Google Cloud access: requested" }));
  expect(screen.getByRole("dialog")).toBeTruthy();
});
