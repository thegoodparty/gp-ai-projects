"""grounding.py — PDF text extraction and per-item source grounding.

Two-layer grounding per agenda item:
  pdf_passage  — 2000-char window from the raw PDF (primary)
  norm_passage — normalized description + staff_recommendation (fallback)

Per-claim span extraction uses keyword overlap to find the most relevant
3-sentence window within the item's pdf_passage.

Citation grounding fuzzy-matches the generator's declared sourceCitations
quotes against the PDF to produce a 0–1 confidence score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class GroundingResult:
    pdf_available: bool
    pdf_passage: str           # 2000-char window for this item (empty if not found)
    norm_passage: str          # normalized description + staff recommendation
    pdf_page: int = 0          # 1-indexed page number (0 if not located)
    citation_grounding: dict = field(default_factory=dict)
    # field_name → {"quote": str, "score": float, "span": str}
    source_passage: str = ""   # verbatim passage declared by the generator (highest priority)


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 60) -> tuple[str, list[int]]:
    """Extract full text from PDF bytes. Returns (full_text, page_offsets)."""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n = min(len(doc), max_pages)
    page_texts = [doc[i].get_text() for i in range(n)]
    offsets: list[int] = []
    pos = 0
    for t in page_texts:
        offsets.append(pos)
        pos += len(t) + 1
    return "\n".join(page_texts), offsets


def page_for_offset(offset: int, page_offsets: list[int]) -> int:
    for i in range(len(page_offsets) - 1, -1, -1):
        if offset >= page_offsets[i]:
            return i + 1
    return 1


# ── Storage helpers ───────────────────────────────────────────────────────────

def load_pdf_bytes_from_files(agenda_files: list[dict], storage) -> bytes | None:
    """Load PDF bytes from an agenda_files list (normalized JSON sources)."""
    ordered = sorted(
        agenda_files,
        key=lambda af: (0 if "packet" in af.get("name", "").lower() else 1),
    )
    for af in ordered:
        url = af.get("url", "")
        if af.get("type") not in ("storage_pdf", "local_pdf"):
            continue
        if url.startswith("/Users/") or url.startswith("/home/"):
            continue
        result = _try_load(url, storage)
        if result is not None:
            return result
        if "_packet." in url:
            result = _try_load(url.replace("_packet.", "."), storage)
            if result is not None:
                return result
    return None


def _try_load(key: str, storage) -> bytes | None:
    try:
        if storage.exists(key):
            return storage.read_bytes(key)
    except Exception:
        pass
    return None


# ── Item location ─────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _find_item_offset(
    pdf_text: str,
    title: str,
    threshold: float = 0.6,
    lookahead_chars: int = 800,
) -> int | None:
    """Locate the character offset for an agenda item title in the PDF.

    Tries sliding windows of 1-4 consecutive lines so that section headers
    split across multiple PDF lines still produce a strong match.  Among
    candidates above threshold, prefers the occurrence followed by the richest
    text (most words in the next lookahead_chars) -- favours staff-report
    pages over table-of-contents rows.
    """
    clean_title = _clean(title)
    if not clean_title:
        return None

    lines = pdf_text.split("\n")
    positions: list[int] = []
    pos = 0
    for line in lines:
        positions.append(pos)
        pos += len(line) + 1

    candidates: list[tuple[float, int]] = []

    for i, _ in enumerate(lines):
        best_ratio = 0.0
        for window in range(1, 5):
            if i + window > len(lines):
                break
            combined = " ".join(
                _clean(l) for l in lines[i: i + window] if _clean(l)
            )
            if not combined or len(combined) < 4:
                continue
            if clean_title in combined or combined in clean_title:
                r = 0.95
            else:
                r = SequenceMatcher(None, clean_title, combined).ratio()
            if r > best_ratio:
                best_ratio = r

        if best_ratio >= threshold:
            candidates.append((best_ratio, positions[i]))

    if not candidates:
        return None

    best = max(r for r, _ in candidates)
    viable = [(r, off) for r, off in candidates if r >= best - 0.1]

    def _richness(offset: int) -> int:
        return len(pdf_text[offset: offset + lookahead_chars].split())

    return max(viable, key=lambda x: _richness(x[1]))[1]

def extract_item_passage(
    pdf_text: str,
    title: str,
    window_chars: int = 2000,
    threshold: float = 0.6,
) -> str | None:
    """Extract a 2000-char window from the PDF around the agenda item title."""
    offset = _find_item_offset(pdf_text, title, threshold)
    if offset is None:
        return None
    passage = pdf_text[offset: offset + window_chars].strip()
    passage = re.sub(r"\n{3,}", "\n\n", passage)
    passage = re.sub(r" {2,}", " ", passage)
    return passage or None


# ── Normalized passage ────────────────────────────────────────────────────────

def build_norm_passage(norm_item: dict | None) -> str:
    if not norm_item:
        return ""
    parts = [
        norm_item.get("source_passage", "") or "",   # verbatim PDF text from extraction phase
        norm_item.get("description", "") or "",
        norm_item.get("staff_recommendation", "") or "",
        " ".join(str(a) for a in norm_item.get("fiscal_amounts", [])),
    ]
    section = norm_item.get("section", "")
    if section:
        parts.insert(0, f"Agenda section: {section}")
    return "\n".join(p for p in parts if p).strip()


def find_norm_item(title: str, norm_items: list[dict], threshold: float = 0.5) -> dict | None:
    best_ratio = 0.0
    best = None
    t = title.lower().strip()
    for item in norm_items:
        r = SequenceMatcher(None, t, (item.get("title") or "").lower().strip()).ratio()
        if r > best_ratio:
            best_ratio = r
            best = item
    return best if best_ratio >= threshold else None


# ── Per-claim span extraction ─────────────────────────────────────────────────

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "can", "could", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "and", "but", "or",
    "nor", "so", "yet", "both", "either", "neither", "not", "no",
    "this", "that", "these", "those", "it", "its", "they", "their",
    "there", "here", "which", "who", "whom", "what", "where", "when",
    "how", "all", "any", "each", "every", "more", "most", "other",
    "such", "than", "too", "very", "just", "because", "if", "while",
})

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def extract_relevant_span(
    claim_text: str,
    pdf_passage: str,
    n_sentences: int = 3,
    fallback_chars: int = 400,
) -> str:
    """Return the n_sentences window in pdf_passage most relevant to claim_text."""
    def _keywords(text: str) -> set[str]:
        tokens = re.findall(r"\b[a-z0-9]+\b", text.lower())
        return {t for t in tokens if t not in _STOPWORDS and len(t) > 2}

    claim_kw = _keywords(claim_text)
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(pdf_passage) if s.strip()]

    if not claim_kw or not sentences:
        return pdf_passage[:fallback_chars]

    scores = [len(claim_kw & _keywords(s)) for s in sentences]
    best_score = max(scores)

    if best_score == 0:
        return pdf_passage[:fallback_chars]

    best_idx = scores.index(best_score)
    half = n_sentences // 2
    start = max(0, best_idx - half)
    end = min(len(sentences), start + n_sentences)
    start = max(0, end - n_sentences)

    return " ".join(sentences[start:end])


# ── Citation grounding ────────────────────────────────────────────────────────

def match_citation_to_pdf(quote: str, pdf_text: str) -> tuple[float, str]:
    """Fuzzy-match a citation quote against the PDF. Returns (score, matched_span).

    Scores: 1.0 = exact, 0.75+ = near-verbatim, 0.50–0.74 = partial, <0.50 = likely synthetic.
    """
    if not quote or not pdf_text:
        return 0.0, ""

    clean_q = _clean(quote)
    if not clean_q:
        return 0.0, ""

    if clean_q in _clean(pdf_text):
        return 1.0, quote

    q_words = {
        w for w in re.findall(r"\b[a-z0-9]+\b", clean_q)
        if w not in _STOPWORDS and len(w) > 2
    }
    keyword_threshold = max(1, len(q_words) // 2)
    lines = [ln for ln in pdf_text.split("\n") if ln.strip()]
    q_len = len(clean_q)

    candidates: list[int] = []
    for i, line in enumerate(lines):
        line_lower = line.lower()
        shared = sum(1 for w in q_words if w in line_lower)
        if shared >= keyword_threshold:
            candidates.append(i)

    if not candidates:
        candidates = [
            i for i, ln in enumerate(lines)
            if 0.3 <= len(_clean(ln)) / max(q_len, 1) <= 3.0
        ][:60]

    best_ratio = 0.0
    best_span = ""

    for i in candidates:
        for window in range(1, 4):
            if i + window > len(lines):
                break
            span = " ".join(lines[i: i + window])
            clean_span = _clean(span)
            if not clean_span:
                continue
            len_ratio = len(clean_q) / max(len(clean_span), 1)
            if not (0.25 <= len_ratio <= 4.0):
                continue
            ratio = SequenceMatcher(None, clean_q, clean_span).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_span = span
                if best_ratio >= 0.97:
                    return best_ratio, best_span

    return best_ratio, best_span


# ── Item grounding builder ────────────────────────────────────────────────────

def build_item_grounding(
    item,                       # ItemContext
    norm_item: dict | None,
    pdf_text: str | None,
) -> GroundingResult:
    """Build a GroundingResult for one agenda item."""
    norm_passage = build_norm_passage(norm_item)

    pdf_passage = ""
    pdf_found = False
    pdf_page = 0

    if pdf_text:
        passage = extract_item_passage(pdf_text, item.title)
        if passage:
            pdf_passage = passage
            pdf_found = True

    # Match source citations against PDF
    citation_grounding: dict = {}
    for field_name, quote in item.source_citations.items():
        if not quote:
            continue
        if pdf_text:
            score, span = match_citation_to_pdf(quote, pdf_text)
        else:
            score, span = 0.0, ""
        citation_grounding[field_name] = {
            "quote": quote,
            "score": round(score, 3),
            "span": span,
        }

    return GroundingResult(
        pdf_available=pdf_found,
        pdf_passage=pdf_passage,
        norm_passage=norm_passage,
        pdf_page=pdf_page,
        citation_grounding=citation_grounding,
        source_passage=item.source_passage,
    )
