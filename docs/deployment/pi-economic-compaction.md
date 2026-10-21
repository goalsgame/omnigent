# Cost-aware compaction for native Pi

Native Pi normally compacts near its model context capacity, not a provider's
long-context price boundary. Large cached prompts still cost money. Operators can
opt into earlier compaction for exact model IDs with a nonsecret environment map:

```sh
OMNIGENT_PI_COMPACTION_TARGETS='{"vendor/large-model":200000}'
```

Pass this variable to managed hosts; the host forwards it to runners. It applies
when the native Pi provider launch generates managed model/settings files. The
model ID is exact and can contain slashes. For each provider serving that ID,
Omnigent writes Pi's canonical provider/model `compaction.modelOverrides` entry.
Models absent from the map and models with already-smaller/tighter thresholds
are unchanged. Changing models uses the matching per-model policy on subsequent
checks. This is not a provider context-window override or a premium-model budget.

For a 1,050,000-token model and a target of 200,000, the generated threshold reserve
is 850,000. **The summarization reserve does not become 850,000.** The bridge uses
Pi's supported `session_before_compact` custom-summary hook and canonical `compact`
implementation with the original reserve (normally 16,384), original retention
settings, current model/authentication, custom instructions and abort signal.
Returned summary usage is retained for native accounting. Native Pi still owns
trigger timing, persistence, context rebuilding and continuation; no asynchronous
manual compact is injected into an active tool loop.

The original default history-summary output cap remains `floor(0.8 * 16384)`
(13,107 tokens), subject to the model output cap; split-turn summaries use Pi's
normal smaller budget. Existing global disable, retained-token settings and tighter
model overrides remain respected. Trusted project settings follow Pi's ordinary
precedence and may override the generated threshold; a different effective reserve
uses native semantics, not Omnigent's summary-budget reinterpretation.

Targets must be positive safe integers, and matching models must leave room for
retained context plus summary. Invalid operator configuration fails launch rather
than silently disabling the policy. An unreadable runtime policy, runtime import or
summarization failure cancels that compaction rather than falling back to a summary
using the inflated trigger reserve. Native Pi reports cancelled compaction through
its ordinary lifecycle; operators should inspect persistent failures.

The nonsecret `omnigent-compaction.json` sidecar belongs to the per-session managed
Pi directory. No global user settings or project files are rewritten. Removing the
map and relaunching resets the generated policy. Existing running processes do not
pick it up automatically: deploy a compatible runner and use the operator's approved
workspace-preserving restart/migration path. No forced compaction or active-session
interruption is implied by configuration alone.

A 200k target leaves headroom below a 272k price tier, but it is not a hard cap:
large user messages, system/tools and tool-result batches can overshoot. Compaction
has a summary cost and creates a new cache prefix; benchmark complete tasks rather
than assuming every saved context token is a net saving. Persist durable task state
and use bounded handoffs for independent implementation jobs. Provider-side billing
budgets remain the backstop; this feature is not a spending limit.

After an authorized rollout, verify in a disposable native Pi session that an
ordinary tool loop compacts near the selected target and retains unfinished work,
that summary output limits stay normal, and that provider usage shows smaller next
prompts with a subsequent cache warm-up. Test a model switch and an unconfigured
model. Local mock/runtime tests do not establish live savings or summary quality.
