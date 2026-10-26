"""Point 7 : brouillon de réponse (In-Reply-To, References, « Re: », threadId), Cc/Cci, adresses validées."""

import base64
import json
from email import message_from_bytes
from email.policy import default

from fake_gmail import plain_message, stored_draft

from nexus_agymail import server


def last_draft(gmail):
    message = gmail.drafts[-1]["message"]
    return message, message_from_bytes(base64.urlsafe_b64decode(message["raw"]), policy=default)


def original(gmail, subject: str = "Facture d'octobre") -> None:
    gmail.add(
        plain_message(
            "m1",
            "t1",
            "Bonjour, voici la facture.",
            subject=subject,
            Message_ID="<orig@example.com>",
            References="<debut@example.com>",
        )
    )


def test_brouillon_de_reponse(gmail):
    original(gmail)
    data = json.loads(
        server.create_draft(
            body="Merci, bien reçu.", replyToMessageId="m1", cc="Bob <bob@example.com>", bcc="compta@example.com"
        )
    )
    assert data["status"] == "success", data
    message, parsed = last_draft(gmail)
    assert message["threadId"] == "t1"
    assert parsed["In-Reply-To"] == "<orig@example.com>"
    assert parsed["References"] == "<debut@example.com> <orig@example.com>"
    assert parsed["Subject"] == "Re: Facture d'octobre"
    assert parsed["To"] == "Alice <alice@example.com>"
    assert parsed["Cc"] == "Bob <bob@example.com>"
    assert parsed["Bcc"] == "compta@example.com"


def test_reponse_par_fil_et_re_non_double(gmail):
    original(gmail, subject="RE: Devis")
    data = json.loads(server.create_draft(body="Ok.", threadId="t1"))
    assert data["status"] == "success", data
    message, parsed = last_draft(gmail)
    assert parsed["Subject"] == "RE: Devis"
    assert parsed["In-Reply-To"] == "<orig@example.com>" and message["threadId"] == "t1"


def test_nouveau_brouillon_simple(gmail):
    data = json.loads(server.create_draft(body="Salut", to="ami@example.com", subject="Coucou"))
    assert data["status"] == "success"
    message, parsed = last_draft(gmail)
    assert "threadId" not in message and parsed["In-Reply-To"] is None


def test_adresses_invalides_refusees(gmail):
    for bad in (
        "pas-une-adresse",
        "a@b",
        "ok@example.com, n'importe quoi",
        "a@example.com\nBcc: espion@example.com",
        "",
    ):
        data = json.loads(server.create_draft(body="x", to=bad, subject="s"))
        assert data["status"] == "error" and data["code"] == "invalid_argument", (bad, data)
    data = json.loads(server.create_draft(body="x", to="ok@example.com", cc="faux", subject="s"))
    assert data["status"] == "error"
    assert gmail.drafts == []


def test_lecture_d_un_brouillon(gmail):
    stored_draft(
        gmail, "d1", "Bonjour Bob,\nle devis est signé.", attachment="devis.pdf", In_Reply_To="<x@example.com>"
    )
    data = json.loads(server.get_draft(draftId="d1"))
    assert data["status"] == "success", data
    draft = data["draft"]
    assert draft["id"] == "d1" and draft["threadId"] == "t-d1"
    assert "bob@example.com" in draft["to"] and "Devis signé" in draft["subject"]
    assert "le devis est signé." in draft["body"] and draft["body"].startswith("⟦mail:")
    assert draft["inReplyTo"] == "<x@example.com>"
    assert [a["type"] for a in draft["attachments"]] == ["application/pdf"]
    assert "devis.pdf" in draft["attachments"][0]["name"]
    assert gmail.sent == []  # lire n'envoie jamais


def test_lecture_d_un_brouillon_tronquee_et_identifiant_invalide(gmail):
    stored_draft(gmail, "d1", "x" * 500)
    draft = json.loads(server.get_draft(draftId="d1", maxChars=200))["draft"]
    assert draft["truncated"] is True and draft["body_chars"] >= 500
    data = json.loads(server.get_draft(draftId="../d1"))
    assert data["status"] == "error"
