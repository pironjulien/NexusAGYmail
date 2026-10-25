"""Lecture des messages Gmail : corps texte (HTML converti), jeu de caractères, pièces jointes, citations, taille.

Tout ce qui sort d'ici reste du contenu de mail : donnée non fiable, encadrée par server.py avant d'être rendue.
"""

from __future__ import annotations

import base64
import re
from email.message import Message
from html.parser import HTMLParser
from typing import Any

# --- décodage


def b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


# ISO-8859-1 annoncé est en pratique du Windows-1252 (règle WHATWG) : mêmes lettres, plus les guillemets et l'euro.
_CHARSET_ALIASES = {"iso-8859-1": "cp1252", "iso8859-1": "cp1252", "latin1": "cp1252", "latin-1": "cp1252"}


def decode_bytes(raw: bytes, charset: str | None) -> str:
    """Décode avec le jeu de caractères déclaré, puis UTF-8, Windows-1252, et Latin-1 (qui ne peut pas échouer)."""
    candidates: list[str] = []
    if charset:
        name = charset.strip().strip('"').lower()
        candidates.append(_CHARSET_ALIASES.get(name, name))
    candidates += ["utf-8", "cp1252"]
    for codec in candidates:
        try:
            return raw.decode(codec)
        except LookupError, UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _headers_message(part: dict[str, Any]) -> Message:
    message = Message()
    for header in part.get("headers") or []:
        name = header.get("name", "")
        if name.lower() in ("content-type", "content-disposition"):
            message[name] = header.get("value", "")
    return message


def header(headers: list[dict[str, str]], name: str) -> str:
    """Valeur d'un en-tête, sans tenir compte de la casse du nom."""
    target = name.lower()
    for item in headers:
        if item.get("name", "").lower() == target:
            return item.get("value", "")
    return ""


# --- HTML vers texte

_SKIP_TAGS = {"head", "style", "script", "noscript", "template", "svg", "object", "title"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "table", "thead", "tbody",
    "tr", "ul", "ol", "li", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr",
    "center", "form", "address", "figure", "figcaption",
}  # fmt: skip
# Début de l'historique cité : tout ce qui suit est ignoré (réponse au-dessus de la citation).
_QUOTE_START_CLASSES = ("gmail_quote", "yahoo_quoted", "moz-cite-prefix")
_QUOTE_START_IDS = ("divrplyfwdmsg", "appendonsend")
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|max-height\s*:\s*0(?![.\d])"
)


class _HtmlToText(HTMLParser):
    def __init__(self, drop_quotes: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.drop_quotes = drop_quotes
        self.out: list[str] = []
        self.skip_tag: str | None = None
        self.skip_depth = 0
        self.stopped = False
        self.quotes_removed = False
        self.pre = 0
        self.links: list[tuple[str, int]] = []

    def _newline(self, count: int = 1) -> None:
        self.out.append("\n" * count)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.stopped:
            return
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        attributes = {k.lower(): (v or "") for k, v in attrs}
        classes = attributes.get("class", "").lower()
        element_id = attributes.get("id", "").lower()
        if self.drop_quotes and (any(c in classes for c in _QUOTE_START_CLASSES) or element_id in _QUOTE_START_IDS):
            self.stopped = True
            self.quotes_removed = True
            return
        hidden = "hidden" in attributes or bool(_HIDDEN_STYLE.search(attributes.get("style", "").lower()))
        if tag not in _VOID_TAGS and (tag in _SKIP_TAGS or hidden or (self.drop_quotes and tag == "blockquote")):
            # Texte caché (display:none...) : classique pour glisser une consigne à un agent, jamais rendu.
            self.skip_tag, self.skip_depth = tag, 1
            if tag == "blockquote":
                self.quotes_removed = True
            return
        if tag == "br":
            self._newline()
        elif tag in _BLOCK_TAGS:
            self._newline(2 if tag in ("p", "table", "blockquote", "h1", "h2", "h3") else 1)
        if tag == "li":
            self.out.append("- ")
        elif tag in ("td", "th"):
            self.out.append(" ")
        elif tag == "pre":
            self.pre += 1
        elif tag == "a":
            self.links.append((attributes.get("href", "").strip(), len(self.out)))

    def handle_endtag(self, tag: str) -> None:
        if self.stopped:
            return
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if tag == "a" and self.links:
            href, start = self.links.pop()
            text = "".join(self.out[start:]).strip()
            if _useful_link(href, text):
                self.out.append(f" ({href})")
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in _BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self.stopped or self.skip_tag:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        raw = "".join(self.out).replace("\xa0", " ")
        lines = [line.strip() for line in raw.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _useful_link(href: str, text: str) -> bool:
    lowered = href.lower()
    if not lowered.startswith(("http://", "https://", "mailto:")):
        return False  # ancres, javascript:, cid: (images intégrées)
    bare = href.removeprefix("mailto:")
    return bool(text) and text not in (href, bare)


def html_to_text(html: str, drop_quotes: bool = False) -> tuple[str, bool]:
    """Texte lisible d'un HTML de mail (styles et scripts retirés, entités décodées, liens utiles gardés)."""
    parser = _HtmlToText(drop_quotes)
    parser.feed(html)
    parser.close()
    return parser.text(), parser.quotes_removed


# --- citations dans le texte

_ATTRIBUTION = re.compile(
    r"^(?:Le|On|Am|El)\b(?P<middle>.{0,300})\b(?:a\s+écrit|wrote|schrieb|escribió)\s*:\s*$",
    re.IGNORECASE,
)
# Une vraie ligne d'attribution porte une heure, une année, une date chiffrée ou une adresse e-mail
# (« Le 16 oct. 2026 à 14:03, X <x@y> a écrit : ») ; « Le rapport que l'architecte a écrit : » n'en a aucune.
_ATTRIBUTION_EVIDENCE = re.compile(
    r"\d{1,2}\s*[:h]\s*\d{2}|\b(?:19|20)\d{2}\b|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|[^\s<>@]+@[^\s<>@]+\.[a-z]{2,}",
    re.IGNORECASE,
)


def _is_attribution(line: str) -> bool:
    match = _ATTRIBUTION.match(line)
    return bool(match and _ATTRIBUTION_EVIDENCE.search(match.group("middle")))


def _quote_follows(lines: list[str], start: int) -> bool:
    """Ce qui suit est une citation : lignes « > », ou plus rien (bloc cité HTML déjà retiré)."""
    rest = [line.strip() for line in lines[start:] if line.strip()]
    return not rest or rest[0].startswith(">")


_ORIGINAL_MARKER = re.compile(
    r"^-{2,}\s*(?:original message|message d'origine|message original|mensaje original|ursprüngliche nachricht)\s*-{2,}$",
    re.IGNORECASE,
)
_OUTLOOK_FROM = re.compile(r"^\*?(?:De|From)\s*:\*?\s", re.IGNORECASE)
_OUTLOOK_FIELDS = re.compile(r"^\*?(?:Envoyé|Sent|Date|À|To|Objet|Subject)\s*:", re.IGNORECASE)


def strip_quoted_text(text: str) -> tuple[str, bool]:
    """Retire les lignes « > » et l'historique cité : attribution datée (« Le 16 oct. 2026 à 14:03, X a écrit : »,
    « On …, 2026 wrote: ») suivie d'une citation, marqueur « message d'origine » ou en-tête Outlook."""
    lines = text.splitlines()
    kept: list[str] = []
    removed = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if (
            (_is_attribution(stripped) and _quote_follows(lines, index + 1))
            or (following and _is_attribution(f"{stripped} {following}") and _quote_follows(lines, index + 2))
            or _ORIGINAL_MARKER.match(stripped)
            or (
                _OUTLOOK_FROM.match(stripped)
                and sum(bool(_OUTLOOK_FIELDS.match(x.strip())) for x in lines[index + 1 : index + 5]) >= 2
            )
        ):
            removed = True
            break
        if stripped.startswith(">"):
            removed = True
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip(), removed


# --- parcours MIME


def _is_attachment(part: dict[str, Any], mime: str) -> bool:
    if mime.startswith("multipart/"):
        return False
    if part.get("filename"):
        return True
    disposition = _headers_message(part).get_content_disposition()
    return disposition == "attachment" or mime not in ("text/plain", "text/html")


# Texte brut de façade qui renvoie vers la version HTML (notifications, banques, lettres d'information).
_HTML_ONLY_HINTS = re.compile(
    r"activ\w*\s+(?:le\s+|l')?html|enable\s+html|html[- ](?:capable|compatible|version)|"
    r"(?:view|read|open)\s+(?:this\s+(?:e-?mail|message)\s+)?(?:in|on)\s+(?:your|a)\s+(?:web\s+)?browser|"
    r"view\s+(?:it\s+)?online|(?:voir|afficher|consulter|lire)\s+(?:ce\s+(?:e-?mail|mail|message|courriel)\s+)?"
    r"(?:dans\s+(?:votre|un)\s+navigateur|en\s+ligne)|version\s+(?:en\s+ligne|web)|"
    r"ne\s+(?:prend|supporte)\s+pas\s+(?:en\s+charge\s+)?(?:le\s+|les\s+)?(?:format\s+)?html|"
    r"does\s+not\s+support\s+html",
    re.IGNORECASE,
)
_GOLDEN = 1.618


def poor_plain_text(plain: str, html_text: str) -> bool:
    """True si la partie text/plain d'un multipart/alternative est indigente face au texte tiré du HTML : bien plus
    courte (moins de 1/φ⁴ ≈ 15 %), ou plus courte que 1/φ ≈ 62 % et renvoyant vers la version HTML (« activer le HTML », « voir dans le
    navigateur »...)."""
    plain_len = len(" ".join(plain.split()))
    html_len = len(" ".join(html_text.split()))
    if html_len < 200 or plain_len >= html_len:
        return False
    if plain_len < html_len / _GOLDEN**4:
        return True
    return plain_len < html_len / _GOLDEN and bool(_HTML_ONLY_HINTS.search(plain))


ROOT_REF = "racine"  # la partie racine (partId vide) : jamais « 0 », qui désigne son premier enfant


def _part_ref(part: dict[str, Any], path: str) -> str:
    """Référence stable d'une partie : son partId Gmail, sinon sa position (« 1.0 »), « racine » pour le message
    lui-même. L'attachmentId de Gmail change d'une lecture à l'autre : il ne sert jamais de référence."""
    return str(part.get("partId") or "") or path or ROOT_REF


# --- découpe exacte du message brut (octets d'origine, sans re-sérialisation)

_HEADER_END = re.compile(rb"\r?\n\r?\n")
_IDENTITY_ENCODINGS = ("", "7bit", "8bit", "binary")


def _split_entity(entity: bytes) -> tuple[bytes, bytes]:
    """(en-têtes, corps) d'une entité MIME, coupée à la première ligne vide, octets intacts."""
    if entity.startswith((b"\r\n", b"\n")):
        return b"", entity[2 if entity.startswith(b"\r\n") else 1 :]
    match = _HEADER_END.search(entity)
    return (entity[: match.start()], entity[match.end() :]) if match else (entity, b"")


def _entity_headers(headers: bytes) -> Message:
    from email.parser import BytesHeaderParser
    from email.policy import compat32

    return BytesHeaderParser(policy=compat32).parsebytes(headers + b"\r\n\r\n")


def _children(body: bytes, boundary: bytes) -> list[bytes] | None:
    """Sous-entités d'un corps multipart, octets intacts (la fin de ligne avant un délimiteur lui appartient)."""
    delimiter = re.compile(rb"(?:\A|\r?\n)--" + re.escape(boundary) + rb"(--)?[ \t]*(?:\r?\n|\Z)")
    matches = list(delimiter.finditer(body))
    parts: list[bytes] = []
    for current, following in zip(matches, matches[1:], strict=False):
        if current.group(1):
            break
        parts.append(body[current.end() : following.start()])
    closed = any(m.group(1) for m in matches)
    return parts if closed else None


def raw_part(raw: bytes, ref: str, expected_type: str) -> bytes | None:
    """Octets exacts d'une partie du message brut désignée par sa référence (« 1.0 », « racine »), après contrôle de
    son type : contenu décodé pour une pièce ordinaire, message encapsulé tel quel pour un message/rfc822. None si la
    partie est introuvable, d'un autre type, ou si sa fidélité n'est pas assurée."""
    entity = raw
    path = [] if ref == ROOT_REF else ref.split(".")
    for index in path:
        headers, body = _split_entity(entity)
        boundary = _entity_headers(headers).get_boundary()
        if not boundary or not index.isdigit():
            return None
        children = _children(body, boundary.encode("utf-8", "surrogateescape"))
        if children is None or int(index) >= len(children):
            return None
        entity = children[int(index)]
    headers, body = _split_entity(entity)
    parsed = _entity_headers(headers)
    if parsed.get_content_type() != expected_type.lower():
        return None
    encoding = str(parsed.get("Content-Transfer-Encoding") or "").strip().lower()
    if expected_type.lower() == "message/rfc822":
        return body if encoding in _IDENTITY_ENCODINGS else None
    from email import message_from_bytes
    from email.policy import compat32

    data = message_from_bytes(entity, policy=compat32).get_payload(decode=True)
    return data if isinstance(data, bytes) else None


def attachment_parts(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(référence, partie) de chaque pièce jointe, dans l'ordre du message (même parcours que `extract`)."""
    found: list[tuple[str, dict[str, Any]]] = []

    def walk(part: dict[str, Any], path: str) -> None:
        mime = (part.get("mimeType") or "").lower()
        if _is_attachment(part, mime):
            found.append((_part_ref(part, path), part))
        elif mime.startswith("multipart/"):
            for index, child in enumerate(part.get("parts") or []):
                walk(child, f"{path}.{index}" if path else str(index))

    walk(payload, "")
    return found


def attachment_info(ref: str, part: dict[str, Any]) -> dict[str, Any]:
    mime = (part.get("mimeType") or "").lower()
    return {
        "part_id": ref,
        "name": part.get("filename") or "(sans nom)",
        "type": mime or "inconnu",
        "size": int((part.get("body") or {}).get("size") or 0),
    }


def extract(payload: dict[str, Any], drop_quotes: bool = False) -> dict[str, Any]:
    """Corps texte, pièces jointes (référence, nom, type, taille, sans contenu) et citations retirées d'un payload
    Gmail."""
    attachments = [attachment_info(ref, part) for ref, part in attachment_parts(payload)]

    def walk(part: dict[str, Any]) -> tuple[str, str, bool]:
        """(type, texte, citations HTML retirées) de la partie retenue."""
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        if _is_attachment(part, mime):
            return "", "", False
        if mime.startswith("multipart/"):
            results = [walk(child) for child in part.get("parts") or []]
            texts = [result for result in results if result[1]]
            if not texts:
                return "", "", False
            if mime == "multipart/alternative":
                plain = next((t for t in texts if t[0] == "text/plain"), None)
                html = next((t for t in texts if t[0] == "text/html"), None)
                if plain and html and poor_plain_text(plain[1], html[1]):
                    return html
                return plain or texts[0]
            return texts[0][0], "\n\n".join(t[1] for t in texts), any(t[2] for t in texts)
        data = body.get("data")
        if not data:
            return "", "", False
        text = decode_bytes(b64decode(data), _headers_message(part).get_content_charset())
        if mime == "text/html":
            text, html_quotes = html_to_text(text, drop_quotes)
            return mime, text, html_quotes
        return mime, text, False

    _, text, quotes = walk(payload)
    if drop_quotes:
        text, text_quotes = strip_quoted_text(text)
        quotes = quotes or text_quotes
    else:
        text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    return {"text": text, "attachments": attachments, "quotes_removed": quotes}


# --- taille


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Coupe à `limit` caractères au dernier blanc proche ; True si coupé."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space >= limit * 0.9:
        cut = cut[:space]
    return cut.rstrip(), True


def share_budget(lengths: list[int], budget: int) -> list[int]:
    """Partage équitable d'un budget de caractères : les messages courts gardent tout, le reste est réparti."""
    shares = [0] * len(lengths)
    remaining = budget
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for position, index in enumerate(order):
        share = remaining // (len(lengths) - position)
        shares[index] = min(lengths[index], share)
        remaining -= shares[index]
    return shares
