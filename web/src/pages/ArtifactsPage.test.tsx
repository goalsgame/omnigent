import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Link } from "@/lib/routing";
import { ArtifactsPage } from "./ArtifactsPage";
import { resolveArtifactRendererUrl } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/host", () => ({ resolveArtifactRendererUrl: vi.fn() }));
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
function show(path = `/artifacts/${artifact.id}`, nextPath?: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      {nextPath && <Link to={nextPath}>Navigate away</Link>}
      <Routes>
        <Route path="/artifacts/:artifactId?" element={<ArtifactsPage />} />
      </Routes>
    </MemoryRouter>,
  );
}
afterEach(cleanup);
beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(resolveArtifactRendererUrl).mockImplementation((url) => url ?? "/v1/artifact-renderer");
});

it("previews untrusted HTML only inside an opaque sandbox without a bridge", async () => {
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json(artifact))
    .mockResolvedValueOnce(new Response("<script>window.x=1</script><h1>HTML</h1>"));
  show();
  const frame = await screen.findByTitle("Published HTML artifact");
  expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  expect(frame).toHaveAttribute("src", "/v1/artifact-renderer");
  expect(frame).not.toHaveAttribute("srcdoc");
  const post = vi.spyOn((frame as HTMLIFrameElement).contentWindow!, "postMessage");
  fireEvent.load(frame);
  expect(post).toHaveBeenCalledWith(
    { type: "omnigent.artifact.render", html: "<script>window.x=1</script><h1>HTML</h1>" },
    "*",
  );
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

it.each(["success", "error"])(
  "ignores delayed sharing %s after switching artifacts",
  async (outcome) => {
    let resolve!: (response: Response) => void;
    const pending = new Promise<Response>((r) => {
      resolve = r;
    });
    const next = { ...artifact, id: "b".repeat(32), title: "Next report", kind: "file" };
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(json(artifact))
      .mockResolvedValueOnce(new Response("<h1>A</h1>"))
      .mockReturnValueOnce(pending)
      .mockResolvedValueOnce(json(next));
    show(undefined, `/artifacts/${next.id}`);
    await screen.findByTitle("Published HTML artifact");
    fireEvent.click(screen.getByText("Sharing"));
    fireEvent.change(screen.getByLabelText("User identity or group name"), {
      target: { value: "reader@example.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Grant read access" }));
    fireEvent.click(screen.getByText("Navigate away"));
    await screen.findByRole("heading", { name: "Next report" });
    await act(async () => {
      resolve(outcome === "success" ? json({ ...artifact, title: "Stale report" }) : json({}, 403));
      await pending;
    });
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Next report" })).toBeInTheDocument(),
    );
    expect(screen.queryByText("Stale report")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Grant read access" })).not.toBeDisabled();
  },
);

it("deletes through router navigation while preserving an embedding basename", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json({ ...artifact, kind: "file" }))
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
    .mockResolvedValueOnce(json({ data: [] }));
  render(
    <MemoryRouter basename="/embedded" initialEntries={[`/embedded/artifacts/${artifact.id}`]}>
      <Routes>
        <Route path="/artifacts/:artifactId?" element={<ArtifactsPage />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByText("Report");
  fireEvent.click(screen.getByRole("button", { name: "Delete artifact" }));
  expect(await screen.findByText("No published artifacts yet.")).toBeInTheDocument();
  confirm.mockRestore();
});

it("uses the host-mapped renderer route instead of the server path", async () => {
  vi.mocked(resolveArtifactRendererUrl).mockReturnValue(
    "/ajax-api/2.0/omnigent/v1/artifact-renderer",
  );
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json({ ...artifact, renderer_url: "/v1/artifact-renderer" }))
    .mockResolvedValueOnce(new Response("<h1>Report</h1>"));
  show();
  expect(await screen.findByTitle("Published HTML artifact")).toHaveAttribute(
    "src",
    "/ajax-api/2.0/omnigent/v1/artifact-renderer",
  );
});

it("keeps downloads available when an embedded host has no renderer mapping", async () => {
  vi.mocked(resolveArtifactRendererUrl).mockReturnValue(null);
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json(artifact))
    .mockResolvedValueOnce(new Response("<h1>Report</h1>"));
  show();
  expect(await screen.findByRole("alert")).toHaveTextContent("configure an artifact renderer URL");
  expect(screen.queryByTitle("Published HTML artifact")).toBeNull();
  expect(screen.getByRole("button", { name: /Download/ })).toBeInTheDocument();
});
