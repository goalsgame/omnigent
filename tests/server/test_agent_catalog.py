"""Operator catalog validation, upgrade preservation and session creation."""

from pathlib import Path

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.agent_catalog import agent_catalog_names, agent_catalog_presentation
from omnigent.server.app import _ensure_default_agents
from omnigent.server.routes._session_create_validation import validate_session_agent
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def test_catalog_config_defaults_and_empty() -> None:
    assert agent_catalog_names({}) is None
    assert agent_catalog_names({"agents": {"allowed_names": []}}) == frozenset()
    assert agent_catalog_names({"agents": {"allowed_names": ["custom-pi"]}}) == {"custom-pi"}


@pytest.mark.parametrize(
    "section",
    [[], {"allowed_names": "custom-pi"}, {"allowed_names": [""]}, {"allowed_names": [1]}],
)
def test_invalid_catalog_config(section: object) -> None:
    with pytest.raises(ValueError):
        agent_catalog_names({"agents": section})


def test_explicit_catalog_seeds_only_declared_agents(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyAgentStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    cache = AgentCache(artifacts, tmp_path / "cache")
    spec = tmp_path / "custom-pi.yaml"
    spec.write_text("name: custom-pi\nexecutor:\n  harness: pi-native\nprompt: Help the team.\n")
    monkeypatch.setenv("OMNIGENT_BUILTIN_AGENT_DIRS", str(spec))
    store.catalog_names = frozenset({"custom-pi"})
    _ensure_default_agents(store, artifacts, cache)
    assert [a.name for a in store.list().data] == ["custom-pi"]
    assert store.get_by_name("pi-native-ui") is None
    agent = store.get_by_name("custom-pi")
    assert agent is not None
    _ensure_default_agents(store, artifacts, cache)
    assert store.get_by_name("custom-pi").id == agent.id


def test_catalog_upgrade_filters_before_pagination_and_retains_old_rows(db_uri: str) -> None:
    store = SqlAlchemyAgentStore(db_uri)
    for index, name in enumerate(["old-a", "custom-pi", "old-b", "custom-claude", "old-c"]):
        store.create(f"{index:032x}", name, f"bundle/{name}")
    store.catalog_names = frozenset({"custom-pi", "custom-claude"})
    first = store.list(limit=1, order="asc")
    assert [a.name for a in first.data] == ["custom-pi"]
    assert first.has_more
    second = store.list(limit=1, after=first.last_id, order="asc")
    assert [a.name for a in second.data] == ["custom-claude"]
    assert not second.has_more
    assert store.get_by_name("old-a") is not None
    store.catalog_names = frozenset()
    assert store.list().data == []
    store.catalog_names = None
    assert len(store.list().data) == 5


@pytest.mark.asyncio
async def test_hidden_template_cannot_start_new_session_but_remains_readable(db_uri: str) -> None:
    store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent = store.create("f" * 32, "pi-native-ui", "bundle/old")
    store.catalog_names = frozenset({"custom-pi"})
    with pytest.raises(OmnigentError) as exc:
        await validate_session_agent(
            user_id=None,
            agent_id=agent.id,
            agent_store=store,
            permission_store=None,
            conversation_store=conv_store,
        )
    assert exc.value.code == ErrorCode.FORBIDDEN
    assert store.get(agent.id) is not None


def test_catalog_presentation() -> None:
    assert agent_catalog_presentation({}) == (None, {})
    assert agent_catalog_presentation(
        {
            "agents": {
                "allowed_names": ["assistant", "reviewer"],
                "default_name": "assistant",
                "display_names": {"reviewer": "Code Reviewer"},
            }
        }
    ) == ("assistant", {"reviewer": "Code Reviewer"})


@pytest.mark.parametrize(
    "section",
    [
        {"default_name": ""},
        {"display_names": []},
        {"display_names": {"x": ""}},
        {"display_names": {" assistant": "Assistant"}},
        {"display_names": {"assistant ": "Assistant"}},
        {"allowed_names": [], "default_name": "hidden"},
        {"allowed_names": [], "display_names": {"hidden": "Hidden"}},
    ],
)
def test_catalog_presentation_rejects_invalid_config(section: dict) -> None:
    with pytest.raises(ValueError):
        agent_catalog_presentation({"agents": section})


@pytest.mark.asyncio
async def test_catalog_presentation_is_exposed_on_discovery(db_uri: str, tmp_path: Path) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from omnigent.server.routes.builtin_agents import create_builtin_agents_router

    store = SqlAlchemyAgentStore(db_uri)
    store.create("a" * 32, "assistant", "missing/assistant")
    store.create("b" * 32, "reviewer", "missing/reviewer")
    cache = AgentCache(LocalArtifactStore(str(tmp_path)), tmp_path / "cache")
    app = FastAPI()
    app.include_router(
        create_builtin_agents_router(
            store,
            cache,
            default_agent_name="assistant",
            agent_display_names={"assistant": "Pi", "reviewer": "Code Reviewer"},
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/agents")
    assert response.status_code == 200
    rows = {row["name"]: row for row in response.json()["data"]}
    assert rows["assistant"]["default_for_new_sessions"] is True
    assert rows["reviewer"]["default_for_new_sessions"] is False
    assert rows["reviewer"]["display_name"] == "Code Reviewer"


@pytest.mark.asyncio
async def test_session_list_exposes_origin_for_disabled_template_and_user_upload(client, db_uri):
    store = SqlAlchemyAgentStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    server = store.create("a" * 32, "retired", "bundle/server")
    user = store.create_user_agent("b" * 32, "custom-upload", "bundle/user", None)
    conversations.create_conversation(conversation_id="c" * 32, agent_id=server.id)
    conversations.create_conversation(conversation_id="d" * 32, agent_id=user.id)
    response = await client.get("/v1/sessions")
    assert response.status_code == 200
    kinds = {row["agent_id"]: row["agent_kind"] for row in response.json()["data"]}
    assert kinds == {server.id: "server", user.id: "user"}
    assert store.get_kinds([]) == {}
    assert store.get_kinds(["e" * 32]) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [["private-project-reviewer"], []])
async def test_catalog_names_require_authenticated_discovery(
    db_uri, tmp_path, runtime_init, names
):
    from httpx import ASGITransport, AsyncClient

    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifacts, tmp_path / "cache"),
        auth_provider=UnifiedAuthProvider(
            "header", local_single_user=False, header_name="X-Test-User"
        ),
        server_config={"agents": {"allowed_names": names}},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        public = await client.get("/v1/info")
        assert public.status_code == 200
        assert "agent_catalog_names" not in public.json()
        assert "private-project-reviewer" not in public.text
        denied = await client.get("/v1/agents")
        assert denied.status_code == 401
        assert "private-project-reviewer" not in denied.text
        catalog = await client.get("/v1/agents", headers={"X-Test-User": "person@example.test"})
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["agent_catalog_names"] == names
