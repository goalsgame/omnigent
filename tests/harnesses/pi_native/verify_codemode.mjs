/** Offline smoke of native Pi discovery and Omnigent's nested authorization path. */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const packageRoot =
  process.argv[2] ||
  path.join(
    execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim(),
    "@earendil-works/pi-coding-agent",
  );
const extensionPath =
  process.argv[3] ||
  "/build/omnigent/resources/pi_native/omnigent_pi_native_extension.js";
const expectedVersion =
  process.argv[4] || fs.readFileSync("/opt/omnigent/pi-version", "utf8").trim();
assert.equal(
  JSON.parse(fs.readFileSync(path.join(packageRoot, "package.json"), "utf8"))
    .version,
  expectedVersion,
);
const [major, minor] = process.versions.node.split(".").map(Number);
assert.ok(
  major > 22 || (major === 22 && minor >= 19),
  "Pi requires Node >=22.19",
);
const {
  createAgentSession,
  createCodemodeExtension,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  SettingsManager,
} = await import(pathToFileURL(path.join(packageRoot, "dist/index.js")));
const require = createRequire(import.meta.url);
const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "pi-discovery-"));
const configPath = path.join(workspace, "bridge.json");
const tools = Array.from({ length: 170 }, (_, i) => ({
  name: `docs__lookup_${i}`,
  description: "Search documentation. ".repeat(150),
  parameters: {
    type: "object",
    properties: { query: { type: "string" } },
    required: ["query"],
    additionalProperties: false,
  },
  exposure: "codemode",
}));
let relayToken = "smoke-relay-first";
const bridgeConfig = {
  serverUrl: "http://server.invalid",
  sessionId: "smoke-session",
  authHeaders: { authorization: "Bearer expired-direct-token" },
  relayUrl: "http://relay.test",
  relayToken,
  tools,
};
fs.writeFileSync(configPath, JSON.stringify(bridgeConfig));
process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;
const originalFetch = globalThis.fetch;
const calls = [];
const policyCalls = [];
const events = [];
const seenRelayTokens = new Set();
const providerRequests = [];
let mode = "allow";
let approvalRequests = 0;
const jsonResponse = (body) =>
  new Response(JSON.stringify(body), {
    headers: { "content-type": "application/json" },
  });
globalThis.fetch = async (url, options = {}) => {
  if (String(url).startsWith("http://provider.test/")) {
    const body = JSON.parse(options.body);
    providerRequests.push(body);
    assert.ok(
      !body.tools.some((tool) => tool.function?.name?.startsWith("docs__")),
    );
    assert.ok(
      JSON.stringify(body).length < 60000,
      "Full catalog leaked into provider request",
    );
    return new Response(
      "data: " +
        JSON.stringify({
          id: "offline-response",
          model: "offline-test",
          choices: [
            { index: 0, delta: { content: "OK" }, finish_reason: "stop" },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
        }) +
        "\n\ndata: [DONE]\n\n",
      { headers: { "content-type": "text/event-stream" } },
    );
  }
  assert.ok(
    String(url).startsWith("http://relay.test/"),
    "Unexpected network request",
  );
  const body = JSON.parse(options.body);
  assert.equal(
    new Headers(options.headers).get("authorization"),
    `Bearer ${relayToken}`,
  );
  seenRelayTokens.add(relayToken);
  if (String(url).endsWith("/events")) {
    events.push(body);
    return jsonResponse({});
  }
  if (String(url).endsWith("/policies/evaluate")) {
    policyCalls.push(body.event.data.name);
    return jsonResponse({
      result:
        body.event.data.name === "write"
          ? "POLICY_ACTION_DENY"
          : "POLICY_ACTION_ALLOW",
      reason: "smoke-denied",
    });
  }
  assert.ok(String(url).endsWith("/mcp"));
  calls.push(body.params);
  if (mode === "ask" && !body.params.inputResponses) {
    approvalRequests++;
    return jsonResponse({
      result: {
        resultType: "input_required",
        requestState: "smoke-state",
        inputRequests: { "smoke-approval": {} },
      },
    });
  }
  if (mode === "ask") {
    assert.equal(body.params.requestState, "smoke-state");
    assert.equal(body.params.inputResponses["smoke-approval"].action, "accept");
  }
  return jsonResponse({
    result: {
      content: [
        { type: "text", text: mode === "deny" ? "smoke-denied" : "relay-ok" },
      ],
      isError: mode === "deny",
    },
  });
};
let session;
try {
  const settingsManager = SettingsManager.inMemory({
    compaction: { enabled: false },
    retry: { enabled: false },
  });
  const loader = new DefaultResourceLoader({
    cwd: workspace,
    agentDir: path.join(workspace, "agent"),
    settingsManager,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    extensionFactories: [createCodemodeExtension(), require(extensionPath)],
  });
  await loader.reload();
  assert.deepEqual(loader.getExtensions().errors, []);
  const modelRuntime = await ModelRuntime.create({
    authPath: path.join(workspace, "auth.json"),
    modelsPath: path.join(workspace, "models.json"),
  });
  await modelRuntime.setRuntimeApiKey("openai", "offline-dummy");
  ({ session } = await createAgentSession({
    modelRuntime,
    cwd: workspace,
    agentDir: path.join(workspace, "agent"),
    resourceLoader: loader,
    settingsManager,
    sessionManager: SessionManager.inMemory(workspace),
    model: {
      id: "offline-test",
      name: "Offline test",
      provider: "openai",
      api: "openai-completions",
      baseUrl: "http://provider.test/v1",
      reasoning: false,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 128000,
      maxTokens: 2048,
    },
  }));
  await session.bindExtensions({});
  assert.ok(session.getActiveToolNames().includes("codemode"));
  assert.ok(session.getActiveToolNames().includes("read"));
  assert.ok(
    !session.getActiveToolNames().some((name) => name.startsWith("docs__")),
  );
  assert.equal(
    session.getCallableToolNames().filter((name) => name.startsWith("docs__"))
      .length,
    170,
  );
  await session.prompt("Reply with OK.");
  assert.equal(providerRequests.length, 1);
  const codemode = session.agent.state.tools.find(
    (tool) => tool.name === "codemode",
  );
  assert.ok(
    codemode.description.length < 20000,
    "Tool catalog exceeded native inline budget",
  );
  let callNumber = 0;
  const execute = (code) => {
    const id = `smoke-outer-${++callNumber}`;
    session.agent.state.messages.push({
      role: "assistant",
      api: "openai-completions",
      provider: "offline",
      model: "offline-test",
      content: [
        { type: "toolCall", id, name: "codemode", arguments: { code } },
      ],
      usage: {
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      },
      stopReason: "toolUse",
      timestamp: Date.now(),
    });
    return codemode.execute(id, { code }, new AbortController().signal);
  };
  const text = (result) =>
    result.content
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("\n");
  const found = await execute('text(await describeTool("docs__lookup_169"));');
  assert.match(text(found), /docs__lookup_169/);
  assert.equal(calls.length, 0, "Discovery must not execute the tool");
  const result = await execute(
    'text(await tools.docs__lookup_169({ query: "smoke" }));',
  );
  assert.match(text(result), /relay-ok/);
  assert.equal(calls.at(-1).name, "docs__lookup_169");
  assert.equal(
    policyCalls.filter((name) => name === "docs__lookup_169").length,
    0,
    "Bridged calls must not prompt twice",
  );
  const sequential = await execute(
    'text(await tools.docs__lookup_169({ query: "first" })); text(await tools.docs__lookup_168({ query: "second" }));',
  );
  assert.match(text(sequential), /relay-ok/);
  const nestedItems = events.filter(
    (event) => event.type === "external_conversation_item" &&
      event.data.item_data.call_id?.includes("/"),
  );
  assert.ok(nestedItems.length >= 6, "Sequential nested calls and results must be mirrored");
  for (const event of nestedItems) {
    assert.ok(event.data.item_data.parent_call_id, "Nested audit items must retain their parent");
  }
  relayToken = "smoke-relay-rotated";
  fs.writeFileSync(configPath, JSON.stringify({ ...bridgeConfig, relayToken }));
  mode = "deny";
  const denied = await execute(
    'text(await tools.docs__lookup_169({ query: "smoke" }));',
  );
  assert.match(text(denied), /smoke-denied/);
  mode = "ask";
  const approved = await execute(
    'text(await tools.docs__lookup_169({ query: "smoke" }));',
  );
  assert.match(text(approved), /relay-ok/);
  assert.equal(approvalRequests, 1);
  assert.ok(policyCalls.includes("docs__lookup_169"));
  const forbiddenFile = path.join(workspace, "forbidden.txt");
  const blockedWrite = await execute(
    `text(await tools.write({ path: ${JSON.stringify(forbiddenFile)}, content: "forbidden" }));`,
  );
  assert.match(text(blockedWrite), /smoke-denied/);
  assert.ok(
    !fs.existsSync(forbiddenFile),
    "Nested native tools must retain policy enforcement",
  );
  assert.ok(
    events.some(
      (e) =>
        e.type === "external_conversation_item" &&
        e.data.item_type === "function_call",
    ),
  );
  assert.equal(
    seenRelayTokens.size,
    2,
    "Relay credentials must refresh between calls",
  );
  console.log(
    "Pi version, bounded catalog, discovery, relay identity, allow/deny/approval and nested native policy smoke passed",
  );
} finally {
  await session?.dispose();
  globalThis.fetch = originalFetch;
  delete process.env.OMNIGENT_PI_NATIVE_CONFIG;
  fs.rmSync(workspace, { recursive: true, force: true });
}
