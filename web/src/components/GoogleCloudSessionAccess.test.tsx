import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
afterEach(cleanup);

it("defaults to off and sends an explicit session-bound decision", async () => {
  fetchMock
    .mockResolvedValueOnce(response(status("off")))
    .mockResolvedValueOnce(response(status("allowed")));
  render(<GoogleCloudSessionAccess sessionId="session-one" />);
  fireEvent.click(await screen.findByRole("button", { name: "Google Cloud: off" }));
  expect(screen.getByText(/user@example.com/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Allow for this session" }));
  await screen.findByRole("button", { name: "Google Cloud: allowed" });
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
  fireEvent.click(await screen.findByRole("button", { name: "Google Cloud: allowed" }));
  fireEvent.click(screen.getByRole("button", { name: "Revoke access" }));
  await screen.findByRole("button", { name: "Google Cloud: off" });
  expect(fetchMock.mock.calls[1][1]?.body).toContain('"decision":"denied"');
});

it("does not treat a failed grant as approval", async () => {
  fetchMock
    .mockResolvedValueOnce(response(status("pending")))
    .mockResolvedValueOnce(response({}, 409));
  render(<GoogleCloudSessionAccess sessionId="session-one" />);
  fireEvent.click(await screen.findByRole("button", { name: "Allow for this session" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not update");
  expect(screen.queryByRole("button", { name: "Google Cloud: allowed" })).toBeNull();
});

it("hides controls for a nonowner or an unsupported shared host", async () => {
  fetchMock.mockResolvedValue(response({}, 403));
  render(<GoogleCloudSessionAccess sessionId="session-one" />);
  await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  expect(screen.queryByRole("button")).toBeNull();
});
