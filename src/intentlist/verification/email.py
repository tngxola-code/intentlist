"""Email verification: free local checks first, then a paid provider only for addresses that survive.

Status vocabulary (what the client sees): valid, invalid, catch_all, risky, disposable, role_based,
syntax_error, domain_error, unknown. Only `valid` counts toward a batch; `catch_all` ships on a
separate tab; everything else is excluded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from importlib import resources
from typing import Protocol

import dns.exception
import dns.resolver
import httpx
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..models import EmailStatus, EmailVerification, utcnow
from ..normalize import norm_email
from ..settings import Settings

log = logging.getLogger(__name__)


@dataclass
class VerificationOutcome:
    status: EmailStatus
    provider: str
    sub_status: str | None = None
    raw: dict = field(default_factory=dict)
    billable: bool = False


def _load_list(name: str) -> frozenset[str]:
    text = resources.files("intentlist.data").joinpath(name).read_text(encoding="utf-8")
    return frozenset(ln.strip().lower() for ln in text.splitlines() if ln.strip() and not ln.startswith("#"))


@lru_cache
def disposable_domains() -> frozenset[str]:
    return _load_list("disposable_domains.txt")


@lru_cache
def role_localparts() -> frozenset[str]:
    return _load_list("role_localparts.txt")


def _has_mail_host(domain: str) -> bool | None:
    """True/False when DNS answers; None when DNS itself is unavailable."""
    try:
        dns.resolver.resolve(domain, "MX", lifetime=5)
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        return False
    except dns.resolver.NoAnswer:
        try:
            dns.resolver.resolve(domain, "A", lifetime=5)
            return True
        except dns.exception.DNSException:
            return False
    except dns.exception.DNSException:
        return None


def local_checks(email: str, check_dns: bool = True, allow_test_domains: bool = False) -> VerificationOutcome | None:
    """Returns a terminal outcome if the address fails a free check, else None (go to provider)."""
    try:
        validated = validate_email(email, check_deliverability=False, test_environment=allow_test_domains)
    except EmailNotValidError as exc:
        return VerificationOutcome(EmailStatus.syntax_error, "local", str(exc)[:120])
    local, domain = validated.local_part.lower(), validated.domain.lower()
    if domain in disposable_domains():
        return VerificationOutcome(EmailStatus.disposable, "local", "disposable_domain")
    if local.split("+")[0] in role_localparts():
        return VerificationOutcome(EmailStatus.role_based, "local", "role_localpart")
    if check_dns and _has_mail_host(domain) is False:
        return VerificationOutcome(EmailStatus.domain_error, "local", "no_mx")
    return None


class Verifier(Protocol):
    name: str
    cost_per_check: float

    def verify(self, email: str) -> VerificationOutcome: ...


class _Retryable(Exception):
    pass


class _HttpVerifier:
    name = "http"
    cost_per_check = 0.0

    def __init__(self, api_key: str, cost: float, client: httpx.Client | None = None):
        if not api_key:
            raise ValueError(f"{self.name}: API key not configured")
        self.api_key, self.cost_per_check = api_key, cost
        self.client = client or httpx.Client(timeout=30)

    @retry(retry=retry_if_exception_type(_Retryable), wait=wait_exponential(min=1, max=20), stop=stop_after_attempt(4),
           reraise=True)
    def _get(self, url: str, params: dict) -> dict:
        resp = self.client.get(url, params=params)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Retryable(str(resp.status_code))
        resp.raise_for_status()
        return resp.json()


class ZeroBounceVerifier(_HttpVerifier):
    """https://www.zerobounce.net/docs/email-validation-api-quickstart/"""

    name = "zerobounce"
    url = "https://api.zerobounce.net/v2/validate"

    def verify(self, email: str) -> VerificationOutcome:
        data = self._get(self.url, {"api_key": self.api_key, "email": email, "ip_address": ""})
        if data.get("error"):
            raise RuntimeError(f"zerobounce: {data['error']}")
        status, sub = (data.get("status") or "").lower(), (data.get("sub_status") or "").lower()
        mapped = {
            "valid": EmailStatus.valid, "invalid": EmailStatus.invalid, "catch-all": EmailStatus.catch_all,
            "unknown": EmailStatus.unknown, "spamtrap": EmailStatus.risky, "abuse": EmailStatus.risky,
            "do_not_mail": EmailStatus.risky,
        }.get(status, EmailStatus.unknown)
        if sub == "role_based" or sub == "role_based_catch_all":
            mapped = EmailStatus.role_based
        elif sub == "disposable":
            mapped = EmailStatus.disposable
        elif sub in ("failed_syntax_check", "possible_typo") and mapped == EmailStatus.invalid:
            mapped = EmailStatus.syntax_error
        elif sub == "no_dns_entries":
            mapped = EmailStatus.domain_error
        return VerificationOutcome(mapped, self.name, sub or status, data, billable=True)


class MillionVerifierVerifier(_HttpVerifier):
    """https://developer.millionverifier.com/"""

    name = "millionverifier"
    url = "https://api.millionverifier.com/api/v3/"

    def verify(self, email: str) -> VerificationOutcome:
        data = self._get(self.url, {"api": self.api_key, "email": email, "timeout": 20})
        if data.get("error"):
            raise RuntimeError(f"millionverifier: {data['error']}")
        result = (data.get("result") or "").lower()
        mapped = {"ok": EmailStatus.valid, "catch_all": EmailStatus.catch_all, "unknown": EmailStatus.unknown,
                  "error": EmailStatus.unknown, "disposable": EmailStatus.disposable,
                  "invalid": EmailStatus.invalid}.get(result, EmailStatus.unknown)
        if data.get("role") and mapped in (EmailStatus.valid, EmailStatus.catch_all):
            mapped = EmailStatus.role_based
        return VerificationOutcome(mapped, self.name, data.get("subresult") or result, data, billable=True)


class LocalOnlyVerifier:
    """No paid provider: an address that passes local checks is `unknown` (never counted)."""

    name, cost_per_check = "local", 0.0

    def verify(self, email: str) -> VerificationOutcome:
        return VerificationOutcome(EmailStatus.unknown, self.name, "mailbox_not_checked")


class FixtureVerifier:
    """Deterministic statuses for demos/tests, keyed by the address's domain label."""

    name, cost_per_check = "fixture", 0.0
    RULES = {"catchall": EmailStatus.catch_all, "bounce": EmailStatus.invalid, "risky": EmailStatus.risky}

    def verify(self, email: str) -> VerificationOutcome:
        label = email.rsplit("@", 1)[-1].split(".")[0].split("-")[0]
        return VerificationOutcome(self.RULES.get(label, EmailStatus.valid), self.name, label)


def build_verifier(settings: Settings) -> Verifier:
    kind = settings.email_verifier
    if kind == "zerobounce":
        return ZeroBounceVerifier(settings.zerobounce_api_key or "", settings.cost_email_verification)
    if kind == "millionverifier":
        return MillionVerifierVerifier(settings.millionverifier_api_key or "", settings.cost_email_verification)
    if kind == "local":
        return LocalOnlyVerifier()
    if kind == "fixture":
        return FixtureVerifier()
    raise ValueError(f"unknown email verifier {kind!r}")


class EmailVerificationService:
    def __init__(self, verifier: Verifier, cache_days: int = 30, check_dns: bool = True):
        self.verifier, self.cache_days, self.check_dns = verifier, cache_days, check_dns

    def verify(self, session: Session, email: str) -> VerificationOutcome:
        key = norm_email(email)
        cutoff = utcnow() - timedelta(days=self.cache_days)
        cached = session.scalar(
            select(EmailVerification).where(EmailVerification.email_norm == key, EmailVerification.checked_at >= cutoff)
            .order_by(EmailVerification.checked_at.desc()).limit(1))
        if cached is not None and cached.status != EmailStatus.unknown:
            return VerificationOutcome(cached.status, f"cache:{cached.provider}", cached.sub_status, {})
        allow_test = isinstance(self.verifier, FixtureVerifier)
        outcome = local_checks(email, self.check_dns, allow_test) or self.verifier.verify(email)
        session.add(EmailVerification(email_norm=key, provider=outcome.provider, status=outcome.status,
                                      sub_status=outcome.sub_status, raw=outcome.raw))
        return outcome
