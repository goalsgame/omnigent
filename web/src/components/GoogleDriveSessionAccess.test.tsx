import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GoogleDriveSessionAccess, GoogleDriveSharingNotice } from "./GoogleDriveSessionAccess";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
const fetchMock = vi.mocked(authenticatedFetch);
const response = (body: unknown, code = 200) =>
  ({ ok: code >= 200 && code < 300, status: code, json: async () => body }) as Response;

const bob = {
  principal: "bob@example.com",
  label: "bob@example.com",
  level: 2,
  can_use_agent: true,
};
const reader = {
  principal: "reader@example.com",
  label: "reader@example.com",
  level: 1,
  can_use_agent: false,
};
const viewer = {
  connected: true,
  email: "me@example.com",
  generation: "gen-1",
  can_grant: true,
  can_manage: true,
};
const grant = (overrides = {}) => ({
  id: "grant-1",
  kind: "drive",
  file_id: null,
  name: null,
  access: "read",
  account: "me@example.com",
  mine: true,
  state: "active",
  new_audience: [],
  ...overrides,
});
const snapshot = (overrides = {}) => ({
  session_id: "s1",
  viewer,
  audience: [],
  grants: [],
  ...overrides,
});

beforeEach(() => vi.resetAllMocks());
afterEach(() => cleanup());

const open = async (label: RegExp | string) =>
  fireEvent.click(await screen.findByRole("button", { name: label }));

it("stays hidden when the session is not accessible", async () => {
  fetchMock.mockResolvedValue(response({}, 404));
  const { container } = render(<GoogleDriveSessionAccess sessionId="s1" />);
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  expect(container.innerHTML).toBe("");
});

it("grants directly in a private session", async () => {
  fetchMock
    .mockResolvedValueOnce(response(snapshot()))
    .mockResolvedValueOnce(response(snapshot({ grants: [grant()] })));
  render(<GoogleDriveSessionAccess sessionId="s1" />);
  await open("Google Drive access: off");
  fireEvent.click(screen.getByRole("button", { name: "Allow view-only" }));
  await screen.findByText("Active");
  const [path, init] = fetchMock.mock.calls[1];
  expect(path).toBe("/v1/sessions/s1/google-drive/grants");
  expect(JSON.parse(init?.body as string)).toEqual({
    kind: "drive",
    access: "read",
    generation: "gen-1",
    audience: {},
  });
});

it("requires confirming every participant in a shared session", async () => {
  fetchMock
    .mockResolvedValueOnce(response(snapshot({ audience: [bob] })))
    .mockResolvedValueOnce(
      response({ detail: { message: "changed", audience: [bob, reader] } }, 409),
    )
    .mockResolvedValueOnce(response(snapshot({ audience: [bob, reader], grants: [grant()] })));
  render(<GoogleDriveSessionAccess sessionId="s1" />);
  await open("Google Drive access: off");
  fireEvent.click(screen.getByRole("button", { name: "Allow view and edit" }));
  expect(screen.getByText("Share Google Drive access?")).toBeTruthy();
  expect(screen.getByText(/anything in your Drive/)).toBeTruthy();
  expect(screen.getByText("bob@example.com")).toBeTruthy();
  expect(fetchMock).toHaveBeenCalledTimes(1);

  fireEvent.click(screen.getByRole("button", { name: "Share with everyone" }));
  await screen.findByText("reader@example.com");
  expect(screen.getByRole("alert").textContent).toMatch(/Sharing changed/);
  fireEvent.click(screen.getByRole("button", { name: "Share with everyone" }));
  await screen.findByText("Active");
  const body = JSON.parse(fetchMock.mock.calls[2][1]?.body as string);
  expect(body).toMatchObject({
    kind: "drive",
    access: "write",
    audience: { "bob@example.com": 2, "reader@example.com": 1 },
  });
});

it("lets the owner review and resume a paused grant", async () => {
  fetchMock
    .mockResolvedValueOnce(
      response(
        snapshot({
          audience: [bob],
          grants: [grant({ state: "suspended", new_audience: [bob] })],
        }),
      ),
    )
    .mockResolvedValueOnce(response(snapshot({ audience: [bob], grants: [grant()] })));
  render(<GoogleDriveSessionAccess sessionId="s1" />);
  await open("Google Drive access: needs confirmation");
  expect(screen.getByText(/Paused because people were added/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Review and resume" }));
  fireEvent.click(screen.getByRole("button", { name: "Resume for everyone" }));
  await screen.findByText("Active");
  expect(fetchMock.mock.calls[1][0]).toBe("/v1/sessions/s1/google-drive/grants/grant-1/confirm");
  expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({
    audience: { "bob@example.com": 2 },
  });
});

it("searches the viewer's Drive and adds a file", async () => {
  fetchMock
    .mockResolvedValueOnce(response(snapshot()))
    .mockResolvedValueOnce(
      response({ files: [{ id: "f1234567890", name: "Plan", mime_type: "doc", can_edit: true }] }),
    )
    .mockResolvedValueOnce(
      response(
        snapshot({ grants: [grant({ kind: "file", file_id: "f1234567890", name: "Plan" })] }),
      ),
    );
  render(<GoogleDriveSessionAccess sessionId="s1" />);
  await open("Google Drive access: off");
  fireEvent.change(screen.getByLabelText("Search your Drive"), { target: { value: "plan" } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  fireEvent.click(await screen.findByRole("button", { name: "Add Plan with editing" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  expect(fetchMock.mock.calls[1][0]).toBe("/v1/connections/google_drive/files?q=plan");
  expect(JSON.parse(fetchMock.mock.calls[2][1]?.body as string)).toMatchObject({
    kind: "file",
    file: "f1234567890",
    access: "write",
  });
});

it("shows grants to readers without offering to grant or revoke", async () => {
  fetchMock.mockResolvedValue(
    response({
      session_id: "s1",
      viewer: { ...viewer, can_grant: false, can_manage: false },
      grants: [grant({ mine: false, account: "owner@example.com" })],
    }),
  );
  render(<GoogleDriveSessionAccess sessionId="s1" />);
  await open("Google Drive access: 1 active");
  expect(screen.getByText(/owner@example.com/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull();
  expect(screen.queryByLabelText("Search your Drive")).toBeNull();
});

it("warns in the share dialog when Drive access exists", async () => {
  fetchMock.mockResolvedValue(response(snapshot({ grants: [grant()] })));
  render(<GoogleDriveSharingNotice sessionId="s1" />);
  expect(await screen.findByText("This session can use Google Drive")).toBeTruthy();
  expect(screen.getByText(/me@example.com applies to everyone/)).toBeTruthy();
});
