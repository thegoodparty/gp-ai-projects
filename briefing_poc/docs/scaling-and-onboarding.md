# Scaling and Onboarding

How we onboard new cities, what we learned from 3 cities, connector priorities, and the path to national scale.

---

## What We Learned from 3 Cities

| City | Legislative System | Entity Type | Key Learning |
|------|--------------------|-------------|--------------|
| Charlotte | Legistar (REST API) | City Council | Full pipeline validation; discovered LINC budget API; exposed need for discussion narratives |
| Wake County | Legistar (REST API) | Board of Commissioners | Proved city/county flexibility; different Databricks filter (`County` vs `City`); different governing body terminology |
| Raleigh | eSCRIBE (JSON API) | City Council | Proved multi-platform support; required new collector; exposed meeting type discovery as manual |

### Honest Time Breakdown

| Phase | Charlotte (first city) | Wake County (second, same platform) | Raleigh (third, new platform) |
|-------|-----------------------|--------------------------------------|-------------------------------|
| Research & discovery | ~4 hours | ~1 hour | ~2 hours |
| Write city_config.json | ~1 hour | ~30 min | ~45 min |
| Build/adapt collector | N/A (first) | N/A (reused Legistar) | ~4 hours (new eSCRIBE collector) |
| Run pipeline | ~30 min | ~20 min | ~25 min |
| Debug & fix issues | ~3 hours | ~1 hour | ~2 hours |
| **Total** | **~8 hours** | **~3 hours** | **~9 hours** |

**The repeatable case is Wake County** — reusing an existing connector, it took ~3 hours.

### Key Insight: Two Effort Buckets

1. **One-time platform work** (build a connector): A few hours, but only done once per platform
2. **Per-city configuration**: 1-3 hours of research + config, mostly manual

At national scale, bucket 1 is finite (6-7 platforms cover ~80% of US cities 25k+). **Bucket 2 is the bottleneck** — it's the work that happens for every single city.

---

## Onboarding Process: Step by Step

### Prerequisites

| Credential | Used By | How to Get |
|-----------|---------|------------|
| `GEMINI_API_KEY` | Scripts 04, 05, 06b, 08 | Google AI Studio |
| `DATABRICKS_API_KEY` + host/path | Script 06 | DevOps / Data team |
| `AWS_PROFILE=work` | Script 06 | AWS credential config |
| `TAVILY_API_KEY` | Script 08 (optional fallback) | tavily.com |

### Step 1: Find the Legislative System (10-30 min)

Try platforms in this order:

1. **Legistar** — `https://webapi.legistar.com/v1/{slug}/bodies` with common slugs (`{city}{state}`, `{city}`, `{county}`). 200 response = confirmed.
2. **eSCRIBE** — `https://pub-{city}{state}.escribemeetings.com`. Page loads = confirmed.
3. **BoardDocs** — `https://go.boarddocs.com/{state}/{city}/Board.nsf/Public`.
4. **CivicPlus** — Check city agenda page for `civicplus.com` or `municode.com` links.
5. **PrimeGov** — `https://{city}.primegov.com`.
6. **City website** — Look for "Agendas & Minutes" page, which usually links to their platform.

### Step 2: Find the Budget Data Source (5-10 min)

NC cities: Go to `https://linc.osbm.nc.gov/explore/dataset/government/`, find the exact municipality name (case-sensitive), verify data exists for both `government` and `property-tax-rate` datasets.

Other states: Find the equivalent open fiscal data API. Each state is different.

### Step 3: Determine Databricks Filter (10-20 min)

```sql
-- Cities use Residence_Addresses_City
SELECT COUNT(*) FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_nc_uniform
WHERE UPPER(Residence_Addresses_City) = 'RALEIGH'

-- Counties use Residence_Addresses_County
SELECT COUNT(*) FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_nc_uniform
WHERE UPPER(Residence_Addresses_County) = 'WAKE'
```

### Step 4: Research Local News Outlets (15-30 min)

Identify 7-12 local sources: major newspaper, TV affiliates (ABC/NBC/CBS/FOX), independent outlets, Axios local, public radio. You need their domain names for the search filter.

### Step 5: Write city_config.json (15-30 min)

Copy an existing config and modify. `topic_to_issue_map` can be left empty — script 06b auto-generates it.

### Step 6: Run the Pipeline (~30-60 min)

```bash
# Core pipeline
uv run python briefing_poc/scripts/01_collect_legislative.py --city {slug}
uv run python briefing_poc/scripts/02_collect_budget.py --city {slug}
uv run python briefing_poc/scripts/03_extract_pdfs.py --city {slug}
uv run python briefing_poc/scripts/04_run_analysis.py --city {slug}

# Constituent analysis (requires Databricks + AWS credentials)
AWS_PROFILE=work uv run python briefing_poc/scripts/06_collect_constituent_data.py --city {slug}
uv run python briefing_poc/scripts/06b_map_topics_to_issues.py --city {slug}
uv run python briefing_poc/scripts/07_council_vs_constituent.py --city {slug}

# Discussion & quick wins
uv run python briefing_poc/scripts/08_collect_discussions.py --city {slug}
uv run python briefing_poc/scripts/09_generate_quick_wins.py --city {slug}

# Final briefing (run last — pulls from everything above)
uv run python briefing_poc/scripts/05_assemble_briefing.py --city {slug}
```

All scripts are resumable — safe to re-run.

### Step 7: Verify Output

Check these files exist and look reasonable:
- `cities/{slug}/data/legistar/matters.json` — 50+ items
- `cities/{slug}/data/analysis/pass1_legislative_overview.json` — 5-10 topic areas
- `cities/{slug}/data/constituent/demographics.json` — voter counts
- `cities/{slug}/data/briefing/*_briefing.md` — 5-15 pages

---

## Connector Priority: What to Build Next

We have 3 connectors built. The output schema is locked in, so each new connector is a thin translation layer.

### Current Coverage

| Connector | Platform | Cities | Status |
|-----------|----------|--------|--------|
| Legistar | Granicus (REST API) | ~1,800 | Built |
| BoardDocs | Diligent (AJAX scraping) | ~1,200 | Built |
| eSCRIBE | Diligent (JSON API) | ~200 | Built |
| **Subtotal** | | **~3,200** | |

### Next Connectors (Priority Order)

#### 1. CivicPlus (CivicClerk) — ~4,200 cities

**This is the clear #1.** More customers than our existing 3 connectors combined. Building this one connector roughly doubles our total coverage.

- **API:** Has a public API (requires access token from CivicPlus Support). Also has an Integration Hub.
- **Estimated build time:** 1-2 days (API-based, same pattern as Legistar).
- **Note:** CivicPlus also owns Municode Meetings. Investigate whether Municode Meetings shares the same API as CivicClerk — if so, one connector covers both.

#### 2. iCompass (Diligent) — ~500 cities

Part of the Diligent family (same parent as BoardDocs and eSCRIBE).

- **API:** No public API documented — likely requires scraping.
- **Estimated build time:** 1-2 days (scraping, similar to BoardDocs approach).

#### 3. PrimeGov / OneMeeting — ~180 cities (big metros)

Covers Los Angeles, San Francisco, San Antonio, Las Vegas, Honolulu.

- **API:** Semi-public — already reverse-engineered by the Council Data Project (civic-scraper).
- **Estimated build time:** 1 day.

#### 4. Peak Agenda (Granicus) — unknown count, small/medium cities

Part of Granicus (same as Legistar), targets municipalities under 70,000 population. May use similar API architecture — worth investigating.

- **Estimated build time:** 1-2 days (potentially similar to Legistar).

### Skip

- **Novus Agenda (Granicus)** — EOL September 2025. Cities migrating to OneMeeting or Peak Agenda. Not worth building a connector for a deprecated platform.

### Coverage Projection

| Step | Connector Added | Cities Added | Cumulative |
|------|----------------|-------------|------------|
| Current | Legistar + BoardDocs + eSCRIBE | ~3,200 | ~3,200 |
| +1 | CivicPlus | ~4,200 | ~7,400 |
| +2 | iCompass | ~500 | ~7,900 |
| +3 | PrimeGov/OneMeeting | ~180 | ~8,100 |
| +4 | Peak Agenda | ~200-500 | ~8,300-8,600 |

Some overlap between platforms (cities may use different systems for different boards), so actual unique city counts will be somewhat lower.

---

## Automation Roadmap

### What's Manual Today

| Step | Time | Automatable? |
|------|------|-------------|
| Find legislative system | 10-30 min | Mostly — try known URL patterns |
| Find Legistar client slug | 5-15 min | Yes — brute-force slug patterns |
| Find eSCRIBE meeting types | 10-15 min | Partially — scrape the dropdown |
| Find LINC municipality name | 5 min | Yes — fuzzy match against API |
| Determine Databricks filter | 10-20 min | Yes — query + fuzzy match |
| Research local news outlets | 15-30 min | Partially — web search + LLM |
| Write city_config.json | 15-30 min | Partially — generate from discovery |

**Total manual time per city: ~1.5-3 hours.**

### Tier 1: Automate Discovery (~1 week to build)

Build a `00_discover_city.py` script that auto-discovers platform, budget source, Databricks filter, and news outlets. Estimated success rate: ~60-70% for cities on known platforms. Cuts per-city manual work from 1.5-3 hours to ~30 minutes.

### Tier 2: Config Generator (~1 week to build)

Combine all discovery functions into a single command that generates `city_config.json` with human review for: governing body name, eSCRIBE meeting types, Haystaq issue columns, news outlet list.

### Tier 3: Batch Onboarding (1-2 weeks to build)

Batch-discover and run pipeline for all cities in a state. Requires: target city list, parallel execution, error reporting, human review queue.

---

## State-by-State Budget Data

The hard problem for national scaling. LINC works for all NC cities, but each state has its own source:

| Approach | States | Effort |
|----------|--------|--------|
| Socrata-based (similar to LINC) | CA, NY, and others | 1-2 days per state |
| Partial data available | TX, FL | 2-3 days per state |
| No centralized source | Many states | Skip budget section or extract from city PDFs |

**Recommendation:** Identify top 10 states by GoodParty candidate volume, build collectors for those first.

---

## National Scaling Assessment

### The Municipal Landscape

| Tier | Population | Count | Digital Platform? |
|------|-----------|-------|-------------------|
| Large cities (100k+) | 100,000+ | ~310 | Almost all use a major platform |
| Mid-size cities (25k-100k) | 25-100k | ~880 | Most use a major platform |
| Small cities (10k-25k) | 10-25k | ~1,400 | ~60% use a platform |
| Small towns (<10k) | Under 10k | ~16,900 | ~20-30% use any platform |

**Key insight:** Covering the top ~2,500 municipalities (25k+ population) reaches the vast majority of the US population while only requiring 6-7 collectors.

### Cost at Scale

| Scale | Cities | LLM Cost | Effort |
|-------|--------|----------|--------|
| NC only | ~50 | ~$5 | 2-3 weeks |
| Top 100 US cities | 100 | ~$10 | 1-2 months |
| 25k+ population | ~2,500 | ~$250 | 3-6 months |
| All US municipalities | ~19,500 | ~$2,000 | 12+ months |

LLM costs are negligible. The real cost is engineering time for collectors, state budget sources, and per-city configuration.

### Realistic Automation Rates

| Target | Automation Rate | Human Time Per City |
|--------|----------------|---------------------|
| NC cities (on Legistar or eSCRIBE) | ~90% | ~10 min review |
| Top 100 US cities | ~70% | ~30 min review |
| All 2,500 cities (25k+) | ~50% | Variable |
| Full 19,500 municipalities | Not feasible without LLM-assisted scraping | N/A |

---

## Recommended Next Steps (Ordered by Impact)

1. **Build `00_discover_city.py`** (Tier 1 automation) — cuts per-city work from hours to minutes. ~1 week.

2. **Build CivicPlus connector** — doubles coverage from ~3,200 to ~7,400 cities. 1-2 days.

3. **Batch-onboard remaining NC cities** — prove the workflow at state scale (~50 cities). 2-3 weeks with Tier 1 tooling.

4. **Add one more state** (e.g., CA or NY) — forces generalization of budget data and Haystaq templates. 2-3 weeks.

5. **Build iCompass + PrimeGov connectors** — fills the next coverage gaps. 2-3 days total.

6. **Decide on the long tail** — do we need cities under 25k? If yes, invest in LLM-assisted scraping. If no, focus on the ~2,500 cities where 80% of the US population lives.
