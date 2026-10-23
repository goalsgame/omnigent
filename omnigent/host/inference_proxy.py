"""Prepare session inference credentials and exchange them for short-lived bearers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

import httpx


def _valid_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return bool(
        parts.scheme == "https"
        and parts.netloc
        and not parts.username
        and not parts.password
        and not parts.query
        and not parts.fragment
        and (
            parts.path.startswith("/v1/inference/sessions/")
            or (parts.path.startswith("/v1/inference/workspaces/") and "/sessions/" in parts.path)
        )
    )


def _system_powershell() -> str:
    import ctypes

    directory = ctypes.create_unicode_buffer(32768)
    if sys.platform == "win32":
        length = ctypes.windll.kernel32.GetSystemDirectoryW(directory, len(directory))
    else:
        raise OSError("PowerShell lookup requires Windows")
    if not 0 < length < len(directory):
        raise OSError("Windows system directory unavailable")
    path = PureWindowsPath(directory.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # cmd expands percent variables and may enable delayed bang expansion inside quotes.
    if not path.is_absolute() or any(c in str(path) for c in '"%!^\r\n'):
        raise ValueError("Unsafe Windows system directory")
    return str(path)


def _refresh_command(args: list[str]) -> str:
    if sys.platform != "win32":
        return shlex.join(args)
    # Encode Python arguments so cmd cannot expand their path metacharacters.
    literals = ["'" + arg.replace("'", "''") + "'" for arg in args]
    script = "$ErrorActionPreference='Stop'; & " + " ".join(literals) + "; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (
        '"'
        + _system_powershell()
        + '" -NoLogo -NoProfile -NonInteractive -EncodedCommand '
        + encoded
    )


def _validate_target(proxy: dict) -> None:
    if not _valid_url(proxy.get("url")):
        raise ValueError("Server inference requires HTTPS")
    if proxy.get("harness") not in (
        "openai-agents",
        "pi-native",
        "claude-native",
        "codex-native",
    ) or (
        not isinstance(proxy.get("models"), list)
        or not proxy["models"]
        or any(not isinstance(model, str) or not model for model in proxy["models"])
    ):
        raise ValueError("Invalid inference harness or model list")
    from omnigent.inference_config import parse_inference_config

    if "model_limits" in proxy or "model_reasoning" in proxy:
        parse_inference_config(
            {
                "providers": {"proxy": {}},
                "inference": {
                    "harnesses": {
                        proxy["harness"]: {
                            "provider": "proxy",
                            "model_allowlist": proxy["models"],
                            **{
                                key: proxy[key]
                                for key in ("model_limits", "model_reasoning")
                                if key in proxy
                            },
                        }
                    }
                },
            }
        )
    apis = proxy.get("model_apis", {})
    if (
        not isinstance(apis, dict)
        or (
            apis
            and (
                proxy["harness"] not in ("pi-native", "claude-native")
                or set(apis) != set(proxy["models"])
            )
        )
        or any(api not in ("openai-completions", "anthropic-messages") for api in apis.values())
        or (
            proxy["harness"] == "claude-native"
            and (not apis or any(api != "anthropic-messages" for api in apis.values()))
        )
    ):
        raise ValueError("Invalid inference model API mapping")


def prepare(config: dict, directory: Path, *, credential_files: list[Path] | None = None) -> dict:
    proxy = config.get("server_inference")
    if proxy is None:
        return config
    if (
        not isinstance(proxy, dict)
        or set(proxy)
        - {"url", "grant", "models", "harness", "model_apis", "model_limits", "model_reasoning"}
        or not {"url", "grant", "models", "harness"} <= set(proxy)
    ):
        raise ValueError("Invalid server inference configuration")
    _validate_target(proxy)
    url = proxy["url"]
    if not isinstance(proxy["grant"], str) or len(proxy["grant"]) != 64:
        raise ValueError("Invalid inference grant")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(json.dumps(proxy, sort_keys=True).encode()).hexdigest()
    fd, filename = tempfile.mkstemp(prefix=digest + "-", suffix=".json", dir=directory)
    credential = Path(filename)
    if credential_files is not None:
        credential_files.append(credential)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"url": url, "grant": proxy["grant"]}, stream)
    except BaseException:
        credential.unlink(missing_ok=True)
        raise
    result = runtime_config(proxy, credential)
    result["server_inference_binding"] = {
        "url": proxy["url"],
        "harness": proxy["harness"],
        "models": proxy["models"],
        "credential_file": str(credential),
        **{
            key: proxy[key]
            for key in ("model_apis", "model_limits", "model_reasoning")
            if key in proxy
        },
    }
    return result


def claude_model_pins(models: list[str]) -> dict[str, str]:
    """Pin Claude Code's model aliases to permitted ids, so background calls stay routable."""
    pins: dict[str, str] = {"default": models[0]}
    for alias in ("fable", "opus", "sonnet", "haiku"):
        match = next((model for model in models if alias in model.rsplit("/", 1)[-1]), None)
        if match is not None:
            pins[alias] = match
    if "haiku" not in pins:
        pins["haiku"] = pins.get("sonnet", models[0])
    return pins


def runtime_config(proxy: dict, credential: Path) -> dict:
    _validate_target(proxy)
    url = proxy["url"]
    args = [sys.executable, "-P", "-m", "omnigent.host.inference_proxy", str(credential)]
    command = _refresh_command(args)
    apis = proxy.get("model_apis", {})
    anthropic = {
        "base_url": url,
        "auth_command": command,
        "auth_refresh_interval_ms": 60000,
        **(
            {"models": claude_model_pins(proxy["models"])}
            if proxy["harness"] == "claude-native"
            else {}
        ),
    }
    return {
        "providers": {
            "server-inference": {
                "kind": "gateway",
                **({"anthropic": anthropic} if "anthropic-messages" in apis.values() else {}),
                "openai": {
                    "base_url": url,
                    "auth_command": command,
                    "wire_api": "responses" if proxy["harness"] == "codex-native" else "chat",
                    "auth_refresh_interval_ms": 60000,
                },
            }
        },
        "inference": {
            "harnesses": {
                proxy["harness"]: {
                    "provider": "server-inference",
                    "model_allowlist": proxy["models"],
                    "default_model": proxy["models"][0],
                    **({"model_apis": apis} if apis else {}),
                    **({"model_limits": proxy["model_limits"]} if "model_limits" in proxy else {}),
                    **(
                        {"model_reasoning": proxy["model_reasoning"]}
                        if "model_reasoning" in proxy
                        else {}
                    ),
                }
            }
        },
    }


def main() -> int:
    try:
        config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        if not _valid_url(config["url"]):
            raise ValueError("HTTPS required")
        response = httpx.post(
            config["url"] + "/token",
            headers={"Authorization": "Bearer " + config["grant"]},
            timeout=8,
            trust_env=False,
            follow_redirects=False,
        )
        response.raise_for_status()
        token = response.json()["token"]
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError("Invalid credential")
        sys.stdout.write(token + "\n")
        return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError, httpx.HTTPError):
        print("Session inference authentication unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
