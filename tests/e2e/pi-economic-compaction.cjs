// Real native Pi lifecycle against a local synthetic OpenAI endpoint. No provider calls.
const fs = require("node:fs"),
  path = require("node:path"),
  http = require("node:http");
const assert = require("node:assert/strict");
const [binary, extension, root] = process.argv.slice(2);
(async () => {
  const cli = fs.realpathSync(binary);
  const runtime = await import(
    require("node:url").pathToFileURL(path.join(path.dirname(cli), "index.js"))
      .href
  );
  // The bridge resolves Pi's public runtime beside the actual CLI, as in production.
  process.argv[1] = cli;
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    if (!req.url.endsWith("/chat/completions")) {
      res.writeHead(202, { "content-type": "application/json" });
      res.end("{}");
      return;
    }
    const payload = JSON.parse(body);
    requests.push(payload);
    const summary = payload.messages.some(
      (m) => typeof m.content === "string" && m.content.includes("summar"),
    );
    const text = summary
      ? "## Goal\nContinue the unfinished task safely.\n## Next Steps\nVerify changes."
      : "Task continued safely.";
    res.writeHead(200, { "content-type": "text/event-stream" });
    const frame = {
      id: "test",
      object: "chat.completion.chunk",
      model: "vendor/large-model",
      choices: [
        {
          index: 0,
          delta: { role: "assistant", content: text },
          finish_reason: null,
        },
      ],
    };
    res.write("data: " + JSON.stringify(frame) + "\n\n");
    res.write(
      "data: " +
        JSON.stringify({
          ...frame,
          choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
          usage: {
            prompt_tokens: 100,
            completion_tokens: 10,
            total_tokens: 110,
          },
        }) +
        "\n\n",
    );
    res.end("data: [DONE]\n\n");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let session;
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    fs.writeFileSync(
      path.join(root, "models.json"),
      JSON.stringify({
        providers: {
          managed: {
            baseUrl: base + "/v1",
            api: "openai-completions",
            apiKey: "synthetic-key",
            models: [
              {
                id: "vendor/large-model",
                name: "Synthetic model",
                reasoning: false,
                input: ["text"],
                cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
                contextWindow: 1050000,
                maxTokens: 128000,
              },
            ],
          },
        },
      }),
    );
    const config = path.join(root, "bridge.json");
    fs.writeFileSync(
      config,
      JSON.stringify({
        sessionId: "synthetic-session",
        serverUrl: base,
        conversationUrl: base,
        inboxDir: path.join(root, "inbox"),
      }),
    );
    process.env.OMNIGENT_PI_NATIVE_CONFIG = config;
    process.env.PI_CODING_AGENT_DIR = root;
    const settings = runtime.SettingsManager.create(root, root);
    const models = await runtime.ModelRuntime.create({
      authPath: path.join(root, "auth.json"),
      modelsPath: path.join(root, "models.json"),
    });
    const model = models.getModel("managed", "vendor/large-model");
    assert.ok(model);
    const loader = new runtime.DefaultResourceLoader({
      cwd: root,
      agentDir: root,
      settingsManager: settings,
      noExtensions: true,
      noSkills: true,
      noPromptTemplates: true,
      noThemes: true,
      extensionFactories: [require(extension)],
    });
    await loader.reload();
    const manager = runtime.SessionManager.create(
      root,
      path.join(root, "sessions"),
    );
    const usage = {
      input: 210000,
      output: 10,
      cacheRead: 0,
      cacheWrite: 0,
      totalTokens: 210010,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
    };
    manager.appendMessage({
      role: "user",
      content: "unfinished state ".repeat(55000),
      timestamp: Date.now() - 1000,
    });
    manager.appendMessage({
      role: "assistant",
      content: [{ type: "text", text: "Work is pending." }],
      api: "openai-completions",
      provider: "managed",
      model: "vendor/large-model",
      usage,
      stopReason: "stop",
      timestamp: Date.now() - 500,
    });
    manager.appendMessage({
      role: "user",
      content: "recent task state ".repeat(5500),
      timestamp: Date.now() - 300,
    });
    manager.appendMessage({
      role: "assistant",
      content: [{ type: "text", text: "Latest step pending." }],
      api: "openai-completions",
      provider: "managed",
      model: "vendor/large-model",
      usage,
      stopReason: "stop",
      timestamp: Date.now() - 200,
    });
    ({ session } = await runtime.createAgentSession({
      cwd: root,
      agentDir: root,
      model,
      modelRuntime: models,
      settingsManager: settings,
      sessionManager: manager,
      resourceLoader: loader,
      tools: [],
      thinkingLevel: "off",
    }));
    assert.equal(settings.getCompactionSettings(model).reserveTokens, 850000);
    const events = [];
    session.subscribe((event) => events.push(event));
    await session.bindExtensions({});
    await session.prompt("Continue the unfinished task.");
    const compacted = manager
      .getEntries()
      .filter((e) => e.type === "compaction");
    assert.equal(
      compacted.length,
      1,
      JSON.stringify({
        events: events.map((e) => ({ type: e.type, error: e.errorMessage })),
        requests: requests.map((r) => ({
          max: r.max_tokens ?? r.max_completion_tokens,
          characters: JSON.stringify(r.messages).length,
        })),
      }),
    );
    assert.ok(compacted[0].usage && compacted[0].usage.totalTokens > 0);
    assert.ok(
      requests.some(
        (r) =>
          (r.max_tokens ?? r.max_completion_tokens) === Math.floor(0.8 * 16384),
      ),
    );
    const last = requests.at(-1);
    assert.ok(JSON.stringify(last.messages).length < 150000);
    assert.equal(session.model.contextWindow, 1050000);
    assert.ok(session.getLastAssistantText());
    assert.equal(
      requests.length,
      2,
      "One summary and one continuation request; no turn replay",
    );
    assert.equal(
      manager
        .getEntries()
        .filter(
          (e) =>
            e.type === "message" &&
            e.message.role === "user" &&
            (e.message.content === "Continue the unfinished task." ||
              (Array.isArray(e.message.content) &&
                e.message.content.some(
                  (b) =>
                    b.type === "text" &&
                    b.text === "Continue the unfinished task.",
                ))),
        ).length,
      1,
    );
    const persisted = runtime.SessionManager.open(manager.getSessionFile());
    assert.equal(
      persisted.getEntries().filter((e) => e.type === "compaction").length,
      1,
    );
    assert.ok(
      persisted
        .buildSessionProjection()
        .messages.some((m) => JSON.stringify(m).includes("unfinished")),
    );
    console.log(
      JSON.stringify({
        compactions: compacted.length,
        providerRequests: requests.length,
        postCompactionCharacters: JSON.stringify(last.messages).length,
      }),
    );
  } finally {
    if (session) session.dispose();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
})()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
