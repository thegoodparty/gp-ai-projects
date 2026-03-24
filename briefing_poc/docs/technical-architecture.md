# Technical Architecture

## Pipeline Overview

The pipeline is a sequence of 10 scripts that collect, extract, analyze, and assemble data into a briefing. All scripts are config-driven via `--city` CLI arg and `city_config.json`.

```
Raw Data Collection          Analysis & Enrichment           Output
─────────────────           ────────────────────           ──────
01 Legislative data    ──►  04 6-pass LLM analysis    ──►  05 Briefing assembly
02 Budget/fiscal data  ──►  06b Topic-to-issue mapping ──►     (runs last, pulls
03 PDF text extraction ──►  07 Council vs constituent  ──►      from everything)
06 Constituent data    ──►  08 Discussion narratives   ──►
                            09 Quick win generation    ──►
```

### Script-by-Script Breakdown

| Step | Script | What It Does | Time | Cost |
|------|--------|-------------|------|------|
| 1 | `01_collect_legislative.py` | Dispatches to the right collector (Legistar, BoardDocs, or eSCRIBE) based on config. Downloads meetings, agenda items, votes, PDFs. | 5-20 min | Free |
| 2 | `02_collect_budget.py` | 44 years of fiscal data from NC LINC/OSBM API. Revenue, expenditure, tax rates, per-capita metrics. | ~2 min | Free |
| 3 | `03_extract_pdfs.py` | Text extraction via PyMuPDF, table extraction via pdfplumber. Outputs one JSON per PDF. | 1-38 min | Free |
| 4 | `04_run_analysis.py` | 6 structured LLM passes using Gemini 2.5 Flash with Pydantic output schemas. | ~2 min | ~$0.02 |
| 5 | `05_assemble_briefing.py` | Assembles 15-section briefing with citations, constituent data, mismatch, quick wins, discussions. Run last — it pulls from all other outputs. | ~35 sec | ~$0.005 |
| 6 | `06_collect_constituent_data.py` | Queries Databricks for Haystaq voter modeling data: demographics, issue scores, zip breakdown. | ~30 sec | Free |
| 6b | `06b_map_topics_to_issues.py` | LLM maps council topics (from Pass 1) to Haystaq voter score columns. | ~5 sec | ~$0.001 |
| 7 | `07_council_vs_constituent.py` | Computes mismatch between council agenda time % and voter priority scores. | instant | Free |
| 8 | `08_collect_discussions.py` | Uses Gemini grounded web search + Tavily fallback to find news coverage for contentious items. | ~4 min | ~$0.02 |
| 9 | `09_generate_quick_wins.py` | Generates concrete actions tied to constituent gaps. | instant | Free |

Every script is **resumable** — it checks for existing output and skips completed work. Safe to re-run.

---

## Collector Architecture

Each legislative platform has its own collector module in `collectors/`. All produce the **same JSON output schema** so downstream scripts work unchanged.

### Output Schema (Common to All Collectors)

| File | Description |
|------|-------------|
| `bodies.json` | Committees/boards (BodyId, BodyName, BodyTypeName) |
| `events.json` | Meetings (EventId, EventDate, EventBodyName) |
| `matters.json` | Legislative items (MatterId, MatterTitle, MatterTypeName, MatterStatusName) |
| `event_items/{id}.json` | Agenda items per meeting (EventItemActionText, EventItemRollCallFlag) |
| `attachments/*.pdf` | Downloaded PDF documents |
| `persons.json` | Council members |
| `votes/{id}.json` | Roll-call vote records |

### Collector Implementations

| Collector | File | API Type | Vote Data | PDF Attachments |
|-----------|------|----------|-----------|-----------------|
| **Legistar** | `collectors/legistar.py` | REST API (OData) | Rich (roll-call votes) | Yes (staff reports) |
| **BoardDocs** | `collectors/boarddocs.py` | AJAX POST (Lotus Domino) | None | Yes (limited) |
| **eSCRIBE** | `collectors/escribemeetings.py` | JSON API + HTML parsing | None | Yes (agendas, minutes) |

Each collector follows the same pattern:

```python
@dataclass
class CollectorConfig:
    """Configuration — what to collect and where to save."""

@dataclass
class CollectorResult:
    """Summary — what was collected."""

async def collect(config: CollectorConfig) -> CollectorResult:
    """Collect data and save to config.output_dir."""
```

Script 01 bridges `city_config.json` to the appropriate collector config.

---

## 6-Pass LLM Analysis

Script 04 runs 6 structured analysis passes using Gemini 2.5 Flash. Each pass uses Pydantic output schemas for structured, typed JSON output.

| Pass | Input | Output | What It Produces |
|------|-------|--------|-----------------|
| Pass 1 | All matter titles + types | `pass1_legislative_overview.json` | Topic areas, matter counts, key matters per topic |
| Pass 2 | Event items + action text + votes | `pass2_vote_analysis.json` | Vote patterns, unanimous/non-unanimous counts, key dissent items, frequent movers |
| Pass 3 | Budget data (fiscal + tax rate) | `pass3_budget_analysis.json` | Revenue/expenditure trends, tax rate history, fiscal pressures |
| Pass 4 | Extracted PDF text (top 50 docs) | `pass4_document_summaries.json` | Document summaries with key issues, fiscal impact, recommendations |
| Pass 5 | Bodies + event items per body | `pass5_committee_analysis.json` | Committee profiles, workload distribution, composition |
| Pass 6 | Passes 1-5 combined | `pass6_synthesis.json` | Executive summary, key themes, immediate priorities, knowledge gaps |

---

## Config System

All city-specific values live in `cities/{slug}/city_config.json`. Scripts are city-agnostic.

### Key Config Sections

| Section | Purpose | Example |
|---------|---------|---------|
| `city` | Name, state, display names | `"name": "Charlotte", "state_code": "nc"` |
| `legislative.system` | Which collector to use | `"legistar"`, `"boarddocs"`, or `"escribemeetings"` |
| `legistar` / `boarddocs` / `escribemeetings` | Platform-specific config | Client slug, base URL, meeting types |
| `budget` | LINC API filter | Municipality name for the `where` clause |
| `data_collection` | Time window | Lookback days, display period |
| `entity` | Governing body type | Council vs. Board of Commissioners |
| `databricks` | Voter data filter | City or county name for Haystaq queries |
| `haystaq` | Issue score columns | Column names, display names, thresholds |
| `discussions` | News search config | Local news domains, max items |
| `topic_to_issue_map` | Council topic → Haystaq column mapping | Auto-generated by script 06b |

### Config Loader

`scripts/city_config.py` provides a singleton `cfg` object that parses `--city` from CLI, loads the JSON config, and exposes typed properties. Every script imports from here.

---

## Data Directory Structure

Each city has a self-contained data directory with all collected and generated files:

```
cities/{slug}/data/
├── legistar/              # Raw legislative data (all collectors write here)
│   ├── bodies.json
│   ├── events.json
│   ├── matters.json
│   ├── persons.json
│   ├── event_items/       # One JSON per meeting
│   ├── votes/             # One JSON per vote record
│   └── attachments/       # Downloaded PDFs
├── budget/                # Fiscal data
│   ├── government_fiscal.json
│   └── property_tax_rate.json
├── extracted/             # PDF text extraction (one JSON per PDF)
├── analysis/              # LLM analysis passes
│   ├── pass1_legislative_overview.json
│   ├── pass2_vote_analysis.json
│   ├── pass3_budget_analysis.json
│   ├── pass4_document_summaries.json
│   ├── pass5_committee_analysis.json
│   ├── pass6_synthesis.json
│   ├── council_vs_constituent.json
│   └── quick_wins.json
├── constituent/           # Voter modeling data
│   ├── demographics.json
│   ├── issue_scores.json
│   └── zip_breakdown.json
├── discussions/           # News coverage
│   └── discussion_narratives.json
└── briefing/              # Final output
    ├── {slug}_council_briefing.md
    └── briefing_metadata.json
```

---

## Conversational Layer

### Two-Tier Architecture

The conversational AI uses a two-tier approach to handle the large data volumes:

**Tier 1 — System Context (~155K tokens for Charlotte):**
All pre-computed analysis passes, constituent data, mismatch analysis, discussion narratives, and quick wins loaded as the AI's system context. This gives it everything needed to walk through the CPO's 6-step flow and answer most questions.

**Tier 2 — On-Demand Retrieval (tool use):**
When the official asks deeper questions, the AI calls tools to fetch raw source data:

| Tool | Returns | Use Case |
|------|---------|----------|
| `get_matter_details(matter_id)` | Full matter record + attachment list | "Tell me more about rezoning petition 2025-118" |
| `get_document_text(filename)` | Extracted PDF text | "What did the staff report say about that water project?" |
| `get_vote_record(event_item_id)` | Roll-call vote: who voted yes/no | "How did Council Member X vote on that?" |
| `get_event_items(event_id)` | Full agenda for a meeting | "What else was on the agenda that day?" |
| `search_matters(query)` | Matching matters by keyword | "Has the council discussed bike lanes?" |

### Why Agentic, Not RAG

The analysis passes ARE the retrieval result. We spent ~$0.03/city pre-computing structured insights — RAG would redundantly search for what's already in context.

- **Most questions don't need retrieval.** The analysis data already contains the answers.
- **Drill-down questions are specific, not semantic.** "What did the staff report say about rezoning 2025-118?" is a lookup by ID, not a semantic search.
- **The CPO's flow is guided, not open-ended.** The AI knows what data it needs at each step.

RAG could help later (Phase 3) for searching across 870+ extracted PDFs when the user asks about something not covered in the analysis passes.

### Verifiability

Every claim traces back through structured data to public sources:

| Data Layer | Verifiable? | How |
|------------|------------|-----|
| Vote outcomes, tallies | Fully | Legistar URLs (public record) |
| Dollar amounts, budgets | Fully | LINC datasets and PDFs |
| Council member activity | Fully | Official record (action text) |
| Constituent priorities | Internally | Reproducible Databricks queries |
| Council-vs-constituent mismatch | Fully | Pure math from structured data |
| Topic categorization | Partially | LLM-generated, reviewable |
| Discussion narratives | Weakly | News source URLs provided, but LLM may paraphrase incorrectly |

The one gap is Haystaq voter scores — proprietary modeled data that the official can't independently query.

---

## LLM Strategy

| Purpose | Model | Why |
|---------|-------|-----|
| Pipeline analysis (6 passes, assembly, topic mapping, discussions) | Gemini 2.5 Flash | Cheap (~$0.04/city), 1M context window, good structured output |
| Conversational layer | Claude Sonnet 4.5 | Better tool use and conversational quality |

### Key Technical Decisions

- **No vector DB.** Analysis data fits in context window. Defer to Phase 3.
- **No Parquet/DuckDB.** Access pattern is "load all data for one city, format as strings for LLM." JSON loading takes <1 second per city.
- **Pre-compute everything possible.** The 6 analysis passes compress 3.8M tokens of raw data into ~164K tokens of structured insight. The conversational AI doesn't re-analyze.
- **Gemini for analysis, Claude for conversation.** Each model's strengths match the task.

### Data Storage: Migration Path

The POC stores everything as JSON files on disk. This works for 3+ cities but won't work at scale. The original Technical Proposal suggested S3 + Parquet + DuckDB + pgvector — the POC answered several questions about what's actually needed:

| Question | POC Answer |
|----------|-----------|
| Do we need vector search? | **No, not for Phase 1.** Charlotte uses ~230K prompt tokens across all passes — well under Gemini's 1M context window. Even with 12+ months of data, we stay under 500K tokens. |
| Does Parquet + DuckDB outperform JSON? | **Not for our access pattern.** We load all data for one city and format it as strings for LLM prompts. No GROUP BY, no JOIN — the LLM does the analysis. JSON loads in <1 second per city. |
| Does Haystaq need separate storage? | **Pre-compute and store.** Databricks queries take ~30 seconds but require AWS + Databricks credentials. Pre-computing at city onboarding is simpler than requiring credentials at runtime. |

**Recommended phasing:**

| Phase | Storage | When |
|-------|---------|------|
| **Phase 1 (current)** | JSON files on disk per city | Sufficient for 3+ cities, no infrastructure needed |
| **Phase 2 (prototype)** | S3 for raw data, PostgreSQL for analysis results and constituent data | Small product prototype with web UI. Skip Parquet and vector search. |
| **Phase 3 (scale)** | Add Parquet + DuckDB if cross-city aggregation is needed. Add pgvector if prompt tokens exceed context window limits. | When we're past ~10 states or need cross-city analytics |

The key insight: the original proposal estimated LLM costs at $1.75-2.55/briefing and assumed vector search would be necessary. The POC proved costs are $0.04-0.06/briefing and context windows are large enough to skip vector search entirely. This simplifies the production architecture significantly.

---

## Shared Dependencies

The pipeline uses shared modules from the `gp-ai-projects` monorepo:

| Module | Path | Purpose |
|--------|------|---------|
| `GeminiClient` | `shared/llm_gemini.py` | Gemini API wrapper with structured output |
| `DatabricksClient` | `shared/databricks_client.py` | Databricks SQL queries for Haystaq data |
| `TavilyClient` | `shared/tavily_client.py` | Web search fallback for discussion narratives |

Environment variables: `GEMINI_API_KEY`, `DATABRICKS_API_KEY`, `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `TAVILY_API_KEY` (optional), `ANTHROPIC_API_KEY` (for chat prototype).
