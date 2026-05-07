"""eo_orientation_spec.py — QAProjectSpec stub for EO orientation briefings.

Implement this adapter when the EO orientation briefing format is finalised.
The engine, routing, and adjudication code require no changes — only this file.

Minimal implementation checklist:
  extract_identity     — map orientation document identity to IdentityContext
                         (title, date, city_slug; declared_priority_count if applicable)
  extract_items        — map briefing sections to ItemContext
                         (each reviewable section is one item)
  extract_modeled_context — map any Haystaq-equivalent data to ModeledContext

Materiality override (optional):
  Override get_claim_weight_map() if claim types relevant to orientation
  briefings have different blocking behaviour than meeting briefings.
  Example: vote_or_decision_fact may not apply to orientation documents.

Usage (once implemented):
    from qa.inputs.eo_orientation_spec import EOOrientationSpec
    from qa.engine.runner import run_qa

    spec = EOOrientationSpec()
    project_input = spec.to_project_input(raw_payload)
    result = run_qa(project_input, spec, normalized, haystaq, pdf_bytes, cfg, output_dir)
"""
from __future__ import annotations

from qa.engine.models import IdentityContext, ItemContext, ModeledContext
from qa.inputs.project_spec import QAProjectSpec


class EOOrientationSpec(QAProjectSpec):
    document_type = "eo_orientation"

    def extract_identity(self, raw: dict) -> IdentityContext:
        raise NotImplementedError("EOOrientationSpec.extract_identity — implement when format is finalised")

    def extract_items(self, raw: dict) -> list[ItemContext]:
        raise NotImplementedError("EOOrientationSpec.extract_items — implement when format is finalised")

    def extract_modeled_context(self, raw: dict) -> ModeledContext | None:
        raise NotImplementedError("EOOrientationSpec.extract_modeled_context — implement when format is finalised")
