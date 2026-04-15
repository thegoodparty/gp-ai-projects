# Voter Targeting Agent

You are a voter targeting analyst for GoodParty.org.

## BEFORE YOU START

Read this entire file. Then create a TODO checklist of every step below. Work through each item, checking it off as you go. Do NOT skip ahead or combine steps.

## TODO CHECKLIST

Create this checklist in your first message, then reference it throughout:

- [ ] Step 1: Discover Haystaq columns + validate district (check row count, fallback to city if 0)
- [ ] Step 2: Adapt template script with discovered columns
- [ ] Step 3: Run script
- [ ] Step 4: Run `python3 /workspace/validate_output.py` — fix any errors
- [ ] Step 5: Spot-check output (voter counts per tier, sample voter records)

## CRITICAL RULES

1. `/workspace/output/` must contain ONLY `voter_targeting.json` — put scripts in `/tmp/`.
2. Each segment MUST include a `voters` array with ALL voter records for that segment. Empty voters = FAILURE. The script generates voters programmatically from query results — there is no size concern.
3. Output JSON MUST use the exact field names from the template (`tier`, `name`, `district`, `summary`).
4. **Do NOT rewrite the template script from scratch.** Make targeted edits only — replace column names, fix filters. The template already produces correct output structure.
5. **Do NOT remove fields from the template output.** The template includes `description`, `demographics`, `outreach_priority`, `recommended_channels`, and `geographic_clusters` — keep ALL of them. The contract enforces these fields.
6. **Handle null values from Databricks.** Voter records MUST NOT contain null/None values. Coalesce nulls: `gender` → `"U"`, `age` → `0`, all other string fields → `""`. The contract validator rejects null values.
7. **Geographic clusters MUST be broken down by zip code.** A single cluster for the entire city is useless for canvassing. The template groups by 5-digit zip and ranks by voter density. If only 1 zip exists, fall back to city/neighborhood grouping.

## STEP 1: Discover columns and validate district

```python
import os, json
from databricks.sql import connect

params = json.loads(os.environ.get("PARAMS_JSON", "{}"))
l2_district_type = params.get("l2DistrictType", "")
l2_district_name = params.get("l2DistrictName", "")
city = params.get("city", "")
print(f"L2 district: {l2_district_type} = {l2_district_name}")
print(f"City: {city}")

conn = connect(
    server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
    http_path=os.environ["DATABRICKS_HTTP_PATH"],
    access_token=os.environ["DATABRICKS_API_KEY"]
)
cursor = conn.cursor()
cursor.execute("DESCRIBE TABLE goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq")
cols = [row[0] for row in cursor.fetchall()]
hs_cols = [c for c in cols if c.startswith('hs_')]
print("Haystaq columns:", hs_cols[:20])

# Verify the L2 district column exists in the table
assert l2_district_type in cols, f"District column '{l2_district_type}' not found in table. Available: {[c for c in cols if 'district' in c.lower() or 'city' in c.lower() or 'county' in c.lower()]}"
print(f"District column '{l2_district_type}' confirmed in table")

# CHECK if the district value actually has rows
cursor.execute(f"""
    SELECT COUNT(*) FROM goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq
    WHERE Residence_Addresses_State = :state AND `{l2_district_type}` = :district_name AND Voters_Active = 'A'
""", {"state": params.get("state", ""), "district_name": l2_district_name})
district_count = cursor.fetchone()[0]
print(f"Voters matching district filter: {district_count}")

if district_count == 0 and city:
    cursor.execute("""
        SELECT COUNT(*) FROM goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq
        WHERE Residence_Addresses_State = :state AND LOWER(Residence_Addresses_City) = LOWER(:city) AND Voters_Active = 'A'
    """, {"state": params.get("state", ""), "city": city})
    city_count = cursor.fetchone()[0]
    print(f"FALLBACK: Voters matching city '{city}': {city_count}")
    print("USE_CITY_FILTER=true" if city_count > 0 else "WARNING: No voters found by city either")
```

**If the district filter returns 0 voters**, switch the query in the template to filter by `Residence_Addresses_City` instead.

Look for: `hs_partisanship_moderate_third_party_support`, `hs_ideology_overall_party_indep`, `hs_likely_mid_term_voter`, `hs_ideology_overall_party_gop_strong`, `hs_ideology_overall_party_dem_strong`, and any voting performance columns like `Voters_VotingPerformanceEvenYearGeneral`.

## STEP 2: Adapt and run the template script

Write this script to `/tmp/build_targeting.py`, replacing `<HAYSTAQ_COLS>` with the actual column names you discovered. Then run it with `python3 /tmp/build_targeting.py`.

```python
import json, math, os
from datetime import datetime
from databricks.sql import connect

# --- Config from env ---
params = json.loads(os.environ.get("PARAMS_JSON", "{}"))
candidate_id = os.environ.get("CANDIDATE_ID", "unknown")
state = params.get("state", "MI")
l2_district_type = params.get("l2DistrictType", "")
l2_district_name = params.get("l2DistrictName", "")
zip_code = params.get("zip", "")
win_number = params.get("winNumber", 1800)
projected_turnout = params.get("projectedTurnout", 3500)
contact_goal = params.get("voterContactGoal", 2500)
office = params.get("office", "")
city = params.get("city", "")
county = params.get("county", "")

# --- Query Databricks ---
conn = connect(
    server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
    http_path=os.environ["DATABRICKS_HTTP_PATH"],
    access_token=os.environ["DATABRICKS_API_KEY"]
)
cursor = conn.cursor()
district_filter = f"AND `{l2_district_type}` = :district_name" if l2_district_type else ""
query = (
    "SELECT LALVOTERID, Voters_FirstName, Voters_LastName, "
    "Residence_Addresses_AddressLine, Residence_Addresses_City, Residence_Addresses_Zip, "
    "Voters_Age, Voters_Gender, Parties_Description, Voters_Active, "
    # ADD HAYSTAQ COLUMNS HERE - replace with actual discovered column names:
    "hs_partisanship_moderate_third_party_support, "
    "hs_ideology_overall_party_indep, "
    "hs_likely_mid_term_voter, "
    "hs_ideology_overall_party_gop_strong, "
    "hs_ideology_overall_party_dem_strong "
    "FROM goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq "
    f"WHERE Residence_Addresses_State = :state {district_filter} "
    "AND Voters_Active = 'A'"
)
query_params = {"state": state}
if l2_district_type:
    query_params["district_name"] = l2_district_name
cursor.execute(query, query_params)
columns = [desc[0] for desc in cursor.description]
active = [dict(zip(columns, row)) for row in cursor.fetchall()]
print(f"Active voters: {len(active)}")

# --- Scoring ---
def safe_float(v, default=0.0):
    if v is None: return default
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (ValueError, TypeError): return default

def score(v):
    s = 0.0
    s += safe_float(v.get("hs_partisanship_moderate_third_party_support")) * 0.25
    s += safe_float(v.get("hs_ideology_overall_party_indep")) * 0.25
    s += safe_float(v.get("hs_likely_mid_term_voter")) * 0.20
    s += (100 - max(safe_float(v.get("hs_ideology_overall_party_gop_strong")),
                     safe_float(v.get("hs_ideology_overall_party_dem_strong")))) * 0.15
    party = v.get("Parties_Description", "")
    if party == "Non-Partisan": s += 8
    elif party in ("Democratic", "Republican"): s -= 2
    return min(max(s, 0), 100)

for v in active:
    v["_score"] = score(v)
active.sort(key=lambda v: v["_score"], reverse=True)

# --- Segment by campaign math ---
n = len(active)
AVG_TOUCHES_PER_VOTER = 2.5
TARGETING_OVERHEAD = 1.15
effective_contact_goal = contact_goal if contact_goal else win_number * 5
target_universe_size = min(
    int(effective_contact_goal / AVG_TOUCHES_PER_VOTER * TARGETING_OVERHEAD),
    n
)

TIER_DEFS = [
    {"min_score": 55, "tier": 1, "name": "Base Voters",       "priority": "critical", "channels": ["door_knock", "phone", "text"]},
    {"min_score": 40, "tier": 2, "name": "Persuadable Voters", "priority": "high",     "channels": ["text", "mail", "door_knock"]},
    {"min_score": 25, "tier": 3, "name": "Stretch Voters",     "priority": "medium",   "channels": ["digital_ads", "social_media", "mail"]},
    {"min_score": 0,  "tier": 4, "name": "Awareness Only",     "priority": "low",      "channels": ["digital_ads", "social_media"]},
]

tier_voters = {td["tier"]: [] for td in TIER_DEFS}
for v in active:
    for td in TIER_DEFS:
        if v["_score"] >= td["min_score"]:
            tier_voters[td["tier"]].append(v)
            break

tiers = []
for td in TIER_DEFS:
    voters = tier_voters[td["tier"]]
    tiers.append((voters, td["tier"], td["name"], td["priority"], td["channels"]))

def to_record(v):
    return {
        "voter_id": v.get("LALVOTERID") or "", "first_name": v.get("Voters_FirstName") or "",
        "last_name": v.get("Voters_LastName") or "", "address": v.get("Residence_Addresses_AddressLine") or "",
        "city": v.get("Residence_Addresses_City") or "", "zip": v.get("Residence_Addresses_Zip") or "",
        "age": v.get("Voters_Age") or 0, "gender": v.get("Voters_Gender") or "U",
        "party": v.get("Parties_Description") or "", "voter_status": v.get("Voters_Active") or "",
    }

def age_bucket(age):
    if age is None: return "Unknown"
    if age < 35: return "18-34"
    if age < 55: return "35-54"
    return "55+"

def make_seg(voters, tier, name, priority, channels):
    parties, ages, genders = {}, {}, {}
    for v in voters:
        p = v.get("Parties_Description", "Unknown"); parties[p] = parties.get(p, 0) + 1
        a = age_bucket(v.get("Voters_Age")); ages[a] = ages.get(a, 0) + 1
        g = v.get("Voters_Gender", "U"); genders[g] = genders.get(g, 0) + 1
    return {
        "tier": tier, "name": name, "description": f"Tier {tier} voters by independent appeal score",
        "count": len(voters), "filters_used": ["Independent Appeal Score"],
        "demographics": {"party_breakdown": parties, "age_distribution": ages, "gender_split": genders},
        "outreach_priority": priority, "recommended_channels": channels,
        "voters": [to_record(v) for v in voters],
    }

segments = [make_seg(v, t, nm, p, c) for v, t, nm, p, c in tiers]

# --- Geographic clusters by zip code ---
from collections import Counter

zip_counts = Counter()
zip_cities = {}
for v in active:
    z = (v.get("Residence_Addresses_Zip") or "").strip()[:5]  # 5-digit zip
    if z:
        zip_counts[z] += 1
        if z not in zip_cities:
            zip_cities[z] = (v.get("Residence_Addresses_City") or city).strip()

# Build clusters sorted by voter density (most voters first)
# Filter out zips with <50 district voters (boundary clipping noise)
min_voters = 50
geo_clusters = []
for rank, (z, count) in enumerate(zip_counts.most_common(), 1):
    if count < min_voters:
        continue
    area_city = zip_cities.get(z, city)
    geo_clusters.append({
        "area": f"{area_city} {z}",
        "voter_count": count,
        "density_rank": rank,
    })

# If only 1 zip found, try grouping by city name instead
if len(geo_clusters) <= 1:
    city_counts = Counter()
    for v in active:
        c = (v.get("Residence_Addresses_City") or city).strip()
        if c:
            city_counts[c] += 1
    if len(city_counts) > 1:
        geo_clusters = [
            {"area": c, "voter_count": cnt, "density_rank": rank}
            for rank, (c, cnt) in enumerate(city_counts.most_common(), 1)
        ]

print(f"Geographic clusters: {len(geo_clusters)}")
for gc in geo_clusters[:5]:
    print(f"  {gc['area']}: {gc['voter_count']} voters (rank {gc['density_rank']})")

output = {
    "candidate_id": candidate_id,
    "district": {"state": state, "type": params.get("districtType", ""), "name": office, "city": city, "county": county, "zip": zip_code, "l2_type": l2_district_type, "l2_name": l2_district_name},
    "generated_at": datetime.utcnow().isoformat() + "Z",
    "summary": {
        "total_voters_in_district": len(active),
        "win_number": win_number, "projected_turnout": projected_turnout,
    },
    "segments": segments,
    "geographic_clusters": geo_clusters,
    "methodology": f"Contact-goal-driven targeting: {effective_contact_goal} total contacts / {AVG_TOUCHES_PER_VOTER} avg touches per voter = {target_universe_size} target universe. Composite Independent Appeal Score: hs_partisanship_moderate_third_party_support (25%), hs_ideology_overall_party_indep (25%), hs_likely_mid_term_voter (20%), inverse strong partisan (15%), party bonus (+8 Non-Partisan, -2 major party). Tiers by score threshold: Base (55+), Persuadable (40-55), Stretch (25-40), Awareness (<25). Geographic clusters by zip code, sorted by voter density.",
}

os.makedirs("/workspace/output", exist_ok=True)
with open("/workspace/output/voter_targeting.json", "w") as f:
    json.dump(output, f, indent=2, default=str)

for s in segments:
    print(f"Tier {s['tier']}: {s['name']} — {s['count']} voters, {len(s['voters'])} in array")
print("DONE")
```

## STEP 3: Adapt the script

Before running, you MUST:
1. Replace the Haystaq column names in the SELECT with the ones you found in Step 1
2. Update the `score()` function if the column names differ
3. Add any additional useful Haystaq columns you discovered (voting performance, persuadability, etc.)

Then run: `python3 /tmp/build_targeting.py`

## STEP 4: Validate and verify

Run the contract validator:

```bash
python3 /workspace/validate_output.py
```

If it prints FAIL, read the errors, fix the script, and re-run. Do NOT finish until it prints PASS.

Then spot-check:

```bash
python3 -c "
import json
d = json.load(open('/workspace/output/voter_targeting.json'))
print(f'Total voters: {d[\"summary\"][\"total_voters_in_district\"]}')
for s in d['segments']:
    v = s['voters'][0] if s['voters'] else {}
    print(f'Tier {s[\"tier\"]}: {s[\"name\"]} — {len(s[\"voters\"])} voters, sample keys: {list(v.keys())[:5]}')

# Check geographic clusters
gc = d.get('geographic_clusters', [])
print(f'\nGeographic clusters: {len(gc)}')
if len(gc) <= 1:
    print('WARNING: Only 1 cluster — check zip code diversity in voter data')
for c in gc[:10]:
    print(f'  {c[\"area\"]}: {c[\"voter_count\"]} voters (rank {c[\"density_rank\"]})')
"
```

If any tier has 0 voters, the script has a bug — fix and re-run.
If geographic clusters is 1, the data may only have one zip — acceptable for small towns but investigate for cities.
