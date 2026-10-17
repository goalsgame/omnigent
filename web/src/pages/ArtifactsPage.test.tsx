import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ArtifactsPage } from "./ArtifactsPage";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
const artifact = {
  id: "a".repeat(32),
  title: "Report",
  filename: "report.html",
  kind: "html",
  byte_size: 12,
  expires_at: 1800000000,
  can_manage: true,
  grants: [],
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
function show(path = `/artifacts/${artifact.id}`) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/artifacts/:artifactId?" element={<ArtifactsPage />} />
      </Routes>
    </MemoryRouter>,
  );
}
afterEach(cleanup);
beforeEach(() => vi.mocked(authenticatedFetch).mockReset());

it("previews untrusted HTML only inside an opaque sandbox without a bridge", async () => {
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json(artifact))
    .mockResolvedValueOnce(new Response("<script>window.x=1</script><h1>HTML</h1>"));
  show();
  const frame = await screen.findByTitle("Published HTML artifact");
  expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  expect(frame.getAttribute("srcdoc")).toContain("connect-src 'none'");
  expect(frame.getAttribute("srcdoc")).toContain("base-uri 'none'");
  expect(frame.getAttribute("srcdoc")).not.toContain("allow-same-origin");
  expect(screen.queryByText("HTML")).toBeNull();
});

it("grants exact group read access and exposes no agent-side permission bridge", async () => {
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json(artifact))
    .mockResolvedValueOnce(new Response("<h1>x</h1>"))
    .mockResolvedValueOnce(
      json({ ...artifact, grants: [{ principal_type: "group", principal: "engineering" }] }),
    );
  show();
  await screen.findByText("Report");
  fireEvent.click(screen.getByText("Sharing"));
  fireEvent.change(screen.getByLabelText("Principal type"), { target: { value: "group" } });
  fireEvent.change(screen.getByLabelText("User identity or group name"), {
    target: { value: "engineering" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Grant read access" }));
  await waitFor(() =>
    expect(authenticatedFetch).toHaveBeenCalledWith(
      `/v1/artifacts/${artifact.id}/permissions`,
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ grants: [{ principal_type: "group", principal: "engineering" }] }),
      }),
    ),
  );
});

it("shows unavailable metadata without rendering any preview", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(json({}, 404));
  show();
  expect(await screen.findByRole("alert")).toHaveTextContent("not shared with you");
  expect(screen.queryByTitle("Published HTML artifact")).toBeNull();
});

it("lists owner publications and handles an empty catalog", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(json({ data: [] }));
  show("/artifacts");
  expect(await screen.findByText("No published artifacts yet.")).toBeInTheDocument();
});
