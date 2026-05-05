from shared.databricks_client import DatabricksClient
client = DatabricksClient()

city_name = "MINOOKA"
state_code = "il"

demographics = client.execute_query(f'''
    SELECT
        COUNT(*) as total_voters,
        ROUND(AVG(CAST(Voters_Age AS INT)), 1) as avg_age,
        COUNT(DISTINCT CASE WHEN Voters_Gender = "M" THEN LALVOTERID END) as male,
        COUNT(DISTINCT CASE WHEN Voters_Gender = "F" THEN LALVOTERID END) as female
    FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_{state_code}_uniform
    WHERE UPPER(Residence_Addresses_City) = "{city_name}"
''')

print("=== DEMOGRAPHICS ===")
print(demographics.to_string())
