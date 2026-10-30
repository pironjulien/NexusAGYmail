"""Fausse API Gmail au niveau HTTP (aucun réseau) : le vrai client google-api-python-client la parle.

Elle sert les routes utilisées : messages (list filtré par `q` after:/before: et labelIds, get, attachments.get,
batchModify, modify, trash, untrash, send), threads (list, get, modify, trash, untrash), labels (list, create), drafts
(list, create, get, update, send, delete), profil et historique (getProfile, history.list), envoi reprenable (/upload/)
et lots /batch/gmail/v1 ; avec pagination, format metadata/full/raw, des pannes programmables (429, 5xx) pour vérifier
les relances, et des crochets `after` (fonction appelée une fois juste après une requête d'un type donné) pour
simuler un changement concurrent.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.policy import compat32, default
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httplib2
from googleapiclient.discovery import build

API = "https://gmail.googleapis.com"
NEW_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]
V02_SCOPES = NEW_SCOPES[:2]  # jeton de la 0.2.0, sans gmail.modify
WIDE_SCOPES = [*NEW_SCOPES, "https://mail.google.com/"]
_REASONS = {
    200: "OK",
    404: "Not Found",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")  # Gmail garde le remplissage « = »


def leaf(
    mime: str,
    data: bytes,
    charset: str | None = None,
    filename: str = "",
    disposition: str | None = None,
    inline_data: bool = True,
) -> dict[str, Any]:
    """Partie MIME feuille telle que Gmail la rend (données déjà décodées du transfert, en base64url)."""
    content_type = mime + (f'; charset="{charset}"' if charset else "") + (f'; name="{filename}"' if filename else "")
    headers = [{"name": "Content-Type", "value": content_type}]
    if disposition:
        headers.append(
            {"name": "Content-Disposition", "value": disposition + (f'; filename="{filename}"' if filename else "")}
        )
    body: dict[str, Any] = {"size": len(data)}
    if filename:
        body["attachmentId"] = f"att-{filename}"
    if inline_data:
        body["data"] = b64(data)
    return {"partId": "", "mimeType": mime, "filename": filename, "headers": headers, "body": body}


def multi(mime: str, *parts: dict[str, Any]) -> dict[str, Any]:
    return {
        "partId": "",
        "mimeType": mime,
        "filename": "",
        "headers": [{"name": "Content-Type", "value": f'{mime}; boundary="limite"'}],
        "body": {"size": 0},
        "parts": list(parts),
    }


def message(
    message_id: str,
    thread_id: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    snippet: str = "",
    labels: tuple[str, ...] = ("INBOX", "UNREAD"),
) -> dict[str, Any]:
    payload = dict(payload)
    payload["headers"] = [{"name": k, "value": v} for k, v in headers.items()] + list(payload.get("headers") or [])
    return {"id": message_id, "threadId": thread_id, "labelIds": list(labels), "snippet": snippet, "payload": payload}


def plain_message(message_id: str, thread_id: str, text: str, subject: str = "Objet", **headers: str) -> dict[str, Any]:
    base = {
        "From": "Alice <alice@example.com>",
        "To": "moi@example.com",
        "Subject": subject,
        "Date": "Sat, 17 Oct 2026 09:00:00 +0200",
    }
    base.update({k.replace("_", "-"): v for k, v in headers.items()})
    return message(message_id, thread_id, leaf("text/plain", text.encode("utf-8"), "utf-8"), base, snippet=text[:100])


def _payload(part: EmailMessage) -> dict[str, Any]:
    """Partie MIME (email.message) au format payload de Gmail."""
    headers = [{"name": k, "value": str(v)} for k, v in part.items()]
    if part.is_multipart():
        children = [_payload(cast(EmailMessage, child)) for child in part.iter_parts()]
        return {"partId": "", "mimeType": part.get_content_type(), "filename": "", "headers": headers,
                "body": {"size": 0}, "parts": children}  # fmt: skip
    data = cast(bytes, part.get_payload(decode=True)) or b""
    return {
        "partId": "",
        "mimeType": part.get_content_type(),
        "filename": part.get_filename() or "",
        "headers": headers,
        "body": {"size": len(data), "data": b64(data)},
    }


def draft_message(message_id: str, sent: dict[str, Any]) -> dict[str, Any]:
    """Message de brouillon tel que Gmail le stocke, à partir du raw envoyé par le client."""
    raw = base64.urlsafe_b64decode(sent["raw"] + "=" * (-len(sent["raw"]) % 4))
    parsed = cast(EmailMessage, message_from_bytes(raw, policy=default))
    payload = _payload(parsed)
    plain = parsed.get_body(("plain",))
    snippet = str(cast(EmailMessage, plain).get_content())[:100] if plain else ""
    return {
        "id": message_id,
        "threadId": sent.get("threadId", f"t-{message_id}"),
        "labelIds": ["DRAFT"],
        "snippet": snippet,
        "payload": payload,
        "_raw": raw,
    }


def stored_draft(gmail: FakeGmail, draft_id: str, text: str, attachment: str = "", **headers: str) -> None:
    """Brouillon déjà présent dans Gmail (créé par l'utilisateur), avec pièce jointe facultative."""
    msg = EmailMessage()
    base = {"To": "Bob <bob@example.com>", "Subject": "Devis signé"}
    base.update({k.replace("_", "-"): v for k, v in headers.items()})
    for key, value in base.items():
        msg[key] = value
    msg.set_content(text)
    if attachment:
        msg.add_attachment(b"%PDF-1.7 faux", maintype="application", subtype="pdf", filename=attachment)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    gmail.add_draft(draft_id, draft_message(f"m-{draft_id}", {"raw": raw, "threadId": f"t-{draft_id}"}))


class FakeGmail:
    """Faux serveur Gmail : magasin de messages, journal des appels, pannes programmées par type de requête."""

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, list[str]] = {}
        self.drafts: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.faults: dict[str, list[int]] = {}
        self.list_params: list[dict[str, list[str]]] = []
        self.bodies: list[tuple[str, Any]] = []
        self.labels: list[dict[str, str]] = [
            {"id": i, "name": i, "type": "system"}
            for i in ("INBOX", "UNREAD", "STARRED", "IMPORTANT", "TRASH", "SPAM", "SENT", "DRAFT", "CHAT")
        ] + [
            {"id": "Label_1", "name": "Factures", "type": "user"},
            {"id": "Label_2", "name": "Clients/Acme", "type": "user"},
        ]
        self.draft_store: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.blobs: dict[str, bytes] = {}  # contenu des pièces jointes servies par attachments.get
        self.sessions: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self.history_id = 1000  # historyId courant de la boîte
        self.history_floor = 0  # positions plus anciennes : expirées (404)
        self.history: list[dict[str, Any]] = []
        self.after: dict[str, Callable[[], None]] = {}  # crochet à usage unique, par type de requête
        self.lost_sends = 0  # envois traités par Gmail dont la réponse se perd (503 rendu après l'envoi)
        self.garbled_sends = 0  # envois traités par Gmail dont la réponse 200 arrive tronquée (JSON illisible)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    def add(self, msg: dict[str, Any]) -> None:
        self.messages[msg["id"]] = msg
        self.threads.setdefault(msg["threadId"], []).append(msg["id"])

    def receive(self, msg: dict[str, Any]) -> str:
        """Message arrivé dans la boîte : ajouté et inscrit dans l'historique (messageAdded). Rend son historyId."""
        msg.setdefault("internalDate", str(int(time.time() * 1000)))  # date de réception, comme Gmail
        self.add(msg)
        self.history_id += 1
        added = {"id": msg["id"], "threadId": msg["threadId"], "labelIds": list(msg["labelIds"])}
        self.history.append({"id": str(self.history_id), "messages": [added], "messagesAdded": [{"message": added}]})
        return str(self.history_id)

    def service(self) -> Any:
        return build("gmail", "v1", http=self, cache_discovery=False, static_discovery=True)

    # --- interface httplib2.Http attendue par googleapiclient

    def request(
        self, uri: str, method: str = "GET", body: Any = None, headers: Any = None, **_: Any
    ) -> tuple[Any, bytes]:
        path = urlsplit(uri).path
        if path in ("/batch", "/batch/gmail/v1"):
            return self._batch(body, headers or {})
        if path.startswith("/upload/") or path.startswith("/fake-session/"):
            return self._upload(uri, method, body)
        status, data = self._route(method, uri, body)
        if data.get("_garbled"):
            return self._response(status), b'{"id": "s1", "thre'
        return self._response(status), json.dumps(data).encode("utf-8")

    def _hook(self, kind: str) -> None:
        if hook := self.after.pop(kind, None):
            hook()

    def _upload(self, uri: str, method: str, body: Any) -> tuple[Any, bytes]:
        """Envoi reprenable : POST/PUT de session (métadonnées JSON), puis PUT du message entier (message/rfc822),
        rejoué comme drafts.create / drafts.update avec le raw équivalent."""
        parts = urlsplit(uri)
        if parts.path.startswith("/upload/"):
            session = f"s{len(self.sessions) + 1}"
            route = parts.path.removeprefix("/upload")
            self.sessions[session] = (method, route, json.loads(body) if body else {})
            self.calls.append(("upload-session", uri))
            return self._response(200, {"location": f"{API}/fake-session/{session}"}), b""
        method0, route, metadata = self.sessions.pop(parts.path.rsplit("/", 1)[-1])
        if hasattr(body, "read"):  # tranche de flux envoyée par googleapiclient
            body = body.read()
        raw = body if isinstance(body, bytes) else str(body).encode("utf-8")
        if route.endswith("/messages/send"):  # messages.send : les métadonnées sont le message lui-même
            payload = {**metadata, "raw": b64(raw)}
        else:
            payload = {**metadata, "message": dict(metadata.get("message") or {}, raw=b64(raw))}
        status, data = self._route(method0, API + route, json.dumps(payload))
        return self._response(status), json.dumps(data).encode("utf-8")

    # --- routes

    def _fault(self, kind: str) -> int | None:
        queue = self.faults.get(kind) or []
        return queue.pop(0) if queue else None

    @staticmethod
    def _kind(method: str, segments: list[str]) -> str:
        resource, count = segments[0], len(segments)
        last = segments[-1]
        if method == "DELETE" and resource == "drafts" and count == 2:
            return "draft-delete"  # brouillon retiré après l'envoi de son contenu approuvé
        if method == "DELETE" or "batchDelete" in segments:
            return "suppression"  # jamais appelée : vérifiée par les tests
        table = {
            ("GET", "messages", 1): "list",
            ("GET", "messages", 2): "get",
            ("GET", "threads", 1): "thread-list",
            ("GET", "threads", 2): "thread",
            ("GET", "labels", 1): "labels",
            ("GET", "profile", 1): "profile",
            ("GET", "history", 1): "history",
            ("POST", "labels", 1): "label-create",
            ("POST", "drafts", 1): "draft",
            ("GET", "drafts", 1): "drafts",
            ("GET", "drafts", 2): "draft-get",
            ("PUT", "drafts", 2): "draft-update",
        }
        if (method, resource, count) in table:
            return table[(method, resource, count)]
        if method == "GET" and resource == "messages" and count == 4 and segments[2] == "attachments":
            return "attachment"
        if method == "POST" and resource == "drafts" and last == "send":
            return "draft-send"
        if method == "POST" and resource == "messages" and last == "batchModify":
            return "batchModify"
        if method == "POST" and resource == "messages" and last == "send":
            return "message-send"
        if method == "POST" and count == 3 and last in ("modify", "trash", "untrash"):
            return f"{resource[:-1]}-{last}"
        return "autre"

    def _route(self, method: str, uri: str, body: Any, in_batch: bool = False) -> tuple[int, dict[str, Any]]:
        parts = urlsplit(uri)
        query = parse_qs(parts.query)
        segments = parts.path.removeprefix("/gmail/v1/users/me/").split("/")
        kind = self._kind(method, segments)
        self.calls.append((f"lot:{kind}" if in_batch else kind, uri))
        if body:
            self.bodies.append((kind, json.loads(body)))
        fault = None if in_batch else self._fault(kind)
        if fault:
            return fault, {
                "error": {"code": fault, "message": "panne simulée", "errors": [{"reason": "rateLimitExceeded"}]}
            }
        if kind == "list":
            self.list_params.append(query)
            ids = sorted(i for i, m in self.messages.items() if self._matches(m, query))
            start = int(query.get("pageToken", ["0"])[0])
            size = int(query.get("maxResults", ["100"])[0])
            page = ids[start : start + size]
            data: dict[str, Any] = {
                "messages": [{"id": i, "threadId": self.messages[i]["threadId"]} for i in page],
                "resultSizeEstimate": len(ids),
            }
            if start + size < len(ids):
                data["nextPageToken"] = str(start + size)
            return 200, data
        if kind == "get":
            drafts = {d["message"]["id"]: d["message"] for d in self.draft_store.values()}
            msg = self.messages.get(segments[1]) or drafts.get(segments[1])
            if msg is None:
                return 404, {"error": {"code": 404, "message": "Requested entity was not found."}}
            return 200, self._format(msg, query)
        if kind == "thread":
            ids = self.threads.get(segments[1])
            if not ids:
                return 404, {"error": {"code": 404, "message": "Requested entity was not found."}}
            return 200, {"id": segments[1], "messages": [self._format(self.messages[i], query) for i in ids]}
        if kind == "draft":
            draft = json.loads(body)
            self.drafts.append(draft)
            draft_id = f"d{len(self.drafts)}"
            stored = self.add_draft(draft_id, draft_message(f"dm{len(self.drafts)}", draft["message"]))
            return 200, {"id": draft_id, "message": {"id": stored["id"], "threadId": stored["threadId"]}}
        return self._route_more(kind, segments, query, body)

    @staticmethod
    def _matches(msg: dict[str, Any], query: dict[str, list[str]]) -> bool:
        """Filtre de messages.list : chaque libellé de labelIds, et les termes after:/before: (secondes epoch, comparées
        à internalDate) et rfc822msgid: (en-tête Message-ID) de `q`. Un autre terme fait échouer le test plutôt que
        d'être ignoré en silence."""
        if any(label not in msg["labelIds"] for label in query.get("labelIds", [])):
            return False
        received = int(msg.get("internalDate") or 0) // 1000
        for term in " ".join(query.get("q", [])).split():
            if term.startswith("rfc822msgid:"):
                headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers") or []}
                if headers.get("message-id", "").strip().strip("<>") != term.split(":", 1)[1].strip("<>"):
                    return False
                continue
            match = re.fullmatch(r"(after|before):(\d+)", term)
            if not match:
                raise AssertionError(f"fake Gmail: unsupported query term {term!r}")
            bound = int(match.group(2))
            if (match.group(1) == "after" and received < bound) or (match.group(1) == "before" and received >= bound):
                return False
        return True

    def _missing(self) -> tuple[int, dict[str, Any]]:
        return 404, {"error": {"code": 404, "message": "Requested entity was not found."}}

    def _route_more(
        self, kind: str, segments: list[str], query: dict[str, list[str]], body: Any
    ) -> tuple[int, dict[str, Any]]:
        data = json.loads(body) if body else {}
        if kind == "thread-list":
            self.list_params.append(query)
            ids = sorted(self.threads)
            start = int(query.get("pageToken", ["0"])[0])
            size = int(query.get("maxResults", ["100"])[0])
            listing: dict[str, Any] = {"threads": [{"id": i} for i in ids[start : start + size]]}
            listing["resultSizeEstimate"] = len(ids)
            if start + size < len(ids):
                listing["nextPageToken"] = str(start + size)
            return 200, listing
        if kind == "profile":
            return 200, {"emailAddress": "test@example.com", "historyId": str(self.history_id), "messagesTotal": 0}
        if kind == "history":
            self.list_params.append(query)
            start = int(query["startHistoryId"][0])
            if start < self.history_floor:
                return 404, {"error": {"code": 404, "message": "Requested entity was not found."}}
            label = query.get("labelId", [""])[0]
            records = [
                r for r in self.history
                if int(r["id"]) > start and (not label or label in r["messagesAdded"][0]["message"]["labelIds"])
            ]  # fmt: skip
            first = int(query.get("pageToken", ["0"])[0])
            size = int(query.get("maxResults", ["100"])[0])
            history: dict[str, Any] = {"historyId": str(self.history_id)}
            if records[first : first + size]:
                history["history"] = records[first : first + size]
            if first + size < len(records):
                history["nextPageToken"] = str(first + size)
            return 200, history
        if kind == "attachment":
            blob = self.blobs.get(segments[3])
            if blob is None:
                return self._missing()
            return 200, {"attachmentId": segments[3], "size": len(blob), "data": b64(blob)}
        if kind == "labels":
            return 200, {"labels": [dict(label) for label in self.labels]}
        if kind == "label-create":
            if any(label["name"].casefold() == data["name"].casefold() for label in self.labels):
                return 409, {"error": {"code": 409, "message": "Label name exists or conflicts"}}
            label = {"id": f"Label_{len(self.labels) + 1}", "name": data["name"], "type": "user"}
            self.labels.append(label)
            return 200, label
        if kind == "batchModify":
            known = {label["id"] for label in self.labels}
            if any(i not in known for i in data.get("addLabelIds", []) + data.get("removeLabelIds", [])):
                return 400, {"error": {"code": 400, "message": "Invalid label"}}
            for message_id in data["ids"]:
                if message_id in self.messages:
                    self._relabel(self.messages[message_id], data)
            return 204, {}
        if kind in ("message-modify", "message-trash", "message-untrash"):
            msg = self.messages.get(segments[1])
            if msg is None:
                return self._missing()
            self._relabel(msg, self._label_change(kind, data))
            return 200, {"id": msg["id"], "threadId": msg["threadId"], "labelIds": msg["labelIds"]}
        if kind in ("thread-modify", "thread-trash", "thread-untrash"):
            ids = self.threads.get(segments[1])
            if not ids:
                return self._missing()
            for message_id in ids:
                self._relabel(self.messages[message_id], self._label_change(kind, data))
            return 200, {"id": segments[1], "messages": [{"id": i} for i in ids]}
        if kind == "drafts":
            ids = sorted(self.draft_store)
            start = int(query.get("pageToken", ["0"])[0])
            size = int(query.get("maxResults", ["100"])[0])
            page: dict[str, Any] = {
                "drafts": [
                    {"id": i, "message": {"id": self.draft_store[i]["message"]["id"]}}
                    for i in ids[start : start + size]
                ],
                "resultSizeEstimate": len(ids),
            }
            if start + size < len(ids):
                page["nextPageToken"] = str(start + size)
            return 200, page
        if kind == "draft-get":
            draft = self.draft_store.get(segments[1])
            if draft is None:
                return self._missing()
            answer = {"id": draft["id"], "message": self._format(draft["message"], query)}
            self._hook(kind)  # changement concurrent APRÈS la lecture rendue
            return 200, answer
        if kind == "draft-delete":
            if self.draft_store.pop(segments[1], None) is None:
                return self._missing()
            return 204, {}
        if kind == "message-send":
            raw = base64.urlsafe_b64decode(data["raw"] + "=" * (-len(data["raw"]) % 4))
            sent = draft_message(f"s{len(self.sent) + 1}", {"raw": data["raw"], "threadId": data.get("threadId")})
            sent["threadId"] = data.get("threadId") or sent["id"]
            sent["labelIds"] = ["SENT"]
            sent["_raw"] = raw
            sent["internalDate"] = str(int(time.time() * 1000))
            self.sent.append(sent)
            self.add(sent)
            if self.lost_sends:  # parti, mais la réponse n'arrive pas : issue inconnue pour le client
                self.lost_sends -= 1
                return 503, {"error": {"code": 503, "message": "réponse perdue"}}
            if self.garbled_sends:  # parti, réponse 200 tronquée
                self.garbled_sends -= 1
                return 200, {"_garbled": True}
            return 200, {"id": sent["id"], "threadId": sent["threadId"], "labelIds": sent["labelIds"]}
        if kind == "draft-update":
            draft = self.draft_store.get(segments[1])
            if draft is None:
                return self._missing()
            draft["message"] = draft_message(draft["message"]["id"], data["message"])
            return 200, {
                "id": draft["id"],
                "message": {"id": draft["message"]["id"], "threadId": draft["message"]["threadId"]},
            }
        if kind == "draft-send":
            draft = self.draft_store.pop(data.get("id", ""), None)
            if draft is None:
                return self._missing()
            sent = dict(draft["message"], labelIds=["SENT"])
            self.sent.append(sent)
            self.add(sent)
            return 200, {"id": sent["id"], "threadId": sent["threadId"], "labelIds": sent["labelIds"]}
        return 404, {"error": {"code": 404, "message": "route inconnue"}}

    @staticmethod
    def _label_change(kind: str, data: dict[str, Any]) -> dict[str, Any]:
        if kind.endswith("-trash"):
            return {"addLabelIds": ["TRASH"]}
        if kind.endswith("-untrash"):
            return {"removeLabelIds": ["TRASH"]}
        return data

    @staticmethod
    def _relabel(msg: dict[str, Any], change: dict[str, Any]) -> None:
        labels = [x for x in msg["labelIds"] if x not in change.get("removeLabelIds", [])]
        labels += [x for x in change.get("addLabelIds", []) if x not in labels]
        msg["labelIds"] = labels

    def add_draft(self, draft_id: str, msg: dict[str, Any]) -> dict[str, Any]:
        self.draft_store[draft_id] = {"id": draft_id, "message": msg}
        return msg

    @staticmethod
    def _format(msg: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
        if query.get("format", ["full"])[0] == "raw":  # message brut fourni par le test (clé « _raw »)
            return {"id": msg["id"], "threadId": msg["threadId"], "raw": b64(msg.get("_raw") or b"")}
        if query.get("format", ["full"])[0] != "metadata":
            return {k: v for k, v in msg.items() if k != "_raw"}
        wanted = {h.lower() for h in query.get("metadataHeaders", [])}
        headers = [h for h in msg["payload"]["headers"] if not wanted or h["name"].lower() in wanted]
        return {
            **{k: v for k, v in msg.items() if k not in ("payload", "_raw")},
            "payload": {"mimeType": msg["payload"]["mimeType"], "headers": headers},
        }

    @staticmethod
    def _response(status: int, extra: dict[str, str] | None = None) -> Any:
        return httplib2.Response(
            {"status": str(status), "content-type": "application/json; charset=UTF-8", **(extra or {})}
        )

    def _batch(self, body: Any, headers: dict[str, str]) -> tuple[Any, bytes]:
        self.calls.append(("batch", "/batch/gmail/v1"))
        fault = self._fault("batch")
        if fault:
            return self._response(fault), json.dumps({"error": {"code": fault, "message": "panne simulée"}}).encode()
        raw = body if isinstance(body, bytes) else str(body).encode("utf-8")
        content_type = next(v for k, v in headers.items() if k.lower() == "content-type")
        parsed = BytesParser(policy=compat32).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + raw)
        boundary = "limite_lot"
        chunks: list[str] = []
        for part in cast(list[Message], parsed.get_payload()):
            content_id = str(part["Content-ID"])[1:-1]
            inner = str(part.get_payload()).replace("\r\n", "\n")
            method, target, _ = inner.splitlines()[0].split(" ", 2)
            inner_body = inner.partition("\n\n")[2].strip() or None
            status, data = self._route(method, API + target, inner_body, in_batch=True)
            fault = self._fault("batch-item")
            if fault:
                status, data = fault, {"error": {"code": fault, "message": "panne simulée"}}
            chunks.append(
                f"--{boundary}\r\nContent-Type: application/http\r\nContent-ID: <response-{content_id}>\r\n\r\n"
                f"HTTP/1.1 {status} {_REASONS.get(status, 'Erreur')}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(data)}\r\n"
            )
        content = "".join(chunks) + f"--{boundary}--\r\n"
        response = httplib2.Response({"status": "200", "content-type": f"multipart/mixed; boundary={boundary}"})
        return response, content.encode("utf-8")


# --- faux jeton


def fake_token(scopes: list[str], expired: bool = False) -> dict[str, Any]:
    """Faux jeton au format google-auth (aucune valeur réelle)."""
    expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=-1 if expired else 1)
    return {
        "token": "faux-jeton-acces",
        "refresh_token": "faux-jeton-renouvellement",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "faux-client.apps.googleusercontent.com",
        "client_secret": "faux-secret",
        "scopes": scopes,
        "account": "test@example.com",
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
    }


def write_legacy_token(state: Path, info: dict[str, Any]) -> Path:
    path = state / "token.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path
