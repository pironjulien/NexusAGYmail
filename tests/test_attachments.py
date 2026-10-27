"""Pièces jointes : enregistrer (nom sûr, sans écrasement, marque Internet), joindre (fichiers locaux, secrets
refusés, 25 Mo), garder à la mise à jour, et accord d'envoi lié au contenu exact de chaque pièce."""

import base64
import hashlib
import json
import os
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from pathlib import Path
from typing import cast

import pytest
from fake_gmail import leaf, message, multi, stored_draft

from nexus_agymail import fichiers, server

PDF = b"%PDF-1.7 " + bytes(range(256)) * 40


def mail_with_attachments(gmail):
    payload = multi(
        "multipart/mixed",
        leaf("text/plain", "Voir les pièces jointes.".encode(), "utf-8"),
        leaf("application/pdf", PDF, filename="facture.pdf", disposition="attachment", inline_data=False),
        leaf("text/csv", "é;1\r\n".encode("cp1252"), filename="../../Startup/relevé.csv", disposition="attachment"),
    )
    gmail.blobs["att-facture.pdf"] = PDF
    headers = {"From": "Alice <alice@example.com>", "To": "moi@example.com", "Subject": "Factures", "Date": "x"}
    gmail.add(message("m1", "t1", payload, headers))


def raw_of(body):
    return message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]), policy=default)


# --- lecture et enregistrement


def test_lecture_rend_part_id_puis_enregistrement(gmail, isolated_home):
    mail_with_attachments(gmail)
    read = json.loads(server.get_message(messageId="m1"))
    parts = [a["part_id"] for a in read["message"]["attachments"]]
    assert parts == ["1", "2"]
    data = json.loads(server.download_attachment(messageId="m1", partId="1"))
    assert data["status"] == "success", data
    path = Path(data["path"])
    assert path.parent == isolated_home / "Downloads" / "NexusAGYmail" and path.name == "facture.pdf"
    assert path.read_bytes() == PDF and data["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert data["name"].startswith("⟦mail:") and "Never run it" in data["note"]
    assert "attachment" in gmail.kinds()
    if os.name == "nt":
        assert data["mark_of_the_web"] is True
        assert "ZoneId=3" in Path(f"{path}:Zone.Identifier").read_text(encoding="ascii")
    again = json.loads(server.download_attachment(messageId="m1", partId="1"))
    assert Path(again["path"]).name == "facture (2).pdf" and path.read_bytes() == PDF  # jamais d'écrasement


def test_nom_hostile_nettoye_et_octets_intacts(gmail, tmp_path):
    mail_with_attachments(gmail)
    data = json.loads(server.download_attachment(messageId="m1", partId="2", directory=str(tmp_path / "x")))
    path = Path(data["path"])
    assert path.parent == tmp_path / "x" and path.name == "relevé.csv"
    assert path.read_bytes() == "é;1\r\n".encode("cp1252")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("..\\..\\évil.exe", "évil.exe"),
        ("fact‮FDP.exe", "factFDP.exe"),  # inversion de sens retirée
        ("CON.txt", "_CON.txt"),
        ('a<b>:c"|?*.pdf', "a_b__c____.pdf"),
        ("...", "attachment"),
        ("nom. ", "nom"),
        ("x" * 300 + ".pdf", "x" * 146 + ".pdf"),
    ],
)
def test_noms_surs(name, expected):
    assert fichiers.safe_name(name) == expected


def test_enregistrement_refus_clairs(gmail):
    mail_with_attachments(gmail)
    bad = json.loads(server.download_attachment(messageId="m1", partId="9"))
    assert bad["code"] == "invalid_argument" and "1, 2" in bad["error"]
    rel = json.loads(server.download_attachment(messageId="m1", partId="1", directory="relatif"))
    assert rel["code"] == "invalid_argument"
    missing = json.loads(server.download_attachment(messageId="absent", partId="1"))
    assert missing["code"] == "not_found"


# --- joindre


def files(tmp_path: Path) -> tuple[Path, Path]:
    pdf = tmp_path / "devis.pdf"
    pdf.write_bytes(PDF)
    txt = tmp_path / "notes.txt"
    txt.write_bytes("ligne é\r\n".encode("cp1252"))
    return pdf, txt


def test_brouillon_avec_pieces_jointes_par_envoi_reprenable(gmail, tmp_path):
    pdf, txt = files(tmp_path)
    data = json.loads(
        server.create_draft(body="Ci-joint.", to="bob@example.com", subject="Devis", attachments=[str(pdf), str(txt)])
    )
    assert data["status"] == "success", data
    assert [(a["name"], a["size"]) for a in data["draft"]["attachments"]] == [("devis.pdf", len(PDF)), ("notes.txt", 9)]
    assert "upload-session" in gmail.kinds()
    parsed = raw_of(gmail.drafts[-1])
    got = {p.get_filename(): (p.get_content_type(), p.get_payload(decode=True)) for p in parsed.iter_attachments()}
    assert got == {"devis.pdf": ("application/pdf", PDF), "notes.txt": ("text/plain", "ligne é\r\n".encode("cp1252"))}


def test_brouillon_sans_piece_jointe_reste_en_json(gmail):
    json.loads(server.create_draft(body="Salut", to="ami@example.com", subject="Coucou"))
    assert "upload-session" not in gmail.kinds() and "raw" in gmail.drafts[-1]["message"]


def test_secrets_jamais_joints(gmail, isolated_home, tmp_path, monkeypatch):
    ssh = isolated_home / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("clé", encoding="utf-8")
    vault = tmp_path / "vault"  # dossier de secrets propre à l'utilisateur (NEXUS_AGYMAIL_PROTECTED)
    monkeypatch.setenv("NEXUS_AGYMAIL_PROTECTED", str(vault))
    vault.mkdir(parents=True)
    (vault / "cles.txt").write_text("x", encoding="utf-8")
    env = tmp_path / "projet" / ".env"
    env.parent.mkdir()
    env.write_text("API=1", encoding="utf-8")
    for path in (ssh / "id_ed25519", vault / "cles.txt", env, isolated_home / ".nexus" / "nexus-agymail" / "token.bin"):
        data = json.loads(server.create_draft(body="x", to="a@example.com", subject="s", attachments=[str(path)]))
        assert data["code"] == "invalid_argument" and "secrets" in data["error"], (path, data)
    assert gmail.drafts == []


def test_fichier_absent_relatif_ou_trop_gros(gmail, tmp_path, monkeypatch):
    for bad in (str(tmp_path / "absent.pdf"), "relatif.pdf", str(tmp_path)):
        data = json.loads(server.create_draft(body="x", to="a@example.com", subject="s", attachments=[bad]))
        assert data["code"] == "invalid_argument", (bad, data)
    pdf, txt = files(tmp_path)
    monkeypatch.setattr(fichiers, "MAX_ATTACH_BYTES", len(PDF) + 5)
    data = json.loads(server.create_draft(body="x", to="a@example.com", subject="s", attachments=[str(pdf), str(txt)]))
    assert data["code"] == "invalid_argument" and "25 MB" in data["error"]
    assert gmail.drafts == []


# --- mise à jour


def test_mise_a_jour_garde_ajoute_et_retire(gmail, tmp_path):
    stored_draft(gmail, "d1", "Ci-joint le devis.", attachment="devis-signe.pdf")
    pdf, txt = files(tmp_path)
    data = json.loads(server.update_draft(draftId="d1", body="Nouveau texte", attachments=[str(txt)]))
    assert data["status"] == "success", data
    body = [b for kind, b in gmail.bodies if kind == "draft-update"][-1]
    names = [p.get_filename() for p in raw_of(body).iter_attachments()]
    assert names == ["devis-signe.pdf", "notes.txt"]
    json.loads(server.update_draft(draftId="d1", body="Sans", removeAttachments=["devis-signe.pdf"]))
    body = [b for kind, b in gmail.bodies if kind == "draft-update"][-1]
    assert [p.get_filename() for p in raw_of(body).iter_attachments()] == ["notes.txt"]
    bad = json.loads(server.update_draft(draftId="d1", body="x", removeAttachments=["absent.pdf"]))
    assert bad["code"] == "invalid_argument" and "notes.txt" in bad["error"]


# --- envoi : accord lié au contenu exact des pièces


def swap_attachment(gmail, draft_id: str, content: bytes) -> None:
    """Même nom, même taille, autre contenu : seule l'empreinte le voit."""
    msg = EmailMessage()
    msg["To"], msg["Subject"] = "Bob <bob@example.com>", "Devis signé"
    msg.set_content("Ci-joint.")
    msg.add_attachment(content, maintype="application", subtype="pdf", filename="devis.pdf")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    from fake_gmail import draft_message

    gmail.add_draft(draft_id, draft_message(f"m-{draft_id}", {"raw": raw, "threadId": f"t-{draft_id}"}))


def test_piece_jointe_echangee_a_taille_egale_annule_l_accord(gmail):
    swap_attachment(gmail, "d1", b"A" * 100)
    summary = json.loads(server.send_draft(draftId="d1"))
    assert summary["code"] == "confirmation"
    swap_attachment(gmail, "d1", b"B" * 100)
    refused = json.loads(server.send_draft(draftId="d1", confirm=True, accord=summary["accord"]))
    assert refused["sent"] is False and refused["code"] == "accord_refused", refused
    assert "attachments" in refused["reason"] or "parts" in refused["reason"]
    assert gmail.sent == []
    ok = json.loads(server.send_draft(draftId="d1", confirm=True, accord=refused["accord"]))
    assert ok["sent"] is True and len(gmail.sent) == 1


# --- relecture de Codex (0.5.0)


@pytest.mark.parametrize(
    "relative",
    [".env.local", "app/.env.production", "prod.env", ".claude.json", ".claude/settings.json", "certs/serveur.pem",
     ".git-credentials", ".config/gh/hosts.yml"],
)  # fmt: skip
def test_famille_des_secrets_refusee(isolated_home, relative):
    path = isolated_home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("SYNTHETIC_TEST_ONLY", encoding="utf-8")
    with pytest.raises(ValueError, match="secrets"):
        fichiers.load(str(path))


def test_modeles_env_et_fichiers_ordinaires_permis(tmp_path):
    for name in (".env.example", "environnement.txt", "rapport.pdf"):
        path = tmp_path / name
        path.write_text("x", encoding="utf-8")
        assert fichiers.load(str(path))[0] == name


def test_sans_lien_physique_jamais_d_ecrasement(tmp_path, monkeypatch):
    """Disque sans liens physiques et autre écrivain qui prend le nom juste avant : son fichier reste intact."""

    def no_link(src, dst):
        if Path(dst).name == "doc.pdf":  # course : ce nom est pris entre le choix et l'écriture
            Path(dst).write_bytes(b"AUTRE ECRIVAIN")
        raise OSError("liens physiques non pris en charge")

    monkeypatch.setattr(os, "link", no_link)
    if os.name == "nt":
        path, _ = fichiers.save(b"PIECE", "doc.pdf", tmp_path)
        assert (tmp_path / "doc.pdf").read_bytes() == b"AUTRE ECRIVAIN"
        assert path.name != "doc.pdf" and path.read_bytes() == b"PIECE"
    else:
        with pytest.raises(ValueError):
            fichiers.save(b"PIECE", "doc.pdf", tmp_path)
        assert (tmp_path / "doc.pdf").read_bytes() == b"AUTRE ECRIVAIN"
    assert not list(tmp_path.glob(".nexus-agymail-*"))


def forwarded_draft(gmail, with_raw: bool) -> bytes:
    """Brouillon avec un message joint (message/rfc822) décrit par ses seules sous-parties, sans octets : forme
    possible côté Gmail, qui oblige à relire le message brut."""
    outer = (
        b'To: Bob <bob@example.com>\r\nSubject: Tr: Contrat\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="B1"\r\n\r\n'
        b"--B1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nVoir le message joint.\r\n"
        b'--B1\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename="contrat.eml"\r\n\r\n'
        + INNER
        + b"\r\n--B1--\r\n"
    )
    payload = multi(
        "multipart/mixed",
        leaf("text/plain", b"Voir le message joint.\n", "utf-8"),
        {
            "partId": "",
            "mimeType": "message/rfc822",
            "filename": "contrat.eml",
            "headers": [{"name": "Content-Type", "value": "message/rfc822"}],
            "body": {"size": 812},
            "parts": [leaf("text/plain", b"Texte du message transfere.", "utf-8")],
        },
    )
    msg = message("m-d1", "t-d1", payload, {"To": "Bob <bob@example.com>", "Subject": "Tr: Contrat"}, labels=("DRAFT",))
    if with_raw:
        msg["_raw"] = outer
    gmail.add_draft("d1", msg)
    return INNER


# Message transféré tel qu'il circule (CRLF) : rendu à l'octet près, jamais re-sérialisé.
INNER = b"From: Carole <carole@example.com>\r\nSubject: Contrat\r\n\r\nLine one.\r\nLine two.\r\n"


def forwarded_check(gmail) -> None:
    body = [b for kind, b in gmail.bodies if kind == "draft-update"][-1]
    part = next(raw_of(body).iter_attachments())
    assert part.get_content_type() == "message/rfc822"
    forwarded = cast(list[EmailMessage], part.get_payload())[0]
    assert forwarded["From"] == "Carole <carole@example.com>" and forwarded["Subject"] == "Contrat"
    assert str(forwarded.get_content()).splitlines() == ["Line one.", "Line two."]


def test_message_joint_relu_brut_et_garde_a_la_mise_a_jour(gmail):
    inner = forwarded_draft(gmail, with_raw=True)
    saved = json.loads(server.download_attachment(messageId="m-d1", partId="1"))
    assert saved["status"] == "success" and Path(saved["path"]).read_bytes() == inner  # CRLF intacts
    data = json.loads(server.update_draft(draftId="d1", body="Nouveau texte"))
    assert data["status"] == "success", data
    forwarded_check(gmail)
    body = [b for kind, b in gmail.bodies if kind == "draft-update"][-1]
    assert inner in base64.urlsafe_b64decode(body["message"]["raw"])  # rejoint octet pour octet
    again = json.loads(server.download_attachment(messageId="m-d1", partId="1"))
    assert Path(again["path"]).read_bytes() == inner


def test_message_joint_a_la_racine(gmail):
    """Racine message/rfc822 (partId vide) : référence « racine », jamais « 0 » (qui serait son premier enfant)."""
    payload = {
        "partId": "",
        "mimeType": "message/rfc822",
        "filename": "fwd.eml",
        "headers": [{"name": "Content-Type", "value": "message/rfc822"}],
        "body": {"size": 142},
        "parts": [leaf("text/plain", b"Line one.\r\nLine two.\r\n", "utf-8")],
    }
    msg = message("m-d1", "t-d1", payload, {"To": "Bob <bob@example.com>", "Subject": "Fwd"}, labels=("DRAFT",))
    msg["_raw"] = b"To: Bob <bob@example.com>\r\nSubject: Fwd\r\nContent-Type: message/rfc822\r\n\r\n" + INNER
    gmail.add_draft("d1", msg)
    read = json.loads(server.get_message(messageId="m-d1"))
    assert [a["part_id"] for a in read["message"]["attachments"]] == ["racine"]
    saved = json.loads(server.download_attachment(messageId="m-d1", partId="racine"))
    assert saved["status"] == "success" and Path(saved["path"]).read_bytes() == INNER
    data = json.loads(server.update_draft(draftId="d1", body="Texte", to="bob@example.com"))
    assert data["status"] == "success", data
    forwarded_check(gmail)


def test_type_inattendu_ou_encodage_douteux_refuse(gmail):
    forwarded_draft(gmail, with_raw=True)
    raw = gmail.draft_store["d1"]["message"]["_raw"]
    gmail.draft_store["d1"]["message"]["_raw"] = raw.replace(
        b"Content-Type: message/rfc822\r\n", b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n"
    )
    data = json.loads(server.update_draft(draftId="d1", body="x"))
    assert data["status"] == "error" and "nothing was changed" in data["error"]
    gmail.draft_store["d1"]["message"]["_raw"] = raw.replace(b"message/rfc822", b"text/plain")
    data = json.loads(server.update_draft(draftId="d1", body="x"))
    assert data["status"] == "error" and "draft-update" not in gmail.kinds()


def test_piece_illisible_refusee_sans_rien_modifier(gmail):
    forwarded_draft(gmail, with_raw=False)
    data = json.loads(server.update_draft(draftId="d1", body="Nouveau texte"))
    assert data["status"] == "error" and "contrat.eml" in data["error"] and "nothing was changed" in data["error"]
    assert "draft-update" not in gmail.kinds() and "upload-session" not in gmail.kinds()
    summary = json.loads(server.send_draft(draftId="d1"))
    assert summary["status"] == "error" and gmail.sent == []


def test_piece_tronquee_refusee(gmail):
    mail_with_attachments(gmail)
    gmail.blobs["att-facture.pdf"] = PDF[:-10]
    data = json.loads(server.download_attachment(messageId="m1", partId="1"))
    assert data["status"] == "error" and "could not be read in full" in data["error"]


INNER_8BIT = (
    "From: Café <nobody@example.invalid>\r\nSubject: Réunion\r\nContent-Type: text/plain; charset=utf-8\r\n"
    "Content-Transfer-Encoding: 8bit\r\n\r\nUn café à 10 h.\r\n"
).encode()


def test_eml_8bit_et_ordres_melanges(gmail, tmp_path):
    """Pièce ordinaire avant un .eml, deux .eml, .eml UTF-8 en 8 bits : composés, octets intacts partout."""
    pdf, _ = files(tmp_path)
    eml1, eml2 = tmp_path / "a.eml", tmp_path / "b.eml"
    eml1.write_bytes(INNER_8BIT)
    eml2.write_bytes(INNER)
    data = json.loads(
        server.create_draft(body="x", to="bob@example.com", subject="s", attachments=[str(pdf), str(eml1), str(eml2)])
    )
    assert data["status"] == "success", data
    assert [(a["name"], a["size"]) for a in data["draft"]["attachments"]] == [
        ("devis.pdf", len(PDF)), ("a.eml", len(INNER_8BIT)), ("b.eml", len(INNER)),
    ]  # fmt: skip
    raw = base64.urlsafe_b64decode(gmail.drafts[-1]["message"]["raw"])
    assert INNER_8BIT in raw and INNER in raw and b"NEXUS-AGYMAIL-EML" not in raw
    draft_id = data["draft"]["id"]
    message_id = gmail.draft_store[draft_id]["message"]["id"]
    for ref, expected in (("2", INNER_8BIT), ("3", INNER)):
        saved = json.loads(server.download_attachment(messageId=message_id, partId=ref))
        assert saved["status"] == "success" and Path(saved["path"]).read_bytes() == expected, saved
    data = json.loads(server.update_draft(draftId=draft_id, body="Nouveau"))
    assert data["status"] == "success", data
    body = [b for kind, b in gmail.bodies if kind == "draft-update"][-1]
    raw = base64.urlsafe_b64decode(body["message"]["raw"])
    assert INNER_8BIT in raw and INNER in raw and PDF not in raw  # PDF en base64
    for ref, expected in (("2", INNER_8BIT), ("3", INNER)):
        saved = json.loads(server.download_attachment(messageId=message_id, partId=ref))
        assert Path(saved["path"]).read_bytes() == expected
