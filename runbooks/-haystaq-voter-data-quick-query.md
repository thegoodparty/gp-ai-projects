# Haystaq Voter Data — Quick Query Guide

## Setup

```bash
cd ~/Development/gp-ai-projects
source .venv/bin/activate
export AWS_PROFILE=work
```

Requires env vars: `DATABRICKS_API_KEY`, `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`

## Tables

All tables live in `goodparty_data_catalog.dbt`. Replace `{state}` with lowercase state code (nc, co, ak, etc.)

| Table | What's in it |
|-------|--------------|
| `stg_dbt_source__l2_s3_{state}_uniform` | Voter demographics (name, age, address, party, registration) |
| `stg_dbt_source__l2_s3_{state}_haystaq_dna_scores` | Predictive scores (0-100) for ~300 issue/behavioral dimensions |
| `stg_dbt_source__l2_s3_{state}_haystaq_dna_flags` | Binary flags (Yes/No) |

**Join key across all three:** `LALVOTERID`

## Common Queries

### Find a voter by name

```python
from shared.databricks_client import DatabricksClient
client = DatabricksClient()

result = client.execute_query('''
    SELECT LALVOTERID, Voters_FirstName, Voters_LastName, Voters_Age,
           Residence_Addresses_City, Parties_Description
    FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_nc_uniform
    WHERE UPPER(Voters_LastName) LIKE "%SMITH%"
      AND UPPER(Voters_FirstName) LIKE "%JOHN%"
    LIMIT 20
''')
print(result.to_string())
```

### Get all scores for a voter

```python
voter_id = "LALCO140917748"  # from the uniform table

scores = client.execute_query(f'''
    SELECT *
    FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_nc_haystaq_dna_scores
    WHERE LALVOTERID = "{voter_id}"
''')

# Show high-confidence scores (>50)
scores_t = scores.T
scores_t.columns = ['value']
high = scores_t[scores_t['value'].apply(lambda x: isinstance(x, (int, float)) and x > 50)]
print(high.sort_values('value', ascending=False).to_string())
```

### Aggregate scores for a city/area

```python
result = client.execute_query('''
    SELECT
        COUNT(*) as voter_count,
        AVG(CAST(s.hs_climate_change_believer AS DOUBLE)) as climate_believer,
        AVG(CAST(s.hs_gun_control_support AS DOUBLE)) as gun_control,
        AVG(CAST(s.hs_abortion_pro_choice AS DOUBLE)) as pro_choice,
        AVG(CAST(s.hs_affordable_housing_gov_has_role AS DOUBLE)) as housing_gov_role,
        AVG(CAST(s.hs_most_important_policy_item_environment AS DOUBLE)) as env_priority,
        AVG(CAST(s.hs_most_important_policy_item_economics AS DOUBLE)) as econ_priority
    FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_nc_uniform u
    JOIN goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_nc_haystaq_dna_scores s
      ON u.LALVOTERID = s.LALVOTERID
    WHERE UPPER(u.Residence_Addresses_City) = "ASHEVILLE"
''')
print(result.T.to_string())
```

### Get flags for a voter

```python
flags = client.execute_query(f'''
    SELECT *
    FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_nc_haystaq_dna_flags
    WHERE LALVOTERID = "{voter_id}"
''')
flags_t = flags.T
flags_t.columns = ['value']
print(flags_t[flags_t['value'] == 'Yes'].to_string())
```

## Useful Uniform Columns

| Column | Description |
|--------|-------------|
| `LALVOTERID` | Unique voter ID (join key) |
| `Voters_FirstName`, `Voters_LastName` | Name |
| `Voters_Age`, `Voters_BirthDate` | Age/DOB |
| `Voters_Gender` | M/F |
| `Residence_Addresses_City`, `_State`, `_Zip` | Location |
| `Parties_Description` | Party registration |

## Key Score Prefixes

| Prefix | Category | Example |
|--------|----------|---------|
| `hs_most_important_policy_*` | What voters prioritize | `_item_environment`, `_item_economics` |
| `hs_ideology_*` | Political leaning | `_general_liberal`, `_fiscal_conserv` |
| `hs_*_support` / `hs_*_oppose` | Issue positions | `hs_gun_control_support` |
| `hs_likely_*` | Turnout predictions | `hs_likely_presidential_voter` |
| `hs_responsiveness_*` | Best contact method | `_sms`, `_email`, `_live` |
| `hs_trump_*` / `hs_harris_*` | Candidate sentiment | `hs_trump_approval` |

## Tips

- Scores are 0-100. Higher = stronger signal for that attribute.
- ~300 score columns per voter. Use `SELECT * ... LIMIT 1` to explore column names.
- Filter by city: `WHERE UPPER(Residence_Addresses_City) = "CITYNAME"`
- Filter by zip: `WHERE Residence_Addresses_Zip LIKE "28801%"`
- Always `CAST(s.column AS DOUBLE)` when using `AVG()` on score columns.
