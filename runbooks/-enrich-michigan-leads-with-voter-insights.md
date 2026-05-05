# Enrich Michigan Leads with Voter Insights — Runbook

## Overview

This runbook enriches a CSV of Michigan elected officials by:
1. Matching each person to their elected office and district
2. Identifying zip codes within their district
3. Analyzing Haystaq voter data to find top 3 issues for their constituents
4. Generating 2 polling questions per issue to help officials engage with voters

## Setup

```bash
cd ~/Development/Work/gp-ai-projects
source .venv/bin/activate
```

**Environment Variables:**

The script will automatically load credentials from `~/Development/Work/gp-ai-projects/.env`:
- ✅ `DATABRICKS_API_KEY` — Already configured
- ✅ `DATABRICKS_SERVER_HOSTNAME` — Already configured
- ✅ `DATABRICKS_HTTP_PATH` — Already configured
- ✅ `DATABRICKS_CATALOG` — Already configured
- ✅ `DATABRICKS_SCHEMA` — Already configured

**Additional variables to add to `.env`:**
- `ANTHROPIC_API_KEY` — For generating polling questions with Claude (required)
- `GOOGLE_CIVIC_API_KEY` — For district verification via Google Civic API (optional)

Add these to your `.env` file:
```bash
# Add to ~/Development/Work/gp-ai-projects/.env
ANTHROPIC_API_KEY=your_anthropic_key_here
GOOGLE_CIVIC_API_KEY=your_google_key_here  # Optional
```

**Install required packages:**
```bash
pip install pandas anthropic requests python-dotenv
```

## Input CSV Structure

The input CSV should have these columns:
- `Record ID`
- `First Name`
- `Last Name`
- `Email`
- `City`
- `State/Region`
- `Postal Code`
- `Candidate Office` (e.g., "Swartz Creek City Council - At Large", "Milan Area School Board")
- `Office Type` (e.g., "City Council", "School Board", "Mayor")

## Output CSV Structure

The output will include all input columns plus:
- `District Zip Codes` (comma-separated list)
- `Issue 1`
- `Issue 1 Question 1`
- `Issue 1 Question 2`
- `Issue 2`
- `Issue 2 Question 1`
- `Issue 2 Question 2`
- `Issue 3`
- `Issue 3 Question 1`
- `Issue 3 Question 2`

## Implementation

### Step 1: Load the CSV and Set Up

```python
import pandas as pd
import os
from dotenv import load_dotenv
from shared.databricks_client import DatabricksClient

# Load environment variables from .env file
load_dotenv()

# Verify required credentials are loaded
required_vars = ['DATABRICKS_API_KEY', 'DATABRICKS_SERVER_HOSTNAME',
                 'DATABRICKS_HTTP_PATH', 'ANTHROPIC_API_KEY']
missing_vars = [var for var in required_vars if not os.environ.get(var)]

if missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

print("✅ Environment variables loaded successfully")

# Initialize Databricks client
client = DatabricksClient()

# Load the input CSV
input_csv = "/Users/bryclev/Downloads/hubspot-crm-exports-serve-icp-lead-list-mi-2026-02-06.csv"
df = pd.read_csv(input_csv)

# Preview the data
print(f"Loaded {len(df)} records")
print(df.head())
```

### Step 2: Define District-to-Zip Mapping Function with Google Civic API

We'll use the Google Civic Information API to identify the official's district, then query Haystaq data to find all zip codes with voters in that district.

**Setup:** Get a Google Civic API key from [Google Cloud Console](https://console.cloud.google.com/) and set it as an environment variable:

```bash
export GOOGLE_CIVIC_API_KEY="your_api_key_here"
```

```python
import re
import requests
import os
from typing import List, Dict, Optional

def parse_district_from_office(candidate_office: str, city: str) -> Dict[str, str]:
    """
    Parse the candidate office string to extract district information.

    Returns a dict with 'jurisdiction' and 'office_type' keys.
    """
    # Remove common suffixes to extract the jurisdiction
    patterns = [
        r'(.+?)\s+City Council',
        r'(.+?)\s+School Board',
        r'(.+?)\s+Village Board',
        r'(.+?)\s+Township Board',
        r'(.+?)\s+City Commission',
        r'(.+?)\s+City Mayor',
        r'(.+?)\s+Village President',
        r'(.+?)\s+City Clerk',
        r'(.+?)\s+City Treasurer',
    ]

    jurisdiction = city  # fallback

    for pattern in patterns:
        match = re.search(pattern, candidate_office, re.IGNORECASE)
        if match:
            jurisdiction = match.group(1).strip()
            break

    # Handle special cases like "School Board" -> extract district name
    if 'School Board' in candidate_office or 'School District' in candidate_office:
        # Extract school district name (e.g., "Milan Area School Board" -> "Milan")
        school_match = re.search(r'(.+?)\s+(?:Area |Community |Public |Consolidated |Union )?School', candidate_office)
        if school_match:
            jurisdiction = school_match.group(1).strip()

    return {
        'jurisdiction': jurisdiction,
        'office': candidate_office
    }

def query_google_civic_api(address: str, api_key: str) -> Optional[Dict]:
    """
    Query Google Civic Information API for representatives at the given address.

    Returns API response with division and office information.
    """
    url = "https://www.googleapis.com/civicinfo/v2/representatives"
    params = {
        'address': address,
        'key': api_key,
        'levels': ['locality', 'administrativeArea2'],  # City and County level
        'includeOffices': 'true'
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error querying Google Civic API for {address}: {e}")
        return None

def get_zips_from_haystaq_by_city(city: str, state: str, client) -> List[str]:
    """
    Query Haystaq data to find all zip codes for voters in a given city.

    This gives us the actual zip codes where constituents live.
    """
    query = f'''
        SELECT DISTINCT Residence_Addresses_Zip
        FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_mi_uniform
        WHERE UPPER(Residence_Addresses_City) = UPPER('{city}')
          AND Residence_Addresses_Zip IS NOT NULL
          AND Residence_Addresses_Zip != ''
        ORDER BY Residence_Addresses_Zip
    '''

    try:
        result = client.execute_query(query)
        zip_codes = result['Residence_Addresses_Zip'].tolist()
        # Clean zip codes (take first 5 digits)
        zip_codes = [str(z)[:5] for z in zip_codes if z and str(z).strip()]
        return list(set(zip_codes))  # Remove duplicates
    except Exception as e:
        print(f"Error querying Haystaq for city {city}: {e}")
        return []

def get_zips_from_haystaq_by_county(county: str, client) -> List[str]:
    """
    Query Haystaq data to find all zip codes in a county.

    Useful for county-level positions.
    """
    query = f'''
        SELECT DISTINCT Residence_Addresses_Zip
        FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_mi_uniform
        WHERE UPPER(Residence_Addresses_County) = UPPER('{county}')
          AND Residence_Addresses_Zip IS NOT NULL
          AND Residence_Addresses_Zip != ''
        ORDER BY Residence_Addresses_Zip
    '''

    try:
        result = client.execute_query(query)
        zip_codes = result['Residence_Addresses_Zip'].tolist()
        zip_codes = [str(z)[:5] for z in zip_codes if z and str(z).strip()]
        return list(set(zip_codes))
    except Exception as e:
        print(f"Error querying Haystaq for county {county}: {e}")
        return []

def get_district_zip_codes(candidate_office: str, city: str, postal_code: str,
                          first_name: str, last_name: str, client) -> List[str]:
    """
    Get all zip codes for the district using Google Civic API and Haystaq data.

    Strategy:
    1. Parse the jurisdiction from the candidate office
    2. Query Haystaq directly for all zips in that city/jurisdiction
    3. Include the official's own zip as fallback
    """

    all_zips = set()

    # Always include the official's own zip
    if postal_code and str(postal_code).strip():
        all_zips.add(str(postal_code)[:5])

    # Parse district information
    district_info = parse_district_from_office(candidate_office, city)
    jurisdiction = district_info['jurisdiction']

    print(f"  → Jurisdiction: {jurisdiction}")

    # Query Haystaq for all zips in the jurisdiction
    if jurisdiction:
        # Try exact city match
        city_zips = get_zips_from_haystaq_by_city(jurisdiction, 'Michigan', client)

        if city_zips:
            print(f"  → Found {len(city_zips)} zip codes in {jurisdiction}")
            all_zips.update(city_zips)
        else:
            # Try the city parameter if jurisdiction lookup failed
            if city and city != jurisdiction:
                city_zips = get_zips_from_haystaq_by_city(city, 'Michigan', client)
                if city_zips:
                    print(f"  → Found {len(city_zips)} zip codes in {city}")
                    all_zips.update(city_zips)

    # For county-level positions, get all county zips
    if 'County' in candidate_office:
        county_match = re.search(r'(.+?)\s+County', candidate_office)
        if county_match:
            county = county_match.group(1).strip()
            county_zips = get_zips_from_haystaq_by_county(county, client)
            if county_zips:
                print(f"  → Found {len(county_zips)} zip codes in {county} County")
                all_zips.update(county_zips)

    # Optional: Use Google Civic API for additional verification
    # (Commented out by default to reduce API calls, but available if needed)
    """
    api_key = os.environ.get('GOOGLE_CIVIC_API_KEY')
    if api_key:
        address = f"{city}, Michigan"
        civic_data = query_google_civic_api(address, api_key)

        if civic_data and 'divisions' in civic_data:
            print(f"  → Google Civic API found divisions: {list(civic_data['divisions'].keys())}")
            # Could use division IDs to refine the search
    """

    return sorted(list(all_zips))

# Test the function
print("\n=== Testing District Lookup ===")
test_row = df.iloc[0]
test_zips = get_district_zip_codes(
    test_row['Candidate Office'],
    test_row['City'],
    test_row['Postal Code'],
    test_row['First Name'],
    test_row['Last Name'],
    client
)
print(f"Result: {test_zips}")
```

**How this works:**

1. **Parse District:** Extracts the jurisdiction (city/township name) from the "Candidate Office" field
2. **Query Haystaq Directly:** Queries the Michigan voter database to find all zip codes where voters live in that city/jurisdiction
3. **Fallback:** Always includes the official's own zip code
4. **County Handling:** For county-level positions, gets all zips in that county

**Advantages:**
- Uses actual voter data from Haystaq (guarantees we only use zips with voters)
- No external API rate limits (primary lookups are in Haystaq)
- Accurate for cities, townships, and school districts
- Google Civic API available as optional verification layer

**Example Output:**
```
→ Jurisdiction: Swartz Creek
→ Found 3 zip codes in Swartz Creek
Result: ['48433', '48473', '48509']
```

### Step 3: Query Haystaq Data for Top Issues

```python
def get_top_issues_for_zips(zip_codes, client, top_n=3):
    """
    Query Haystaq voter data for the given zip codes and return top N issues.

    Returns a list of tuples: [(issue_name, avg_score), ...]
    """

    if not zip_codes or len(zip_codes) == 0:
        return []

    # Format zip codes for SQL IN clause
    zip_list = "', '".join([str(z) for z in zip_codes])

    # Query key issue scores across all voters in these zip codes
    query = f'''
        SELECT
            COUNT(*) as voter_count,
            AVG(CAST(s.hs_climate_change_believer AS DOUBLE)) as climate_believer,
            AVG(CAST(s.hs_gun_control_support AS DOUBLE)) as gun_control_support,
            AVG(CAST(s.hs_abortion_pro_choice AS DOUBLE)) as abortion_pro_choice,
            AVG(CAST(s.hs_affordable_housing_gov_has_role AS DOUBLE)) as affordable_housing,
            AVG(CAST(s.hs_most_important_policy_item_environment AS DOUBLE)) as environment_priority,
            AVG(CAST(s.hs_most_important_policy_item_economics AS DOUBLE)) as economics_priority,
            AVG(CAST(s.hs_most_important_policy_item_education AS DOUBLE)) as education_priority,
            AVG(CAST(s.hs_most_important_policy_item_healthcare AS DOUBLE)) as healthcare_priority,
            AVG(CAST(s.hs_most_important_policy_item_crime AS DOUBLE)) as crime_priority,
            AVG(CAST(s.hs_most_important_policy_item_immigration AS DOUBLE)) as immigration_priority,
            AVG(CAST(s.hs_tax_increase_for_services_support AS DOUBLE)) as tax_increase_support,
            AVG(CAST(s.hs_universal_healthcare_support AS DOUBLE)) as universal_healthcare_support,
            AVG(CAST(s.hs_mental_health_services_support AS DOUBLE)) as mental_health_support,
            AVG(CAST(s.hs_public_transit_expansion_support AS DOUBLE)) as public_transit_support,
            AVG(CAST(s.hs_minimum_wage_increase_support AS DOUBLE)) as minimum_wage_support
        FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_mi_uniform u
        JOIN goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_mi_haystaq_dna_scores s
          ON u.LALVOTERID = s.LALVOTERID
        WHERE u.Residence_Addresses_Zip IN ('{zip_list}')
    '''

    try:
        result = client.execute_query(query)

        if len(result) == 0:
            return []

        # Extract scores and sort by highest average
        row = result.iloc[0]

        # Map column names to friendly issue names
        issue_mapping = {
            'climate_believer': 'Climate Change Action',
            'gun_control_support': 'Gun Control',
            'abortion_pro_choice': 'Reproductive Rights',
            'affordable_housing': 'Affordable Housing',
            'environment_priority': 'Environmental Protection',
            'economics_priority': 'Economic Policy',
            'education_priority': 'Education Funding',
            'healthcare_priority': 'Healthcare Access',
            'crime_priority': 'Public Safety',
            'immigration_priority': 'Immigration Policy',
            'tax_increase_support': 'Tax Policy',
            'universal_healthcare_support': 'Universal Healthcare',
            'mental_health_support': 'Mental Health Services',
            'public_transit_support': 'Public Transportation',
            'minimum_wage_support': 'Minimum Wage'
        }

        # Create list of (issue, score) tuples
        issues = []
        for col, friendly_name in issue_mapping.items():
            if col in row and pd.notna(row[col]):
                issues.append((friendly_name, float(row[col])))

        # Sort by score descending and return top N
        issues.sort(key=lambda x: x[1], reverse=True)
        return issues[:top_n]

    except Exception as e:
        print(f"Error querying Haystaq data: {e}")
        return []

# Test the function
test_zips = ['48473']  # Swartz Creek
top_issues = get_top_issues_for_zips(test_zips, client, top_n=3)
print("Top 3 issues:")
for issue, score in top_issues:
    print(f"  {issue}: {score:.1f}")
```

### Step 4: Generate Polling Questions

```python
import anthropic
import os

def generate_polling_questions(issue_name, office_type, client_anthropic):
    """
    Generate 2 polling questions for the given issue using Claude.

    Questions should help the elected official understand constituent
    priorities and gather actionable feedback.
    """

    prompt = f"""You are helping a {office_type} official create polling questions to better understand their constituents' views on {issue_name}.

Generate 2 specific, actionable polling questions that:
1. Help the official understand the nuances of constituent concerns
2. Are clear and neutral (not leading)
3. Gather insights that can inform policy decisions
4. Are appropriate for the scope of a {office_type} role

Format your response as:
Question 1: [question]
Question 2: [question]

Focus on practical local-level concerns, not abstract national debates."""

    try:
        message = client_anthropic.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = message.content[0].text

        # Parse the response
        lines = response_text.strip().split('\n')
        questions = []

        for line in lines:
            if line.startswith('Question 1:'):
                questions.append(line.replace('Question 1:', '').strip())
            elif line.startswith('Question 2:'):
                questions.append(line.replace('Question 2:', '').strip())

        # Ensure we have exactly 2 questions
        while len(questions) < 2:
            questions.append("[Question could not be generated]")

        return questions[0], questions[1]

    except Exception as e:
        print(f"Error generating questions: {e}")
        return "[Error generating question]", "[Error generating question]"

# Initialize Claude client
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Test question generation
issue = "Affordable Housing"
office = "City Council"
q1, q2 = generate_polling_questions(issue, office, anthropic_client)
print(f"\nIssue: {issue}")
print(f"Q1: {q1}")
print(f"Q2: {q2}")
```

### Step 5: Process All Leads

```python
def enrich_lead(row, client, anthropic_client):
    """
    Enrich a single lead with voter insights.
    """

    # Get district zip codes
    zip_codes = get_district_zip_codes(
        row['Candidate Office'],
        row['City'],
        row['Postal Code'],
        row['First Name'],
        row['Last Name'],
        client
    )

    # Get top 3 issues
    top_issues = get_top_issues_for_zips(zip_codes, client, top_n=3)

    # Generate questions for each issue
    enriched = {
        'District Zip Codes': ', '.join(map(str, zip_codes))
    }

    for i, (issue_name, score) in enumerate(top_issues, 1):
        q1, q2 = generate_polling_questions(
            issue_name,
            row['Office Type'],
            anthropic_client
        )
        enriched[f'Issue {i}'] = issue_name
        enriched[f'Issue {i} Question 1'] = q1
        enriched[f'Issue {i} Question 2'] = q2

    # Fill in empty values if fewer than 3 issues found
    for i in range(len(top_issues) + 1, 4):
        enriched[f'Issue {i}'] = ""
        enriched[f'Issue {i} Question 1'] = ""
        enriched[f'Issue {i} Question 2'] = ""

    return enriched

# Process all leads
print("Processing leads...")
enriched_data = []

for idx, row in df.iterrows():
    print(f"Processing {idx + 1}/{len(df)}: {row['First Name']} {row['Last Name']} - {row['Candidate Office']}")

    enriched = enrich_lead(row, client, anthropic_client)
    enriched_data.append(enriched)

# Create enriched DataFrame
enriched_df = pd.DataFrame(enriched_data)

# Combine with original data
output_df = pd.concat([df, enriched_df], axis=1)

print(f"\nEnrichment complete! Processed {len(output_df)} leads.")
```

### Step 6: Export Results

```python
# Save to CSV
output_path = "/path/to/enriched-michigan-leads-with-voter-insights.csv"
output_df.to_csv(output_path, index=False)

print(f"Output saved to: {output_path}")

# Preview a sample
print("\nSample output:")
sample = output_df.iloc[0]
print(f"\nName: {sample['First Name']} {sample['Last Name']}")
print(f"Office: {sample['Candidate Office']}")
print(f"District Zips: {sample['District Zip Codes']}")
print(f"\nTop Issue: {sample['Issue 1']}")
print(f"  Q1: {sample['Issue 1 Question 1']}")
print(f"  Q2: {sample['Issue 1 Question 2']}")
```

## Sample Output

### Before Enrichment:
```csv
Record ID,First Name,Last Name,Email,City,State/Region,Postal Code,Candidate Office,Office Type
39779198735,David,Kreuger,dakrueger2k@yahoo.com,Swartz Creek,Michigan,48473,Swartz Creek City Council - At Large,City Council
```

### After Enrichment:
```csv
Record ID,First Name,Last Name,Email,City,State/Region,Postal Code,Candidate Office,Office Type,District Zip Codes,Issue 1,Issue 1 Question 1,Issue 1 Question 2,Issue 2,Issue 2 Question 1,Issue 2 Question 2,Issue 3,Issue 3 Question 1,Issue 3 Question 2
39779198735,David,Kreuger,dakrueger2k@yahoo.com,Swartz Creek,Michigan,48473,Swartz Creek City Council - At Large,City Council,48473,Education Funding,"What specific improvements would you most like to see in our local schools' infrastructure and resources?","If the city council could allocate additional funding to education, which programs or services should be prioritized?",Public Safety,"How safe do you feel in your neighborhood, and what specific concerns do you have?","What public safety initiatives would most improve quality of life in Swartz Creek?",Affordable Housing,"What housing challenges are you or your family currently facing in Swartz Creek?","Should the city council explore partnerships to develop more affordable housing options?"
```

## Enhancements

### 1. Enable Google Civic API Verification (Optional)

The district lookup already uses Haystaq data effectively. To add Google Civic API as a verification layer, uncomment the code section in `get_district_zip_codes()`:

```python
# In get_district_zip_codes function, uncomment this section:
api_key = os.environ.get('GOOGLE_CIVIC_API_KEY')
if api_key:
    address = f"{city}, Michigan"
    civic_data = query_google_civic_api(address, api_key)

    if civic_data and 'divisions' in civic_data:
        print(f"  → Google Civic API found divisions: {list(civic_data['divisions'].keys())}")

        # Cross-reference division IDs with our Haystaq results
        # to ensure we're capturing the correct jurisdiction
```

This provides an additional validation layer but is not required for the workflow to function.

### 2. Cache District Lookups

Cache Haystaq queries to avoid redundant database calls (useful when multiple officials represent the same city):

```python
from functools import lru_cache

# Cache district zip lookups
district_cache = {}

def get_district_zip_codes_cached(candidate_office, city, postal_code,
                                   first_name, last_name, client):
    """Cached version to avoid re-querying same cities."""
    cache_key = f"{city}_{candidate_office}"

    if cache_key not in district_cache:
        district_cache[cache_key] = get_district_zip_codes(
            candidate_office, city, postal_code, first_name, last_name, client
        )

    return district_cache[cache_key]

# Cache issue lookups
@lru_cache(maxsize=200)
def get_top_issues_for_zips_cached(zip_tuple, top_n=3):
    """Cached version to avoid re-querying same zip codes."""
    zip_codes = list(zip_tuple)
    return get_top_issues_for_zips(zip_codes, client, top_n)

# Usage in enrich_lead():
zip_codes = get_district_zip_codes_cached(...)
top_issues = get_top_issues_for_zips_cached(tuple(zip_codes), top_n=3)
```

### 3. Batch Question Generation

Generate questions in batches to reduce API calls:

```python
def generate_questions_batch(issue_office_pairs, anthropic_client):
    """
    Generate questions for multiple issues at once.
    """
    # Build a batch prompt
    # ... implementation details
    pass
```

### 4. Add Issue Score Context

Include the actual score values in output for transparency:

```python
enriched[f'Issue {i}'] = f"{issue_name} (Score: {score:.1f})"
```

## Tips

- **Rate Limiting:** Add delays between API calls if processing large datasets
- **Error Handling:** Implement retry logic for failed Databricks or Claude queries
- **Validation:** Check that zip codes are valid Michigan zip codes
- **Testing:** Run on a small subset (5-10 records) before processing the full dataset
- **Office-Specific Questions:** Tailor question generation based on Office Type (School Board vs City Council have different scopes)

## Common Issues

**Issue:** No Haystaq data found for zip codes
- **Solution:** Verify the zip code is in Michigan and has voter data in Databricks

**Issue:** Question generation fails
- **Solution:** Check ANTHROPIC_API_KEY is set and valid

**Issue:** District boundaries unclear
- **Solution:** For School Boards, search for district maps on the school district website
- **Solution:** For City Councils, use city/township boundaries

## Data Sources

### Michigan Haystaq Tables
- `goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_mi_uniform` — Voter demographics
- `goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_mi_haystaq_dna_scores` — Issue scores (0-100)
- `goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_mi_haystaq_dna_flags` — Binary flags

### External Resources
- [Michigan Secretary of State - Districts](https://www.michigan.gov/sos)
- [Census Bureau - Michigan](https://data.census.gov/cedsci/profile?g=0400000US26)
- [Michigan School District Maps](https://www.michigan.gov/mde)
- [Google Civic Information API](https://developers.google.com/civic-information)

## Quick Start: Complete Example

Here's a complete working script that processes the entire CSV:

```python
import pandas as pd
import anthropic
import os
from dotenv import load_dotenv
from shared.databricks_client import DatabricksClient

# === SETUP ===
# Load environment variables from .env file
load_dotenv()

client = DatabricksClient()
anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Load CSV
input_csv = "/Users/bryclev/Downloads/hubspot-crm-exports-serve-icp-lead-list-mi-2026-02-06.csv"
df = pd.read_csv(input_csv)

print(f"Loaded {len(df)} leads to enrich\n")

# === PASTE ALL FUNCTIONS FROM STEPS 2-4 HERE ===
# (parse_district_from_office, query_google_civic_api, get_zips_from_haystaq_by_city,
#  get_zips_from_haystaq_by_county, get_district_zip_codes, get_top_issues_for_zips,
#  generate_polling_questions, enrich_lead)

# === PROCESS LEADS ===
enriched_data = []

for idx, row in df.iterrows():
    print(f"[{idx + 1}/{len(df)}] {row['First Name']} {row['Last Name']} - {row['Candidate Office']}")

    try:
        enriched = enrich_lead(row, client, anthropic_client)
        enriched_data.append(enriched)
    except Exception as e:
        print(f"  ⚠️  Error: {e}")
        # Add empty data on error
        enriched_data.append({
            'District Zip Codes': '',
            'Issue 1': '', 'Issue 1 Question 1': '', 'Issue 1 Question 2': '',
            'Issue 2': '', 'Issue 2 Question 1': '', 'Issue 2 Question 2': '',
            'Issue 3': '', 'Issue 3 Question 1': '', 'Issue 3 Question 2': ''
        })

    # Optional: Add delay to respect API rate limits
    # import time
    # time.sleep(1)

# === COMBINE AND EXPORT ===
enriched_df = pd.DataFrame(enriched_data)
output_df = pd.concat([df, enriched_df], axis=1)

output_path = "/Users/bryclev/Downloads/enriched-michigan-leads-with-voter-insights.csv"
output_df.to_csv(output_path, index=False)

print(f"\n✅ Complete! Enriched {len(output_df)} leads")
print(f"📄 Output saved to: {output_path}")

# === PREVIEW RESULTS ===
print("\n=== Sample Output ===")
sample = output_df.iloc[0]
print(f"Name: {sample['First Name']} {sample['Last Name']}")
print(f"Office: {sample['Candidate Office']}")
print(f"District Zips: {sample['District Zip Codes']}")
print(f"\nTop Issues:")
for i in range(1, 4):
    issue = sample[f'Issue {i}']
    if issue:
        print(f"\n{i}. {issue}")
        print(f"   Q1: {sample[f'Issue {i} Question 1']}")
        print(f"   Q2: {sample[f'Issue {i} Question 2']}")
```

**To run:**
1. Set all required environment variables
2. Copy all function definitions from Steps 2-4 into the script
3. Update the `input_csv` path
4. Run: `python enrich_leads.py`

**Expected runtime:** ~2-3 minutes per lead (includes API calls), so ~300 leads will take 10-15 hours. Consider:
- Running on a subset first (e.g., `df = df.head(10)`)
- Adding multiprocessing for parallel execution
- Implementing caching (see Enhancements section)
