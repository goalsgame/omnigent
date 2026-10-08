import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GoogleCloudIntegrationControl } from "./GoogleCloudIntegrationControl";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
const fetchMock = vi.mocked(authenticatedFetch);
const response = (body: unknown, ok = true) => ({ ok, json: async () => body }) as Response;
beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState({}, "", "/settings");
});
afterEach(cleanup);

it("shows an unconnected account and a connect action", async () => {
  fetchMock.mockResolvedValue(response({ connected: false, email: null }));
  render(<GoogleCloudIntegrationControl />);
  expect(await screen.findByRole("button", { name: "Connect Google Cloud" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Disconnect" })).toBeNull();
});

it("disconnects and refreshes the account status", async () => {
  fetchMock
    .mockResolvedValueOnce(response({ connected: true, email: "user@example.com" }))
    .mockResolvedValueOnce(response({ disconnected: true }))
    .mockResolvedValueOnce(response({ connected: false, email: null }));
  render(<GoogleCloudIntegrationControl />);
  fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
  expect(await screen.findByRole("button", { name: "Connect Google Cloud" })).toBeTruthy();
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/v1/connections/google_cloud/disconnect", {
    method: "POST",
  });
});

it("keeps a failed status distinct from a disconnected account and permits retry", async () => {
  fetchMock
    .mockResolvedValueOnce(response({}, false))
    .mockResolvedValueOnce(response({ connected: true, email: "user@example.com" }));
  render(<GoogleCloudIntegrationControl />);
  fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
  expect(await screen.findByText(/Connected as user@example.com/)).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("displays OAuth errors and removes the callback marker", async () => {
  window.history.replaceState({}, "", "/settings?google_cloud=error#integrations");
  fetchMock.mockResolvedValue(response({ connected: false, email: null }));
  render(<GoogleCloudIntegrationControl />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not connect Google Cloud");
  expect(window.location.search).toBe("");
  expect(window.location.hash).toBe("#integrations");
});
