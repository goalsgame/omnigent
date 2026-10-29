# Session memory

Session memory keeps a digest of what each session decided and discussed, so
people and agents can find it later by asking in plain language. Once a
top-level session has been quiet for a while, the server sends its messages to
a Gemini model on Vertex AI, which records:

- a short summary and topics;
- **decisions**, with why they were made and the options rejected;
- **discussions** of questions or designs, with the positions taken;
- **open questions** still unresolved;
- **tangents** that came up beside the session's main goal.

Each entry cites the messages it came from. Entries are embedded with a Vertex
AI embedding model and stored in PostgreSQL with pgvector. For a plain-language
query, session search matches them by meaning and by any word, alongside its
title and message matches, and returns them as `memories` on each hit. A query
with quoted phrases, `OR` or `-word` matches entries by that syntax only. The digest is shown in the
session's **Memory** tab and served by `GET /v1/sessions/{id}/digest`.

Digests follow the session's permissions: search returns only sessions the
caller can read, and an agent's `search_sessions` only sessions everyone in
its session can read. Deleting a session deletes its digest; archived sessions
are left out of search.

## How digests are written

A background task in the server looks for top-level sessions that changed and
have had no activity for `idle_minutes`. It leases one session at a time, so
several server replicas do not repeat work, and sends the model:

- the session title;
- the previous digest, if any, which the model updates rather than replaces;
- the messages and compaction summaries since the previous digest, oldest
  first, up to about 400,000 characters. A longer backlog is digested in
  several passes.

Sub-agent sessions, tool calls and tool output are not sent. Each message is
cut to 8,000 characters. A failed attempt is retried with exponential backoff,
up to a day apart; the error is shown until a digest succeeds. Entries are then
embedded, and new entries without an embedding are retried on the next pass.

## Configuration

Session memory needs PostgreSQL with the `vector` extension available. Add a
`session_memory` block to the server configuration:

```yaml
session_memory:
  digest:
    project: my-gcp-project
    location: eu
    model: gemini-3.8-flash
  embeddings:
    project: my-gcp-project
    location: eu
    model: gemini-embedding-2
    dimensions: 768
  idle_minutes: 15
  min_similarity: 0.6
```

| Key | Meaning |
| --- | --- |
| `digest` | Vertex AI project, location and Gemini model used with `generateContent`. |
| `embeddings` | Vertex AI project, location and model used with `embedContent`, and the embedding size (1–2000, default 768). |
| `idle_minutes` | Minutes without activity before a session is digested (default 15). |
| `min_similarity` | Lowest cosine similarity for a match by meaning (default 0.6). |

`location` may be a region such as `europe-west1` or the `eu`, `us` or
`global` endpoints; the model must be served there. The server authenticates
with Application Default Credentials and needs `aiplatform.endpoints.predict`
in the configured projects.

At startup the server runs `CREATE EXTENSION IF NOT EXISTS vector` and adds
an `embedding vector(<dimensions>)` column with an HNSW cosine index to
`session_memory_entries`. If the database user may not create extensions, a
database administrator must create `vector` in the Omnigent database first.
Replicas preparing the column at the same time are serialized with an advisory
lock. Changing the embedding `model` or `dimensions` replaces the column, and
every entry is embedded again.

Configuring session memory sends session transcripts to the configured
Vertex AI models. Without the block, no digests are written, search matches
only titles and messages, the digest route answers 404, and the Memory tab is
hidden.
