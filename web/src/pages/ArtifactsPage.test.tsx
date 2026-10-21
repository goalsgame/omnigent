import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { basenamedRouting, Link, RoutingProvider } from "@/lib/routing";
import { ArtifactsPage } from "./ArtifactsPage";
import { getOmnigentTransformShareLink, resolveArtifactRendererUrl } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/host", () => ({
  resolveArtifactRendererUrl: vi.fn(),
  getOmnigentTransformShareLink: vi.fn(),
}));
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
  sharing_revision: "c".repeat(64),
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
  vi.mocked(getOmnigentTransformShareLink).mockReturnValue(undefined);
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

it("links to a full-page authenticated view in a separate tab", async () => {
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json(artifact))
    .mockResolvedValueOnce(new Response("<h1>Report</h1>"));
  show();
  const link = await screen.findByRole("link", { name: "Open full page" });
  expect(link).toHaveAttribute("href", `/artifacts/${artifact.id}?view=full`);
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
});

it("preserves transformed host URL context and hash while selecting full-page mode", async () => {
  const transform = vi.fn(
    (path: string) => `https://host.example${path}?workspace=42&view=details#report`,
  );
  vi.mocked(getOmnigentTransformShareLink).mockReturnValue(transform);
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json(artifact))
    .mockResolvedValueOnce(new Response("<h1>Report</h1>"));
  render(
    <MemoryRouter initialEntries={[`/artifacts/${artifact.id}`]}>
      <Routes>
        <Route path="/artifacts/:artifactId?" element={<ArtifactsPage />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByRole("link", { name: "Open full page" })).toHaveAttribute(
    "href",
    `https://host.example/artifacts/${artifact.id}?workspace=42&view=full#report`,
  );
  expect(transform).toHaveBeenCalledWith(`/artifacts/${artifact.id}`);
});

it("transforms the rebased mount path for an embedded host", async () => {
  const transform = vi.fn((path: string) => `https://host.example${path}?workspace=42#section`);
  vi.mocked(getOmnigentTransformShareLink).mockReturnValue(transform);
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json(artifact))
    .mockResolvedValueOnce(new Response("<h1>Report</h1>"));
  render(
    <MemoryRouter initialEntries={[`/mount/artifacts/${artifact.id}`]}>
      <RoutingProvider value={basenamedRouting("/mount")}>
        <Routes>
          <Route path="/mount/artifacts/:artifactId" element={<ArtifactsPage />} />
        </Routes>
      </RoutingProvider>
    </MemoryRouter>,
  );
  expect(await screen.findByRole("link", { name: "Open full page" })).toHaveAttribute(
    "href",
    `https://host.example/mount/artifacts/${artifact.id}?workspace=42&view=full#section`,
  );
  expect(transform).toHaveBeenCalledWith(`/mount/artifacts/${artifact.id}`);
});

it("renders only the isolated viewport in full-page mode", async () => {
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json(artifact))
    .mockResolvedValueOnce(new Response("<h1>Report</h1>"));
  show(`/artifacts/${artifact.id}?view=full`);
  const frame = await screen.findByTitle("Published HTML artifact");
  expect(frame).toHaveAttribute("sandbox", "allow-scripts");
  expect(frame).toHaveAttribute("src", "/v1/artifact-renderer");
  expect(screen.getByRole("main", { name: "Full-page artifact" })).toHaveClass("fixed", "inset-0");
  expect(screen.queryByText("Sharing")).toBeNull();
  expect(screen.queryByRole("button", { name: "Download" })).toBeNull();
  expect(authenticatedFetch).toHaveBeenCalledWith(`/v1/artifacts/${artifact.id}/preview`);
});

it("rechecks authorization on a full-page open", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(json({}, 404));
  show(`/artifacts/${artifact.id}?view=full`);
  expect(await screen.findByRole("alert")).toHaveTextContent("not shared with you");
  expect(screen.queryByTitle("Published HTML artifact")).toBeNull();
  expect(screen.getByRole("link", { name: "Artifact details" })).toBeInTheDocument();
});

it("keeps the full-page link under the embedding basename", async () => {
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json(artifact))
    .mockResolvedValueOnce(new Response("<h1>Report</h1>"));
  render(
    <MemoryRouter basename="/embedded" initialEntries={[`/embedded/artifacts/${artifact.id}`]}>
      <Routes>
        <Route path="/artifacts/:artifactId?" element={<ArtifactsPage />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByRole("link", { name: "Open full page" })).toHaveAttribute(
    "href",
    `/embedded/artifacts/${artifact.id}?view=full`,
  );
});

it("does not offer full-page rendering for download-only files", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(json({ ...artifact, kind: "file" }));
  show();
  await screen.findByText("Report");
  expect(screen.queryByRole("link", { name: "Open full page" })).toBeNull();
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
        body: JSON.stringify({
          grants: [{ principal_type: "group", principal: "engineering" }],
          revision: artifact.sharing_revision,
        }),
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

it("copies the host-transformed share URL with the clipboard fallback", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(json({ ...artifact, kind: "file" }));
  vi.mocked(getOmnigentTransformShareLink).mockReturnValue(
    (path) => `https://public.example${path}?workspace=42`,
  );
  const clipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const exec = Object.getOwnPropertyDescriptor(document, "execCommand");
  let copied = "";
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  Object.defineProperty(document, "execCommand", {
    configurable: true,
    value: vi.fn(() => {
      copied = document.querySelector("textarea")?.value ?? "";
      return true;
    }),
  });
  try {
    show();
    await screen.findByText("Report");
    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    await waitFor(() =>
      expect(copied).toBe(`https://public.example/artifacts/${artifact.id}?workspace=42`),
    );
  } finally {
    if (clipboard) Object.defineProperty(navigator, "clipboard", clipboard);
    else Reflect.deleteProperty(navigator, "clipboard");
    if (exec) Object.defineProperty(document, "execCommand", exec);
    else Reflect.deleteProperty(document, "execCommand");
  }
});

it("rejects stale sharing without presenting the attempted grant as saved", async () => {
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json(artifact))
    .mockResolvedValueOnce(new Response("<h1>Report</h1>"))
    .mockResolvedValueOnce(json({}, 409));
  show();
  await screen.findByText("Report");
  fireEvent.click(screen.getByText("Sharing"));
  fireEvent.change(screen.getByLabelText("Principal type"), { target: { value: "group" } });
  fireEvent.change(screen.getByLabelText("User identity or group name"), {
    target: { value: "engineering" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Grant read access" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Sharing changed in another window");
  expect(screen.queryByText("group: engineering", { exact: false })).toBeNull();
  expect(authenticatedFetch).toHaveBeenLastCalledWith(
    `/v1/artifacts/${artifact.id}/permissions`,
    expect.objectContaining({
      body: JSON.stringify({
        grants: [{ principal_type: "group", principal: "engineering" }],
        revision: artifact.sharing_revision,
      }),
    }),
  );
});

it.each([200, 403])("downloads authenticated bytes or reports rejection (%s)", async (status) => {
  const response = new Response("exact bytes", { status });
  const blob = vi.spyOn(response, "blob");
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(json({ ...artifact, kind: "file" }))
    .mockResolvedValueOnce(response);
  const create = vi.fn((_blob: Blob) => "blob:artifact-download"),
    revoke = vi.fn();
  const createDescriptor = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  const revokeDescriptor = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: create });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revoke });
  let filename = "",
    href = "";
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    filename = this.download;
    href = this.href;
  });
  try {
    show();
    await screen.findByText("Report");
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Download" }));
    });
    expect(authenticatedFetch).toHaveBeenLastCalledWith(`/v1/artifacts/${artifact.id}/content`);
    if (status === 403) {
      expect(screen.getByRole("alert")).toHaveTextContent("Download unavailable");
      expect(create).not.toHaveBeenCalled();
      expect(blob).not.toHaveBeenCalled();
    } else {
      expect(click).toHaveBeenCalled();
      expect(await create.mock.calls[0][0].text()).toBe("exact bytes");
      expect(blob).toHaveBeenCalledOnce();
      expect(filename).toBe("report.html");
      expect(href).toBe("blob:artifact-download");
      expect(revoke).not.toHaveBeenCalled();
      act(() => vi.advanceTimersByTime(30_000));
      expect(revoke).toHaveBeenCalledWith("blob:artifact-download");
    }
  } finally {
    vi.useRealTimers();
    click.mockRestore();
    if (createDescriptor) Object.defineProperty(URL, "createObjectURL", createDescriptor);
    else Reflect.deleteProperty(URL, "createObjectURL");
    if (revokeDescriptor) Object.defineProperty(URL, "revokeObjectURL", revokeDescriptor);
    else Reflect.deleteProperty(URL, "revokeObjectURL");
  }
});
