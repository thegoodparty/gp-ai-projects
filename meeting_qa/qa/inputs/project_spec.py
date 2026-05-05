"""project_spec.py — Abstract base and normalised input contract.

To add a new project type:
  1. Subclass QAProjectSpec
  2. Implement extract_identity, extract_items, extract_modeled_context
  3. Optionally override get_claim_weight_map for project-specific materiality rules
  4. No changes to the engine, routing, or adjudication code

See qa/inputs/eo_orientation_spec.py for a documented stub.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from qa.engine.models import IdentityContext, ItemContext, ModeledContext, ProjectInput


class QAProjectSpec(ABC):
    document_type: str = "unknown"

    @abstractmethod
    def extract_identity(self, raw: dict) -> IdentityContext: ...

    @abstractmethod
    def extract_items(self, raw: dict) -> list[ItemContext]: ...

    @abstractmethod
    def extract_modeled_context(self, raw: dict) -> ModeledContext | None: ...

    def _document_id(self, raw: dict) -> str:
        return ""

    def get_claim_weight_map(self) -> dict[str, str]:
        """Return claim_type → weight_tier for this project.
        Defaults to the global CLAIM_TYPE_WEIGHT_TIER. Override to customise
        materiality rules without touching the engine.
        """
        from qa.extraction.claim_types import CLAIM_TYPE_WEIGHT_TIER
        return dict(CLAIM_TYPE_WEIGHT_TIER)

    def to_project_input(self, raw: dict) -> ProjectInput:
        return ProjectInput(
            document_id=self._document_id(raw),
            document_type=self.document_type,
            identity=self.extract_identity(raw),
            items=self.extract_items(raw),
            modeled_context=self.extract_modeled_context(raw),
            raw=raw,
        )
