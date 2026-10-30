"""Tests boite noire du moteur natif C# (.NET 10) de NexusAGYmail (Phase 4 : parité vérifiée et bascule réversible)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NATIVE_DIR = ROOT / "native"
CSPROJ = NATIVE_DIR / "NexusAGYmail.csproj"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows engine")

EXPECTED_TOOLS = {
    "auth_status",
    "search_threads",
    "get_message",
    "get_thread",
    "list_new_messages",
    "download_attachment",
    "create_draft",
    "list_labels",
    "create_label",
    "list_drafts",
    "get_draft",
    "update_draft",
    "send_draft",
    "label_message",
    "unlabel_message",
    "label_thread",
    "unlabel_thread",
    "trash_message",
    "trash_thread",
    "untrash_message",
    "untrash_thread",
}


@cache
def _native_exe() -> Path:
    explicit = os.environ.get("NEXUS_AGYMAIL_TEST_EXE")
    if explicit:
        return Path(explicit)
    local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    digest = hashlib.sha256()
    for source in sorted(p for p in NATIVE_DIR.iterdir() if p.suffix in (".cs", ".csproj", ".json", ".props")):
        digest.update(source.read_bytes())
    digest.update(ROOT.joinpath("src/nexus_agymail/garde-fous.json").read_bytes())
    exe = Path(local_app_data) / "NexusAGYmail" / "test-builds" / digest.hexdigest()[:16] / "NexusAGYmail.exe"
    if not exe.is_file():
        build_root = Path(local_app_data) / "NexusAGYmail" / "build"
        cmd = [
            "dotnet",
            "publish",
            str(CSPROJ),
            "-c",
            "Release",
            "-r",
            "win-x64",
            "--self-contained",
            "true",
            "--no-restore",
            "-p:PublishSingleFile=true",
            "-o",
            str(exe.parent),
            f"-p:LocalBuildRoot={build_root}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"dotnet publish failed: {res.stderr}\n{res.stdout}"
    assert exe.is_file(), f"NexusAGYmail.exe missing at {exe}"
    return exe


def test_native_version() -> None:
    exe = _native_exe()
    res = subprocess.run([str(exe), "--version"], capture_output=True, text=True, check=False)
    assert res.returncode == 0
    assert "NexusAGYmail" in res.stdout
    assert "1.0.1" in res.stdout


def test_native_selftest() -> None:
    exe = _native_exe()
    res = subprocess.run([str(exe), "--selftest"], capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"Selftest failed: {res.stdout}\n{res.stderr}"
    assert "Self-tests finished: 39 passed, 0 failed" in res.stdout


def test_native_mcp_initialize_and_tools() -> None:
    exe = _native_exe()
    proc = subprocess.Popen(
        [str(exe), "--mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=NO_WINDOW,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    try:
        init_req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        proc.stdin.write(init_req + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        init_resp = json.loads(line)
        assert init_resp.get("jsonrpc") == "2.0"
        assert init_resp.get("id") == 1
        assert init_resp.get("result", {}).get("serverInfo", {}).get("name") == "nexus-agymail"

        tools_req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        proc.stdin.write(tools_req + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        tools_resp = json.loads(line)
        assert tools_resp.get("id") == 2
        tools_list = tools_resp.get("result", {}).get("tools", [])
        names = {t["name"] for t in tools_list}
        assert EXPECTED_TOOLS.issubset(names), f"Missing tools: {EXPECTED_TOOLS - names}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
