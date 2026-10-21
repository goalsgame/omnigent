"""Mounted config to API discovery, SDK resources and real Pi skill discovery."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from omnigent.runner.native.orchestration import _build_pi_native_args
from omnigent.server import app as server_app
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.tools.builtins.load_skill import LoadSkillTool
from omnigent.tools.builtins.read_skill_file import ReadSkillFileTool
from tests.server.test_app import _independent_seed_stores


@pytest.mark.asyncio
async def test_operator_skill_reaches_catalog_sdk_and_native_pi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pi = shutil.which("pi")
    if pi is None:
        pytest.skip("Pi CLI is required for native discovery")
    source = tmp_path / "assistant"
    source.mkdir()
    (source / "assistant.yaml").write_text(
        "name: assistant\nexecutor:\n  harness: pi-native\n"
        "  model: test-model\nprompt: Preserve the original prompt.\n"
    )
    namespace_skill = source / "skills/ops/deploy"
    namespace_skill.mkdir(parents=True)
    (namespace_skill / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Prepare deployment checks\n---\nCheck the plan."
    )
    monkeypatch.setenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, str(source))
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "true")
    for name in (
        "_ensure_default_native_agents",
        "_ensure_default_acp_agents",
        "_ensure_default_debby_agent",
        "_ensure_default_polly_agent",
    ):
        monkeypatch.setattr(server_app, name, lambda *args: None)
    stores = _independent_seed_stores(tmp_path, "e2e")
    uri = f"sqlite:///{tmp_path / 'e2e.db'}"
    config = {
        "operator_skills": {
            "library": {
                "review-demo": {
                    "SKILL.md": "---\nname: review-demo\ndescription: Review the demo\n---\n"
                    "Read [the contract](references/contract.md) before reviewing.",
                    "references/contract.md": "Check caller contracts before reporting defects.",
                }
            },
            "agents": {"assistant": ["review-demo"]},
        }
    }
    from omnigent.runtime import init as init_runtime

    conversation_store = SqlAlchemyConversationStore(uri)
    file_store = SqlAlchemyFileStore(uri)
    init_runtime(
        agent_store=stores.agent_store,
        conversation_store=conversation_store,
        agent_cache=stores.agent_cache,
        file_store=file_store,
        artifact_store=stores.artifact_store,
    )
    app = server_app.create_app(
        agent_store=stores.agent_store,
        artifact_store=stores.artifact_store,
        conversation_store=conversation_store,
        file_store=file_store,
        agent_cache=stores.agent_cache,
        server_config=config,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            result = await client.get("/v1/agents")
            assert result.status_code == 200, result.text
            row = next(agent for agent in result.json()["data"] if agent["name"] == "assistant")
            assert {"review-demo", "deploy"} <= {skill["name"] for skill in row["skills"]}
        agent = stores.agent_store.get_by_name("assistant")
        assert agent is not None
        loaded = stores.agent_cache.load(agent.id, agent.bundle_location)
        assert loaded.spec.instructions == "Preserve the original prompt."
        tool = LoadSkillTool(loaded.spec.skills)
        content = tool.invoke(json.dumps({"name": "review-demo"}), None)
        assert "references/contract.md" in content
        resource = ReadSkillFileTool(loaded.spec.skills).invoke(
            json.dumps({"skill_name": "review-demo", "path": "references/contract.md"}), None
        )
        assert resource == "Check caller contracts before reporting defects."
        args = _build_pi_native_args(
            terminal_launch_args=["--mode", "rpc", "--no-session", "--no-extensions"],
            extension_path=tmp_path / "unused.mjs",
            session_dir=tmp_path / "sessions",
            external_session_id=None,
            bundle_dir=loaded.workdir,
            skills_filter=["review-demo", "deploy"],
        )
        # No extension or model invocation is needed to inspect Pi's loaded commands.
        args = args[2:]
        env = dict(os.environ)
        env["PI_CODING_AGENT_DIR"] = str(tmp_path / "pi-home")
        completed = subprocess.run(
            [pi, *args],
            input=json.dumps({"id": "skills", "type": "get_commands"}) + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=True,
        )
        responses = [
            json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")
        ]
        command_result = next(item for item in responses if item.get("id") == "skills")
        assert command_result["success"], command_result
        assert {"skill:review-demo", "skill:deploy"} <= {
            command["name"] for command in command_result["data"]["commands"]
        }
