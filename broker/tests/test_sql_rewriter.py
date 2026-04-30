import pytest

from broker.sql_rewriter import (
    RewriteResult,
    ScopeViolation,
    rewrite_query,
    validate_parameters,
)

ALLOWED_TABLE = "goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq"
SHORT_TABLE = "int__l2_nationwide_uniform_w_haystaq"

TEST_SCOPE = {
    "state": "NC",
    "cities": ["Fayetteville"],
    "districts": [],
    "allowed_tables": [ALLOWED_TABLE],
    "max_rows": 50000,
}



# ---------------------------------------------------------------------------
# 1. MUST-REJECT: Stacked statements
# ---------------------------------------------------------------------------
class TestStackedStatements:
    def test_select_then_drop(self):
        with pytest.raises(ScopeViolation, match="stacked_statements"):
            rewrite_query(f"SELECT LALVOTERID FROM {ALLOWED_TABLE}; DROP TABLE foo", TEST_SCOPE)

    def test_two_selects(self):
        with pytest.raises(ScopeViolation, match="stacked_statements"):
            rewrite_query(
                f"SELECT LALVOTERID FROM {ALLOWED_TABLE}; SELECT Voters_Age FROM {ALLOWED_TABLE}",
                TEST_SCOPE,
            )

    def test_unicode_semicolon(self):
        sql = f"SELECT LALVOTERID FROM {ALLOWED_TABLE}\uFF1B DROP TABLE foo"
        with pytest.raises(ScopeViolation, match="stacked_statements"):
            rewrite_query(sql, TEST_SCOPE)


# ---------------------------------------------------------------------------
# 2. MUST-REJECT: Disallowed table in root FROM
# ---------------------------------------------------------------------------
class TestDisallowedCrossCatalogBasenameConfusion:
    """An agent must NOT be able to reach a same-basename table in a
    different catalog/db by just typing the basename with a bogus prefix.
    The scope says `goodparty_data_catalog.dbt.int__l2_nationwide_uniform_w_haystaq`
    is allowed; `evil_catalog.public.int__l2_nationwide_uniform_w_haystaq`
    happens to end in the same basename but lives somewhere the ticket never
    authorized. Current code accepted it via the basename suffix match.
    """

    def test_rejects_bare_basename_when_not_in_allowed(self):
        # Sanity: bare basename with no catalog/db IS allowed (agents query
        # like this via the shorthand the runner uses).
        result = rewrite_query(f"SELECT LALVOTERID FROM {SHORT_TABLE} WHERE Voters_Active='A'", TEST_SCOPE)
        assert result.sql.strip().upper().startswith("SELECT")

    def test_rejects_cross_catalog_same_basename(self):
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(
                "SELECT LALVOTERID FROM evil_catalog.public.int__l2_nationwide_uniform_w_haystaq "
                "WHERE Voters_Active='A'",
                TEST_SCOPE,
            )

    def test_rejects_different_db_same_basename(self):
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(
                "SELECT LALVOTERID FROM goodparty_data_catalog.other_db.int__l2_nationwide_uniform_w_haystaq "
                "WHERE Voters_Active='A'",
                TEST_SCOPE,
            )


class TestDisallowedTableFrom:
    def test_system_info_schema(self):
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query("SELECT * FROM system.information_schema.tables", TEST_SCOPE)

    def test_random_table(self):
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query("SELECT * FROM some_catalog.some_schema.some_table", TEST_SCOPE)

    def test_unqualified_unknown(self):
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query("SELECT * FROM unknown_table", TEST_SCOPE)


# ---------------------------------------------------------------------------
# 3. MUST-REJECT: Disallowed table in JOIN
# ---------------------------------------------------------------------------
class TestDisallowedTableJoin:
    def test_join_system_iam(self):
        sql = (
            f"SELECT v.LALVOTERID FROM {ALLOWED_TABLE} v "
            "JOIN system.iam.users u ON v.LALVOTERID = u.id"
        )
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(sql, TEST_SCOPE)

    def test_left_join_bad_table(self):
        sql = (
            f"SELECT v.LALVOTERID FROM {ALLOWED_TABLE} v "
            "LEFT JOIN other_catalog.schema.bad_table b ON v.LALVOTERID = b.id"
        )
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(sql, TEST_SCOPE)

    def test_cross_join_bad_table(self):
        sql = (
            f"SELECT v.LALVOTERID FROM {ALLOWED_TABLE} v "
            "CROSS JOIN system.information_schema.columns"
        )
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(sql, TEST_SCOPE)


# ---------------------------------------------------------------------------
# 4. MUST-REJECT: Disallowed table in subquery
# ---------------------------------------------------------------------------
class TestDisallowedTableSubquery:
    def test_in_subquery(self):
        sql = (
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} "
            "WHERE LALVOTERID IN (SELECT id FROM system.iam.users)"
        )
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(sql, TEST_SCOPE)

    def test_exists_subquery(self):
        sql = (
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} v "
            "WHERE EXISTS (SELECT 1 FROM system.x.y WHERE y.id = v.LALVOTERID)"
        )
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(sql, TEST_SCOPE)

    def test_from_subquery_bad_inner(self):
        sql = "SELECT * FROM (SELECT * FROM system.information_schema.tables) sub"
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(sql, TEST_SCOPE)


# ---------------------------------------------------------------------------
# 5. MUST-REJECT: Disallowed table in CTE
# ---------------------------------------------------------------------------
class TestDisallowedTableCTE:
    def test_cte_bad_table(self):
        sql = f"WITH evil AS (SELECT * FROM system.x.y) SELECT * FROM {ALLOWED_TABLE}"
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(sql, TEST_SCOPE)

    def test_cte_union_bad_table(self):
        sql = (
            f"WITH data AS (SELECT LALVOTERID FROM {ALLOWED_TABLE} "
            "UNION ALL SELECT id FROM system.x.y) "
            "SELECT * FROM data"
        )
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(sql, TEST_SCOPE)

    def test_cte_references_cte_with_bad_source(self):
        sql = (
            "WITH step1 AS (SELECT * FROM system.bad.table), "
            "step2 AS (SELECT * FROM step1) "
            f"SELECT * FROM {ALLOWED_TABLE}"
        )
        with pytest.raises(ScopeViolation, match="disallowed_table"):
            rewrite_query(sql, TEST_SCOPE)


# ---------------------------------------------------------------------------
# 6. MUST-ACCEPT: Any column name (column allowlist removed)
# ---------------------------------------------------------------------------
class TestAnyColumnAccepted:
    def test_select_any_column(self):
        result = rewrite_query(f"SELECT ssn FROM {ALLOWED_TABLE}", TEST_SCOPE)
        assert result.statement_type == "select"

    def test_select_star_allowed(self):
        result = rewrite_query(f"SELECT * FROM {ALLOWED_TABLE}", TEST_SCOPE)
        assert result.statement_type == "select"

    def test_select_arbitrary_columns(self):
        result = rewrite_query(
            f"SELECT Voters_FirstName, Voters_LastName, hs_partisanship_score FROM {ALLOWED_TABLE}",
            TEST_SCOPE,
        )
        assert result.statement_type == "select"

    def test_where_any_column(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} WHERE email = 'x'",
            TEST_SCOPE,
        )
        assert result.statement_type == "select"

    def test_order_by_any_column(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} ORDER BY secret_field",
            TEST_SCOPE,
        )
        assert result.statement_type == "select"


# ---------------------------------------------------------------------------
# 8. MUST-REJECT: Non-SELECT verbs
# ---------------------------------------------------------------------------
class TestNonSelectVerbs:
    def test_insert(self):
        with pytest.raises(ScopeViolation, match="disallowed_verb"):
            rewrite_query(f"INSERT INTO {ALLOWED_TABLE} VALUES (1)", TEST_SCOPE)

    def test_delete(self):
        with pytest.raises(ScopeViolation, match="disallowed_verb"):
            rewrite_query(f"DELETE FROM {ALLOWED_TABLE}", TEST_SCOPE)

    def test_update(self):
        with pytest.raises(ScopeViolation, match="disallowed_verb"):
            rewrite_query(f"UPDATE {ALLOWED_TABLE} SET Voters_Age = 1", TEST_SCOPE)

    def test_drop(self):
        with pytest.raises(ScopeViolation, match="disallowed_verb"):
            rewrite_query(f"DROP TABLE {ALLOWED_TABLE}", TEST_SCOPE)

    def test_create(self):
        with pytest.raises(ScopeViolation, match="disallowed_verb"):
            rewrite_query("CREATE TABLE foo (id INT)", TEST_SCOPE)

    def test_merge(self):
        with pytest.raises(ScopeViolation, match="disallowed_verb"):
            rewrite_query(
                f"MERGE INTO {ALLOWED_TABLE} USING source ON 1=1 WHEN MATCHED THEN DELETE",
                TEST_SCOPE,
            )


# ---------------------------------------------------------------------------
# 9. MUST-REJECT: Forbidden functions
# ---------------------------------------------------------------------------
class TestForbiddenFunctions:
    def test_range(self):
        with pytest.raises(ScopeViolation, match="forbidden_function"):
            rewrite_query("SELECT * FROM range(1000)", TEST_SCOPE)

    def test_read_files(self):
        with pytest.raises(ScopeViolation, match="forbidden_function"):
            rewrite_query("SELECT * FROM read_files('s3://bucket/path')", TEST_SCOPE)

    def test_explode(self):
        with pytest.raises(ScopeViolation, match="forbidden_function"):
            rewrite_query(
                f"SELECT explode(LALVOTERID) FROM {ALLOWED_TABLE}", TEST_SCOPE
            )

    def test_rejects_explode_outer(self):
        with pytest.raises(ScopeViolation, match="forbidden_function"):
            rewrite_query(
                f"SELECT explode_outer(LALVOTERID) FROM {ALLOWED_TABLE}", TEST_SCOPE
            )

    def test_rejects_posexplode(self):
        with pytest.raises(ScopeViolation, match="forbidden_function"):
            rewrite_query(
                f"SELECT posexplode(LALVOTERID) FROM {ALLOWED_TABLE}", TEST_SCOPE
            )

    def test_rejects_posexplode_outer(self):
        with pytest.raises(ScopeViolation, match="forbidden_function"):
            rewrite_query(
                f"SELECT posexplode_outer(LALVOTERID) FROM {ALLOWED_TABLE}", TEST_SCOPE
            )

    def test_rejects_inline(self):
        with pytest.raises(ScopeViolation, match="forbidden_function"):
            rewrite_query(
                f"SELECT inline(LALVOTERID) FROM {ALLOWED_TABLE}", TEST_SCOPE
            )

    def test_rejects_inline_outer(self):
        with pytest.raises(ScopeViolation, match="forbidden_function"):
            rewrite_query(
                f"SELECT inline_outer(LALVOTERID) FROM {ALLOWED_TABLE}", TEST_SCOPE
            )

    def test_rejects_stack(self):
        with pytest.raises(ScopeViolation, match="forbidden_function"):
            rewrite_query(
                f"SELECT stack(2, LALVOTERID, Voters_Age) FROM {ALLOWED_TABLE}", TEST_SCOPE
            )


# ---------------------------------------------------------------------------
# 10. MUST-REJECT: Parameter mismatch
# ---------------------------------------------------------------------------
class TestParameterMismatch:
    def test_missing_param(self):
        with pytest.raises(ScopeViolation, match="parameter_mismatch"):
            validate_parameters("SELECT * WHERE x = %(foo)s", {"bar": 1})

    def test_extra_param(self):
        with pytest.raises(ScopeViolation, match="parameter_mismatch"):
            validate_parameters("SELECT * WHERE x = %(foo)s", {"foo": 1, "bar": 2})

    def test_no_placeholders_with_params(self):
        with pytest.raises(ScopeViolation, match="parameter_mismatch"):
            validate_parameters("SELECT * FROM t", {"foo": 1})

    def test_placeholders_with_no_params(self):
        with pytest.raises(ScopeViolation, match="parameter_mismatch"):
            validate_parameters("SELECT * WHERE x = %(foo)s", {})


# ---------------------------------------------------------------------------
# 11. MUST-ACCEPT: Simple SELECTs
# ---------------------------------------------------------------------------
class TestMustAcceptSimple:
    def test_simple_select_fq(self):
        result = rewrite_query(
            f"SELECT LALVOTERID, Voters_Age FROM {ALLOWED_TABLE}", TEST_SCOPE
        )
        assert isinstance(result, RewriteResult)
        assert result.statement_type == "select"
        assert "LALVOTERID" in result.sql

    def test_simple_select_short_name(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {SHORT_TABLE}", TEST_SCOPE
        )
        assert isinstance(result, RewriteResult)
        assert result.statement_type == "select"

    def test_select_with_where_allowed(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} WHERE Voters_Age > 18",
            TEST_SCOPE,
        )
        assert isinstance(result, RewriteResult)
        assert result.statement_type == "select"

    def test_select_with_group_by(self):
        result = rewrite_query(
            f"SELECT Voters_Age, COUNT(*) FROM {ALLOWED_TABLE} GROUP BY Voters_Age",
            TEST_SCOPE,
        )
        assert result.statement_type == "select"

    def test_select_with_order_by(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} ORDER BY Voters_Age",
            TEST_SCOPE,
        )
        assert result.statement_type == "select"

    def test_select_count_star(self):
        result = rewrite_query(
            f"SELECT COUNT(*) FROM {ALLOWED_TABLE}", TEST_SCOPE
        )
        assert result.statement_type == "select"

    def test_select_sum_aggregate(self):
        result = rewrite_query(
            f"SELECT SUM(Voters_Age) FROM {ALLOWED_TABLE}", TEST_SCOPE
        )
        assert result.statement_type == "select"

    def test_select_with_limit_within_max(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} LIMIT 100", TEST_SCOPE
        )
        assert result.statement_type == "select"
        assert "100" in result.sql

    def test_describe_allowed_table(self):
        result = rewrite_query(f"DESCRIBE TABLE {ALLOWED_TABLE}", TEST_SCOPE)
        assert result.statement_type == "describe"


# ---------------------------------------------------------------------------
# 12. SCOPE ENFORCEMENT: State override
# ---------------------------------------------------------------------------
class TestScopeEnforcementState:
    def test_rejects_agent_state_filter(self):
        with pytest.raises(ScopeViolation, match="scope_predicate_override"):
            rewrite_query(
                f"SELECT LALVOTERID FROM {ALLOWED_TABLE} WHERE Residence_Addresses_State = 'TX'",
                TEST_SCOPE,
            )

    def test_rejects_agent_city_filter(self):
        with pytest.raises(ScopeViolation, match="scope_predicate_override"):
            rewrite_query(
                f"SELECT LALVOTERID FROM {ALLOWED_TABLE} WHERE Residence_Addresses_City = 'Raleigh'",
                TEST_SCOPE,
            )

    def test_rejects_agent_state_even_if_correct(self):
        with pytest.raises(ScopeViolation, match="scope_predicate_override"):
            rewrite_query(
                f"SELECT LALVOTERID FROM {ALLOWED_TABLE} WHERE Residence_Addresses_State = 'NC'",
                TEST_SCOPE,
            )

    def test_no_user_scope_columns_still_injects(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} WHERE Voters_Age > 30",
            TEST_SCOPE,
        )
        assert "NC" in result.sql
        assert "Fayetteville" in result.sql


# ---------------------------------------------------------------------------
# 13. SCOPE ENFORCEMENT: OR-escape prevention
# ---------------------------------------------------------------------------
class TestScopeEnforcementOrEscape:
    def test_or_true_wrapped_in_parens(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} WHERE Voters_Age > 18 OR 1=1",
            TEST_SCOPE,
        )
        sql_upper = result.sql.upper()
        assert "NC" in result.sql
        state_pos = sql_upper.find("RESIDENCE_ADDRESSES_STATE")
        or_pos = sql_upper.find(" OR ", sql_upper.find("WHERE"))
        assert state_pos < or_pos

    def test_nested_or_wrapped(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} WHERE Voters_Age > 18 OR Voters_Age < 5",
            TEST_SCOPE,
        )
        assert "NC" in result.sql
        assert "Fayetteville" in result.sql

    def test_no_where_still_gets_scope(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE}", TEST_SCOPE
        )
        assert "NC" in result.sql
        assert "Fayetteville" in result.sql


# ---------------------------------------------------------------------------
# 14. SCOPE ENFORCEMENT: LIMIT clamp
# ---------------------------------------------------------------------------
class TestScopeEnforcementLimit:
    def test_limit_exceeds_max(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} LIMIT 1000000", TEST_SCOPE
        )
        assert "50000" in result.sql
        assert "1000000" not in result.sql

    def test_no_limit_injected(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE}", TEST_SCOPE
        )
        assert "50000" in result.sql

    def test_limit_within_max_preserved(self):
        result = rewrite_query(
            f"SELECT LALVOTERID FROM {ALLOWED_TABLE} LIMIT 500", TEST_SCOPE
        )
        assert "500" in result.sql


# ---------------------------------------------------------------------------
# 15. PARAMETER VALIDATION: must-accept
# ---------------------------------------------------------------------------
class TestParameterValidationAccept:
    def test_matching_params(self):
        validate_parameters("SELECT * WHERE x = %(foo)s AND y = %(bar)s", {"foo": 1, "bar": 2})

    def test_no_placeholders_no_params(self):
        validate_parameters("SELECT * FROM t", {})

    def test_single_placeholder(self):
        validate_parameters("SELECT * WHERE x = %(age)s", {"age": 30})


# ---------------------------------------------------------------------------
# H4: SQL injection via scope state with apostrophe
# ---------------------------------------------------------------------------
class TestScopeInjectionSafe:
    def test_state_with_apostrophe(self):
        scope = {**TEST_SCOPE, "state": "O'Brien"}
        result = rewrite_query(f"SELECT LALVOTERID FROM {ALLOWED_TABLE}", scope)
        assert isinstance(result, RewriteResult)
        assert "O\\'Brien" in result.sql or "O''Brien" in result.sql


# ---------------------------------------------------------------------------
# M9: Empty cities list generates invalid SQL
# ---------------------------------------------------------------------------
class TestEmptyCities:
    def test_empty_cities_no_error(self):
        scope = {**TEST_SCOPE, "cities": []}
        result = rewrite_query(f"SELECT LALVOTERID FROM {ALLOWED_TABLE}", scope)
        assert isinstance(result, RewriteResult)
        assert "NC" in result.sql
        assert "IN" not in result.sql.upper().split("WHERE")[1] if "WHERE" in result.sql.upper() else True


# ---------------------------------------------------------------------------
# L4: Missing scope keys don't crash
# ---------------------------------------------------------------------------
class TestMissingScopeKeys:
    def test_missing_state_key(self):
        scope = {
            "cities": ["Fayetteville"],
            "allowed_tables": [ALLOWED_TABLE],
            "max_rows": 50000,
        }
        result = rewrite_query(f"SELECT LALVOTERID FROM {ALLOWED_TABLE}", scope)
        assert isinstance(result, RewriteResult)

    def test_missing_cities_key(self):
        scope = {
            "state": "NC",
            "allowed_tables": [ALLOWED_TABLE],
            "max_rows": 50000,
        }
        result = rewrite_query(f"SELECT LALVOTERID FROM {ALLOWED_TABLE}", scope)
        assert isinstance(result, RewriteResult)
        assert "NC" in result.sql
