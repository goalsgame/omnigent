"""Logging reconfiguration must not silently disconnect OTLP export."""

import logging

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

from omnigent.runtime import telemetry


def test_init_restores_bridge_after_logging_configuration(monkeypatch):
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider(shutdown_on_exit=False)
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    monkeypatch.setattr(telemetry, "_initialized", True)
    monkeypatch.setattr(telemetry, "_logs_initialized", True)
    monkeypatch.setattr(telemetry, "_log_provider", provider)
    root = logging.getLogger()
    package = logging.getLogger("omnigent")
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(package, "handlers", [])
    monkeypatch.setattr(package, "propagate", False)
    try:
        telemetry.init()
        telemetry.init()
        for logger in (root, package):
            assert len(logger.handlers) == 1
            logger.handle(
                logging.LogRecord(
                    logger.name, logging.ERROR, __file__, 30, "test failure", (), None
                )
            )
        assert len(exporter.get_finished_logs()) == 2
        # Simulate Alembic/Uvicorn replacing root handlers.
        root.handlers.clear()
        telemetry.init()
        assert len(root.handlers) == 1
    finally:
        provider.shutdown()


def test_exporter_errors_do_not_feed_back_into_exporter():
    record = logging.LogRecord(
        "opentelemetry.exporter.otlp", logging.ERROR, __file__, 43, "export failed", (), None
    )
    assert telemetry._SafeLogFilter().filter(record) is False
