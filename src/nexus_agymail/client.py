"""Client de l'API Gmail v1 : recherche, lecture, fils, brouillons, libellés, rangement, corbeille, envoi confirmé.

Aucune suppression définitive : ni delete, ni batchDelete, ni vidage de corbeille (la portée gmail.modify ne les
permet d'ailleurs pas). L'envoi ne passe que par un brouillon existant (drafts.send), après confirmation.

Chaque requête est relancée avec attente exponentielle sur 429 et 5xx (num_retries de google-api-python-client) ;
les requêtes groupées (BatchHttpRequest) relancent elles-mêmes les seuls éléments refusés pour quota.
"""

from __future__ import annotations

import base64
import hashlib
import io
import random
import re
import secrets
import socket
import time
import weakref
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parseaddr
from typing import Any

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from . import fichiers, mime
from .auth import REAUTH_HINT, GmailAuthError, get_credentials

NUM_RETRIES = 5
MAX_RESULTS_LIMIT = 100
THREADS_LIMIT = 50  # comme search_threads du serveur Gmail officiel
THREAD_MESSAGES = 5  # messages résumés par fil dans une recherche (les plus récents)
BATCH_SIZE = 50  # Gmail conseille au plus 50 requêtes par lot
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
SUMMARY_HEADERS = ["From", "To", "Subject", "Date"]
REPLY_HEADERS = ["Message-ID", "References", "Subject", "From", "Reply-To", "To", "Cc"]


def get_service() -> Any:
    """Client de l'API Gmail (découverte embarquée, aucune requête réseau à la construction)."""
    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _backoff(attempt: int) -> float:
    return min(32.0, 2.0**attempt) * random.uniform(0.5, 1.0)


def _status(exc: BaseException) -> int:
    return int(getattr(getattr(exc, "resp", None), "status", 0) or 0)


def _execute(request: Any) -> Any:
    return request.execute(num_retries=NUM_RETRIES)


def _execute_batch(batch: Any) -> None:
    for attempt in range(NUM_RETRIES + 1):
        try:
            batch.execute()
            return
        except HttpError as exc:
            if _status(exc) not in RETRY_STATUSES or attempt == NUM_RETRIES:
                raise
        _sleep(_backoff(attempt + 1))


def _batch_run(
    service: Any, ids: list[str], make: Callable[[str], Any]
) -> tuple[dict[str, Any], dict[str, BaseException]]:
    """Requêtes groupées (une par identifiant, lots de 50) ; les éléments refusés en 429/5xx sont relancés avec
    attente exponentielle. Renvoie (réponses par identifiant, erreurs définitives par identifiant)."""
    results: dict[str, Any] = {}
    failures: dict[str, BaseException] = {}
    retry: list[str] = []

    def callback(request_id: str, response: Any, exception: Any) -> None:
        if exception is None:
            results[request_id] = response
        elif isinstance(exception, HttpError) and _status(exception) in RETRY_STATUSES:
            retry.append(request_id)
        else:
            failures[request_id] = exception

    pending = list(dict.fromkeys(ids))
    for attempt in range(NUM_RETRIES + 1):
        retry.clear()
        for start in range(0, len(pending), BATCH_SIZE):
            batch = service.new_batch_http_request(callback=callback)
            for item in pending[start : start + BATCH_SIZE]:
                batch.add(make(item), request_id=item)
            _execute_batch(batch)
        if not retry:
            return results, failures
        if attempt == NUM_RETRIES:
            break
        _sleep(_backoff(attempt + 1))
        pending = list(retry)
    raise GmailApiError(f"Gmail API: quota exceeded (429) after {NUM_RETRIES} retries, try again later")


def _batch_get(service: Any, ids: list[str], **params: Any) -> dict[str, dict[str, Any]]:
    """messages.get groupés ; la première erreur définitive est levée."""
    results, failures = _batch_run(
        service, ids, lambda message_id: service.users().messages().get(userId="me", id=message_id, **params)
    )
    if failures:
        raise next(iter(failures.values()))
    return results


class GmailApiError(Exception):
    """Erreur de l'API Gmail déjà formulée pour l'agent."""


class LabelError(ValueError):
    """Libellé introuvable ou réservé, avec un message qui dit quoi faire."""


def _summary(meta: dict[str, Any]) -> dict[str, Any]:
    headers = (meta.get("payload") or {}).get("headers") or []
    return {
        "id": meta.get("id"),
        "threadId": meta.get("threadId"),
        "date": mime.header(headers, "Date"),
        "from": mime.header(headers, "From"),
        "to": mime.header(headers, "To"),
        "subject": mime.header(headers, "Subject"),
        "snippet": meta.get("snippet", ""),
        "labels": meta.get("labelIds", []),
    }


def clamp_max_results(value: int) -> int:
    return max(1, min(MAX_RESULTS_LIMIT, int(value)))


def search_messages(
    query: str = "is:unread", max_results: int = 10, page_token: str | None = None, service: Any = None
) -> dict[str, Any]:
    """Une page de résultats d'une requête Gmail ; nextPageToken donne la page suivante."""
    svc = service or get_service()
    count = clamp_max_results(max_results)
    params: dict[str, Any] = {"userId": "me", "q": query, "maxResults": count}
    if page_token:
        params["pageToken"] = page_token
    page = _execute(svc.users().messages().list(**params))
    ids = [ref["id"] for ref in page.get("messages") or []]
    metas = _batch_get(svc, ids, format="metadata", metadataHeaders=SUMMARY_HEADERS) if ids else {}
    return {
        "messages": [_summary(metas[i]) for i in ids if i in metas],
        "nextPageToken": page.get("nextPageToken"),
        "resultSizeEstimate": page.get("resultSizeEstimate"),
        "max_results": count,
    }


def search_threads(
    query: str = "",
    page_size: int = 20,
    page_token: str | None = None,
    include_trash: bool = False,
    service: Any = None,
) -> dict[str, Any]:
    """Une page de fils pour une requête Gmail (comme search_threads du serveur officiel) : chaque fil donne son
    nombre de messages et le résumé des plus récents (sans corps), lus en requêtes groupées (format metadata)."""
    svc = service or get_service()
    count = max(1, min(THREADS_LIMIT, int(page_size)))
    params: dict[str, Any] = {"userId": "me", "q": query or "", "maxResults": count}
    if include_trash:  # l'API mêle corbeille et spam : le spam reste exclu
        params.update(includeSpamTrash=True, q=f"{query or ''} -in:spam".strip())
    if page_token:
        params["pageToken"] = page_token
    page = _execute(svc.users().threads().list(**params))
    ids = [ref["id"] for ref in page.get("threads") or []]
    found, failures = _batch_run(
        svc,
        ids,
        lambda thread_id: (
            svc.users().threads().get(userId="me", id=thread_id, format="metadata", metadataHeaders=SUMMARY_HEADERS)
        ),
    )
    if failures:
        raise next(iter(failures.values()))
    threads = []
    for thread_id in ids:
        if thread_id not in found:
            continue
        messages = found[thread_id].get("messages") or []
        threads.append(
            {
                "id": thread_id,
                "messageCount": len(messages),
                "messages": [_summary(m) for m in messages[-THREAD_MESSAGES:]],
            }
        )
    return {
        "threads": threads,
        "nextPageToken": page.get("nextPageToken"),
        "resultSizeEstimate": page.get("resultSizeEstimate"),
        "page_size": count,
    }


def _full(msg: dict[str, Any], drop_quotes: bool) -> dict[str, Any]:
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    content = mime.extract(payload, drop_quotes=drop_quotes)
    result = _summary(msg)
    result["cc"] = mime.header(headers, "Cc")
    result.update(content)
    return result


def get_message(message_id: str, service: Any = None) -> dict[str, Any]:
    """Message complet : en-têtes, texte (HTML converti), pièces jointes listées sans contenu."""
    svc = service or get_service()
    return _full(_execute(svc.users().messages().get(userId="me", id=message_id, format="full")), drop_quotes=False)


def get_thread(thread_id: str, service: Any = None) -> dict[str, Any]:
    """Fil complet dans l'ordre, citations des réponses précédentes retirées."""
    svc = service or get_service()
    thread = _execute(svc.users().threads().get(userId="me", id=thread_id, format="full"))
    return {"id": thread.get("id"), "messages": [_full(m, drop_quotes=True) for m in thread.get("messages") or []]}


# --- suivi incrémental (users.history.list)

HISTORY_PAGE = 500  # enregistrements par page d'historique (maximum de l'API)
SKIPPED_LABELS = frozenset({"DRAFT", "SPAM", "TRASH"})  # jamais des « nouveaux messages »


class HistoryExpired(Exception):
    """Position d'historique trop ancienne ou invalide (404 de users.history.list) : repartir de maintenant."""


def get_profile(service: Any = None) -> dict[str, Any]:
    """Adresse du compte et historyId courant de la boîte (users.getProfile)."""
    svc = service or get_service()
    profile = _execute(svc.users().getProfile(userId="me"))
    return {"emailAddress": str(profile.get("emailAddress") or ""), "historyId": str(profile.get("historyId") or "")}


def _kept(labels: Any) -> bool:
    return not SKIPPED_LABELS & set(labels or [])


def list_history(
    start_history_id: str, label_id: str = "INBOX", limit: int = 20, service: Any = None
) -> dict[str, Any]:
    """Messages ajoutés depuis start_history_id (exclu), dans l'ordre d'arrivée, enregistrements entiers.

    Au plus `limit` messages (un enregistrement n'est jamais coupé) : s'il en reste, `more` et la position rendue est
    celle du dernier enregistrement lu ; sinon la position courante de la boîte. HistoryExpired si Gmail ne connaît
    plus la position (historique gardé au moins une semaine environ, parfois moins)."""
    svc = service or get_service()
    start = str(start_history_id or "").strip()
    if not start.isdigit():
        raise ValueError(f"startHistoryId must be a number, got {start!r}")
    count = clamp_max_results(limit)
    params: dict[str, Any] = {
        "userId": "me",
        "startHistoryId": start,
        "historyTypes": ["messageAdded"],
        "maxResults": HISTORY_PAGE,
    }
    if label_id:
        params["labelId"] = label_id
    ids: list[str] = []
    while True:
        try:
            page = _execute(svc.users().history().list(**params))
        except HttpError as exc:
            if _status(exc) == 404:
                raise HistoryExpired(f"history position {start} is too old or unknown") from exc
            raise
        records = page.get("history") or []
        for index, record in enumerate(records):
            for added in record.get("messagesAdded") or []:
                msg = added.get("message") or {}
                if msg.get("id") and msg["id"] not in ids and _kept(msg.get("labelIds")):
                    ids.append(msg["id"])
            left = index + 1 < len(records) or bool(page.get("nextPageToken"))
            if len(ids) >= count and left:
                return {"ids": ids, "historyId": str(record.get("id") or start), "more": True}
        if not page.get("nextPageToken"):
            return {"ids": ids, "historyId": str(page.get("historyId") or start), "more": False}
        params["pageToken"] = page["nextPageToken"]


def messages_since(after_epoch: int, label_id: str = "INBOX", limit: int = 20, service: Any = None) -> dict[str, Any]:
    """Repli quand l'historique a expiré : messages reçus depuis une date (recherche `after:`), plus récents
    d'abord, au plus `limit`."""
    svc = service or get_service()
    params: dict[str, Any] = {"userId": "me", "q": f"after:{int(after_epoch)}", "maxResults": clamp_max_results(limit)}
    if label_id:
        params["labelIds"] = [label_id]
    page = _execute(svc.users().messages().list(**params))
    ids = [ref["id"] for ref in page.get("messages") or [] if ref.get("id")]
    return {"ids": ids, "more": bool(page.get("nextPageToken"))}


def summaries(ids: list[str], service: Any = None) -> list[dict[str, Any]]:
    """Résumés (sans corps) dans l'ordre donné ; un message supprimé entre-temps (404), en brouillon, spam ou
    corbeille est omis."""
    svc = service or get_service()
    found, failures = _batch_run(
        svc,
        ids,
        lambda message_id: (
            svc.users().messages().get(userId="me", id=message_id, format="metadata", metadataHeaders=SUMMARY_HEADERS)
        ),
    )
    for failure in failures.values():
        if not (isinstance(failure, HttpError) and _status(failure) == 404):
            raise failure
    return [_summary(found[i]) for i in ids if i in found and _kept(found[i].get("labelIds"))]


# --- pièces jointes


class _RawMessage:
    """Message brut (format raw), lu au plus une fois et seulement si une pièce jointe n'a pas ses octets dans le
    payload (message joint message/rfc822 décrit par ses seules sous-parties)."""

    def __init__(self, svc: Any, message_id: str) -> None:
        self.svc, self.message_id = svc, message_id
        self.raw: bytes | None = None

    def part(self, ref: str, expected_type: str) -> bytes | None:
        """Octets exacts de la partie (découpe du brut, type vérifié) ; None si introuvable ou douteuse."""
        if self.raw is None:
            got = _execute(self.svc.users().messages().get(userId="me", id=self.message_id, format="raw"))
            self.raw = mime.b64decode(got.get("raw") or "")
        return mime.raw_part(self.raw, ref, expected_type)


def _part_bytes(svc: Any, message_id: str, ref: str, part: dict[str, Any], raw: _RawMessage) -> bytes:
    """Contenu exact d'une pièce jointe (en ligne, attachments.get, ou message brut) ; GmailApiError si ses octets
    ne peuvent pas être récupérés en entier : jamais une pièce vide ou tronquée à sa place."""
    body = part.get("body") or {}
    size = int(body.get("size") or 0)
    name = part.get("filename") or "(sans nom)"
    data: bytes | None = None
    if body.get("data"):
        data = mime.b64decode(body["data"])
    elif attachment_id := body.get("attachmentId"):
        got = _execute(svc.users().messages().attachments().get(userId="me", messageId=message_id, id=attachment_id))
        data = mime.b64decode(got.get("data") or "")
    elif (mime_type := (part.get("mimeType") or "").lower()) == "message/rfc822":  # relu dans le message brut
        return _require(raw.part(ref, mime_type) or None, name, size, exact=False)  # taille annoncée ≠ octets bruts
    elif size or part.get("parts"):
        data = raw.part(ref, mime_type)
    else:
        data = b""
    return _require(data, name, size, exact=True)


def _require(data: bytes | None, name: str, size: int, exact: bool) -> bytes:
    if data is None or (exact and size and len(data) != size):
        got = "nothing" if data is None else f"{len(data)} bytes"
        raise GmailApiError(
            f"attachment {name!r} could not be read in full ({got} of {size} announced): nothing was changed"
        )
    return data


def _message_attachments(svc: Any, message_id: str, payload: dict[str, Any]) -> list[tuple[dict[str, Any], bytes]]:
    """(description, contenu) de chaque pièce jointe d'un message déjà lu en format full."""
    raw = _RawMessage(svc, message_id)
    return [
        (mime.attachment_info(ref, part), _part_bytes(svc, message_id, ref, part, raw))
        for ref, part in mime.attachment_parts(payload)
    ]


def download_attachment(message_id: str, part_id: str, service: Any = None) -> tuple[dict[str, Any], bytes]:
    """Une pièce jointe d'un message (ou d'un brouillon, par son messageId) : (description, contenu)."""
    message_id = (message_id or "").strip()
    if not _ID.fullmatch(message_id):
        raise ValueError("messageId empty or invalid")
    wanted = (part_id or "").strip()
    svc = service or get_service()
    msg = _execute(svc.users().messages().get(userId="me", id=message_id, format="full"))
    parts = mime.attachment_parts(msg.get("payload") or {})
    for ref, part in parts:
        if ref == wanted:
            info = mime.attachment_info(ref, part)
            data = _part_bytes(svc, message_id, ref, part, _RawMessage(svc, message_id))
            info["from"] = mime.header((msg.get("payload") or {}).get("headers") or [], "From")
            return info, data
    known = ", ".join(ref for ref, _ in parts) or "aucune"
    raise ValueError(f"partId {wanted!r} not found in this message (attachments: {known}; see get_message)")


def _load_files(paths: list[str] | None) -> list[tuple[str, str, bytes]]:
    files = [fichiers.load(p) for p in dict.fromkeys(p for p in paths or [] if p and p.strip())]
    _check_total(files)
    return files


def _check_total(files: list[tuple[str, str, bytes]]) -> None:
    total = sum(len(data) for _, _, data in files)
    if total > fichiers.MAX_ATTACH_BYTES:
        raise ValueError(f"attachments: {total} bytes in total, more than the Gmail limit (25 MB)")


# --- brouillons

_ADDRESS = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_REPLY_PREFIX = re.compile(r"^\s*(?:re|ré|réf|aw|sv|vs)\s*:", re.IGNORECASE)


def plain_address(value: str) -> str:
    """Adresse e-mail seule d'un en-tête (« Nom <a@example.com> » → « a@example.com ») si elle est bien formée, sinon ''."""
    _, address = parseaddr(value or "")
    return address if _ADDRESS.match(address) else ""


def normalize_addresses(value: str, field: str, required: bool = False) -> str:
    """Valide une liste d'adresses (« Nom <a@example.com>, c@example.com ») ; ValueError au moindre doute."""
    value = (value or "").strip()
    if not value:
        if required:
            raise ValueError(f"{field}: no address")
        return ""
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field}: line breaks are not allowed")
    pairs = getaddresses([value])
    clean: list[str] = []
    for name, address in pairs:
        local, _, domain = address.rpartition("@")
        try:
            ascii_domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            ascii_domain = ""
        candidate = f"{local}@{ascii_domain}"
        if not local or not ascii_domain or not _ADDRESS.match(candidate):
            raise ValueError(f"{field}: invalid address {address or value!r}")
        clean.append(formataddr((name, candidate)))
    if not clean:
        raise ValueError(f"{field}: no valid address")
    return ", ".join(clean)


def _reply_context(svc: Any, reply_to_message_id: str, thread_id: str) -> dict[str, Any] | None:
    if reply_to_message_id:
        original = _execute(
            svc.users()
            .messages()
            .get(userId="me", id=reply_to_message_id, format="metadata", metadataHeaders=REPLY_HEADERS)
        )
        if thread_id and original.get("threadId") != thread_id:
            raise ValueError("replyToMessageId does not belong to threadId")
        return original
    if thread_id:
        thread = _execute(
            svc.users().threads().get(userId="me", id=thread_id, format="metadata", metadataHeaders=REPLY_HEADERS)
        )
        messages = thread.get("messages") or []
        if not messages:
            raise ValueError(f"thread {thread_id} empty or not found")
        return messages[-1]
    return None


def create_draft(
    body: str,
    to: str = "",
    subject: str = "",
    cc: str = "",
    bcc: str = "",
    reply_to_message_id: str = "",
    thread_id: str = "",
    attachments: list[str] | None = None,
    service: Any = None,
) -> dict[str, Any]:
    """Crée un brouillon sans l'envoyer ; en réponse : In-Reply-To, References, « Re: » et threadId. `attachments` :
    chemins absolus de fichiers locaux à joindre."""
    if "\r" in subject or "\n" in subject:
        raise ValueError("subject: line breaks are not allowed")
    files = _load_files(attachments)
    svc = service or get_service()
    original = _reply_context(svc, reply_to_message_id, thread_id)
    in_reply_to = references = ""
    if original:
        headers = (original.get("payload") or {}).get("headers") or []
        subject = subject or mime.header(headers, "Subject")
        if not _REPLY_PREFIX.match(subject):
            subject = f"Re: {subject}".rstrip()
        to = to or mime.header(headers, "Reply-To") or mime.header(headers, "From")
        in_reply_to = mime.header(headers, "Message-ID")
        if in_reply_to:
            refs = mime.header(headers, "References").split()
            if in_reply_to not in refs:
                refs.append(in_reply_to)
            references = " ".join(refs)
    msg = _compose(body, to, subject, cc, bcc, in_reply_to, references, files)
    target_thread = (original or {}).get("threadId") or thread_id
    created = _execute(_draft_request(svc, msg, target_thread))
    return _draft_result(created, msg, subject, "draft_created")


def _draft_request(svc: Any, msg: EmailMessage, thread_id: str, draft_id: str = "") -> Any:
    """drafts.create (ou drafts.update si draft_id). Sans pièce jointe : message en JSON (raw) ; avec : envoi
    reprenable du message entier (message/rfc822), seul chemin accepté par Gmail au-delà de 5 Mo."""
    drafts = svc.users().drafts()
    message: dict[str, Any] = {"threadId": thread_id} if thread_id else {}
    body: dict[str, Any] = {"id": draft_id, "message": message} if draft_id else {"message": message}
    if not msg.is_multipart():
        message["raw"] = _raw(msg)
        return drafts.update(userId="me", id=draft_id, body=body) if draft_id else drafts.create(userId="me", body=body)
    media = MediaIoBaseUpload(io.BytesIO(_bytes(msg)), mimetype="message/rfc822", resumable=True)
    if draft_id:
        return drafts.update(userId="me", id=draft_id, body=body, media_body=media)
    return drafts.create(userId="me", body=body, media_body=media)


def _compose(
    body: str,
    to: str,
    subject: str,
    cc: str,
    bcc: str,
    in_reply_to: str = "",
    references: str = "",
    files: list[tuple[str, str, bytes]] | None = None,
) -> EmailMessage:
    """Message d'un brouillon : adresses validées, aucun retour à la ligne dans les en-têtes, pièces jointes
    (nom, type MIME, contenu) ajoutées après le texte."""
    if "\r" in subject or "\n" in subject:
        raise ValueError("subject: line breaks are not allowed")
    msg = EmailMessage()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg["To"] = normalize_addresses(to, "to", required=True)
    if cc_clean := normalize_addresses(cc, "cc"):
        msg["Cc"] = cc_clean
    if bcc_clean := normalize_addresses(bcc, "bcc"):
        msg["Bcc"] = bcc_clean
    msg["Subject"] = subject
    msg.set_content(body)
    for name, mime_type, data in files or []:
        maintype, _, subtype = mime_type.partition("/")
        if mime_type == "message/rfc822":  # message joint : rejoint octet pour octet (base64 interdit pour ce type)
            if msg.get_content_type() != "multipart/mixed":
                msg.make_mixed()
            part = EmailMessage()
            part["Content-Type"] = "message/rfc822"
            part.add_header("Content-Disposition", "attachment", filename=name)
            if any(byte >= 0x80 for byte in data):
                part["Content-Transfer-Encoding"] = "8bit"
            # Marqueur ASCII unique remplacé par les octets d'origine à la sérialisation (_bytes) : jamais reparsé
            # ni re-sérialisé (CRLF, 8 bits et en-têtes intacts).
            marker = f"NEXUS-AGYMAIL-EML-{secrets.token_hex(16)}"
            part.set_payload(marker)
            msg.attach(part)
            _raw_parts(msg)[marker] = data
            continue
        msg.add_attachment(  # octets transmis tels quels (base64), même pour un type texte
            data, maintype=maintype, subtype=subtype or "octet-stream", filename=name
        )
    return msg


_RAW_PARTS: weakref.WeakKeyDictionary[EmailMessage, dict[str, bytes]] = weakref.WeakKeyDictionary()


def _raw_parts(msg: EmailMessage) -> dict[str, bytes]:
    """Octets d'origine des messages joints d'un brouillon, par marqueur."""
    return _RAW_PARTS.setdefault(msg, {})


def _bytes(msg: EmailMessage) -> bytes:
    """Octets du message (Gmail normalise les fins de ligne) ; chaque message joint y reprend ses octets d'origine
    à la place de son marqueur (présent une seule fois, sinon erreur : rien n'est envoyé)."""
    out = msg.as_bytes()
    for marker, data in _raw_parts(msg).items():
        token = marker.encode("ascii")
        if out.count(token) != 1:
            raise GmailApiError("attached message: marker missing or ambiguous at serialization, nothing was sent")
        out = out.replace(token, data)
    return out


def _raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(_bytes(msg)).decode("ascii")


def _draft_result(created: dict[str, Any], msg: EmailMessage, subject: str, status: str) -> dict[str, Any]:
    return {
        "id": created.get("id"),
        "messageId": (created.get("message") or {}).get("id"),
        "threadId": (created.get("message") or {}).get("threadId"),
        "to": msg["To"],
        "cc": msg["Cc"] or "",
        "bcc": msg["Bcc"] or "",
        "subject": subject,
        "inReplyTo": msg["In-Reply-To"] or "",
        "attachments": [
            {
                "name": part.get_filename() or "",
                "type": part.get_content_type(),
                "size": len(_raw_parts(msg).get(str(part.get_payload()), b"") or part.get_payload(decode=True) or b""),
            }
            for part in msg.iter_attachments()
        ],
        "status": status,
    }


def _draft_summary(draft: dict[str, Any]) -> dict[str, Any]:
    message = draft.get("message") or {}
    headers = (message.get("payload") or {}).get("headers") or []
    return {
        "id": draft.get("id"),
        "messageId": message.get("id"),
        "threadId": message.get("threadId"),
        "date": mime.header(headers, "Date"),
        "to": mime.header(headers, "To"),
        "cc": mime.header(headers, "Cc"),
        "bcc": mime.header(headers, "Bcc"),
        "subject": mime.header(headers, "Subject"),
        "snippet": message.get("snippet", ""),
    }


def list_drafts(
    max_results: int = 10, page_token: str | None = None, query: str = "", service: Any = None
) -> dict[str, Any]:
    """Une page de brouillons (1 à 100, filtrés par une requête Gmail), détails lus en requêtes groupées."""
    svc = service or get_service()
    count = clamp_max_results(max_results)
    params: dict[str, Any] = {"userId": "me", "maxResults": count}
    if query:
        params["q"] = query
    if page_token:
        params["pageToken"] = page_token
    page = _execute(svc.users().drafts().list(**params))
    ids = [ref["id"] for ref in page.get("drafts") or []]
    details, failures = _batch_run(
        svc, ids, lambda draft_id: svc.users().drafts().get(userId="me", id=draft_id, format="metadata")
    )
    if failures:
        raise next(iter(failures.values()))
    return {
        "drafts": [_draft_summary(details[i]) for i in ids if i in details],
        "nextPageToken": page.get("nextPageToken"),
        "resultSizeEstimate": page.get("resultSizeEstimate"),
        "max_results": count,
    }


def _draft_id(draft_id: str) -> str:
    value = (draft_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("draftId empty or invalid")
    return value


def get_draft(draft_id: str, service: Any = None, with_content: bool = False) -> dict[str, Any]:
    """Brouillon complet : en-têtes, texte, pièces jointes listées. with_content : contenu de chaque pièce jointe
    lu aussi (`files`, dans l'ordre) et son empreinte SHA-256 ajoutée à sa description (`sha256`)."""
    draft_id = _draft_id(draft_id)
    svc = service or get_service()
    draft = _execute(svc.users().drafts().get(userId="me", id=draft_id, format="full"))
    message = draft.get("message") or {}
    payload = message.get("payload") or {}
    headers = payload.get("headers") or []
    result = _draft_summary(draft)
    result["inReplyTo"] = mime.header(headers, "In-Reply-To")
    result["references"] = mime.header(headers, "References")
    result.update(mime.extract(payload))
    result["parts_digest"] = parts_digest(payload)
    if with_content:
        pairs = _message_attachments(svc, str(message.get("id") or ""), payload)
        result["attachments"] = [{**info, "sha256": hashlib.sha256(data).hexdigest()} for info, data in pairs]
        result["files"] = [(info["name"], info["type"], data) for info, data in pairs]
    return result


def parts_digest(payload: dict[str, Any]) -> str:
    """Empreinte de toutes les parties lues en entier (texte, HTML, petites pièces) : une modification de la seule
    version HTML, invisible dans le texte rendu, change aussi l'accord d'envoi."""
    digest = hashlib.sha256()

    def walk(part: dict[str, Any]) -> None:
        body = part.get("body") or {}
        digest.update(f"{part.get('mimeType') or ''}|{part.get('filename') or ''}|{body.get('size') or 0}|".encode())
        digest.update(str(body.get("data") or "").encode("ascii", "replace") + b"\n")
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return digest.hexdigest()[:24]


def update_draft(
    draft_id: str,
    body: str,
    to: str = "",
    subject: str = "",
    cc: str = "",
    bcc: str = "",
    attachments: list[str] | None = None,
    remove_attachments: list[str] | None = None,
    service: Any = None,
) -> dict[str, Any]:
    """Remplace le texte d'un brouillon ; destinataires et objet vides = inchangés ; fil et réponse conservés.

    Les pièces jointes déjà présentes sont gardées (drafts.update remplace tout le message : elles sont relues puis
    rejointes), sauf celles nommées dans `remove_attachments` ; `attachments` en ajoute (chemins absolus).
    """
    draft_id = _draft_id(draft_id)
    if not (body or "").strip():
        raise ValueError("body is empty: give the full new text of the draft")
    added = _load_files(attachments)
    svc = service or get_service()
    current = get_draft(draft_id, service=svc, with_content=True)
    removed = {n.strip() for n in remove_attachments or [] if n and n.strip()}
    unknown = removed - {name for name, _, _ in current["files"]}
    if unknown:
        names = ", ".join(a["name"] for a in current["attachments"]) or "aucune"
        raise ValueError(f"removeAttachments: {', '.join(sorted(unknown))} not in the draft (attachments: {names})")
    files = [f for f in current["files"] if f[0] not in removed] + added
    _check_total(files)
    subject = subject or current["subject"]
    msg = _compose(
        body,
        to or current["to"],
        subject,
        cc or current["cc"],
        bcc or current["bcc"],
        current["inReplyTo"],
        current["references"],
        files,
    )
    updated = _execute(_draft_request(svc, msg, current.get("threadId") or "", draft_id=draft_id))
    return _draft_result(updated, msg, subject, "draft_updated")


def send_draft(draft_id: str, service: Any = None) -> dict[str, Any]:
    """Envoie un brouillon existant (drafts.send). Seul chemin d'envoi : aucun envoi direct sans brouillon."""
    draft_id = _draft_id(draft_id)
    svc = service or get_service()
    sent = _execute(svc.users().drafts().send(userId="me", body={"id": draft_id}))
    return {"id": sent.get("id"), "threadId": sent.get("threadId"), "labels": sent.get("labelIds", [])}


# --- libellés, rangement, corbeille

MAX_ITEMS = 100  # éléments (messages + fils) par appel de rangement ou de corbeille
SYSTEM_ACTIONS: dict[str, tuple[list[str], list[str]]] = {
    "archive": ([], ["INBOX"]),
    "unarchive": (["INBOX"], []),
    "read": ([], ["UNREAD"]),
    "unread": (["UNREAD"], []),
    "star": (["STARRED"], []),
    "unstar": ([], ["STARRED"]),
    "important": (["IMPORTANT"], []),
    "not_important": ([], ["IMPORTANT"]),
}
LABEL_ACTIONS = ("add_labels", "remove_labels")
ACTIONS = (*SYSTEM_ACTIONS, *LABEL_ACTIONS)
# Jamais posés ni retirés par modify : corbeille (trash_* / untrash_*), spam, brouillons, envoyés, chat.
RESERVED_LABELS = frozenset({"TRASH", "SPAM", "DRAFT", "SENT", "CHAT"})
_ID = re.compile(r"[A-Za-z0-9_-]+")


def list_labels(service: Any = None) -> list[dict[str, Any]]:
    """Libellés du compte : système d'abord, puis ceux de l'utilisateur, par nom."""
    svc = service or get_service()
    labels = _execute(svc.users().labels().list(userId="me")).get("labels") or []
    items = [{"id": lab.get("id"), "name": lab.get("name"), "type": lab.get("type", "user")} for lab in labels]
    return sorted(items, key=lambda lab: (lab["type"] != "system", str(lab["name"]).casefold()))


def resolve_labels(svc: Any, names: list[str], create_missing: bool = False) -> tuple[list[str], list[dict[str, Any]]]:
    """Noms de libellés → identifiants (nom exact, puis sans casse, puis identifiant).

    Tout est vérifié avant la moindre création : un nom réservé est refusé, un nom inconnu aussi, sauf
    create_missing=True (il est alors créé). Renvoie (identifiants dans l'ordre demandé, libellés créés).
    """
    wanted = list(dict.fromkeys(n.strip() for n in names or [] if n and n.strip()))
    if not wanted:
        raise ValueError("labelIds: no label")
    labels = list_labels(service=svc)
    exact = {str(lab["name"]): lab for lab in labels}
    folded = {str(lab["name"]).casefold(): lab for lab in labels}
    by_id = {str(lab["id"]): lab for lab in labels}
    found: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name in wanted:
        lab = exact.get(name) or folded.get(name.casefold()) or by_id.get(name)
        label_id = str(lab["id"]) if lab else name.upper()
        if label_id in RESERVED_LABELS:
            hint = "use trash_* / untrash_*" if label_id == "TRASH" else "no tool touches it"
            raise LabelError(f"reserved label {name!r}: never through label_* / unlabel_* ({hint})")
        if lab is None:
            if any(not part.strip() for part in name.split("/")):
                raise LabelError(f"invalid label name {name!r}")
            missing.append(name)
        else:
            found[name] = lab
    if missing and not create_missing:
        raise LabelError(f"label not found: {', '.join(missing)}; see list_labels, or createMissing=true to create it")
    created: list[dict[str, Any]] = []
    for name in missing:
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        lab = _execute(svc.users().labels().create(userId="me", body=body))
        found[name] = lab
        created.append({"id": lab.get("id"), "name": lab.get("name")})
    return [str(found[name]["id"]) for name in wanted], created


def create_label(name: str, parents: bool = True, service: Any = None) -> dict[str, Any]:
    """Crée un libellé (« Projets/2026 » : sous-libellé) ; parents=True crée aussi « Projets » s'il manque. Un libellé
    qui existe déjà est rendu tel quel (rien de créé), un nom réservé ou vide est refusé."""
    name = "/".join(part.strip() for part in (name or "").strip().split("/"))
    if not name:
        raise ValueError("displayName: empty label name")
    parts = name.split("/")
    names = ["/".join(parts[: i + 1]) for i in range(len(parts))] if parents else [name]
    svc = service or get_service()
    system = {
        str(v).casefold()
        for lab in list_labels(service=svc)
        if lab["type"] == "system"
        for v in (lab["id"], lab["name"])
    }
    if name.casefold() in system or name.upper() in RESERVED_LABELS:
        raise LabelError(f"system label {name!r}: never created (see list_labels)")
    ids, created = resolve_labels(svc, names, create_missing=True)
    return {"id": ids[-1], "name": name, "created": created}


def _ids(values: list[str] | None, field: str) -> list[str]:
    ids = list(dict.fromkeys(v.strip() for v in values or [] if v and v.strip()))
    bad = [i for i in ids if not _ID.fullmatch(i)]
    if bad:
        raise ValueError(f"{field}: invalid ID {bad[0]!r}")
    return ids


def _targets(message_ids: list[str] | None, thread_ids: list[str] | None) -> tuple[list[str], list[str]]:
    messages, threads = _ids(message_ids, "messageId"), _ids(thread_ids, "threadId")
    if not messages and not threads:
        raise ValueError("no ID: give messageId or threadId")
    total = len(messages) + len(threads)
    if total > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items per call ({total} requested): split into several calls")
    return messages, threads


def _failed(failures: dict[str, BaseException]) -> list[dict[str, str]]:
    return [{"id": item, **describe_error(exc)} for item, exc in failures.items()]


def modify(
    action: str,
    message_ids: list[str] | None = None,
    thread_ids: list[str] | None = None,
    labels: list[str] | None = None,
    create_missing: bool = False,
    service: Any = None,
) -> dict[str, Any]:
    """Range des messages (un seul batchModify) ou des fils (threads.modify groupés) ; au plus 100 éléments."""
    name = (action or "").strip().lower()
    if name not in ACTIONS:
        raise ValueError(f"unknown action {action!r}: {', '.join(ACTIONS)}")
    messages, threads = _targets(message_ids, thread_ids)
    if name in SYSTEM_ACTIONS and labels:
        raise ValueError("labels only with add_labels or remove_labels")
    svc = service or get_service()
    created: list[dict[str, Any]] = []
    if name in SYSTEM_ACTIONS:
        add, remove = SYSTEM_ACTIONS[name]
    else:
        label_ids, created = resolve_labels(svc, labels or [], create_missing and name == "add_labels")
        add, remove = (label_ids, []) if name == "add_labels" else ([], label_ids)
    body = {"addLabelIds": add, "removeLabelIds": remove}
    if messages:
        _execute(svc.users().messages().batchModify(userId="me", body={"ids": messages, **body}))
    done, failures = _batch_run(
        svc, threads, lambda thread_id: svc.users().threads().modify(userId="me", id=thread_id, body=body)
    )
    return {
        "action": name,
        "messages": messages,
        "threads": [t for t in threads if t in done],
        "failed": _failed(failures),
        "addLabelIds": add,
        "removeLabelIds": remove,
        "created": created,
    }


def trash(
    message_ids: list[str] | None = None,
    thread_ids: list[str] | None = None,
    restore: bool = False,
    service: Any = None,
) -> dict[str, Any]:
    """Met à la corbeille (ou en sort si restore) ; jamais de suppression définitive. Au plus 100 éléments."""
    messages, threads = _targets(message_ids, thread_ids)
    svc = service or get_service()
    verb = "untrash" if restore else "trash"
    done_m, fail_m = _batch_run(
        svc, messages, lambda message_id: getattr(svc.users().messages(), verb)(userId="me", id=message_id)
    )
    done_t, fail_t = _batch_run(
        svc, threads, lambda thread_id: getattr(svc.users().threads(), verb)(userId="me", id=thread_id)
    )
    return {
        "action": verb,
        "messages": [m for m in messages if m in done_m],
        "threads": [t for t in threads if t in done_t],
        "failed": _failed({**fail_m, **fail_t}),
    }


# --- erreurs


def describe_error(exc: BaseException) -> dict[str, str]:
    """Erreur exacte pour l'agent (code stable + message), sans jamais exposer le jeton."""
    if isinstance(exc, GmailAuthError):
        return {"code": exc.code, "error": str(exc)}
    if isinstance(exc, RefreshError):
        if "invalid_grant" in str(exc):
            return {"code": "revoked", "error": f"token revoked or expired: sign in again ({REAUTH_HINT})"}
        return {"code": "refused", "error": f"authentication refused by Google: {exc}"}
    if isinstance(exc, HttpError):
        status = _status(exc)
        reason = getattr(exc, "reason", "") or str(exc)
        if status == 404:
            return {"code": "not_found", "error": f"not found (404): {reason}"}
        if status in (401, 403):
            return {
                "code": "forbidden",
                "error": f"access denied ({status}): {reason} (scopes: see auth_status)",
            }
        if status == 429:
            return {"code": "quota", "error": f"Gmail quota exceeded (429) after {NUM_RETRIES} retries: {reason}"}
        return {"code": "api", "error": f"Gmail API {status}: {reason}"}
    if isinstance(exc, GmailApiError):
        return {"code": "quota", "error": str(exc)}
    if isinstance(exc, LabelError):
        return {"code": "label", "error": str(exc)}
    if isinstance(exc, (TransportError, httplib2.HttpLib2Error, socket.gaierror, ConnectionError, TimeoutError)):
        return {"code": "network", "error": f"network: {exc}"}
    if isinstance(exc, ValueError):
        return {"code": "invalid_argument", "error": f"invalid argument: {exc}"}
    return {"code": "internal", "error": f"{type(exc).__name__}: {exc}"}
