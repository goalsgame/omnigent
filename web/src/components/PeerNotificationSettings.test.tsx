import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PeerNotificationSettings } from "./PeerNotificationSettings";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
const fetchMock = vi.mocked(authenticatedFetch);
const settings = { enabled: true, wake_on_message: false, can_manage: true };
const response = (body: unknown, status = 200) =>
  ({ ok: status === 200, status, json: async () => body }) as Response;
beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

it("requires an explicit owner decision to wake sleeping sessions", async () => {
  fetchMock
    .mockResolvedValueOnce(response(settings))
    .mockResolvedValueOnce(response({ ...settings, wake_on_message: true }));
  render(<PeerNotificationSettings sessionId="recipient" />);
  fireEvent.click(await screen.findByRole("button", { name: "Agent message notifications" }));
  const wake = screen.getByRole("checkbox", { name: /Wake sleeping/ });
  expect(wake).not.toBeChecked();
  fireEvent.click(wake);
  await waitFor(() => expect(wake).toBeChecked());
  expect(fetchMock).toHaveBeenLastCalledWith("/v1/sessions/recipient/peer-notifications", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: true, wake_on_message: true }),
  });
});

it("makes shared session settings read only", async () => {
  fetchMock.mockResolvedValue(response({ ...settings, can_manage: false }));
  render(<PeerNotificationSettings sessionId="shared" />);
  fireEvent.click(await screen.findByRole("button", { name: "Agent message notifications" }));
  for (const checkbox of screen.getAllByRole("checkbox")) expect(checkbox).toBeDisabled();
  expect(screen.getByText(/Only the session owner/)).toBeVisible();
});

it("reports a failed save without claiming wake was enabled", async () => {
  fetchMock.mockResolvedValueOnce(response(settings)).mockResolvedValueOnce(response({}, 403));
  render(<PeerNotificationSettings sessionId="recipient" />);
  fireEvent.click(await screen.findByRole("button", { name: "Agent message notifications" }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Wake sleeping/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not save");
  expect(screen.getByRole("checkbox", { name: /Wake sleeping/ })).not.toBeChecked();
});
