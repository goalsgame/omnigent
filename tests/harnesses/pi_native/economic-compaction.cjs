const assert = require("node:assert/strict"),
  fs = require("node:fs"),
  path = require("node:path");
const [extension, root] = process.argv.slice(1);
const agentDir = path.join(root, "agent");
fs.mkdirSync(agentDir);
process.env.PI_CODING_AGENT_DIR = agentDir;
const runtime = path.join(root, "node_modules/@earendil-works/pi-coding-agent");
fs.mkdirSync(runtime, { recursive: true });
fs.writeFileSync(
  path.join(runtime, "package.json"),
  JSON.stringify({
    name: "@earendil-works/pi-coding-agent",
    type: "module",
    exports: "./index.js",
  }),
);
fs.writeFileSync(path.join(runtime, "cli.js"), "");
process.argv[1] = path.join(runtime, "cli.js");
fs.writeFileSync(
  path.join(runtime, "index.js"),
  `export async function compact(...args){globalThis.compactArgs=args;if(globalThis.failStream)await args[7](args[1],{},{});return {summary:'synthetic summary',firstKeptEntryId:'kept',tokensBefore:210000,details:{readFiles:[]},usage:{input:17,output:9,cost:{total:0.01}}}}`,
);
const config = path.join(root, "config.json");
fs.writeFileSync(
  config,
  JSON.stringify({ serverUrl: "http://example.test", sessionId: "test" }),
);
process.env.OMNIGENT_PI_NATIVE_CONFIG = config;
const handlers = {};
const pi = {
  on(n, h) {
    handlers[n] = h;
  },
  registerCommand() {},
  getThinkingLevel() {
    return "medium";
  },
};
require(extension)(pi);
const key = "managed/vendor/large-model",
  file = path.join(agentDir, "omnigent-compaction.json");
fs.writeFileSync(
  file,
  JSON.stringify({
    [key]: {
      triggerReserveTokens: 850000,
      summaryReserveTokens: 16384,
      targetTokens: 200000,
    },
  }),
);
const abort = new AbortController();
const event = {
  preparation: {
    settings: { enabled: true, reserveTokens: 850000, keepRecentTokens: 20000 },
    tokensBefore: 210000,
  },
  signal: abort.signal,
  customInstructions: "retain unfinished work",
};
const ctx = {
  model: {
    provider: "managed",
    id: "vendor/large-model",
    contextWindow: 1050000,
    maxTokens: 128000,
    headers: { "X-Test": "test" },
  },
  modelRegistry: {
    streamSimple(model, context, options) {
      globalThis.streamArgs = [model, context, options];
      return "synthetic-stream";
    },
  },
  sessionManager: {
    getSessionId() {
      return "native-session";
    },
  },
};
(async () => {
  const result = await handlers.session_before_compact(event, ctx);
  assert.equal(result.compaction.summary, "synthetic summary");
  assert.equal(result.compaction.usage.cost.total, 0.01);
  assert.equal(globalThis.compactArgs[0].settings.reserveTokens, 16384);
  assert.equal(globalThis.compactArgs[0].settings.keepRecentTokens, 20000);
  assert.equal(globalThis.compactArgs[1].contextWindow, 1050000);
  assert.equal(globalThis.compactArgs[5], abort.signal);
  assert.equal(globalThis.compactArgs[4], event.customInstructions);
  assert.equal(globalThis.compactArgs[11], "native-session");
  assert.equal(
    globalThis.compactArgs[7](ctx.model, {}, {}),
    "synthetic-stream",
  );
  assert.equal(globalThis.streamArgs[0], ctx.model);
  assert.equal(event.preparation.settings.reserveTokens, 850000);
  globalThis.compactArgs = null;
  assert.equal(
    await handlers.session_before_compact(
      { ...event, preparation: { settings: { reserveTokens: 900000 } } },
      ctx,
    ),
    undefined,
  );
  assert.equal(globalThis.compactArgs, null); // Respect a different project override.
  assert.equal(
    await handlers.session_before_compact(event, {
      ...ctx,
      model: { ...ctx.model, id: "other" },
    }),
    undefined,
  );
  for (const invalid of [
    null,
    [],
    false,
    { [key]: null },
    { [key]: false },
    { [key]: [] },
    { [key]: "bad" },
  ]) {
    fs.writeFileSync(file, JSON.stringify(invalid));
    assert.deepEqual(await handlers.session_before_compact(event, ctx), {
      cancel: true,
    });
  }
  fs.writeFileSync(file, "{bad");
  assert.deepEqual(await handlers.session_before_compact(event, ctx), {
    cancel: true,
  });
  fs.writeFileSync(
    file,
    JSON.stringify({
      [key]: { triggerReserveTokens: 850000, summaryReserveTokens: 16384 },
    }),
  );
  globalThis.failStream = true;
  ctx.modelRegistry.streamSimple = () => {
    throw new Error("secret provider error");
  };
  assert.deepEqual(await handlers.session_before_compact(event, ctx), {
    cancel: true,
  });
  fs.unlinkSync(file);
  assert.equal(await handlers.session_before_compact(event, ctx), undefined);
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
