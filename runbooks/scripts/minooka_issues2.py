from shared.databricks_client import DatabricksClient
client = DatabricksClient()

city_name = "MINOOKA"
state_code = "il"

issue_scores = client.execute_query(f'''
    SELECT
        COUNT(*) as voter_count,

        -- Housing & Development (LOCAL)
        AVG(CAST(s.hs_affordable_housing_gov_has_role AS DOUBLE)) as housing_gov_role,

        -- Public Safety (LOCAL)
        AVG(CAST(s.hs_most_important_policy_keep_safe AS DOUBLE)) as keep_safe_priority,
        AVG(CAST(s.hs_police_trust_yes AS DOUBLE)) as police_trust,

        -- Infrastructure & Transportation (LOCAL)
        AVG(CAST(s.hs_public_transit_support AS DOUBLE)) as public_transit_support,

        -- Local Environment & Sustainability (LOCAL)
        AVG(CAST(s.hs_most_important_policy_item_environment AS DOUBLE)) as env_priority,
        AVG(CAST(s.hs_climate_change_believer AS DOUBLE)) as climate_believer,

        -- Local Economic Development (LOCAL)
        AVG(CAST(s.hs_most_important_policy_item_economics AS DOUBLE)) as econ_priority,

        -- Social/Helping People
        AVG(CAST(s.hs_most_important_policy_item_help_people AS DOUBLE)) as help_people_priority,

        -- Local Taxes & Budget (LOCAL)
        AVG(CAST(s.hs_tax_cuts_support AS DOUBLE)) as tax_cuts_support,

        -- Ideology indicators (context)
        AVG(CAST(s.hs_ideology_general_liberal AS DOUBLE)) as ideology_liberal,
        AVG(CAST(s.hs_ideology_general_conservative AS DOUBLE)) as ideology_conservative,
        AVG(CAST(s.hs_ideology_fiscal_conserv AS DOUBLE)) as ideology_fiscal_conserv

    FROM goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_{state_code}_uniform u
    JOIN goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_{state_code}_haystaq_dna_scores s
      ON u.LALVOTERID = s.LALVOTERID
    WHERE UPPER(u.Residence_Addresses_City) = "{city_name}"
''')

print("=== ISSUE SCORES ===")
print(issue_scores.T.to_string())
