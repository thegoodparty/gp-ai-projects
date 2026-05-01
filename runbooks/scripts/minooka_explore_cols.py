from shared.databricks_client import DatabricksClient
client = DatabricksClient()

# Get one row to see available columns
result = client.execute_query('''
    SELECT *
    FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_il_haystaq_dna_scores
    LIMIT 1
''')

# Show all columns that start with hs_most_important or hs_affordable or hs_police or hs_ideology or hs_infrastructure or hs_climate
cols = [c for c in result.columns if any(c.startswith(p) for p in [
    'hs_most_important', 'hs_affordable', 'hs_police', 'hs_ideology',
    'hs_climate', 'hs_public_transit', 'hs_minimum_wage', 'hs_tax'
])]

print("Available relevant columns:")
for c in sorted(cols):
    print(f"  {c}")
