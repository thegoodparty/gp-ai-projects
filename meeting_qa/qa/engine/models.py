"""models.py — Core data models for the QA engine.

All modules import from here. No module-level logic — pure dataclasses only.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IdentityContext:
    title: str
    date: str
    city_slug: str
    declared_priority_count: int | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class ModeledContext:
    """Structured modelled/contextual data passed to every judge (e.g. Haystaq voter scores)."""
    available: bool
    voter_count: int = 0
    issues: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class ItemContext:
    """One reviewable item (e.g. a priority issue in a meeting briefing)."""
    slug: str
    title: str
    text_fields: dict[str, str]        # field_name → text
    source_citations: dict[str, str]   # field_name → citation quote declared by the generator
    source_documents: list[dict] = field(default_factory=list)
    source_passage: str = ""           # verbatim passage the generator used (sourcePassage field)


@dataclass
class ProjectInput:
    """Normalised project payload consumed by the engine. Built by a QAProjectSpec adapter."""
    document_id: str
    document_type: str
    identity: IdentityContext
    items: list[ItemContext]
    modeled_context: ModeledContext | None = None
    raw: dict = field(default_factory=dict)   # original payload for spec-specific access


@dataclass
class ClaimCandidate:
    """A single extracted claim ready for adjudication."""
    item_slug: str
    item_title: str
    source_field: str           # which text field this claim came from
    claim_text: str
    claim_type: str             # one of qa.extraction.claim_types constants
    weight_tier: str            # "blockable" | "regular" — derived from claim_type
    blocking_candidate: bool    # True iff weight_tier is in QARunConfig.blockable_tiers
    why_material: str           # extractor's one-line explanation of materiality
    expected_source_type: str   # "pdf" | "modeled_context" | "normalized"
    should_skip: bool = False
    skip_reason: str = ""
    index: int = 0              # set by runner; stable reference across all lists


@dataclass
class EvidenceBundle:
    """All evidence passed to a judge for one claim."""
    matched_local_span: str           # best span shown to Judge 1 as primary source
    full_item_passage: str            # 2000-char PDF window (diagnostic; not sent to judges)
    normalized_passage: str           # normalized description + staff recommendation
    citation_quote: str = ""          # verbatim quote declared by the generator
    citation_grounding_score: float = 0.0   # 0–1 fuzzy match of that quote against PDF
    matched_local_span_source: str = ""     # "source_passage" | "citation_match" | "keyword_match" | ""
    modeled_context: ModeledContext | None = None
    full_pdf_text: str = ""           # full extracted PDF text for Judge 2 escalation


@dataclass
class JudgeDecision:
    judge_name: str
    accuracy_category: str    # one of qa.adjudication.prompts.ACCURACY_CATEGORIES
    rationale: str
    flag_better_source: bool = False


@dataclass
class ClaimAdjudication:
    """All decisions for a single claim, from extraction through Phase 2."""
    claim: ClaimCandidate
    bundle: EvidenceBundle
    phase1: JudgeDecision | None = None
    phase2: JudgeDecision | None = None

    @property
    def final_verdict(self) -> str:
        """Phase 2 verdict if triggered, else Phase 1. Empty string if neither ran."""
        if self.phase2 is not None:
            return self.phase2.accuracy_category
        if self.phase1 is not None:
            return self.phase1.accuracy_category
        return ""


@dataclass
class DeterministicResult:
    check_name: str
    blocks: bool
    reason: str
    details: dict = field(default_factory=dict)
    needs_llm_verification: bool = False  # True for content checks; False for structural (title/date/slug/count)
    llm_verdict: str = ""                 # "Confirmed" | "Cleared" | "" (empty if not verified or LLM skipped)
    llm_rationale: str = ""


@dataclass
class RouteDecision:
    final_status: str            # "OK" | "Block"
    reason_code: str
    human_reason: str
    triggered_by: str = ""       # "deterministic" | "claim_adjudication" | ""
    blocking_adjudications: list[ClaimAdjudication] = field(default_factory=list)
    all_adjudications: list[ClaimAdjudication] = field(default_factory=list)
    deterministic_results: list[DeterministicResult] = field(default_factory=list)
