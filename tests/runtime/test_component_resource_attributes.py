"""Workspace resource identity must not follow a server-only release."""

import os

import pytest

from omnigent.runtime import telemetry


@pytest.fixture(autouse=True)
def isolate_service_environment(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "omni-server")


@pytest.mark.parametrize("component", ["omni-host", "omni-runner", "omni-harness"])
def test_workspace_components_override_inherited_server_attributes(monkeypatch, component):
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.version=server-release")
    monkeypatch.setenv(
        "OMNIGENT_OTEL_RUNNER_RESOURCE_ATTRIBUTES", "service.version=runner-release"
    )
    monkeypatch.setattr(telemetry, "_initialized", False)
    observed = []
    for name in [
        "_init_otel_traces",
        "_init_otel_metrics",
        "_init_otel_logs",
        "_instrument_httpx",
    ]:
        monkeypatch.setattr(
            telemetry, name, lambda *args: observed.append(os.environ["OTEL_RESOURCE_ATTRIBUTES"])
        )
    telemetry.init(component)
    assert observed == ["service.version=runner-release"] * 4
    assert telemetry._otel_resource().attributes["service.version"] == "runner-release"


@pytest.mark.parametrize("component", ["omni-server", "custom-service", None])
def test_server_and_custom_components_keep_standard_attributes(monkeypatch, component):
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "omni-server")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.version=server-release")
    monkeypatch.setenv(
        "OMNIGENT_OTEL_RUNNER_RESOURCE_ATTRIBUTES", "service.version=runner-release"
    )
    monkeypatch.setattr(telemetry, "_initialized", False)
    for name in [
        "_init_otel_traces",
        "_init_otel_metrics",
        "_init_otel_logs",
        "_instrument_httpx",
    ]:
        monkeypatch.setattr(telemetry, name, lambda *args: None)
    telemetry.init(component)
    assert os.environ["OTEL_RESOURCE_ATTRIBUTES"] == "service.version=server-release"


def test_unset_override_is_backwards_compatible_and_disabled_is_inert(monkeypatch):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.version=existing")
    monkeypatch.delenv("OMNIGENT_OTEL_RUNNER_RESOURCE_ATTRIBUTES", raising=False)
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(telemetry, "_initialized", False)
    for name in [
        "_init_otel_traces",
        "_init_otel_metrics",
        "_init_otel_logs",
        "_instrument_httpx",
    ]:
        monkeypatch.setattr(telemetry, name, lambda *args: None)
    telemetry.init("omni-host")
    assert os.environ["OTEL_RESOURCE_ATTRIBUTES"] == "service.version=existing"
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("OMNIGENT_OTEL_RUNNER_RESOURCE_ATTRIBUTES", "service.version=new")
    telemetry.init("omni-host")
    assert os.environ["OTEL_RESOURCE_ATTRIBUTES"] == "service.version=existing"
