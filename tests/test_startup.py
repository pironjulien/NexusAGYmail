"""Le serveur démarre seul, sans jeton ni réseau, sur tout système (Windows, Linux) : initialize puis tools/list.

C'est ce que vérifient les annuaires MCP (Glama) dans un conteneur Linux ; la CI le lance aussi sous Linux.
"""

from __future__ import annotations

import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from nexus_agymail import __version__

TOOLS = {
    "search_threads", "get_message", "get_thread", "list_labels", "create_draft", "list_drafts", "update_draft",
    "get_draft", "create_label",
    "label_message", "unlabel_message", "label_thread", "unlabel_thread", "trash_message", "trash_thread",
    "untrash_message", "untrash_thread", "send_draft", "download_attachment", "auth_status",
}  # fmt: skip


def test_initialize_and_list_tools(tmp_path):
    # État hors du vrai profil (HOME, USERPROFILE et dossier d'état) : aucun jeton, aucun appel à Google au démarrage.
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "NEXUS_AGYMAIL_HOME": str(tmp_path / "state"),
        "NEXUS_AGYMAIL_UPDATE_CHECK": "0",  # pas de requête à PyPI dans le test
    }
    params = StdioServerParameters(command=sys.executable, args=["-B", "-m", "nexus_agymail"], env=env)

    async def check():
        with anyio.fail_after(60):
            await session_check()

    async def session_check():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "nexus-agymail"
            assert init.server_info.version == __version__
            listed = (await session.list_tools()).tools
            # Ce que lit tout client tiers : en anglais, neutre (« the user »), sous la limite de 2048 caractères.
            assert init.instructions and len(init.instructions) <= 2048
            seen = init.instructions + "".join(tool.model_dump_json() for tool in listed)
            for personal in ("Julien", "@gmail.com", "politique commune", "brouillon"):
                assert personal not in seen, personal
            tools = {tool.name: tool for tool in listed}
            assert set(tools) == TOOLS
            for tool in tools.values():
                hints = tool.annotations
                assert hints is not None and hints.title, tool.name
                assert hints.read_only_hint is not None and hints.destructive_hint is not None, tool.name
                assert hints.idempotent_hint is not None, tool.name
            assert tools["search_threads"].annotations.read_only_hint  # type: ignore[union-attr]
            assert tools["send_draft"].annotations.destructive_hint  # type: ignore[union-attr]
            assert not tools["trash_message"].annotations.destructive_hint  # type: ignore[union-attr]
            schema = tools["search_threads"].input_schema["properties"]
            assert {"query", "pageSize", "pageToken", "includeTrash"} <= set(schema)
            status = await session.call_tool("auth_status", {})
            text = status.content[0].text  # type: ignore[union-attr]
            assert json.loads(text)["present"] is False
        state = json.loads((tmp_path / ".nexus" / "status" / "agymail.json").read_text(encoding="utf-8"))
        assert state["product"] == "agymail" and state["version"] == __version__ and state["state"] == "idle"

    anyio.run(check)
