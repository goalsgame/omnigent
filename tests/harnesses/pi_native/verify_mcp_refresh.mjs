/** Offline check that a running native Pi session follows MCP tools added and removed upstream. */
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const [packageRoot, extensionPath, exposure] = process.argv.slice(2);
const {
  createAgentSession,
  createCodemodeExtension,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  SettingsManager,
} = await import(pathToFileURL(path.join(packageRoot, "dist/index.js")));
const require = createRequire(import.meta.url);
const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "pi-mcp-refresh-"));
const configPath = path.join(workspace, "bridge.json");
const parameters = {
  type: "object",
  properties: { query: { type: "string" } },
  required: ["query"],
};
const listed = (name) => ({ name, description: `The ${name} tool.`, inputSchema: parameters });
const configured = (name) => ({
  name,
  description: `The ${name} tool.`,
  parameters,
  mcp: true,
  ...(exposure === "codemode" ? { exposure } : {}),
});
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://server.invalid",
    sessionId: "refresh-session",
    authHeaders: {},
    relayUrl: "http://relay.test",
    relayToken: "relay-token",
    mcpToolExposure: exposure,
    tools: [configured("docs__alpha"), configured("docs__gamma")],
  }),
);
process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

let upstream = ["docs__alpha", "docs__gamma"];
const listCalls = [];
const toolCalls = [];
const providerRequests = [];
const jsonResponse = (body) =>
  new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
const originalFetch = globalThis.fetch;
globalThis.fetch = async (url, options = {}) => {
  const body = JSON.parse(options.body);
  if (String(url).startsWith("http://provider.test/")) {
    providerRequests.push(body);
    return new Response(
      "data: " +
        JSON.stringify({
          id: "offline",
          model: "offline-test",
          choices: [{ index: 0, delta: { content: "OK" }, finish_reason: "stop" }],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
        }) +
        "\n\ndata: [DONE]\n\n",
      { headers: { "content-type": "text/event-stream" } },
    );
  }
  assert.ok(String(url).startsWith("http://relay.test/"), `Unexpected request ${url}`);
  assert.equal(new Headers(options.headers).get("authorization"), "Bearer relay-token");
  if (String(url).endsWith("/events")) return jsonResponse({});
  if (String(url).endsWith("/policies/evaluate")) {
    return jsonResponse({ result: "POLICY_ACTION_ALLOW" });
  }
  assert.ok(String(url).endsWith("/mcp"));
  if (body.method === "tools/list") {
    listCalls.push(body);
    return jsonResponse({ jsonrpc: "2.0", id: body.id, result: { tools: upstream.map(listed), failures: {} } });
  }
  assert.equal(body.method, "tools/call");
  toolCalls.push(body.params.name);
  if (!upstream.includes(body.params.name)) {
    return jsonResponse({ jsonrpc: "2.0", id: body.id, error: { code: -32000, message: "unknown tool" } });
  }
  return jsonResponse({
    jsonrpc: "2.0",
    id: body.id,
    result: { content: [{ type: "text", text: `ran ${body.params.name}` }], isError: false },
  });
};

const declaredTools = () =>
  (providerRequests.at(-1).tools ?? []).map((tool) => tool.function ?? tool);
const declaredDocsTools = () =>
  declaredTools()
    .map((tool) => tool.name)
    .filter((name) => name?.startsWith("docs__"))
    .sort();
const codemodeDescription = () =>
  declaredTools().find((tool) => tool.name === "codemode")?.description ?? "";

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
    extensionFactories: [
      ...(exposure === "codemode" ? [createCodemodeExtension()] : []),
      require(extensionPath),
    ],
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

  await session.prompt("First turn.");
  if (exposure === "codemode") {
    assert.deepEqual(declaredDocsTools(), []);
    assert.match(codemodeDescription(), /docs__gamma/);
    assert.doesNotMatch(codemodeDescription(), /docs__beta/);
  } else {
    assert.deepEqual(declaredDocsTools(), ["docs__alpha", "docs__gamma"]);
  }

  upstream = ["docs__alpha", "docs__beta"];
  await session.prompt("Second turn.");
  assert.equal(providerRequests.length, 2);
  if (exposure === "codemode") {
    assert.deepEqual(declaredDocsTools(), []);
    assert.match(codemodeDescription(), /docs__beta/);
    assert.doesNotMatch(codemodeDescription(), /docs__gamma/);
    const codemode = session.agent.state.tools.find((tool) => tool.name === "codemode");
    session.agent.state.messages.push({
      role: "assistant",
      api: "openai-completions",
      provider: "offline",
      model: "offline-test",
      content: [{ type: "toolCall", id: "outer-1", name: "codemode", arguments: {} }],
      usage: {
        input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      },
      stopReason: "toolUse",
      timestamp: Date.now(),
    });
    const result = await codemode.execute(
      "outer-1",
      { code: 'text(await tools.docs__beta({ query: "q" }));' },
      new AbortController().signal,
    );
    const text = result.content.filter((block) => block.type === "text").map((b) => b.text).join("\n");
    assert.match(text, /ran docs__beta/);
  } else {
    assert.deepEqual(declaredDocsTools(), ["docs__alpha", "docs__beta"]);
    const beta = session.agent.state.tools.find((tool) => tool.name === "docs__beta");
    const result = await beta.execute("call-1", { query: "q" }, new AbortController().signal);
    assert.equal(result.content[0].text, "ran docs__beta");
    assert.ok(!session.agent.state.tools.some((tool) => tool.name === "docs__gamma"));
  }
  assert.deepEqual(toolCalls, ["docs__beta"]);
  assert.equal(listCalls.length, 2);

  upstream = ["docs__alpha", "docs__gamma"];
  await session.prompt("Third turn.");
  if (exposure !== "codemode") {
    assert.deepEqual(declaredDocsTools(), ["docs__alpha", "docs__gamma"]);
  } else {
    assert.match(codemodeDescription(), /docs__gamma/);
    assert.doesNotMatch(codemodeDescription(), /docs__beta/);
  }
  console.log(`MCP refresh verified (${exposure})`);
} finally {
  await session?.dispose();
  globalThis.fetch = originalFetch;
  delete process.env.OMNIGENT_PI_NATIVE_CONFIG;
  fs.rmSync(workspace, { recursive: true, force: true });
}
