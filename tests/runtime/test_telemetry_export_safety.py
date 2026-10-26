"""Exported diagnostics preserve correlation without leaking credentials."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

from omnigent.runtime import telemetry


def test_log_export_redacts_body_exception_and_nested_attributes(monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider(shutdown_on_exit=False)
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(logger_provider=provider)
    handler.addFilter(telemetry._SafeLogFilter())
    try:
        try:
            raise ValueError("api_key=exception-secret")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "test",
                logging.ERROR,
                __file__,
                26,
                "failed authorization=Bearer %s",
                ("body-secret",),
                sys.exc_info(),
            )
        record.details = {"password": "attribute-secret", "nested": ["api_key=nested-secret"]}
        with telemetry.session_scope("test-session"):
            handler.handle(record)
        exported = exporter.get_finished_logs()[0].log_record
        text = str(exported.body) + str(exported.attributes)
        for secret in ("body-secret", "exception-secret", "attribute-secret", "nested-secret"):
            assert secret not in text
        assert exported.attributes["exception.type"] == "ValueError"
        assert "Traceback" in exported.attributes["exception.stacktrace"]
        assert exported.attributes["session.id"] == "test-session"
        # The file/debug handlers still receive the original record.
        assert "body-secret" in record.getMessage()
        assert record.exc_info is not None
        assert record.details["password"] == "attribute-secret"
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    "key", ["api-key", "api key", "x-api-key", "API   Key", "api_key", "apiKey"]
)
def test_structured_api_key_separators_are_redacted(key) -> None:
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "failed", (), None)
    record.__dict__[key] = "plain-credential"
    record.headers = {key: "nested-credential"}
    cleaned = telemetry._SafeLogFilter().filter(record)
    assert isinstance(cleaned, logging.LogRecord)
    assert cleaned.__dict__[key] == "[REDACTED]"
    assert cleaned.headers == {key: "[REDACTED]"}
    assert record.__dict__[key] == "plain-credential"
    assert record.headers[key] == "nested-credential"


def test_resource_instance_shared_across_signals_but_renewed_after_fork(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "_resource_instance", None)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "test-service")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.version=test-version")
    first = telemetry._otel_resource().attributes
    second = telemetry._otel_resource().attributes
    assert first["service.instance.id"] == second["service.instance.id"]
    assert first["service.name"] == "test-service"
    assert first["service.version"] == "test-version"
    monkeypatch.setattr(telemetry.os, "getpid", lambda: -1)
    child = telemetry._otel_resource().attributes
    assert child["service.instance.id"] != first["service.instance.id"]


def test_docker_preserves_runtime_logging_after_migrations(monkeypatch) -> None:
    from deploy.docker import entrypoint

    monkeypatch.setattr(telemetry, "init", Mock())
    monkeypatch.setenv("OMNIGENT_LOG_LEVEL", "INFO")
    monkeypatch.setattr(
        entrypoint, "_resolve_config", lambda: SimpleNamespace(database_url="test")
    )
    monkeypatch.setattr(
        entrypoint,
        "build_app",
        lambda _: SimpleNamespace(
            app=object(),
            host="127.0.0.1",
            port=8000,
        ),
    )
    root = logging.getLogger()
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.setattr(entrypoint, "run_migrations", lambda _: root.setLevel(logging.WARNING))
    build = Mock()
    monkeypatch.setattr("omnigent.server.graceful_server.shutdown_signaling_server", build)
    entrypoint.main()
    assert root.level == logging.INFO
    assert build.call_args.args[0].log_config is None
    build.return_value.run.assert_called_once_with()


def test_docker_startup_failure_is_instrumented_before_config(monkeypatch, caplog) -> None:
    from deploy.docker import entrypoint

    initialize = Mock()
    monkeypatch.setattr(telemetry, "init", initialize)

    def fail_config():
        initialize.assert_called_once_with("omni-server")
        raise ValueError("invalid deployment configuration")

    monkeypatch.setattr(entrypoint, "_resolve_config", fail_config)
    monkeypatch.setattr("time.sleep", lambda _: None)
    entrypoint.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit, match="1"):
            entrypoint.main()
        assert caplog.records[-1].exc_info[0] is ValueError
    finally:
        entrypoint.logger.removeHandler(caplog.handler)
