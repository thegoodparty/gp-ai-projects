"""meeting_briefing_spec.py — QAProjectSpec adapter for GoodParty meeting briefings."""
from __future__ import annotations

from qa.engine.models import IdentityContext, ItemContext, ModeledContext
from qa.inputs.project_spec import QAProjectSpec

# Fields extracted for claim analysis (primary factual content)
_DETAIL_TEXT_FIELDS = [
    "whatIsHappening",
    "whatDecision",
    "whyItMatters",
    "recommendation",
    "whoIsPresenting",
    "supportingContext",
    "background",
]

# Advisory/coaching fields — model-generated recommendations, not verifiable
# factual claims. Excluded from extraction to avoid unresolvable adjudications.
_ADVISORY_FIELDS = {"actionItem", "askThis", "askThisInTheRoom", "tryThis"}


class MeetingBriefingSpec(QAProjectSpec):
    document_type = "meeting_briefing"

    def _document_id(self, raw: dict) -> str:
        m = raw.get("meeting", {})
        return f"{m.get('citySlug', '')}_{m.get('date', '')}"

    def extract_identity(self, raw: dict) -> IdentityContext:
        m = raw.get("meeting", {})
        es = raw.get("executiveSummary", {})
        return IdentityContext(
            title=m.get("title", ""),
            date=m.get("date", ""),
            city_slug=m.get("citySlug", ""),
            declared_priority_count=es.get("priorityItemCount"),
            extra={
                "time": m.get("time", ""),
                "body": m.get("body", ""),
                "total_agenda_items": es.get("totalAgendaItems", 0),
            },
        )

    def extract_items(self, raw: dict) -> list[ItemContext]:
        items: list[ItemContext] = []
        for issue in raw.get("priorityIssues", []):
            slug = issue.get("slug", "")
            title = issue.get("agendaItemTitle", "")
            detail = issue.get("detail") or {}

            text_fields: dict[str, str] = {}
            for f in _DETAIL_TEXT_FIELDS:
                v = (detail.get(f) or "").strip()
                if v:
                    text_fields[f] = v

            # Per-field citation quotes (supports both list and dict formats)
            source_citations: dict[str, str] = {}
            citations = detail.get("sourceCitations") or []
            if isinstance(citations, list):
                for cit in citations:
                    f = cit.get("field", "")
                    q = cit.get("quote", "")
                    if f and q:
                        source_citations[f] = q
            elif isinstance(citations, dict):
                source_citations = {k: v for k, v in citations.items() if v}

            source_docs = detail.get("supportingDocuments") or []
            source_sections = issue.get("sourceSections") or []
            if source_sections:
                source_passage = "\n\n".join(
                    f"[{s.get('label', 'Source')}]\n{s.get('text', '')}"
                    for s in source_sections
                    if s.get("text", "").strip()
                ).strip()
            else:
                source_passage = (issue.get("sourcePassage") or "").strip()
            if title and source_passage:
                source_passage = f"AGENDA ITEM: {title}\n\n{source_passage}"

            items.append(ItemContext(
                slug=slug,
                title=title,
                text_fields=text_fields,
                source_citations=source_citations,
                source_documents=source_docs if isinstance(source_docs, list) else [],
                source_passage=source_passage,
            ))
        return items

    def extract_modeled_context(self, raw: dict) -> ModeledContext | None:
        cd = raw.get("constituentData") or {}
        if not cd:
            return None
        return ModeledContext(
            available=bool(cd.get("available")),
            voter_count=cd.get("voterCount", 0),
            issues=cd.get("topIssues", []),
            raw=cd,
        )
