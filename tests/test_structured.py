"""Sorties structurées : outputSchema publié pour les outils qui renvoient des données, structuredContent identique au
texte JSON (gardé pour les clients plus anciens), isError sur une erreur ; texte seul pour les autres outils."""

from __future__ import annotations

import json
from typing import Any

import anyio
from fake_gmail import plain_message, stored_draft
from mcp import Client

from nexus_agymail import server

STRUCTURED = {
    "auth_status",
    "search_threads",
    "get_message",
    "get_thread",
    "list_new_messages",
    "list_labels",
    "list_drafts",
    "get_draft",
}


def listed() -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        return {t.name: t for t in await server.mcp.list_tools()}

    return anyio.run(run)


def call(name: str, arguments: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(server.mcp, mode="legacy") as client:
            return await client.call_tool(name, arguments)

    return anyio.run(run)


def test_schemas_de_sortie_publies_pour_les_donnees():
    tools = listed()
    assert {name for name, tool in tools.items() if tool.output_schema} == STRUCTURED
    for name in STRUCTURED:
        schema = tools[name].output_schema
        assert schema["type"] == "object" and "status" in schema["required"], name
        assert "result" not in schema["properties"], name  # plus de {"result": "<json>"} opaque
    search = tools["search_threads"].output_schema
    assert {"threads", "nextPageToken", "count"} <= set(search["properties"])
    assert "from" in search["$defs"]["Message"]["properties"]


def test_structured_content_egal_au_texte(gmail):
    gmail.add(plain_message("m1", "t1", "Bonjour", subject="Devis"))
    stored_draft(gmail, "d1", "Brouillon")
    for name, arguments in [
        ("search_threads", {"query": "devis"}),
        ("get_message", {"messageId": "m1"}),
        ("get_thread", {"threadId": "t1"}),
        ("list_labels", {}),
        ("list_drafts", {}),
        ("get_draft", {"draftId": "d1"}),
        ("list_new_messages", {}),
    ]:
        result = call(name, arguments)
        text = result.content[0].text
        assert result.is_error is False, (name, text)
        assert result.structured_content == json.loads(text), name
        assert result.structured_content["status"] == "success", name


def test_erreur_signalee_is_error(gmail):
    result = call("get_message", {"messageId": "absent"})
    assert result.is_error is True
    assert result.structured_content["code"] == "not_found" == json.loads(result.content[0].text)["code"]


def test_outils_d_action_en_texte_seul(gmail):
    gmail.add(plain_message("m1", "t1", "Bonjour"))
    result = call("trash_message", {"messageId": "m1"})
    assert result.structured_content is None and json.loads(result.content[0].text)["status"] == "success"


def test_fonctions_toujours_appelables_en_texte(gmail):
    gmail.add(plain_message("m1", "t1", "Bonjour"))
    assert isinstance(server.get_message(messageId="m1"), str)
