import pytest

from broker.url_allowlist import (
    HTTP_ALLOWLIST,
    PDF_ALLOWLIST,
    AllowlistViolation,
    check_url_allowed,
)


class TestHttpAllowlist:
    """HTTP_ALLOWLIST is now empty (no domain restriction). SSRF guards in
    http_fetch._validate_url handle private / loopback / metadata IPs."""

    def test_http_allowlist_is_empty_meaning_no_domain_restriction(self):
        assert HTTP_ALLOWLIST == ()

    def test_empty_allowlist_short_circuits_check(self):
        # Empty allowlist = accept everything. SSRF is blocked at the endpoint layer.
        check_url_allowed("https://municodemeetings.com/x", allowlist=HTTP_ALLOWLIST)
        check_url_allowed("https://legistar.granicus.com/x", allowlist=HTTP_ALLOWLIST)
        check_url_allowed("https://badsite.com/exfil", allowlist=HTTP_ALLOWLIST)


class TestCheckUrlAllowedPrimitive:
    """The check_url_allowed function is still a generic primitive usable with
    any allowlist tuple. These tests pin its matching semantics for callers
    who pass an explicit allowlist."""

    _example = (".gov", ".us", "webapi.legistar.com")

    def test_accepts_suffix_match(self):
        check_url_allowed("https://fayettevillenc.gov/agendas", allowlist=self._example)
        check_url_allowed("https://seattle.wa.us/council", allowlist=self._example)

    def test_accepts_exact_match(self):
        check_url_allowed("https://webapi.legistar.com/v1/x", allowlist=self._example)

    def test_rejects_non_matching(self):
        with pytest.raises(AllowlistViolation):
            check_url_allowed("https://evil.com/x", allowlist=self._example)

    def test_rejects_substring_spoof(self):
        # `.gov` must match as a suffix, not a substring. `mygov.ru` should not match.
        with pytest.raises(AllowlistViolation):
            check_url_allowed("https://mygov.ru/agendas", allowlist=self._example)

    def test_rejects_suffix_spoof_in_subdomain(self):
        with pytest.raises(AllowlistViolation):
            check_url_allowed("https://legistar.com.evil.com/x", allowlist=self._example)

    def test_case_insensitive_hostname(self):
        check_url_allowed("https://WEBAPI.LEGISTAR.COM/v1/x", allowlist=self._example)
        check_url_allowed("https://Fayettevillenc.Gov/agendas", allowlist=self._example)


class TestPdfAllowlist:
    def test_pdf_allowlist_is_empty_meaning_no_domain_restriction(self):
        assert PDF_ALLOWLIST == ()

    def test_empty_allowlist_short_circuits_check(self):
        # When passed PDF_ALLOWLIST (empty), any URL is allowed — SSRF guards
        # in the endpoint still apply (private IPs, metadata, loopback).
        check_url_allowed("https://random.example.com/x.pdf", allowlist=PDF_ALLOWLIST)
        check_url_allowed("https://badsite.org/report.pdf", allowlist=PDF_ALLOWLIST)
