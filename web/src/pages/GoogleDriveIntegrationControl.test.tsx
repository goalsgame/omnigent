import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GoogleDriveIntegrationControl } from "./GoogleDriveIntegrationControl";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
const fetchMock = vi.mocked(authenticatedFetch);
const response = (body: unknown, ok = true) => ({ ok, json: async () => body }) as Response;
beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState({}, "", "/settings");
});
afterEach(cleanup);

it("explains that connecting shares nothing until a session grant", async () => {
  fetchMock.mockResolvedValue(response({ connected: true, email: "user@example.com" }));
  render(<GoogleDriveIntegrationControl />);
  expect(await screen.findByText(/Nothing is shared until you add files/)).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledWith("/v1/connections/google_drive/status");
});

it("disconnects through the Drive connection", async () => {
  fetchMock
    .mockResolvedValueOnce(response({ connected: true, email: "user@example.com" }))
    .mockResolvedValueOnce(response({ disconnected: true }))
    .mockResolvedValueOnce(response({ connected: false, email: null }));
  render(<GoogleDriveIntegrationControl />);
  fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
  expect(await screen.findByRole("button", { name: "Connect Google Drive" })).toBeTruthy();
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/v1/connections/google_drive/disconnect", {
    method: "POST",
  });
});

it("displays OAuth errors and removes the callback marker", async () => {
  window.history.replaceState({}, "", "/settings?google_drive=error");
  fetchMock.mockResolvedValue(response({ connected: false, email: null }));
  render(<GoogleDriveIntegrationControl />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not connect Google Drive");
  expect(window.location.search).toBe("");
});
