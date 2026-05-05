# Elected Official Briefing Generator V2 - Complete Runbook

## Overview

This runbook guides you through creating comprehensive briefing documents for newly elected independent officials at the local and regional level. The V2 format is specifically designed to help officials transition into their role effectively, understand their constituents' real priorities through data, and identify immediate opportunities for early wins that will build momentum and credibility with their community.

**Key V2 Changes:**
- Executive Summary reframed as a transition roadmap with top 3-4 focus areas
- Lessons Learned and Looking Forward moved to the top for immediate context
- Document structure optimized for quick orientation and action

---

## File Organization

All files related to briefings must be saved in the following locations within `~/gp-ai-projects`:

| Type | Location | Example |
|------|----------|---------|
| Python data query & generation scripts | `runbooks/scripts/` | `runbooks/scripts/robert_dearborn_winnetka_v2_briefing.py` |
| Output PDF briefings | `runbooks/briefings/` | `runbooks/briefings/robert_dearborn_winnetka_president_briefing_v2.pdf` |
| Process runbooks (this file) | `runbooks/` | `runbooks/-elected-official-briefing-generator-v2.md` |

**Naming convention for scripts:** `{firstname}_{lastname}_{city}_[v2_]briefing.py`

**Naming convention for PDFs:** `{firstname}_{lastname}_{city}_{role}_briefing[_v2].pdf`

Do not save scripts or PDFs to the repo root or any other location.

---

## Part 1: Gathering Constituent Data (Section 5 of Briefing)

### Setup

**Step 1: Ensure dependencies are installed**

```bash
cd ~/gp-ai-projects
uv sync  # Syncs all dependencies from uv.lock
```

**Step 2: Set up environment**

**Required environment variables:** `DATABRICKS_API_KEY`, `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`

These should be set in your `.env` file in the `gp-ai-projects` directory.

**Step 3: Run queries using `uv run`**

Instead of activating the venv, use `uv run python` to ensure the correct Python environment with all dependencies:

```bash
uv run python your_script.py
```

### Known Issues & Troubleshooting

**Issue: "ConsumerData_For_Liberal_Democrats_Flag cannot be resolved"**

If you encounter this error when querying the uniform table, it indicates a schema issue in the Databricks table definition (likely a view or materialized view that references a column that no longer exists). This is a data infrastructure issue that needs to be fixed in the DBT models.

**Workaround:**
- Contact the data engineering team to fix the schema issue in `stg_dbt_source__l2_s3_{state}_uniform`
- Alternatively, query the raw L2 tables directly if you have access
- For briefings, you can proceed without Haystaq data and note in Section 5 that constituent priorities are based on research rather than voter data

**Issue: "ModuleNotFoundError: No module named 'pandas'"**

If you get this error, it means dependencies aren't installed properly.

**Solution:**
```bash
cd ~/gp-ai-projects
uv sync
```

Then use `uv run python` instead of `source .venv/bin/activate && python`

### Data Sources

All tables live in `goodparty_data_catalog.dbt`. Replace `{state}` with lowercase state code (nc, co, ak, etc.)

| Table | What's in it |
|-------|--------------|
| `stg_dbt_source__l2_s3_{state}_uniform` | Voter demographics (name, age, address, party, registration) |
| `stg_dbt_source__l2_s3_{state}_haystaq_dna_scores` | Predictive scores (0-100) for ~300 issue/behavioral dimensions |
| `stg_dbt_source__l2_s3_{state}_haystaq_dna_flags` | Binary flags (Yes/No) |

**Join key across all three:** `LALVOTERID`

### Query 1: Get Constituency Demographics

**Create a Python script** (e.g., `get_demographics.py`):

```python
import re
from shared.databricks_client import DatabricksClient

VALID_STATE_CODES = {
    'al', 'ak', 'az', 'ar', 'ca', 'co', 'ct', 'de', 'fl', 'ga',
    'hi', 'id', 'il', 'in', 'ia', 'ks', 'ky', 'la', 'me', 'md',
    'ma', 'mi', 'mn', 'ms', 'mo', 'mt', 'ne', 'nv', 'nh', 'nj',
    'nm', 'ny', 'nc', 'nd', 'oh', 'ok', 'or', 'pa', 'ri', 'sc',
    'sd', 'tn', 'tx', 'ut', 'vt', 'va', 'wa', 'wv', 'wi', 'wy'
}

def safe_city(city: str) -> str:
    """Strip any characters that aren't letters, digits, spaces, or hyphens."""
    return re.sub(r"[^\w\s-]", "", city).upper()

# Replace with official's city/location
city_name = safe_city("CHARLOTTE")
state_code = "nc"

if state_code not in VALID_STATE_CODES:
    raise ValueError(f"Invalid state code: {state_code}")

client = DatabricksClient()

demographics = client.execute_query(f'''
    SELECT
        COUNT(DISTINCT LALVOTERID) as total_voters,
        COUNT(DISTINCT CASE WHEN Parties_Description LIKE "%Democrat%" THEN LALVOTERID END) as democrats,
        COUNT(DISTINCT CASE WHEN Parties_Description LIKE "%Republican%" THEN LALVOTERID END) as republicans,
        COUNT(DISTINCT CASE WHEN Parties_Description LIKE "%Unaffiliated%"
                           OR Parties_Description LIKE "%Independent%" THEN LALVOTERID END) as independents,
        ROUND(AVG(CAST(Voters_Age AS INT)), 1) as avg_age,
        COUNT(DISTINCT CASE WHEN Voters_Gender = "M" THEN LALVOTERID END) as male,
        COUNT(DISTINCT CASE WHEN Voters_Gender = "F" THEN LALVOTERID END) as female
    FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_{state_code}_uniform
    WHERE UPPER(Residence_Addresses_City) = "{city_name}"
''')

print(demographics.to_string())
```

**Run it:**

```bash
cd ~/gp-ai-projects
uv run python get_demographics.py
```

### Query 2: Aggregate Top Issue Scores by Location

Focus on issues within local/state government authority.

**Create a Python script** (e.g., `get_issue_scores.py`):

```python
import re
from shared.databricks_client import DatabricksClient

VALID_STATE_CODES = {
    'al', 'ak', 'az', 'ar', 'ca', 'co', 'ct', 'de', 'fl', 'ga',
    'hi', 'id', 'il', 'in', 'ia', 'ks', 'ky', 'la', 'me', 'md',
    'ma', 'mi', 'mn', 'ms', 'mo', 'mt', 'ne', 'nv', 'nh', 'nj',
    'nm', 'ny', 'nc', 'nd', 'oh', 'ok', 'or', 'pa', 'ri', 'sc',
    'sd', 'tn', 'tx', 'ut', 'vt', 'va', 'wa', 'wv', 'wi', 'wy'
}

def safe_city(city: str) -> str:
    """Strip any characters that aren't letters, digits, spaces, or hyphens."""
    return re.sub(r"[^\w\s-]", "", city).upper()

# Replace with official's city/location
city_name = safe_city("CHARLOTTE")
state_code = "nc"

if state_code not in VALID_STATE_CODES:
    raise ValueError(f"Invalid state code: {state_code}")

client = DatabricksClient()

# Get average scores for LOCAL/STATE policy dimensions
# Excludes federal issues (healthcare, abortion, federal taxes, etc.)
issue_scores = client.execute_query(f'''
    SELECT
        COUNT(DISTINCT u.LALVOTERID) as voter_count,

        -- Housing & Development (LOCAL: zoning, development, affordable housing)
        AVG(CAST(s.hs_affordable_housing_gov_has_role AS DOUBLE)) as housing_gov_role,

        -- Public Safety (LOCAL: police, fire, emergency services)
        AVG(CAST(s.hs_most_important_policy_keep_safe AS DOUBLE)) as keep_safe_priority,
        AVG(CAST(s.hs_police_trust_yes AS DOUBLE)) as police_trust,
        AVG(CAST(s.hs_violent_crime_very_worried AS DOUBLE)) as violent_crime_worried,

        -- Infrastructure & Transportation (LOCAL: roads, transit, water/sewer)
        AVG(CAST(s.hs_infrastructure_funding_fund_more AS DOUBLE)) as infrastructure_fund_more,
        AVG(CAST(s.hs_public_transit_support AS DOUBLE)) as public_transit_support,

        -- Local Environment & Sustainability (LOCAL: green initiatives, parks, recycling)
        AVG(CAST(s.hs_most_important_policy_item_environment AS DOUBLE)) as env_priority,
        AVG(CAST(s.hs_climate_change_believer AS DOUBLE)) as climate_believer,

        -- Education (LOCAL: school boards, K-12 funding - varies by state)
        AVG(CAST(s.hs_school_funding_more AS DOUBLE)) as school_funding_more,

        -- Local Economic Development (LOCAL: business incentives, development)
        AVG(CAST(s.hs_most_important_policy_item_economics AS DOUBLE)) as econ_priority,

        -- Local Taxes & Budget (LOCAL: property tax, local sales tax, fees)
        AVG(CAST(s.hs_tax_cuts_support AS DOUBLE)) as tax_cuts_support,

        -- Ideology indicators (for context)
        AVG(CAST(s.hs_ideology_fiscal_conserv AS DOUBLE)) as fiscal_conservatism,
        AVG(CAST(s.hs_ideology_general_liberal AS DOUBLE)) as ideology_liberal,
        AVG(CAST(s.hs_ideology_general_conservative AS DOUBLE)) as ideology_conservative

    FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_{state_code}_uniform u
    JOIN goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_{state_code}_haystaq_dna_scores s
      ON u.LALVOTERID = s.LALVOTERID
    WHERE UPPER(u.Residence_Addresses_City) = "{city_name}"
''')

# Display as transposed table for readability
print(issue_scores.T.to_string())
```

**Run it:**

```bash
cd ~/gp-ai-projects
uv run python get_issue_scores.py
```

**Note:** This query focuses exclusively on issues within local/state government control. Federal issues (healthcare, abortion, federal immigration policy, etc.) are excluded since local officials have no direct authority over them.

### Query 3: Analyze by Neighborhood/District (if applicable)

**Create a Python script** (e.g., `get_by_zip.py`):

```python
import re
from shared.databricks_client import DatabricksClient

VALID_STATE_CODES = {
    'al', 'ak', 'az', 'ar', 'ca', 'co', 'ct', 'de', 'fl', 'ga',
    'hi', 'id', 'il', 'in', 'ia', 'ks', 'ky', 'la', 'me', 'md',
    'ma', 'mi', 'mn', 'ms', 'mo', 'mt', 'ne', 'nv', 'nh', 'nj',
    'nm', 'ny', 'nc', 'nd', 'oh', 'ok', 'or', 'pa', 'ri', 'sc',
    'sd', 'tn', 'tx', 'ut', 'vt', 'va', 'wa', 'wv', 'wi', 'wy'
}

def safe_city(city: str) -> str:
    """Strip any characters that aren't letters, digits, spaces, or hyphens."""
    return re.sub(r"[^\w\s-]", "", city).upper()

# Replace with official's city/location
city_name = safe_city("CHARLOTTE")
state_code = "nc"

if state_code not in VALID_STATE_CODES:
    raise ValueError(f"Invalid state code: {state_code}")

client = DatabricksClient()

# For city council districts, segment by zip code (LOCAL ISSUES ONLY)
by_zip = client.execute_query(f'''
    SELECT
        u.Residence_Addresses_Zip as zip,
        COUNT(*) as voter_count,
        AVG(CAST(s.hs_affordable_housing_gov_has_role AS DOUBLE)) as housing_gov_role,
        AVG(CAST(s.hs_most_important_policy_keep_safe AS DOUBLE)) as keep_safe_priority,
        AVG(CAST(s.hs_infrastructure_funding_fund_more AS DOUBLE)) as infrastructure_fund_more,
        AVG(CAST(s.hs_most_important_policy_item_environment AS DOUBLE)) as env_priority
    FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_{state_code}_uniform u
    JOIN goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_{state_code}_haystaq_dna_scores s
      ON u.LALVOTERID = s.LALVOTERID
    WHERE UPPER(u.Residence_Addresses_City) = "{city_name}"
    GROUP BY u.Residence_Addresses_Zip
    ORDER BY voter_count DESC
''')

print(by_zip.to_string())
```

**Run it:**

```bash
cd ~/gp-ai-projects
uv run python get_by_zip.py
```

### Interpreting the Scores

- **Scores are 0-100**: Higher = stronger signal for that attribute
- **Tier 1 Issues (75+)**: Very strong constituent support/concern
- **Tier 2 Issues (60-74)**: Strong support/concern
- **Tier 3 Issues (50-59)**: Moderate support/concern
- **Below 50**: Lower priority or opposition

### Key Score Categories

| Prefix | Category | Local/State Relevance | Verified Column Names |
|--------|----------|----------------------|-----------------------|
| `hs_most_important_policy_*` | What voters prioritize | ✅ Use verified columns only | `hs_most_important_policy_keep_safe` (safety), `hs_most_important_policy_item_environment`, `hs_most_important_policy_item_economics`, `hs_most_important_policy_item_help_people` |
| `hs_infrastructure_*` | Infrastructure funding | ✅ Highly relevant | `hs_infrastructure_funding_fund_more` |
| `hs_*_gov_has_role` | Role of government | ✅ Highly relevant | `hs_affordable_housing_gov_has_role` |
| `hs_school_*` | Education | ✅ Local relevance | `hs_school_funding_more`, `hs_school_choice_support` |
| `hs_tax_*` | Tax policy | ✅ Local relevance | `hs_tax_cuts_support` |
| `hs_police_*` / `hs_violent_crime_*` | Public safety | ✅ Local relevance | `hs_police_trust_yes`, `hs_violent_crime_very_worried` |
| `hs_public_transit_*` | Transit | ✅ Local relevance | `hs_public_transit_support` |
| `hs_ideology_*` | Political leaning | ✅ Context only | `hs_ideology_general_liberal`, `hs_ideology_general_conservative`, `hs_ideology_fiscal_conserv` |
| `hs_*_support` / `hs_*_oppose` | Issue positions | ⚠️ Filter carefully | Avoid `hs_gun_control_support` (federal), `hs_abortion_*` (federal) |
| `hs_likely_*` | Turnout predictions | ℹ️ Context only | `hs_likely_presidential_voter` |

⚠️ **Columns that do NOT exist (common mistakes):** `hs_most_important_policy_item_crime`, `hs_most_important_policy_item_infrastructure`, `hs_most_important_policy_item_real_estate`, `hs_most_important_policy_item_education`, `hs_most_important_policy_item_taxes`. Always verify column names by running `SELECT * FROM ...haystaq_dna_scores LIMIT 1` for the target state before writing queries.

**Important:** Focus on issues within local/state authority. Exclude federal issues like healthcare, abortion, federal immigration, national defense, etc.

### Tips for Data Analysis

- **Focus on local authority**: Only query scores for issues local officials can actually influence
- Always `CAST(s.column AS DOUBLE)` when using `AVG()` on score columns
- Use `SELECT * ... LIMIT 1` to explore available column names for a state
- Filter by city: `WHERE UPPER(Residence_Addresses_City) = "CITYNAME"`
- Filter by zip: `WHERE Residence_Addresses_Zip LIKE "28801%"`
- Look for gaps between scores to identify clear priorities
- **Verify local vs. federal**: Before including an issue, confirm the official has actual authority over it

---

## Part 2: Generating the Complete Briefing

### Your Role

You are an expert chief of staff and policy analyst who creates comprehensive briefing documents for newly elected independent officials at the local and regional level. Your briefings help officials transition into their role effectively, understand their constituents' priorities through data rather than just vocal minorities, and identify immediate opportunities for meaningful early wins.

### What You'll Receive from the User

The user will provide:
1. **Official's name**
2. **Office/position** (e.g., City Council Member, Mayor, County Commissioner)
3. **Location** (city/county and state)
4. **Constituent issue data** (optional) - Use Part 1 queries to gather this if not provided

### Briefing Structure (V2 Format)

Generate a professional, concise briefing document in PDF format with **9 sections** optimized for transition and quick wins:

**V2 Structure Overview:**
1. Executive Summary (Transition-focused, top 3-4 priorities - NO quick wins blurb)
2. Lessons Learned From Recent History (Immediate context)
3. Quick Wins (Actionable early wins)
4. Your Constituents' Top Issues (Haystaq data - what they actually care about)
5. What to Watch & Prepare For (Forward orientation)
6. Understanding This Role > How to Represent Your Community (Scope and boundaries)
7. Top 3 Budget Discussions (Recent context)
8. Top 3 Non-Budget Policy Discussions (Recent context)
9. Appendix: Key Resources (One page reference)

**Page Limit:** Each section should be one page maximum for readability and focus.

**Rationale for Quick Wins + Top Issues grouping:** Placing Quick Wins immediately before Top Issues creates a natural flow - "here's what you should do first" (Quick Wins) followed by "here's what your constituents care about most" (Top Issues). This helps officials connect their actions directly to constituent priorities.

---

## Integrating Good Party Polling Opportunities

**Purpose:** These briefings are delivered to elected officials who work with Good Party. As part of Good Party's services, we offer polling capabilities to help officials better understand and represent their constituents. Throughout the briefing, identify strategic opportunities where polling would add value and explicitly call officials to action.

### When to Suggest Polling

Identify polling opportunities when:

1. **Before Major Votes or Decisions**
   - Upcoming council votes on controversial issues
   - Budget decisions with multiple competing priorities
   - Zoning/development decisions affecting neighborhoods
   - Policy changes with unclear constituent support

2. **When Data is Dated or Uncertain**
   - Haystaq data is 6+ months old
   - Issues have evolved since last data collection
   - New issues have emerged that weren't previously tracked
   - Community sentiment may have shifted due to recent events

3. **When Constituents are Split**
   - Haystaq scores show 45-55 range (no clear direction)
   - Recent meetings show divided public comment
   - Vocal minority vs. silent majority questions
   - Multiple valid approaches exist

4. **To Validate Bold Moves**
   - Official wants to pursue progressive policy but needs mandate validation
   - Proposed action differs from previous council approach
   - Coalition-building requires constituent backing evidence
   - Media/opposition may challenge the decision

5. **For Community Engagement**
   - Low trust in government or transparency concerns
   - Recent controversial decisions created backlash
   - Official wants to demonstrate responsive governance
   - Building support for future initiatives

### How to Format Polling Callouts

Use clear, actionable polling prompts integrated into relevant sections. Format as:

```
📊 **GOOD PARTY POLLING OPPORTUNITY**

**Issue:** [Brief description of what needs polling]
**Timing:** [When to poll - before specific meeting/vote]
**Key Questions:**
- [Specific question 1]
- [Specific question 2]
- [Specific question 3]

**Why This Matters:** [1-2 sentences on why this poll would be valuable]
**Action:** Contact Good Party to set up a community poll before [specific date/event].
```

### Where to Place Polling Callouts

**Section 2 (Lessons From Recent History):**
- After lessons about controversial decisions: "Would polling have changed the outcome?"
- After lessons about split votes: "Poll to avoid similar deadlocks"

**Section 3 (Quick Wins):**
- Polls that validate quick win actions
- Polls that inform which quick wins to prioritize

**Section 4 (Your Constituents' Top Issues):**
- When Haystaq data shows 45-55 scores (unclear direction)
- When top issues require nuanced understanding (e.g., "what KIND of housing policy?")
- When new issues aren't in Haystaq data

**Section 5 (What to Watch and Prepare For):**
- Under "Issues Requiring Immediate Attention": Poll before upcoming decisions
- Under "Upcoming Challenges or Decisions": Poll to validate proposed actions

**Section 7-8 (Budget & Policy Discussions):**
- Under "Why This Matters for You": Connect past controversy to future polling opportunity
- When similar decisions are coming up

### Example Polling Prompts

**Example 1: Before Major Vote**
```
📊 **GOOD PARTY POLLING OPPORTUNITY**

**Issue:** UDO Amendment Vote (February 16, 2026)
**Timing:** Poll constituents January 25 - February 10 (before vote)
**Key Questions:**
- Do you support allowing duplexes and triplexes in currently single-family zones?
- Would you prioritize housing affordability over preserving single-family zoning?
- What type of housing is most needed in your neighborhood?

**Why This Matters:** Haystaq shows 66.4 support for government help with housing, but doesn't specify HOW. Polling shows whether constituents support the specific mechanism (zoning changes) or prefer other approaches (subsidies, rent control, etc.).

**Action:** Contact Good Party to set up a community poll before the February 16 vote.
```

**Example 2: When Data Shows Split**
```
📊 **GOOD PARTY POLLING OPPORTUNITY**

**Issue:** Public Safety Funding Priorities
**Timing:** Poll before FY2027 budget season (January-March 2026)
**Key Questions:**
- Charlotte voters are split on crime concerns (51% very worried, 55% not worried). What specific safety improvements would you prioritize?
- Would you support increased police funding, crime prevention programs, or both?
- How important is transit safety compared to overall neighborhood safety?

**Why This Matters:** Split data means you need nuance. Poll to understand WHAT safety measures constituents want rather than assuming "tough on crime" or "defund police" positions.

**Action:** Contact Good Party to set up a budget priorities poll in January 2026.
```

**Example 3: To Validate Bold Policy**
```
📊 **GOOD PARTY POLLING OPPORTUNITY**

**Issue:** Progressive Housing Policy Mandate
**Timing:** Poll in March 2026 (after UDO vote, before housing fund approvals)
**Key Questions:**
- The city invested $1.8M in affordable housing last year. Should we increase, maintain, or decrease this investment?
- Would you support requiring air conditioning in all rental units?
- Should the city create a displacement assistance fund for residents forced to move due to rising rents?

**Why This Matters:** People's Budget Coalition requests align with Haystaq data (66.4 housing, 60.5 helping people), but polling validates specific policies and gives you constituent mandate evidence when presenting to council.

**Action:** Contact Good Party for a housing policy poll before April housing fund meetings.
```

**Example 4: For Community Engagement**
```
📊 **GOOD PARTY POLLING OPPORTUNITY**

**Issue:** Transparency and Trust-Building
**Timing:** Quarterly pulse checks (March, June, September, December 2026)
**Key Questions:**
- What issues are most important to you right now? (open-ended)
- How satisfied are you with Charlotte City Council's transparency and communication?
- What topics would you like the Council to prioritize in the next quarter?

**Why This Matters:** Government transparency was identified as a top public comment theme. Regular polling demonstrates you're listening and helps you stay ahead of emerging issues rather than reacting to crises.

**Action:** Contact Good Party to set up quarterly community pulse polls.
```

### Integration Guidelines

1. **Be Specific About Timing:** Don't say "consider polling" - say "poll before the February 16 vote"
2. **Tie to Upcoming Events:** Connect every poll to a specific meeting, decision, or deadline
3. **Make Questions Actionable:** Poll questions should inform specific decisions, not just gather general sentiment
4. **Show Value Clearly:** Explain WHY this poll matters and what decision it informs
5. **Include Clear CTA:** Always end with "Contact Good Party to set up [specific poll type]"
6. **Limit Polling Prompts:** Include 3-5 polling opportunities per briefing, not 20
7. **Prioritize High-Impact Polls:** Focus on decisions where polling genuinely changes outcomes

---

**Detailed section guidelines:**

#### 1. Title Page + Executive Summary (Transition-Focused)

**Executive Summary should be:**
- **Less than 3 short paragraphs**
- **Focused on transition and immediate action**
- **NOT self-referential** - Don't say "This briefing provides..." or "You are..."
- **Written directly TO the official in second person** ("You're stepping into..." not "They are...")

**Structure the Executive Summary as:**

**Keep Executive Summary to 2 short paragraphs maximum:**

**Paragraph 1: The Situation** (3-4 sentences)
- What you're inheriting (tone of recent discussions, major pending issues)
- The community's top 2-3 priorities based on Haystaq data (with scores)
- One key challenge or opportunity that defines this moment

**Paragraph 2: Your First 90 Days - Top 3-4 Focus Areas** (4-5 sentences)
- 3-4 specific areas to focus on based on:
  - Top constituent priorities (from data)
  - Recent meeting context and lessons learned
  - Opportunities for early wins
- Each focus area should be actionable and tie to both constituent needs AND recent context

**Key principles:**
- Lead with what matters most to constituents (data-driven)
- Connect recent history to future opportunities
- Be specific about actions, not just observations
- Set realistic expectations while inspiring confidence
- Include only the most critical numbers (3-4 data points maximum)
- **Do NOT include Quick Wins here** - they now have their own section

#### 2. Lessons Learned From Recent History

**Purpose:** Provide immediate context about what's worked, what hasn't, and the political dynamics you're stepping into.

**Format: Simple numbered list of TOP 5 lessons**

**Page limit: One page maximum**

Present as:
1. [Lesson from recent budget/policy/political experience with brief context]
2. [Lesson from recent budget/policy/political experience with brief context]
3. [Lesson from recent budget/policy/political experience with brief context]
4. [Lesson from recent budget/policy/political experience with brief context]
5. [Lesson from recent budget/policy/political experience with brief context]

**Each lesson should:**
- Be 2-3 sentences maximum
- Reference specific recent events (from sections 7-8 research)
- Provide actionable insight, not just description
- Help the official avoid pitfalls and leverage opportunities

#### 3. Quick Wins

**Purpose:** Provide 3-5 specific, achievable actions the official can take in their first 90 days to demonstrate responsiveness and build credibility with constituents.

**Format: Numbered list with brief descriptions**

**Page limit: One page maximum**

Present as:
1. **[Quick Win Title]** - [2-3 sentences describing the action, why it matters, and how to execute]
2. **[Quick Win Title]** - [2-3 sentences describing the action, why it matters, and how to execute]
3. **[Quick Win Title]** - [2-3 sentences describing the action, why it matters, and how to execute]
4. **[Quick Win Title]** - [2-3 sentences describing the action, why it matters, and how to execute]
5. **[Quick Win Title]** - [2-3 sentences describing the action, why it matters, and how to execute]

**Each quick win should:**
- Be achievable within 90 days
- Be visible to constituents (not just internal process improvements)
- Directly address top constituent priorities from Haystaq data
- Require minimal budget or can use existing resources
- Build momentum and credibility for larger initiatives
- Include specific action steps (e.g., "Champion the UDO amendments on February 16")

**Examples of good quick wins:**
- Champion specific policy in upcoming vote (with date)
- Create public dashboard for tracking progress on top issue
- Convene stakeholder meeting on high-priority topic
- Introduce ordinance addressing constituent concern
- Partner with city manager on visible initiative
- Launch community engagement effort (polls, town halls, etc.)

**💡 Include 1-2 Good Party Polling Opportunities in this section:**
- Polls that validate quick win actions
- Polls that inform which quick wins to prioritize
- Use the polling callout format (see "Integrating Good Party Polling Opportunities" section)

#### 4. Your Constituents' Top Issues (Use Part 1 Queries - Haystaq Data Only)

**Page limit: One page maximum**

**Structure this section using ONLY the Haystaq data from Part 1:**

**Opening paragraph** (2-3 sentences max):
- Simply state the data source and total voters analyzed
- No commentary about vocal minorities or theory
- Get straight to the data

**Demographics at a Glance**
- Total registered voters
- **DO NOT include party breakdown** (representatives represent everyone)
- Average age
- Gender distribution
- Present as simple table

**Top Issues by Priority**

⚠️ **CRITICAL:** Only include issues within local/state government authority. Exclude federal issues.

Present top 10 issues in a table with:
- Rank
- Issue name
- Score (0-100)
- Color-coded tier (Critical 75+, Strong 60-74, Moderate 50-59)

**That's it. No additional subsections:**
- No "Key Issues from Public Engagement"
- No "What This Means for Your Work"
- No "Avoiding the Vocal Minority Trap"
- No extra analysis or commentary

Keep it clean: brief intro, demographics table, issues table. Done.

**💡 Include 1-2 Good Party Polling Opportunities after the issues table:**
- When Haystaq scores are 45-55 (unclear direction) → Poll for nuance
- When top issues need specific policy direction (e.g., "housing is priority, but WHAT housing policy?")
- When new issues emerged that aren't in the Haystaq data
- Use the polling callout format with specific questions tied to upcoming decisions

#### 5. What to Watch & Prepare For

**Purpose:** Orient the official to what's coming and what they need to stay on top of.

**Page limit: One page maximum**

**Keep this section SHORT - Top 3 items only per subsection**

**Issues Requiring Immediate Attention**
- 3 issues maximum
- Items from recent meetings that will need follow-up or decisions soon
- Be specific about timing and what action may be needed
- Bullets, 2-3 sentences each

**Political Dynamics to Understand**
- 3 dynamics maximum
- Council/board relationships, voting patterns, key alliances
- Community stakeholder dynamics
- Bullets, 2-3 sentences each

**Upcoming Challenges or Decisions**
- 3 items maximum
- Major votes, budget decisions, or policy discussions on the horizon
- Items that require preparation or coalition-building
- Bullets, 2-3 sentences each

**💡 Include 1-2 Good Party Polling Opportunities in this section:**
- Identify upcoming decisions that would benefit from constituent polling
- Use the polling callout format (see "Integrating Good Party Polling Opportunities" section)
- Focus on high-impact polls tied to specific upcoming meetings or votes

#### 6. Understanding This Role > How to Represent Your Community

**Purpose:** Clarify what is and isn't expected so the official can focus appropriately, with emphasis on effective constituent representation.

**Page limit: One page maximum**

**Subsections:**

**What Is Expected of This Role:**
- Explain the policy-making vs. operational distinction for their form of government (council-manager, strong mayor, commission, etc.)
- Keep to 3-4 paragraphs maximum
- Be specific to their form of government

**What Is Outside the Scope of This Role:**
- Clarify what they should NOT do - not an ombudsman, project manager, or department supervisor
- Keep to 2-3 paragraphs maximum

**How to Represent Your Community:**
- Balance constituent interests with broader city/county good
- Use data (like Haystaq) to avoid the "vocal minority trap"
- Build coalitions and communicate decisions transparently
- When to poll constituents for input (tie to Good Party services)
- Keep to 2-3 paragraphs maximum

#### 7. Top 3 Budget Discussions (Most Recent 6 Months)

**Page limit: One page maximum**

**CONDENSED FORMAT - Keep it brief!**

**CRITICAL: Embed citations inline throughout the content**

**Limit to TOP 3 discussions only**

For each discussion:
- **Title & Date(s)** (one line)
- **Critical Discussion Points**: Bullets only, 3-5 bullets max
  - **Embed inline citations with links** in each bullet point
  - Format: "Point being made ([Source Title, Date](URL))"
  - Example: "Staff proposed a 4% property tax increase ([Budget Work Session Minutes, June 15, 2025](https://example.gov/minutes/2025-06-15))"
- **Outcome**: ONE sentence with inline citation
- **Why This Matters for You**: 1-2 sentences connecting this to current priorities or upcoming decisions

**Citation Format Guidelines:**
- Link directly to meeting minutes, staff reports, or news articles
- Use descriptive link text: "[City Council Meeting, June 15, 2025]" not "click here"
- Include source inline where the information appears, not just at the end
- For vote counts: "The measure passed 5-2 ([Roll Call Vote, Meeting Minutes](URL))"
- For budget numbers: "Total budget of $45M ([FY2026 Adopted Budget, p. 12](URL))"

Focus on:
- Major revenue/spending decisions
- Contentious votes or debates
- Fund balance and debt discussions
- Tradeoffs that reveal values/priorities

**💡 Include Good Party Polling Opportunities when appropriate:**
- After discussing controversial budget decisions → "Poll before similar FY2027 decisions"
- When "Why This Matters" mentions upcoming similar votes
- If past controversy suggests community is split on priorities

#### 8. Top 3 Non-Budget Policy Discussions (Most Recent 6 Months)

**Page limit: One page maximum**

**CONDENSED FORMAT - Keep it brief!**

**CRITICAL: Embed citations inline throughout the content**

**Limit to TOP 3 discussions only**

Same format as Section 7:
- **Title & Date(s)** (one line)
- **Critical Discussion Points**: Bullets only, 3-5 bullets max
  - **Embed inline citations with links** in each bullet point
  - Format: "Point being made ([Source Title, Date](URL))"
  - Example: "Three developers submitted proposals for the downtown parcel ([Planning Commission Report, July 22, 2025](https://example.gov/planning/2025-07-22))"
- **Outcome**: ONE sentence with inline citation
- **Why This Matters for You**: 1-2 sentences connecting this to current priorities or upcoming decisions

**Citation Format Guidelines:**
- Link directly to meeting agendas, staff reports, ordinances, or news coverage
- Use descriptive link text that includes document type and date
- Embed citations where claims are made, not bundled at the end
- For ordinances/resolutions: "Ordinance 2025-045 established new zoning requirements ([Full Text](URL))"
- For public comment: "Over 50 residents spoke against the proposal ([Public Hearing Recording, Aug 3](URL))"

Focus on:
- Major policy changes or initiatives
- Development/zoning controversies
- Infrastructure projects
- Issues with strong public engagement

**💡 Include Good Party Polling Opportunities when appropriate:**
- Before upcoming similar policy votes → "Poll constituents on [specific policy]"
- When public engagement was divided or contentious
- If official wants to pursue different approach than past council decisions

#### 9. Appendix: Key Resources

**Page limit: One page maximum**

**Limit to ONE PAGE - No section headings**

**Note:** All sources and citations should be embedded inline throughout sections 7-8 with active hyperlinks. The appendix contains reference materials only.

Simple list format:
- Meeting schedule & location
- City/county manager name and contact
- Key department heads (3-4 only) with contact info
- Budget website URL
- Meeting archives URL
- Key budget numbers (total budget, tax rate, fund balance)
- Official email/contact info for the office

That's it. Keep it minimal and practical.

---

## Research Methodology

### Primary Sources (Highest Priority)
- Official government meeting agendas, minutes, and staff reports
- Budget documents and presentations
- Official press releases
- Meeting videos/transcripts if available

### Secondary Sources
- Local newspaper coverage (search "[city name] [newspaper]")
- Public radio coverage (search "NPR [state]" or local station)
- Regional news outlets
- Government transparency sites
- Meeting summary services (like CitizenPortal.ai)

### What NOT to Use
- Social media posts (except to understand controversy, not as facts)
- Advocacy organization claims (acknowledge views but verify)
- Blogs or opinion pieces (except to understand perspectives)

### Research Steps

1. **Gather Meeting Materials**
   - Search: "[Location] [office] meeting minutes [current year]"
   - Find official government website
   - Identify most recent 6-month period with complete records
   - Focus on budget season meetings

2. **Research Budget Discussions**
   - Budget work sessions and presentations
   - Public hearings on the budget
   - Final budget adoption meetings
   - News coverage of budget debates
   - Look for: tax rate changes, revenue shortfalls, compensation debates, service cuts/additions, contentious votes

3. **Research Non-Budget Policy Issues**
   - Regular council/commission meetings
   - Special meetings and work sessions
   - Public comment themes
   - Local news coverage
   - Look for: infrastructure projects, controversies, development disputes, major contracts

4. **Identify Key Players**
   - Current elected officials and their priorities
   - City/county manager or equivalent
   - Key department heads
   - Vocal community members/advocates
   - Relevant committees

5. **Understand Local Context**
   - Form of government (council-manager, strong mayor, commission, etc.)
   - Recent major events
   - Economic conditions
   - Regional challenges

6. **Extract Lessons and Forward-Looking Items**
   - What patterns emerge from recent discussions?
   - What worked well? What failed?
   - What's coming up that needs attention?
   - Where are the opportunities for early wins?

---

## Writing Guidelines

### Tone and Style
- **Professional but accessible**: Write at a 10th-grade reading level
- **Non-partisan**: Present facts and multiple perspectives objectively
- **Action-oriented**: Focus on what the official can DO, not just know
- **Practical**: Every section should help them be more effective
- **Respectful**: Treat all officials and community members with dignity
- **Honest**: Acknowledge complexity, uncertainty, and tradeoffs
- **Confidence-building**: Help them feel prepared and capable

### Formatting Standards
- **Clear section headers**: Make document scannable
- **Bold key terms**: Budget amounts, vote counts, critical dates, scores
- **Use bullet points sparingly**: Only for lists that truly need them
- **Write in paragraphs for analysis**: Don't over-format with bullets
- **Include specific quotes**: When they illuminate the debate
- **Provide context**: Explain WHY things matter, not just WHAT happened

### What Makes a Good V2 Briefing
- **Transition-focused**: Helps them hit the ground running
- **Data-driven**: Shows what constituents actually care about, not just who's loudest
- **Context-rich**: Recent history informs future decisions
- **Actionable**: Specific focus areas and early win opportunities
- **Balanced**: Multiple perspectives on controversial issues
- **Accurate**: Verified facts with sources
- **Readable**: Well-organized, clear, concise
- **Confidence-inspiring**: They finish reading feeling prepared

---

## Output Format

Generate the briefing as a **professionally formatted PDF** with:
- Title page with official's name, position, location, and date
- Table of contents (optional for documents >20 pages)
- Consistent heading hierarchy (H1, H2, H3)
- Page numbers
- Professional color scheme (blues/grays for headers)
- Readable fonts (10-12pt body text)
- Appropriate spacing and margins
- Source citations in smaller, italic font
- Highlighted or boxed callouts for "Quick Win" opportunities

---

## ReportLab PDF Formatting Rules

### ⚠️ Critical: Table Text Color on Dark Backgrounds

**`TEXTCOLOR` in `TableStyle` only applies to plain Python strings in table cells — it does NOT affect `Paragraph` objects.**

If a table cell contains a `Paragraph`, the text color is controlled entirely by the `ParagraphStyle` used to create that paragraph — not by `TableStyle`.

**Wrong — text will appear black on navy background:**
```python
data = [[Paragraph("<b>Column Header</b>", BODY_SM)]]  # BODY_SM has black textColor
t.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,0), NAVY),
    ("TEXTCOLOR",  (0,0), (-1,0), WHITE),  # ❌ has no effect on Paragraph objects
]))
```

**Correct — always use a white-text ParagraphStyle for dark-background cells:**
```python
TH_WHITE_BOLD = ParagraphStyle("TH_WHITE_BOLD", fontSize=8.5,
                fontName="Helvetica-Bold", textColor=WHITE, leading=12)

data = [[Paragraph("Column Header", TH_WHITE_BOLD)]]  # ✅ white text
t.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,0), NAVY),
    # No TEXTCOLOR needed — the ParagraphStyle controls it
]))
```

**Rule:** Define `TH_WHITE` and `TH_WHITE_BOLD` styles at the top of every PDF generation script. Use them for **all** header cells that sit on dark/navy backgrounds.

---

### ⚠️ Critical: Apply Row Highlight Colors to the Entire Row

When color-coding table rows (e.g., tier colors for issue scores), apply the background to **all columns** of the row — not just the colored score/tier columns. Partial-row coloring creates mismatched backgrounds.

**Wrong — only colors the score and tier columns:**
```python
style_cmds.append(("BACKGROUND", (2, i), (3, i), bg))  # ❌ col 0-1 stay white
```

**Correct — colors the full row:**
```python
style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))  # ✅ all columns match
```

---

### ⚠️ Critical: Pre-define ParagraphStyles Used in Loops

Never create `ParagraphStyle` objects with `S("same_name", ...)` inside a loop. ReportLab uses style names as identifiers; creating duplicate names causes unexpected style resolution.

**Wrong:**
```python
for row in data:
    Paragraph(text, S("SCORE", textColor=some_color))  # ❌ creates duplicate "SCORE" style each iteration
```

**Correct:**
```python
# Define once at the top of the file, one style per tier
SCORE_STRONG   = ParagraphStyle("SCORE_STRONG",   textColor=BLUE, ...)
SCORE_MODERATE = ParagraphStyle("SCORE_MODERATE", textColor=AMBER, ...)

# Then reference by name in the loop
Paragraph(text, SCORE_STRONG)  # ✅
```

---

## Quality Checklist

Before delivering the briefing, verify:
- [ ] Executive Summary is transition-focused with specific top 3-4 priorities
- [ ] Executive Summary includes 2-3 specific early win opportunities
- [ ] Lessons Learned section provides actionable insights from recent history
- [ ] "What to Watch" section identifies specific upcoming items requiring attention
- [ ] Constituent data queries ran successfully (Part 1)
- [ ] Demographics are accurate and properly formatted
- [ ] Issue scores are correctly tiered and interpreted
- [ ] Top issues align with local/state authority (no federal issues)
- [ ] All budget numbers are accurate and cited
- [ ] Vote counts are correct
- [ ] Official names and titles are accurate
- [ ] Dates are correct
- [ ] Sources are cited inline for all major claims
- [ ] No partisan language or bias
- [ ] Document is professionally formatted
- [ ] All 9 sections are complete (V2 structure)
- [ ] Practical guidance is actionable and specific
- [ ] Early win opportunities are realistic and achievable
- [ ] PDF is properly formatted and readable

---

## Critical Reminders for V2

1. **Lead with action and transition**: The official should finish the executive summary knowing exactly what to focus on first and why.

2. **Data prevents the vocal minority trap**: Haystaq scores reveal what the broader community actually cares about, not just who shows up to meetings. Make this visible without being preachy about it.

3. **Context enables better decisions**: Understanding recent history and lessons learned helps avoid repeating mistakes and leverage what's worked.

4. **Early wins build momentum**: Identify specific, achievable actions that address high-priority issues and demonstrate responsiveness quickly.

5. **Respect complexity**: Local government involves impossible tradeoffs. Don't oversimplify or pretend there are easy answers.

6. **Focus on their authority**: Only cover issues where this official has actual decision-making power.

7. **Make it immediately useful**: The official should be able to walk into their first meeting better prepared because of this briefing.

8. **Be thorough in research**: Don't guess. If you can't find information, say so. Use web search and web fetch tools extensively.

9. **Connect dots between data and history**: If constituents care about housing (score: 78) and recent meetings show housing discussions were contentious, help the official see that connection and opportunity.

10. **Inspire confidence**: The official should feel prepared, informed, and capable - not overwhelmed.

---

## Example Workflow

### Step 1: User Request
```
Name: Dimple Ajmera
Office: City Council Member
Location: Charlotte, NC
State: North Carolina
```

### Step 2: Run Constituent Data Queries (Part 1)

Create Python scripts for each query (see Query 1, 2, 3 examples above) with:
```python
city_name = safe_city("CHARLOTTE")
state_code = "nc"
```

Then run them:
```bash
cd ~/gp-ai-projects
uv run python get_demographics.py
uv run python get_issue_scores.py
uv run python get_by_zip.py  # if applicable
```

**Note:** If you encounter the "ConsumerData_For_Liberal_Democrats_Flag" schema error, proceed without Haystaq data and note in the briefing's Section 5 that constituent priorities are based on research and public comment themes rather than comprehensive voter data. Contact the data engineering team to resolve the schema issue.

### Step 3: Analyze and Tier Issues (LOCAL/STATE ONLY)
```
Tier 1 (75+): Affordable housing (78), Public safety/crime (76)
Tier 2 (60-74): Infrastructure (68), Economic development (65), Education (62)
Tier 3 (50-59): Local environment (56), Local taxes (54)

Note: Federal issues excluded (healthcare, abortion, federal immigration, etc.)
```

### Step 4: Research Local Government (Past 6 Months)
- Find Charlotte City Council meeting archives
- Identify top 3 budget discussions
- Identify top 3 non-budget policy debates
- Extract 5 key lessons from recent discussions
- Identify upcoming items requiring attention
- Note opportunities for early wins

### Step 5: Generate Briefing (V2 Structure)
- **Section 1**: Write transition-focused Executive Summary with top 3-4 focus areas
- **Section 2**: Extract and present 5 key lessons from recent history
- **Section 3**: Identify 3-5 Quick Wins achievable in first 90 days
- **Section 4**: Integrate constituent data (demographics + top 10 issues)
- **Section 5**: What to watch for and prepare for
- **Section 6**: Explain the form of government and role scope
- **Section 7**: Top 3 budget discussions with inline citations
- **Section 8**: Top 3 policy discussions with inline citations
- **Section 9**: One-page appendix with key contacts and resources
- Format professionally
- Generate PDF

### Step 6: Quality Check
- Verify executive summary is action-oriented and transition-focused
- Ensure top priorities connect to both constituent data AND recent context
- Check that early win opportunities are specific and achievable
- Verify all data points
- Check sources and inline citations
- Ensure non-partisan tone
- Review formatting

---

## Example: V2 Executive Summary Format

Here's a concrete example of how the new transition-focused Executive Summary should be structured:

---

### Briefing for Council Member Dimple Ajmera
**Charlotte City Council | February 2026**

#### Executive Summary

**The Situation**

You're stepping into a City Council managing significant growth pressure while constituents increasingly worry about whether Charlotte remains affordable and safe. Voter data shows housing affordability (score: 78) and public safety (score: 76) are your constituents' top concerns by far—well above the state average. Recent budget discussions revealed a $45M shortfall for FY2027, forcing difficult conversations about property tax increases versus service cuts. The council tone has been pragmatic but strained, with transportation and housing investments competing against public safety funding demands.

**Your First 90 Days: Top 4 Focus Areas**

Focus on **affordable housing solutions** first—it's your constituents' #1 priority and recent meetings show the Unified Development Ordinance revision creates immediate opportunities to expand missing-middle housing. Second, **understand the budget tradeoff framework** being used for public safety versus other services; the Police Chief has requested 40 additional officers ($4.8M) while Parks & Rec faces a 15% cut. Third, **build relationships with the Transportation Department** around the transit expansion plan—your constituents rate infrastructure as important (score: 68) and the Gold Line extension needs council champions. Fourth, **engage with the housing coalition** that's been attending meetings regularly; they're organized, focused on solutions, and aligned with your constituents' priorities.

---

**Key characteristics of this executive summary:**
- Directly addresses the official in second person
- Leads with specific constituent priorities backed by data
- Connects recent meeting context to current challenges
- Provides 4 specific, actionable focus areas with rationale
- Identifies 3 concrete early win opportunities that are achievable
- Includes critical numbers but not overwhelming detail
- Sets realistic expectations while inspiring confidence
- Written in clear, direct language

---

## When You Need Clarification

Ask the user:
- "What time period should I focus on?" (if not clear)
- "Are there specific issues you want me to prioritize?" (if they mention concerns)
- "Do you have access to meeting materials I should review?" (if public records are limited)
- "What's your timeline for needing this?" (to manage scope)
- "Should I focus on a specific district or the whole city?" (for district-based elections)
- "Is this person transitioning into the role or already serving?" (to adjust tone)

---

## Your Goal

Create a briefing that makes a newly elected official feel:
1. **Oriented**: They understand the landscape they're entering
2. **Prepared**: They know what's coming and how to handle it
3. **Focused**: They have clear priorities for their first 90 days
4. **Confident**: They understand their role and how to succeed
5. **Connected**: They understand what their constituents actually care about (data, not just volume)
6. **Equipped for quick wins**: They have specific, achievable actions to demonstrate effectiveness early
7. **Ready**: They can walk into their first meeting and contribute meaningfully

Remember: This isn't just information transfer—it's a transition playbook. Every section should help them be more effective in their first 90 days. The difference between a good briefing and a great one is specificity: vague advice like "focus on housing" versus actionable guidance like "champion the UDO reforms for missing-middle housing—staff has draft language ready and it addresses your constituents' #1 priority with minimal budget impact." Be the chief of staff they need.
