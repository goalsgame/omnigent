"""Operator catalog validation, upgrade preservation and session creation."""

from pathlib import Path

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.agent_catalog import agent_catalog_names
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
