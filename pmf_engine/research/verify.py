"""
Quote verification — the load-bearing guardrail for research atoms.

`quote_in(body, quote)` returns whether a quote literally appears in a fetched
page body (after aggressive normalization). Research atoms use this to check
their own citations before returning, and the atom post-verifier re-runs it
as defense-in-depth against LLMs claiming quotes that don't exist.
"""

from __future__ import annotations
import html as _html
import re
from difflib import SequenceMatcher


# Match <script>...</script> / <style>...</style> including their content.
# Case-insensitive, dotall so newlines inside are consumed.
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
# Match any remaining HTML tag (open/close/self-closing) but NOT the < in
# stringified code like "x < 1" (requires a letter or / directly after <).
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def _normalize(s: str, aggressive: bool = True) -> str:
    n = s
    if aggressive:
        # Step 1: strip HTML so a quote written against rendered text matches
        # a source body served as raw HTML. Scripts/styles dropped entirely —
        # their contents would fuzzy-match unrelated quotes.
        n = _SCRIPT_STYLE_RE.sub(" ", n)
        n = _TAG_RE.sub(" ", n)
        # Step 2: decode HTML entities (&amp; → &, &#36; → $, &mdash; → —, …)
        n = _html.unescape(n)

    n = n.lower()

    if aggressive:
        # Unify unicode dashes and quote marks with ASCII equivalents
        n = n.replace("\u2014", "-").replace("\u2013", "-").replace("\u2212", "-")
        n = n.replace("\u2018", "'").replace("\u2019", "'")
        n = n.replace("\u201c", '"').replace("\u201d", '"')
        n = n.replace("\u00a0", " ")  # NBSP → space (common on gov pages)
        # Conflate dashes/dots/whitespace runs — punctuation often differs
        # between source (HTML) and quote (model output)
        n = re.sub(r"[\s\-\.]+", " ", n)
    else:
        n = re.sub(r"\s+", " ", n)
    return n.strip()


def quote_in(body: str, quote: str, aggressive: bool = True) -> dict:
    """Check if `quote` appears in `body` after normalization.

    Returns:
        {
            "match": bool,
            "closest_match": str | None,  # ~80-char window around best fuzzy match
            "similarity": float,          # 0.0-1.0, fraction of quote matched
        }
    """
    quote_n = _normalize(quote, aggressive)
    body_n = _normalize(body, aggressive)

    if not quote_n:
        return {"match": False, "closest_match": None, "similarity": 0.0}

    if quote_n in body_n:
        return {"match": True, "closest_match": quote_n, "similarity": 1.0}

    # Fuzzy: find longest common substring block between body and quote.
    # Similarity = longest block length / quote length (captures "most of the
    # quote is present somewhere" without being dominated by body length).
    sm = SequenceMatcher(None, body_n, quote_n, autojunk=False)
    longest = sm.find_longest_match(0, len(body_n), 0, len(quote_n))
    similarity = longest.size / len(quote_n) if quote_n else 0.0

    closest: str | None = None
    if longest.size > 0:
        start = max(0, longest.a - 30)
        end = min(len(body_n), longest.a + longest.size + 30)
        closest = body_n[start:end]

    return {"match": False, "closest_match": closest, "similarity": similarity}
