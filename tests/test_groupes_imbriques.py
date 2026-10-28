"""En-têtes d'adresses à groupes « : » à la chaîne (fuzzing du 28/10) : le lecteur d'adresses de Python s'appelle
lui-même par groupe et dépassait la pile. Une adresse à envoyer est refusée, un en-tête reçu est illisible, jamais un
plantage."""

from __future__ import annotations

import pytest

from nexus_agymail import client, server

NESTED = "a:" * 2000


def test_liste_a_envoyer_refusee():
    with pytest.raises(ValueError, match="nested address groups"):
        client.normalize_addresses(NESTED, "to")


def test_expediteur_recu_illisible():
    assert client.plain_address(NESTED) == ""


def test_destinataires_du_brouillon_gardes_bruts():
    assert server._role_addresses({"to": NESTED}) == [("To", NESTED.strip())]
