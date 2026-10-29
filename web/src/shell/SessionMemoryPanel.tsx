// Memory tab content for the right-side rail: the session digest written by
// the server's session memory worker — a summary, then decisions, discussions,
// open questions and tangents, each linking back to the messages it cites.

import type { ComponentType } from "react";
import {
  BrainIcon,
  CircleHelpIcon,
  GitBranchIcon,
  MessagesSquareIcon,
  SignpostIcon,
} from "lucide-react";
import {
  type SessionDigestEntry,
  type SessionDigestKind,
  useSessionDigest,
} from "@/hooks/useSessionDigest";
import { Link } from "@/lib/routing";
import { MESSAGE_QUERY_PARAM } from "@/lib/messageDeepLink";

const SECTIONS: {
  kind: SessionDigestKind;
  label: string;
  icon: ComponentType<{ className?: string }>;
}[] = [
  { kind: "decision", label: "Decisions", icon: SignpostIcon },
  { kind: "discussion", label: "Discussions", icon: MessagesSquareIcon },
  { kind: "open_question", label: "Open questions", icon: CircleHelpIcon },
  { kind: "tangent", label: "Tangents", icon: GitBranchIcon },
];

function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
      <BrainIcon className="size-8 text-muted-foreground/50" />
      <p className="text-ui font-medium text-foreground">{title}</p>
      {hint && <p className="max-w-xs text-ui text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Entry({ entry, sessionId }: { entry: SessionDigestEntry; sessionId: string }) {
  const sources = entry.message_ids ?? [];
  return (
    <li className="flex flex-col gap-1 rounded-md border border-border p-2">
      {entry.title && <p className="text-ui font-medium text-foreground">{entry.title}</p>}
      {entry.text && <p className="text-ui text-muted-foreground">{entry.text}</p>}
      {entry.rationale && (
        <p className="text-ui text-muted-foreground">
          <span className="font-medium text-foreground">Why: </span>
          {entry.rationale}
        </p>
      )}
      {entry.alternatives && entry.alternatives.length > 0 && (
        <p className="text-ui text-muted-foreground">
          <span className="font-medium text-foreground">Rejected: </span>
          {entry.alternatives.join("; ")}
        </p>
      )}
      {sources.length > 0 && (
        <p className="flex flex-wrap gap-x-2 text-xs">
          {sources.map((messageId, index) => (
            <Link
              key={messageId}
              to={`/c/${sessionId}?${MESSAGE_QUERY_PARAM}=${encodeURIComponent(messageId)}`}
              className="text-primary hover:underline"
            >
              {sources.length === 1 ? "View message" : `Message ${index + 1}`}
            </Link>
          ))}
        </p>
      )}
    </li>
  );
}

/** Read-only session digest for the session tree rooted at ``rootSessionId``. */
export function SessionMemoryPanel({ rootSessionId }: { rootSessionId: string }) {
  const { data: digest, isLoading, isError } = useSessionDigest(rootSessionId, { poll: true });
  if (isLoading) return <EmptyState title="Loading memory…" />;
  if (isError || digest === undefined) return <EmptyState title="Memory unavailable" />;
  if (digest === null) {
    return <EmptyState title="Session memory is off" hint="This server has no digest model." />;
  }
  if (digest.status === "failed" && !digest.summary) {
    return (
      <EmptyState
        title="Memory could not be written"
        hint={digest.error ?? "The digest model failed and will be retried."}
      />
    );
  }
  if (digest.status === "pending" || (digest.status === "empty" && !digest.summary)) {
    return (
      <EmptyState
        title="No memory yet"
        hint="Decisions and discussions are recorded once the session has been quiet for a while."
      />
    );
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-3">
      {digest.summary && (
        <section className="flex flex-col gap-2">
          <p className="text-ui text-foreground">{digest.summary}</p>
          {digest.topics.length > 0 && (
            <ul className="flex flex-wrap gap-1" aria-label="Topics">
              {digest.topics.map((topic) => (
                <li
                  key={topic}
                  className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                >
                  {topic}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
      {SECTIONS.map(({ kind, label, icon: Icon }) => {
        const entries = digest.entries.filter((entry) => entry.kind === kind);
        if (entries.length === 0) return null;
        return (
          <section key={kind} className="flex flex-col gap-2" aria-label={label}>
            <h3 className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              <Icon className="size-3.5" />
              {label}
            </h3>
            <ul className="flex flex-col gap-2">
              {entries.map((entry) => (
                <Entry
                  key={`${entry.title}\u0000${entry.text}`}
                  entry={entry}
                  sessionId={digest.session_id}
                />
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
