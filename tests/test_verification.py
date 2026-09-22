"""Branch 6: email verification."""
import httpx
import pytest
import respx

from intentlist.models import EmailStatus, EmailVerification
from intentlist.verification.email import (
    EmailVerificationService,
    FixtureVerifier,
    MillionVerifierVerifier,
    ZeroBounceVerifier,
    local_checks,
)


def test_local_checks():
    assert local_checks("not-an-email", check_dns=False).status == EmailStatus.syntax_error
    assert local_checks("x@mailinator.com", check_dns=False).status == EmailStatus.disposable
    assert local_checks("sales@example.org", check_dns=False).status == EmailStatus.role_based
    assert local_checks("jane.doerksen@example.org", check_dns=False) is None


@respx.mock
@pytest.mark.parametrize("payload,expected", [
    ({"status": "valid", "sub_status": ""}, EmailStatus.valid),
    ({"status": "catch-all", "sub_status": ""}, EmailStatus.catch_all),
    ({"status": "invalid", "sub_status": "mailbox_not_found"}, EmailStatus.invalid),
    ({"status": "do_not_mail", "sub_status": "role_based"}, EmailStatus.role_based),
    ({"status": "do_not_mail", "sub_status": "disposable"}, EmailStatus.disposable),
    ({"status": "spamtrap", "sub_status": ""}, EmailStatus.risky),
])
def test_zerobounce_mapping(payload, expected):
    respx.get("https://api.zerobounce.net/v2/validate").mock(return_value=httpx.Response(200, json=payload))
    out = ZeroBounceVerifier("k", 0.008).verify("a@b.com")
    assert out.status == expected and out.billable


@respx.mock
@pytest.mark.parametrize("payload,expected", [
    ({"result": "ok", "role": False}, EmailStatus.valid),
    ({"result": "ok", "role": True}, EmailStatus.role_based),
    ({"result": "catch_all"}, EmailStatus.catch_all),
    ({"result": "invalid"}, EmailStatus.invalid),
    ({"result": "unknown"}, EmailStatus.unknown),
])
def test_millionverifier_mapping(payload, expected):
    respx.get("https://api.millionverifier.com/api/v3/").mock(return_value=httpx.Response(200, json=payload))
    assert MillionVerifierVerifier("k", 0.004).verify("a@b.com").status == expected


@respx.mock
def test_verifier_retries_on_429():
    route = respx.get("https://api.zerobounce.net/v2/validate")
    route.side_effect = [httpx.Response(429), httpx.Response(200, json={"status": "valid"})]
    v = ZeroBounceVerifier("k", 0.008)
    v._get.retry.wait = lambda *a, **k: 0
    assert v.verify("a@b.com").status == EmailStatus.valid


def test_service_caches_results_and_skips_paid_call_for_local_failures(db):
    calls = []

    class Counting(FixtureVerifier):
        def verify(self, email):
            calls.append(email)
            return super().verify(email)

    svc = EmailVerificationService(Counting(), cache_days=30, check_dns=False)
    assert svc.verify(db, "ann.lee@inbox-demo.test").status == EmailStatus.valid
    assert svc.verify(db, "Ann.Lee@inbox-demo.test").provider.startswith("cache:")
    assert svc.verify(db, "sales@inbox-demo.test").status == EmailStatus.role_based
    assert calls == ["ann.lee@inbox-demo.test"]  # one paid call total
    assert db.query(EmailVerification).count() == 2
