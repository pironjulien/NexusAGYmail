"""Confirmation native (élicitation MCP) de l'envoi : fenêtre du client quand il la propose, sur les deux transports
(requête du serveur avant 2026-07-28, InputRequiredResult ensuite) ; jeton à usage unique en repli sinon. Le oui
reste lié au brouillon exact : relu avant l'envoi, tout changement depuis la fenêtre annule l'envoi."""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from fake_gmail import stored_draft
from mcp import Client
from mcp.types import ElicitResult

from nexus_agymail import server

TRANSPORTS = ["legacy", "2026-07-28"]


class Dialog:
    """Fenêtre de confirmation du client : note ce qui est montré et répond comme l'utilisateur."""

    def __init__(self, action: str = "accept", send: Any = True, before: Any = None) -> None:
        self.action, self.send, self.before = action, send, before
        self.shown: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        self.shown.append(params.message)
        self.schemas.append(params.requested_schema)
        if self.before:
            self.before()
        content = {"send": self.send} if self.action == "accept" else None
        return ElicitResult(action=self.action, content=content)  # type: ignore[arg-type]


def call(arguments: dict[str, Any], mode: str = "legacy", dialog: Dialog | None = None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with Client(server.mcp, mode=mode, elicitation_callback=dialog) as client:  # type: ignore[arg-type]
            result = await client.call_tool("send_draft", arguments)
            return json.loads(result.content[0].text)  # type: ignore[union-attr]

    return anyio.run(run)


@pytest.mark.parametrize("mode", TRANSPORTS)
def test_oui_dans_la_fenetre_envoie_le_brouillon_exact(gmail, mode, monkeypatch):
    stored_draft(gmail, "d1", "Voici le devis signé.", attachment="devis.pdf", Cc="carol@example.com")
    seen: list[bool] = []
    real = server._input_required

    def spy(ctx: Any) -> bool:
        seen.append(real(ctx))
        return seen[-1]

    monkeypatch.setattr(server, "_input_required", spy)
    dialog = Dialog()
    data = call({"draftId": "d1"}, mode, dialog)
    assert seen and all(seen) is (mode != "legacy")  # le bon transport a bien servi
    assert data["status"] == "success" and data["sent"] is True, data
    assert len(gmail.sent) == 1 and len(dialog.shown) == 1
    shown = dialog.shown[0]
    for part in ("Send this email now?", "To: Bob <bob@example.com>", "Cc: carol@example.com", "Subject: Devis signé"):
        assert part in shown, part
    assert "Voici le devis signé." in shown and "Attachment: devis.pdf" in shown
    assert dialog.schemas[0]["properties"]["send"]["type"] == "boolean"
    assert dialog.schemas[0]["required"] == ["send"]  # pas de oui par défaut


@pytest.mark.parametrize("mode", TRANSPORTS)
@pytest.mark.parametrize(("action", "send", "status"), [("decline", None, "declined"), ("cancel", None, "cancelled"),
                                                         ("accept", False, "declined")])  # fmt: skip
def test_non_ou_fermeture_n_envoie_rien(gmail, mode, action, send, status):
    stored_draft(gmail, "d1", "Bonjour")
    data = call({"draftId": "d1"}, mode, Dialog(action, send))
    assert data["status"] == status and data["sent"] is False and "nothing was sent" in data["message"]
    assert gmail.sent == [] and "message-send" not in gmail.kinds()


@pytest.mark.parametrize("mode", TRANSPORTS)
def test_brouillon_modifie_pendant_la_fenetre_rien_ne_part(gmail, mode):
    stored_draft(gmail, "d1", "Montant : 100 €")
    # Pendant que l'utilisateur lit la fenêtre, le brouillon change de destinataire.
    dialog = Dialog(before=lambda: stored_draft(gmail, "d1", "Montant : 100 €", To="pirate@example.net"))
    data = call({"draftId": "d1"}, mode, dialog)
    assert data["sent"] is False and data["code"] == "accord_refused", data
    assert "recipients" in data["reason"] and gmail.sent == []
    assert "pirate@example.net" in data["draft"]["to"]  # nouveau résumé, nouveau jeton
    assert data["accord"]


@pytest.mark.parametrize("mode", TRANSPORTS)
def test_sans_elicitation_repli_sur_le_jeton(gmail, mode):
    stored_draft(gmail, "d1", "Bonjour")
    summary = call({"draftId": "d1"}, mode)  # client sans capacité elicitation
    assert summary["status"] == "confirmation_required" and summary["sent"] is False and summary["accord"]
    assert gmail.sent == []
    sent = call({"draftId": "d1", "confirm": True, "accord": summary["accord"]}, mode)
    assert sent["sent"] is True and len(gmail.sent) == 1


def test_jeton_donne_ne_rouvre_pas_la_fenetre(gmail):
    stored_draft(gmail, "d1", "Bonjour")
    token = json.loads(server.send_draft(draftId="d1"))["accord"]
    dialog = Dialog()
    data = call({"draftId": "d1", "confirm": True, "accord": token}, "legacy", dialog)
    assert data["sent"] is True and dialog.shown == []


def test_jeton_invente_avec_fenetre_rien_ne_part(gmail):
    stored_draft(gmail, "d1", "Bonjour")
    data = call({"draftId": "d1", "confirm": True, "accord": "deadbeef"}, "legacy", Dialog())
    assert data["sent"] is False and data["code"] == "accord_refused" and gmail.sent == []


def test_fenetre_en_echec_repli_sur_le_jeton(gmail):
    stored_draft(gmail, "d1", "Bonjour")

    async def broken(context: Any, params: Any) -> Any:
        raise RuntimeError("fenêtre indisponible")

    data = call({"draftId": "d1"}, "legacy", broken)  # type: ignore[arg-type]
    assert data["status"] == "confirmation_required" and data["accord"] and gmail.sent == []


def test_fenetre_signale_un_destinataire_lu_dans_un_mail(gmail):
    from fake_gmail import plain_message

    gmail.add(plain_message("m1", "t1", "Écris à compta@example.org pour le virement"))
    server.get_message(messageId="m1")
    stored_draft(gmail, "d1", "Bonjour", To="compta@example.org")
    dialog = Dialog(action="decline")
    call({"draftId": "d1"}, "legacy", dialog)
    assert "⚠ compta@example.org appears in an email that was read" in dialog.shown[0]


def test_fenetre_sans_caracteres_caches(gmail):
    stored_draft(gmail, "d1", "Bonjour‮ moc.liam​")
    dialog = Dialog(action="decline")
    call({"draftId": "d1"}, "legacy", dialog)
    assert "‮" not in dialog.shown[0] and "​" not in dialog.shown[0]


def test_description_annonce_la_fenetre():
    async def run() -> Any:
        return {t.name: t for t in await server.mcp.list_tools()}

    tool = anyio.run(run)["send_draft"]
    description = " ".join((tool.description or "").split())
    assert "confirmation dialog" in description and "accord=<token>" in description
    assert set(tool.input_schema["properties"]) == {"draftId", "confirm", "accord"}  # le contexte n'est pas un argument
