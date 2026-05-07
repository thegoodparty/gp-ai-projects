"""config.py — QA run configuration.

All runtime options live in QARunConfig. Use QARunConfig.from_env() for
production runs. Override individual fields for targeted runs:

  Full LLM run (default):
      cfg = QARunConfig.from_env()

  Deterministic-only fast path:
      cfg = QARunConfig.from_env()
      cfg.run_extraction = False
      cfg.run_phase1     = False
      cfg.run_phase2     = False
      cfg.emit_workbook  = False
      cfg.emit_trace     = False
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from qa.extraction.claim_types import BLOCKABLE_TIERS

_DEFAULT_JUDGES = "claude:anthropic:claude-sonnet-4-6,gemini:google:gemini-2.5-flash"

_PROVIDER_KEY_ENVS: dict[str, tuple[str, ...]] = {
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "google":    ("gemini_api_key", "GEMINI_API_KEY"),
    "openai":    ("OPEN_AI_API_KEY", "OPENAI_API_KEY"),
}


@dataclass
class JudgeConfig:
    name: str
    provider: str   # "anthropic" | "google" | "openai"
    model: str
    api_key: str

    @classmethod
    def from_str(cls, spec: str) -> JudgeConfig:
        """Parse 'name:provider:model' and resolve the API key from env."""
        parts = spec.strip().split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"Judge spec must be 'name:provider:model', got: {spec!r}")
        name, provider, model = parts
        envs = _PROVIDER_KEY_ENVS.get(provider, (f"{provider.upper()}_API_KEY",))
        api_key = next((os.getenv(e, "") for e in envs if os.getenv(e)), "")
        return cls(name=name, provider=provider, model=model, api_key=api_key)


@dataclass
class QARunConfig:
    # ── Stage switches ────────────────────────────────────────────────────────
    run_extraction:    bool = True   # LLM claim extraction (one call per item)
    run_deterministic: bool = True   # structural / identity / temporal checks
    run_phase1:        bool = True   # triage judge — all non-skipped claims
    run_phase2:        bool = True   # escalation judge — blockable Phase 1 not-OK only

    # ── Execution ─────────────────────────────────────────────────────────────
    phase1_max_workers: int = 2
    phase1_max_tokens:  int = 512
    phase2_max_tokens:  int = 1024   # higher: Phase 2 gets full PDF; rationale can be longer

    # ── Weight tier config (change here to add tiers; nothing else needs updating) ──
    blockable_tiers: frozenset = field(default_factory=lambda: BLOCKABLE_TIERS)

    # ── Outputs ───────────────────────────────────────────────────────────────
    emit_workbook: bool = True   # single-tab xlsx review log
    emit_summary:  bool = True   # human-readable markdown
    emit_trace:    bool = True   # machine-readable JSON trace

    # ── Matching thresholds ───────────────────────────────────────────────────
    fuzzy_label_threshold:      float = 0.9
    fuzzy_title_threshold:      float = 0.85
    citation_high_confidence:   float = 0.90
    citation_medium_confidence: float = 0.75

    # ── Judges ────────────────────────────────────────────────────────────────
    judges: list[JudgeConfig] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> QARunConfig:
        """Load judges from QA_JUDGES env var. All other options use defaults."""
        raw = os.environ.get("QA_JUDGES", _DEFAULT_JUDGES)
        judges = [JudgeConfig.from_str(s) for s in raw.split(",") if s.strip()]
        judges = [j for j in judges if j.api_key]
        return cls(judges=judges)

    @property
    def triage_judge(self) -> JudgeConfig | None:
        """Judge 1: runs on all non-skipped claims (triage)."""
        return self.judges[0] if self.judges else None

    @property
    def escalation_judge(self) -> JudgeConfig | None:
        """Judge 2: runs only on blockable Phase 1 not-OK claims (escalation).
        Falls back to Judge 1 if only one judge is configured."""
        if len(self.judges) >= 2:
            return self.judges[1]
        return self.judges[0] if self.judges else None
