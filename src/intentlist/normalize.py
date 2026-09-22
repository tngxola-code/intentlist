"""Deterministic normalisation used by the evidence lock and entity resolution."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                         " ": " ", " ": " ", " ": " "})
_WS = re.compile(r"\s+")
_TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|sid|s|share|igshid)$", re.IGNORECASE)
_GMAIL = {"gmail.com", "googlemail.com"}


def norm_text(text: str) -> str:
    """Normalisation applied to both page text and evidence quotes before comparing."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return _WS.sub(" ", text).strip().casefold()


def contains(haystack_norm: str, needle: str | None) -> bool:
    if not needle:
        return False
    n = norm_text(needle)
    return bool(n) and n in haystack_norm


def norm_email(email: str) -> str:
    email = email.strip().strip(".,;:<>()[]\"'").lower()
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    if domain in _GMAIL:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def norm_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^\w\s'-]", " ", name.casefold())
    return _WS.sub(" ", name).strip()


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                             if not _TRACKING.match(k)))
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https" if parts.scheme in ("http", "https", "") else parts.scheme, host + port, path, query, ""))


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def domain_matches(domain: str, registered: str) -> bool:
    return domain == registered or domain.endswith("." + registered)


def request_fingerprint(category_slug: str, item_sought: str) -> str:
    tokens = sorted(set(re.findall(r"[a-z0-9]+", norm_name(item_sought))))
    return hashlib.sha256(f"{category_slug}|{' '.join(tokens)}".encode()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
