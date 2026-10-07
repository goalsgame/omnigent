"""Managed host coordinates and request-time inference credentials."""

import json
import stat

import httpx

from omnigent.host import inference_credential as helper


def test_managed_coordinates_are_private_and_replaced(tmp_path, monkeypatch):
    path = tmp_path / "coords"
    monkeypatch.setattr(helper, "_coords_path", lambda: path)
    monkeypatch.setenv("OMNIGENT_HOST_TOKEN", "launch-token")
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    helper.configure_host_inference("https://server.example", "host")
    assert not path.exists()
    monkeypatch.setenv("IS_SANDBOX", "1")
    helper.configure_host_inference("https://server.example", "host")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    monkeypatch.setenv("OMNIGENT_HOST_TOKEN", "new-launch-token")
    helper.configure_host_inference("https://server.example", "host")
    assert json.loads(path.read_text())["host_token"] == "new-launch-token"


def test_helper_refetches_each_invocation_and_sanitizes_failures(tmp_path, monkeypatch, capsys):
    path = tmp_path / "coords"
    path.write_text(
        json.dumps({"server": "https://server.example", "host_id": "host", "host_token": "launch"})
    )
    monkeypatch.setattr(helper, "_coords_path", lambda: path)
    replies = iter(
        [
            {"connected": True, "token": "first"},
            {"connected": True, "token": "renewed"},
            {"connected": False, "detail": "secret"},
        ]
    )

    def get(url, **kwargs):
        assert url == "https://server.example/v1/hosts/host/credentials/openrouter"
        assert kwargs["headers"] == {"X-Omnigent-Host-Token": "launch"}
        assert kwargs["follow_redirects"] is False
        return httpx.Response(200, request=httpx.Request("GET", url), json=next(replies))

    monkeypatch.setattr(helper.httpx, "get", get)
    assert helper.main() == 0
    assert capsys.readouterr().out == "first\n"
    assert helper.main() == 0
    assert capsys.readouterr().out == "renewed\n"
    assert helper.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "OpenRouter workload credential unavailable\n"
