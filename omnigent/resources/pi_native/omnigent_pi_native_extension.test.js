// Unit test for the pi-native bridge extension's interrupt / replay logic.
//
// Regression coverage for F18 (SDK_INTEGRATION_BUG_AUDIT.md): an interrupt that
// arrives while Pi is idle used to arm a 30s replay window that aborted the next
// legitimately-started turn. ExtensionContext.abort() is a silent no-op when the
// agent is idle (it does not throw), so the old requestInterrupt() armed the
// window unconditionally and replayPendingInterrupt() then killed the next turn.
//
// This test drives the real extension through its public surface: it registers
// the event handlers with a mock `pi`, and delivers interrupts through the real
// inbox poller (a temp inbox directory). No network is used (postEvent fails
// closed when config has no serverUrl).
//
// Run with: node omnigent/resources/pi_native/omnigent_pi_native_extension.test.js
//
// Manual reproduction of the original bug (for context):
//   1. Start a native Pi session linked to Omnigent and let it go idle.
//   2. Hit "stop"/interrupt while no turn is running (between turns).
//   3. Send a fresh user message within 30 seconds.
//   Before the fix: the fresh turn is aborted immediately at agent_start /
//   turn_start (and tool calls are blocked) before producing output. After the
//   fix: the idle interrupt is dropped and the fresh turn runs normally.

const fs = require("fs");
const os = require("os");
const path = require("path");

const EXT_PATH = path.resolve(__dirname, "omnigent_pi_native_extension.js");

const harnesses = [];

// Build a fresh extension instance with its own temp inbox directory. Each call
// produces independent closure state (activeResponseId, pendingInterruptUntil,
// latestContext, ...).
function makeHarness({ captureEvents = false, existingTools = [], tools = [], discoveryApi = false } = {}) {
  const inboxDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-native-inbox-"));
  const configPath = path.join(inboxDir, "config.json");
  // A serverUrl + sessionId make postEvent attempt a real fetch; with a mock
  // global fetch that lets a test capture the posted event bodies. Without
  // them postEvent fails closed (the interrupt tests rely on that).
  const config = captureEvents
    ? { inboxDir, bridgeDir: inboxDir, serverUrl: "http://mock", sessionId: "conv_test" }
    : { inboxDir, bridgeDir: inboxDir };
  config.tools = tools;
  fs.writeFileSync(configPath, JSON.stringify(config));
  process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

  // Capture posted event bodies (status edges etc.) instead of hitting network.
  const postedEvents = [];
  if (captureEvents) {
    global.fetch = async (url, opts) => {
      try {
        if (opts && typeof opts.body === "string" && String(url).endsWith("/events")) {
          postedEvents.push(JSON.parse(opts.body));
        }
      } catch (_err) {}
      return { ok: true, status: 204, json: async () => ({}) };
    };
  }

  const handlers = {};
  const registeredTools = {};
  let activeTools = existingTools.filter(t => t.defaultActive !== false).map(t => t.name);
  const pi = {
    on: (name, fn) => {
      handlers[name] = fn;
    },
    registerCommand: () => {},
    registerTool: (tool) => {
      registeredTools[tool.name] = tool;
      if (tool.exposure !== "codemode" && !activeTools.includes(tool.name)) activeTools.push(tool.name);
    },
    getAllTools: () => discoveryApi ? [...existingTools, ...Object.values(registeredTools)] : existingTools,
    sendUserMessage: () => {},
  };

  if (discoveryApi) {
    pi.getActiveTools = () => activeTools.slice();
    pi.setActiveTools = names => { activeTools = names.slice(); };
  }

  // Fresh module-function invocation -> fresh closures.
  delete require.cache[EXT_PATH];
  const mod = require(EXT_PATH);
  mod(pi);

  const h = { pi, handlers, inboxDir, postedEvents, registeredTools };
  harnesses.push(h);
  return h;
}

function statusEdges(postedEvents) {
  return postedEvents
    .filter((e) => e && e.type === "external_session_status" && e.data)
    .map((e) => ({ status: e.data.status, responseId: e.data.response_id }));
}

// ctx mock. `idle` may be true/false (exposes isIdle()) or undefined (no isIdle
// method at all, exercising the activeResponseId fallback path).
function makeCtx({ idle } = {}) {
  const ctx = {
    abortCount: 0,
    abort() {
      this.abortCount += 1;
    },
  };
  if (idle !== undefined) ctx.isIdle = () => idle;
  return ctx;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Drop an interrupt into the inbox and wait until the poller has consumed it
// (the poller unlinks the file after invoking handleInterrupt -> requestInterrupt).
async function deliverInterrupt(h) {
  const file = path.join(h.inboxDir, `int-${Date.now()}-${Math.random().toString(36).slice(2)}.json`);
  fs.writeFileSync(file, JSON.stringify({ type: "interrupt" }));
  const deadline = Date.now() + 3000;
  while (fs.existsSync(file)) {
    if (Date.now() > deadline) throw new Error("interrupt file was not consumed by poller");
    await sleep(20);
  }
  // The poller runs requestInterrupt synchronously before unlinking, so by the
  // time the file is gone the interrupt has been processed.
}

function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) process.exitCode = 1;
}

async function testIdleInterruptDoesNotPoisonNextTurn() {
  const h = makeHarness();
  const idleCtx = makeCtx({ idle: true });
  await h.handlers.session_start({}, idleCtx);

  await deliverInterrupt(h);

  assert(
    "idle interrupt (isIdle) does not abort the idle context",
    idleCtx.abortCount === 0,
    `abortCount=${idleCtx.abortCount}`,
  );

  // A fresh, legitimate turn starts within the (old) 30s window.
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );

  assert(
    "fresh turn after idle interrupt is NOT aborted",
    turnCtx.abortCount === 0,
    `abortCount=${turnCtx.abortCount}`,
  );
  assert(
    "fresh turn's tool_call is NOT blocked after idle interrupt",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testIdleInterruptFallbackNoIsIdle() {
  // No isIdle() on ctx -> requestInterrupt falls back to !activeResponseId.
  // Between turns activeResponseId is null, so this must behave as idle.
  const h = makeHarness();
  const idleCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, idleCtx);

  await deliverInterrupt(h);

  assert(
    "idle interrupt (activeResponseId fallback) does not arm the window",
    idleCtx.abortCount === 0,
    `abortCount=${idleCtx.abortCount}`,
  );

  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );

  assert(
    "fresh turn after fallback idle interrupt is NOT aborted",
    turnCtx.abortCount === 0,
    `abortCount=${turnCtx.abortCount}`,
  );
  assert(
    "fresh turn's tool_call is NOT blocked (fallback)",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testMidTurnInterruptStillAborts() {
  // Regression guard: a genuine mid-turn interrupt must still abort and replay.
  const h = makeHarness();
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);

  await deliverInterrupt(h);

  assert(
    "mid-turn interrupt aborts the live turn",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  // Replay must keep aborting within the window and block in-flight tool calls.
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );
  assert(
    "mid-turn interrupt blocks subsequent tool_call (replay)",
    !!toolResult && toolResult.block === true,
    JSON.stringify(toolResult),
  );
}

async function testAgentLoopInterruptFallbackNoIsIdleBeforeTurnStart() {
  // No isIdle(), and an interrupt lands after agent_start but before
  // turn_start. Older SDKs without isIdle() still need to treat this as part of
  // the live agent loop, not as an idle interrupt to drop.
  const h = makeHarness();
  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);

  await deliverInterrupt(h);

  assert(
    "agent-loop interrupt aborts before turn_start (active loop fallback)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );
  assert(
    "agent-loop interrupt before turn_start replays to block tool_call",
    !!toolResult && toolResult.block === true,
    JSON.stringify(toolResult),
  );
}

async function testMidTurnInterruptFallbackNoIsIdle() {
  // No isIdle() but an agent loop is active -> must still arm.
  const h = makeHarness();
  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);

  await deliverInterrupt(h);

  assert(
    "mid-turn interrupt aborts (activeResponseId fallback)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );
}

async function testAgentStartClearsStaleWindow() {
  // Belt-and-suspenders: even if a window is armed during a live turn, a brand
  // new agent loop must start clean and not abort its first tool call.
  const h = makeHarness();
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  await deliverInterrupt(h);
  assert(
    "window armed during live turn (precondition)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  // A new agent loop begins (e.g. the user's next message) within 30s.
  const nextCtx = makeCtx({ idle: false });
  await h.handlers.agent_start({}, nextCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, nextCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t2", toolName: "do_thing", input: {} },
    nextCtx,
  );

  assert(
    "new agent loop clears stale window (no abort)",
    nextCtx.abortCount === 0,
    `abortCount=${nextCtx.abortCount}`,
  );
  assert(
    "new agent loop's tool_call is NOT blocked",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testTaskPlanPublishesTodos() {
  const h = makeHarness({ captureEvents: true });
  await h.handlers.session_start({}, {});
  const tool = h.registeredTools.manage_todo_list;
  assert("registers manage_todo_list", !!tool);
  assert(
    "requires a task plan before multi-step work",
    tool.promptGuidelines.some((guideline) =>
      guideline.includes(
        "must call manage_todo_list before using other tools",
      ),
    ),
  );

  const result = await tool.execute("todo-1", {
    operation: "write",
    todoList: [
      {
        id: 1,
        title: "Trace rendering",
        description: "Tracing the shared event path",
        status: "in-progress",
      },
      {
        id: 2,
        title: "Run checks",
        description: "Run focused checks",
        status: "not-started",
      },
    ],
  });
  const event = h.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "manage_todo_list publishes the shared todo event",
    JSON.stringify(event && event.data.todos) ===
      JSON.stringify([
        {
          content: "Trace rendering",
          status: "in_progress",
          activeForm: "Tracing the shared event path",
        },
        {
          content: "Run checks",
          status: "pending",
          activeForm: "Run focused checks",
        },
      ]),
    JSON.stringify(event),
  );
  assert(
    "manage_todo_list persists its current list in tool details",
    result.details.todos.length === 2 &&
      result.details.todos[0].status === "in-progress",
    JSON.stringify(result),
  );

  const restored = makeHarness({ captureEvents: true });
  await restored.handlers.session_start(
    {},
    {
      sessionManager: {
        getBranch: () => [
          {
            type: "message",
            message: {
              role: "toolResult",
              toolName: "manage_todo_list",
              details: { todos: result.details.todos },
            },
          },
        ],
      },
    },
  );
  const restoredEvent = restored.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "session_start restores and republishes the latest task plan",
    restoredEvent && restoredEvent.data.todos.length === 2,
    JSON.stringify(restoredEvent),
  );
}

async function testExistingTaskToolIsMirroredWithoutConflict() {
  const h = makeHarness({
    captureEvents: true,
    existingTools: [{ name: "manage_todo_list" }],
  });
  await h.handlers.session_start({}, {});
  assert(
    "reuses an existing manage_todo_list tool",
    !h.registeredTools.manage_todo_list,
  );

  await h.handlers.tool_result(
    {
      toolCallId: "external-todo-1",
      toolName: "manage_todo_list",
      details: {
        todos: [
          {
            id: 1,
            title: "Use the shared tool",
            description: "Using the shared task tool",
            status: "in-progress",
          },
        ],
      },
    },
    {},
  );
  const event = h.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "mirrors an existing task tool into the shared todo event",
    event && event.data.todos[0].content === "Use the shared tool",
    JSON.stringify(event),
  );
}

// The web store only clears its local "streaming" flag when a turn's `idle`
// status edge carries the same response_id as the `running` edge that opened
// it. A fresh id per edge left the composer stuck queueing until a tab switch
// reset the store. Assert agent_start/agent_end share one id.
async function testRunningIdleShareResponseId() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: false });

  await h.handlers.agent_start({}, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);

  const edges = statusEdges(h.postedEvents);
  const running = edges.find((e) => e.status === "running");
  const idle = edges.find((e) => e.status === "idle");

  assert(
    "agent_start posts a running edge with a response_id",
    running !== undefined && typeof running.responseId === "string" && running.responseId.length > 0,
    JSON.stringify(running),
  );
  assert(
    "agent_end posts an idle edge with a response_id",
    idle !== undefined && typeof idle.responseId === "string" && idle.responseId.length > 0,
    JSON.stringify(idle),
  );
  assert(
    "running and idle edges share the same response_id",
    running && idle && running.responseId === idle.responseId,
    `running=${running && running.responseId} idle=${idle && idle.responseId}`,
  );

  // A second turn mints a fresh id, still paired across its own running/idle.
  await h.handlers.agent_start({}, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);
  const edges2 = statusEdges(h.postedEvents);
  const running2 = edges2.filter((e) => e.status === "running");
  const idle2 = edges2.filter((e) => e.status === "idle");
  assert(
    "second turn pairs its own running/idle id and differs from the first",
    running2.length === 2 &&
      idle2.length === 2 &&
      running2[1].responseId === idle2[1].responseId &&
      running2[1].responseId !== running2[0].responseId,
    `turn1=${running2[0].responseId} turn2=${running2[1].responseId}`,
  );
}

async function testSessionStartupDoesNotCompleteATurn() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: true });
  ctx.sessionManager = { getSessionId: () => "saved-native-session" };

  await h.handlers.session_start({}, ctx);

  assert(
    "starting an idle or resumed session does not report a completed turn",
    statusEdges(h.postedEvents).length === 0,
    JSON.stringify(statusEdges(h.postedEvents)),
  );
}

async function testSessionStartMarksInputReady() {
  const h = makeHarness();
  const marker = path.join(h.inboxDir, "input_ready");
  assert("input-ready marker is absent before session_start", !fs.existsSync(marker));

  await h.handlers.session_start({}, makeCtx({ idle: true }));

  assert(
    "session_start writes the input-ready marker once the inbox poller is armed",
    fs.existsSync(marker) && !!h.pi.__omnigentInboxPoller,
  );
}

async function testQueuedPromptDuringStartupStaysRunningUntilAgentEnd() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: true });
  ctx.sessionManager = { getSessionId: () => "queued-native-session" };
  const captureFetch = global.fetch;
  let releaseStartupPatch;
  const startupPatchPending = new Promise((resolve) => {
    releaseStartupPatch = resolve;
  });
  global.fetch = async (url, opts) => {
    if (opts.method === "PATCH") await startupPatchPending;
    return captureFetch(url, opts);
  };
  let agentStartPromise;
  h.pi.sendUserMessage = () => {
    ctx.isIdle = () => false;
    agentStartPromise = h.handlers.agent_start({}, ctx);
  };
  const payloadPath = path.join(h.inboxDir, "first-prompt.json");
  fs.writeFileSync(
    payloadPath,
    JSON.stringify({ id: "first-prompt", type: "user_message", content: "Run the task" }),
  );
  const startup = h.handlers.session_start({}, ctx);
  try {
    const deadline = Date.now() + 3000;
    while (fs.existsSync(payloadPath)) {
      if (Date.now() > deadline) throw new Error("startup prompt was not consumed by poller");
      await sleep(20);
    }
    if (!agentStartPromise) throw new Error("the queued prompt did not start an agent loop");
    await agentStartPromise;
    releaseStartupPatch();
    await startup;

    const beforeEnd = statusEdges(h.postedEvents);
    assert(
      "finishing startup preserves the queued prompt's running turn",
      beforeEnd.length === 1 && beforeEnd[0].status === "running",
      JSON.stringify(beforeEnd),
    );

    await h.handlers.agent_end({ messages: [] }, ctx);
    const afterEnd = statusEdges(h.postedEvents);
    assert(
      "only agent_end completes the queued prompt with the same response_id",
      afterEnd.length === 2 &&
        afterEnd[0].status === "running" &&
        afterEnd[1].status === "idle" &&
        afterEnd[0].responseId === afterEnd[1].responseId,
      JSON.stringify(afterEnd),
    );
  } finally {
    releaseStartupPatch();
    await startup;
    global.fetch = captureFetch;
  }
}


async function testEmptyLengthCompletionFailsVisiblyAndRetryResets() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: false });
  await h.handlers.agent_start({}, ctx);
  await h.handlers.message_end({ message: {
    role: "assistant", content: [], stopReason: "length", timestamp: 123,
  } }, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);
  const errors = h.postedEvents.filter(e => e.data?.item_type === "error");
  assert("empty truncation is visible", errors.length === 1);
  assert("error explains limits and recovery", errors[0].data.item_data.message.includes("compact"));
  assert("empty truncation ends failed", statusEdges(h.postedEvents).at(-1).status === "failed");
  await h.handlers.agent_start({}, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);
  assert("new turn clears previous failure", statusEdges(h.postedEvents).at(-1).status === "idle");
}

async function testLengthCompletionWithToolCallIsNotEmptyFailure() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: false });
  await h.handlers.agent_start({}, ctx);
  await h.handlers.message_end({ message: {
    role: "assistant", content: [{ type: "toolCall", id: "length-tool", name: "read", arguments: {} }],
    stopReason: "length", timestamp: 124,
  } }, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);
  assert("tool call completion is not an empty failure", !h.postedEvents.some(e => e.data?.item_type === "error"));
  assert("tool call ends idle", statusEdges(h.postedEvents).at(-1).status === "idle");
}

async function testConnectorCatalogUsesNativeDiscovery() {
  const tools = Array.from({ length: 170 }, (_, i) => ({
    name: `docs__lookup_${i}`, description: "Find documents",
    parameters: { type: "object", properties: {} }, exposure: "codemode",
  }));
  tools.push({ name: "sys_peer_send", description: "Send a peer message" });
  const h = makeHarness({ tools, discoveryApi: true, existingTools: [
    { name: "read" }, { name: "bash" }, { name: "codemode", defaultActive: false },
  ] });
  await h.handlers.session_start({}, makeCtx({ idle: true }));
  assert("native discovery is enabled", h.pi.getActiveTools().includes("codemode"));
  assert("ordinary tools remain direct", h.pi.getActiveTools().includes("sys_peer_send"));
  assert("file and shell tools remain direct", h.pi.getActiveTools().includes("read") && h.pi.getActiveTools().includes("bash"));
  assert("connector schemas are not declared", !h.pi.getActiveTools().some(name => name.startsWith("docs__")));
  assert("connector tools stay registered", tools.every(tool => h.registeredTools[tool.name]));
  assert("connector tools stay callable from codemode", h.registeredTools.docs__lookup_169.exposure === "codemode");
  assert("connector namespace is discoverable", h.registeredTools.docs__lookup_169.namespace.name === "docs");
  await h.handlers.session_start({}, makeCtx({ idle: true }));
  assert("resume does not re-expose the catalog", !h.pi.getActiveTools().some(name => name.startsWith("docs__")));
}

async function testLegacyPiRetainsConnectorTools() {
  const h = makeHarness({ tools: [{ name: "docs__lookup", exposure: "codemode" }] });
  await h.handlers.session_start({}, makeCtx({ idle: true }));
  assert("legacy Pi keeps direct tool registration", h.registeredTools.docs__lookup && h.registeredTools.docs__lookup.exposure === undefined);
}

async function testPeerNotificationsAreTypedDeduplicatedAndSteered() {
  for (const active of [false, true]) {
    const h = makeHarness({ captureEvents: true });
    const delivered = [];
    h.pi.sendMessage = (message, options) => delivered.push({ message, options });
    const ctx = makeCtx({ idle: !active });
    await h.handlers.session_start({}, ctx);
    if (active) {
      await h.handlers.agent_start({}, ctx);
      await h.handlers.message_start({ message: { role: "user", content: "Human turn" } }, ctx);
    }
    const id = active ? "a".repeat(64) : "b".repeat(64);
    const file = path.join(h.inboxDir, `peer_${id}.json`);
    const payload = JSON.stringify({ id, type: "peer_notification", message_ids: ["c".repeat(32)] });
    fs.writeFileSync(file, payload);
    await new Promise(resolve => setTimeout(resolve, 400));
    assert("peer message delivered exactly once", delivered.length === 1);
    assert("typed as peer content", delivered[0].message.customType === "omnigent-peer-notification");
    assert("idle starts a turn; active steers", delivered[0].options.deliverAs === (active ? "steer" : "followUp"));
    assert("triggers processing", delivered[0].options.triggerTurn === true);
    if (active) {
      const retry = { id, type: "peer_notification", message_ids: ["c".repeat(32)], attempt_id: "d".repeat(32) };
      fs.writeFileSync(file, JSON.stringify(retry));
      await new Promise(resolve => setTimeout(resolve, 400));
      assert("a fresh backend attempt does not duplicate a queued live message", delivered.length === 1);
    }
    assert("not acknowledged before actual context delivery", !fs.existsSync(path.join(h.inboxDir, "peer-receipts", id)));
    await h.handlers.message_start({ message: { role: "custom", ...delivered[0].message } }, ctx);
    assert("receipt persisted after context delivery", fs.existsSync(path.join(h.inboxDir, "peer-receipts", id)));
    fs.writeFileSync(file, payload);
    await new Promise(resolve => setTimeout(resolve, 400));
    assert("retry does not trigger another turn", delivered.length === 1);
    assert("never interrupts tools", ctx.abortCount === 0);
  }
}

async function testPeerSubmissionFailureRetries() {
  const h = makeHarness();
  const ctx = makeCtx({ idle: true });
  await h.handlers.session_start({}, ctx);
  let attempts = 0;
  h.pi.sendMessage = () => { if (++attempts === 1) throw new Error("temporary submission error"); };
  const id = "e".repeat(64);
  fs.writeFileSync(path.join(h.inboxDir, `peer_${id}.json`), JSON.stringify({ id, type: "peer_notification", message_ids: ["c".repeat(32)] }));
  await new Promise(resolve => setTimeout(resolve, 700));
  assert("failed submission retries on the next poll", attempts === 2);
}


async function testPeerActorBindingAndInstructions() {
  for (const active of [false, true]) {
    const h = makeHarness({ captureEvents: true });
    const ctx = makeCtx({ idle: !active });
    const configPath = path.join(h.inboxDir, "config.json");
    const config = JSON.parse(fs.readFileSync(configPath));
    config.systemPrompt = "Agent instructions";
    fs.writeFileSync(configPath, JSON.stringify(config));
    let bindings = 0;
    const fetch = global.fetch;
    global.fetch = async (url, options) => {
      if (String(url).endsWith("/peer-notifications/actor")) {
        bindings++;
        assert("actor callback only supplies batch ID", JSON.stringify(JSON.parse(options.body)) === JSON.stringify({ notification_id: "f".repeat(64) }));
      }
      return fetch(url, options);
    };
    await h.handlers.agent_start({}, ctx);
    if (active) await h.handlers.message_start({ message: { role: "user" } }, ctx);
    await h.handlers.message_start({ message: { role: "custom", customType: "omnigent-peer-notification", details: { notificationId: "f".repeat(64) } } }, ctx);
    assert("only automatic idle turns bind owner", bindings === (active ? 0 : 1));
    const transformed = await h.handlers.context_with_system({ messages: [{ role: "user", content: "Peer context" }] });
    assert("custom-started turns retain agent instructions", transformed.messages[0].content === "Agent instructions" && typeof transformed.messages[0].timestamp === "number");
    global.fetch = async (url, options) => String(url).endsWith("/peer-notifications/actor") ? { status: 403 } : fetch(url, options);
    await h.handlers.agent_start({}, ctx);
    await h.handlers.message_start({ message: { role: "custom", customType: "omnigent-peer-notification", details: { notificationId: "e".repeat(64) } } }, ctx);
    assert("failed identity aborts automatic turn", ctx.abortCount === 1);
    assert("failed identity prevents tool execution", (await h.handlers.tool_call({ toolName: "bash", input: {} }, ctx)).block === true);
    global.fetch = fetch;
  }
}

(async () => {
  try {
    await testPeerActorBindingAndInstructions();
    await testPeerSubmissionFailureRetries();
    await testPeerNotificationsAreTypedDeduplicatedAndSteered();
    await testConnectorCatalogUsesNativeDiscovery();
    await testLegacyPiRetainsConnectorTools();
    await testEmptyLengthCompletionFailsVisiblyAndRetryResets();
    await testLengthCompletionWithToolCallIsNotEmptyFailure();
    await testSessionStartupDoesNotCompleteATurn();
    await testSessionStartMarksInputReady();
    await testQueuedPromptDuringStartupStaysRunningUntilAgentEnd();
    await testRunningIdleShareResponseId();
    await testTaskPlanPublishesTodos();
    await testExistingTaskToolIsMirroredWithoutConflict();
    await testIdleInterruptDoesNotPoisonNextTurn();
    await testIdleInterruptFallbackNoIsIdle();
    await testMidTurnInterruptStillAborts();
    await testAgentLoopInterruptFallbackNoIsIdleBeforeTurnStart();
    await testMidTurnInterruptFallbackNoIsIdle();
    await testAgentStartClearsStaleWindow();
  } finally {
    for (const h of harnesses) {
      if (h.pi.__omnigentInboxPoller) clearInterval(h.pi.__omnigentInboxPoller);
      try {
        fs.rmSync(h.inboxDir, { recursive: true, force: true });
      } catch (_err) {}
    }
  }
})();
