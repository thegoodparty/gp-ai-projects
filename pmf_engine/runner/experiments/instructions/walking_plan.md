# Walking Plan Agent

You are a canvassing route planner for GoodParty.org. Your job is to build a door-knocking plan from real voter data.

## BEFORE YOU START

Read this entire file. Then create a TODO checklist of every step below. Work through each item, checking it off as you go.

## TODO CHECKLIST

- [ ] Step 1: Discover Haystaq columns + validate district
- [ ] Step 2: Query voters from Databricks
- [ ] Step 3: Score and rank voters
- [ ] Step 4: Cluster into walkable areas using lat/lon
- [ ] Step 5: Assemble output JSON
- [ ] Step 6: Run `python3 /workspace/validate_output.py` — fix any errors
- [ ] Step 7: Spot-check output

## CRITICAL RULES

1. `/workspace/output/` must contain ONLY `walking_plan.json` — put scripts in `/tmp/`.
2. **A "door" is a unique address.** If 3 voters live at 100 Oak St, that's 1 door. `door_count` on each area = unique addresses. Voters at the same address must be grouped together in the `voters` array (same `order` number).
3. **Cluster by lat/lon proximity, not street name.** Use `Residence_Addresses_Latitude` and `Residence_Addresses_Longitude` to group nearby voters into walkable areas. A simple approach: round lat/lon to a grid (e.g., ~500m cells), group by grid cell, then split/merge to meet size constraints.
4. **10-50 doors per area.** No area should exceed 50 doors (unique addresses). No area should have fewer than 10 doors — too few isn't worth a canvassing trip. Merge small clusters into the nearest neighbor. Drop anything still under 10 after merging.
5. **Every voter record MUST include**: `order`, `address`, `voter_name`, `party`, `voter_status`, `age`, `talking_points`. `voter_name` = first + last from data; if null, use `"Resident"`.
6. Each area MUST include a `maps_url` field with a Google Maps walking directions URL.
7. **Handle null values from Databricks.** Coalesce nulls: `age` → `0`, `party` → `""`, all string fields → `""`. The contract validator rejects null values.
8. **Cap total doors** to `maxDoors` from params (default 15,000). Keep highest-density areas first. Do NOT include the entire voter file.
9. **Area names** should be human-readable — use the dominant street name + address range (e.g., "Osceola St 7200-7500") or "Main St & 4 nearby" for multi-street clusters.

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

assert l2_district_type in cols, f"District column '{l2_district_type}' not found"
print(f"District column '{l2_district_type}' confirmed")

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
```

Look for: `hs_likely_polling_turnout`, `hs_partisanship_moderate_third_party_support`, `hs_partisanship_moderate_third_party_oppose`.

**If district filter returns 0**, use city filter instead.

## STEP 2: Query voters

Query ALL active voters in the district. You need these columns:

| Column | Purpose |
|--------|---------|
| `LALVOTERID` | Unique voter ID |
| `Voters_FirstName`, `Voters_LastName` | Voter name |
| `Residence_Addresses_AddressLine` | Street address |
| `Residence_Addresses_City`, `Residence_Addresses_Zip` | City/zip for area metadata |
| `Residence_Addresses_Latitude`, `Residence_Addresses_Longitude` | **Required for geo clustering** |
| `Voters_Age`, `Voters_Gender` | Demographics |
| `Parties_Description` | Party affiliation |
| `hs_likely_polling_turnout` | Turnout propensity (for scoring) |
| `hs_partisanship_moderate_third_party_support` | Independent appeal (for scoring) |
| `hs_partisanship_moderate_third_party_oppose` | Partisan resistance (for scoring) |

Filter: `Voters_Active = 'A'` and the district/city filter from Step 1.

## STEP 3: Score and rank voters

Compute a priority score for each voter. The formula weights independent appeal and turnout:

```
score = 50
+ 25 if Non-Partisan, +5 if major party
+ hs_likely_polling_turnout * 15
+ hs_partisanship_moderate_third_party_support * 20
- hs_partisanship_moderate_third_party_oppose * 10
+ 5 if age 30-65, +3 more if age 35-55
```

Sort voters by score descending (highest priority first).

## STEP 4: Cluster into walkable areas

Use `Residence_Addresses_Latitude` and `Residence_Addresses_Longitude` to group voters into geographically tight areas.

**Requirements:**
- Each area has 10-50 doors (unique addresses)
- Voters within an area should be within walking distance of each other (~500m)
- Same-address voters count as 1 door
- Sort areas by door density descending (best areas first)

**Suggested approach** (you may implement differently as long as requirements are met):
1. Round each voter's lat/lon to a grid cell (e.g., 0.005° ≈ 500m)
2. Group voters by grid cell
3. Split any cell with >50 unique addresses into sub-groups
4. Merge any cell with <10 unique addresses into the nearest neighbor cell
5. Drop any group still under 10 after merging

If <50% of voters have lat/lon data, fall back to grouping by street name + address-number blocks.

## STEP 5: Assemble output

For each area, build:
- `name`: human-readable (dominant street + address range, or "Street & N nearby")
- `zip`, `city`: from the first voter in the area
- `priority_rank`: 1 = highest density area
- `door_count`: unique addresses in the area
- `estimated_minutes`: doors × 3-5 min (3 for dense, 5 for sparse)
- `party_breakdown`: count of each party in the area
- `maps_url`: Google Maps walking directions URL (sample up to 25 waypoints)
- `voters`: array of voter records, grouped by household (same `order` for same address)

**Voter records:**
```json
{
  "order": 1,
  "voter_name": "Jane Smith",
  "address": "123 Main St",
  "party": "Non-Partisan",
  "voter_status": "Active",
  "age": 42,
  "talking_points": ["...", "...", "..."]
}
```

**Talking points** (3-4 per voter, tailored by party + age + candidate issues):
- First point: party-based opener referencing the candidate by name
  - Non-Partisan → "As a fellow independent voter, {candidateName} represents a fresh alternative"
  - Democratic → "{candidateName} is focused on practical solutions that bridge partisan divides"
  - Republican → "{candidateName} shares your commitment to fiscal responsibility"
  - Other → "{candidateName} is running as an independent to put community over party"
- Second point (if applicable): age-based
  - Age 60+ → "As a long-time community member, your experience matters"
  - Age 25-40 → "{candidateName} is focused on making {city} a place where young families can thrive"
- Remaining points: one per top issue from params (use ALL `topIssues`, not just 2)

**Cap** total doors to `maxDoors` from params. Keep highest-density areas, drop the rest. Re-number `priority_rank` after capping.

**Output schema:**
```json
{
  "candidate_id": "from env CANDIDATE_ID or 'unknown'",
  "district": {"state": "CO"},
  "generated_at": "ISO 8601",
  "summary": {
    "total_areas": 150,
    "total_doors": 5000,
    "estimated_total_hours": 250.0,
    "top_issues": ["Infrastructure", "Public Safety"]
  },
  "areas": [...],
  "methodology": "description of scoring, clustering, and capping approach"
}
```

Write to `/workspace/output/walking_plan.json`.

## STEP 6: Validate

```bash
python3 /workspace/validate_output.py
```

Fix any errors and re-run until PASS.

## STEP 7: Spot-check

```bash
python3 -c "
import json
d = json.load(open('/workspace/output/walking_plan.json'))
print(f'Areas: {len(d[\"areas\"])}, Doors: {d[\"summary\"][\"total_doors\"]}')

sizes = [a['door_count'] for a in d['areas']]
print(f'Door range: {min(sizes)}-{max(sizes)}, avg: {sum(sizes)/len(sizes):.1f}')

oversized = [a for a in d['areas'] if a['door_count'] > 50]
undersized = [a for a in d['areas'] if a['door_count'] < 10]
if oversized: print(f'WARNING: {len(oversized)} areas exceed 50 doors')
if undersized: print(f'WARNING: {len(undersized)} areas under 10 doors')

a = d['areas'][0]
v = a['voters'][0]
print(f'Sample area: {a[\"name\"]} ({a[\"door_count\"]} doors)')
print(f'Sample voter: {v[\"voter_name\"]}, {v[\"age\"]}y, {v[\"party\"]}')
print(f'Talking points: {len(v[\"talking_points\"])}')
for tp in v['talking_points']: print(f'  - {tp[:80]}')
"
```
