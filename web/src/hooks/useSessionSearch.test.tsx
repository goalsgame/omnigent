import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionSearch } from "./useSessionSearch";
import { authenticatedFetch } from "@/lib/identity";
import { markSessionsDeleting, unmarkSessionsDeleting } from "@/hooks/useConversations";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  authenticatedFetch: vi.fn(),
}));
const fetchMock = vi.mocked(authenticatedFetch);

const hit = (id: string) => ({
  session_id: id,
  title: id,
  created_at: 1,
  updated_at: 1,
  score: 1,
  title_match: true,
  matches: [],
});

function render(query: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return renderHook(() => useSessionSearch(query), { wrapper });
}

beforeEach(() => fetchMock.mockReset());
afterEach(() => unmarkSessionsDeleting());

it("searches and hides sessions that are being deleted", async () => {
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify({ data: [hit("keep"), hit("gone")] }), { status: 200 }),
  );
  markSessionsDeleting(["gone"]);
  const { result } = render("pool");
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data?.map((h) => h.session_id)).toEqual(["keep"]);
  expect(String(fetchMock.mock.calls[0][0])).toBe("/v1/sessions/search?q=pool&limit=8");
});

it("does not retry a client-side timeout, but retries other errors", async () => {
  fetchMock.mockResolvedValue(new Response(JSON.stringify({ data: [] }), { status: 200 }));
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  renderHook(() => useSessionSearch("pool"), { wrapper });
  const query = client.getQueryCache().find({ queryKey: ["session-search", "pool"] });
  const retry = (query!.options as { retry?: unknown }).retry as (
    failureCount: number,
    error: unknown,
  ) => boolean;

  expect(retry(0, new DOMException("timed out", "TimeoutError"))).toBe(false);
  expect(retry(0, new Error("500"))).toBe(true);
  expect(retry(3, new Error("500"))).toBe(false);
});

it("waits for at least two characters", () => {
  render("p");
  expect(fetchMock).not.toHaveBeenCalled();
});
