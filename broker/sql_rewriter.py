import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp


class ScopeViolation(Exception):
    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


@dataclass
class RewriteResult:
    sql: str
    statement_type: str


SCOPE_COLUMNS = frozenset({"Residence_Addresses_State", "Residence_Addresses_City"})

FORBIDDEN_TABLE_FUNCTIONS = frozenset({
    "range", "read_files", "delta", "json", "parquet", "csv",
    "explode", "explode_outer", "posexplode", "posexplode_outer",
    "inline", "inline_outer", "stack",
})

FORBIDDEN_SELECT_FUNCTIONS = frozenset({
    "explode", "explode_outer", "posexplode", "posexplode_outer",
    "inline", "inline_outer", "stack",
})

DISALLOWED_STATEMENT_TYPES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
    exp.Drop, exp.Create, exp.Alter, exp.Grant, exp.Command,
)

_PARAM_RE = re.compile(r":(\w+)\b|%\((\w+)\)s")

_UNICODE_SEMICOLONS = "\uFF1B\uFE14\u037E"


def rewrite_query(sql: str, scope: dict, parameters: dict | None = None) -> RewriteResult:
    _check_stacked_statements(sql)

    try:
        ast = sqlglot.parse_one(sql, dialect="databricks")
    except sqlglot.errors.ParseError as e:
        raise ScopeViolation("parse_error", detail=str(e)) from e

    if isinstance(ast, DISALLOWED_STATEMENT_TYPES):
        raise ScopeViolation("disallowed_verb", detail=type(ast).__name__)

    if isinstance(ast, exp.Describe):
        return _handle_describe(ast, scope)

    if isinstance(ast, exp.Select):
        return _handle_select(ast, scope)

    raise ScopeViolation("unsupported_statement", detail=type(ast).__name__)


def validate_parameters(sql: str, parameters: dict) -> None:
    placeholders = set()
    for named, pyformat in _PARAM_RE.findall(sql):
        placeholders.add(named or pyformat)
    param_keys = set(parameters.keys())
    if placeholders != param_keys:
        missing = placeholders - param_keys
        extra = param_keys - placeholders
        raise ScopeViolation(
            "parameter_mismatch",
            detail=f"missing={missing}, extra={extra}",
        )


def _check_stacked_statements(sql: str):
    for ch in _UNICODE_SEMICOLONS:
        if ch in sql:
            raise ScopeViolation("stacked_statements", detail="unicode semicolon detected")

    statements = sqlglot.parse(sql, dialect="databricks")
    if len(statements) > 1:
        raise ScopeViolation("stacked_statements", detail=f"found {len(statements)} statements")


def _handle_describe(ast: exp.Describe, scope: dict) -> RewriteResult:
    table = ast.find(exp.Table)
    if table:
        cte_names: set[str] = set()
        if not _table_is_allowed(table, scope, cte_names):
            raise ScopeViolation("disallowed_table", detail=_resolve_table_fqn(table))
    return RewriteResult(sql=ast.sql(dialect="databricks"), statement_type="describe")


def _handle_select(ast: exp.Select, scope: dict) -> RewriteResult:
    cte_names = _collect_cte_names(ast)

    _walk_tables(ast, scope, cte_names)
    _check_forbidden_functions(ast)
    _enforce_scope_predicates(ast, scope)
    _clamp_limit(ast, scope)

    return RewriteResult(sql=ast.sql(dialect="databricks"), statement_type="select")


def _collect_cte_names(ast: exp.Select) -> set[str]:
    names = set()
    for cte in ast.find_all(exp.CTE):
        if cte.alias:
            names.add(cte.alias)
    return names


def _resolve_table_fqn(table: exp.Table) -> str:
    parts = []
    if table.catalog:
        parts.append(table.catalog)
    if table.db:
        parts.append(table.db)
    if table.name:
        parts.append(table.name)
    return ".".join(parts)


def _table_is_allowed(table: exp.Table, scope: dict, cte_names: set[str]) -> bool:
    if isinstance(table.this, exp.Anonymous):
        func_name = table.this.this.lower()
        if func_name in FORBIDDEN_TABLE_FUNCTIONS:
            raise ScopeViolation("forbidden_function", detail=func_name)
        raise ScopeViolation("disallowed_table", detail=func_name)

    fqn = _resolve_table_fqn(table)

    if fqn in cte_names or table.name in cte_names:
        return True

    allowed = scope.get("allowed_tables", [])

    if fqn in allowed:
        return True

    for allowed_table in allowed:
        # Match if user specified a partial qualifier that's a proper suffix
        # of the allowed FQN (e.g., allowed=a.b.c, user says b.c → fqn="b.c").
        # This only fires when the user explicitly uses the real db/basename
        # path — spoofed catalogs like `evil_catalog.public.<same-basename>`
        # produce fqn="evil_catalog.public.c" which is NOT a suffix of a.b.c.
        if allowed_table.endswith(f".{fqn}"):
            return True
        # Bare-basename shorthand: user typed just the table name without any
        # catalog/db qualifier. Allowed only when no catalog/db was supplied,
        # to prevent cross-catalog confusion (rejecting `evil.public.<name>`).
        parts = allowed_table.split(".")
        if parts and parts[-1] == table.name and not table.catalog and not table.db:
            return True

    return False


def _walk_tables(ast: exp.Select, scope: dict, cte_names: set[str]):
    for table in ast.find_all(exp.Table):
        if not _table_is_allowed(table, scope, cte_names):
            raise ScopeViolation("disallowed_table", detail=_resolve_table_fqn(table))



def _check_forbidden_functions(ast: exp.Select):
    for node in ast.walk():
        if isinstance(node, exp.Explode):
            raise ScopeViolation("forbidden_function", detail=type(node).__name__.lower())

        if isinstance(node, exp.Inline):
            raise ScopeViolation("forbidden_function", detail="inline")

        if isinstance(node, exp.Anonymous):
            func_name = node.this.lower() if isinstance(node.this, str) else ""
            if func_name in FORBIDDEN_TABLE_FUNCTIONS | FORBIDDEN_SELECT_FUNCTIONS:
                raise ScopeViolation("forbidden_function", detail=func_name)


def _flatten_and(node: exp.Expression):
    if isinstance(node, exp.And):
        yield from _flatten_and(node.left)
        yield from _flatten_and(node.right)
    else:
        yield node


def _condition_touches_scope_column(node: exp.Expression) -> bool:
    for col in node.find_all(exp.Column):
        if col.name in SCOPE_COLUMNS:
            return True
    return False


def _enforce_scope_predicates(ast: exp.Select, scope: dict):
    state = scope.get("state", "")
    cities = scope.get("cities", [])

    state_cond = exp.EQ(
        this=exp.Column(this=exp.to_identifier("Residence_Addresses_State")),
        expression=exp.Literal.string(state),
    )

    scope_predicates = [state_cond]

    if cities:
        city_cond = exp.In(
            this=exp.Column(this=exp.to_identifier("Residence_Addresses_City")),
            expressions=[exp.Literal.string(c) for c in cities],
        )
        scope_predicates.append(city_cond)

    if len(scope_predicates) == 1:
        scope_predicate = scope_predicates[0]
    else:
        scope_predicate = exp.and_(*scope_predicates)

    where = ast.find(exp.Where)
    if where is None:
        ast.set("where", exp.Where(this=scope_predicate))
        return

    existing = where.this

    for part in _flatten_and(existing):
        if _condition_touches_scope_column(part):
            raise ScopeViolation(
                "scope_predicate_override",
                detail="Do not filter by Residence_Addresses_State or Residence_Addresses_City — the broker injects these automatically.",
            )

    user_parts = list(_flatten_and(existing))

    if user_parts:
        if len(user_parts) == 1:
            user_combined = user_parts[0]
        else:
            user_combined = user_parts[0]
            for p in user_parts[1:]:
                user_combined = exp.and_(user_combined, p)

        final = exp.and_(scope_predicate, exp.Paren(this=user_combined))
    else:
        final = scope_predicate

    ast.set("where", exp.Where(this=final))


def _clamp_limit(ast: exp.Select, scope: dict):
    max_rows = scope.get("max_rows", 50000)
    limit_node = ast.args.get("limit")

    if limit_node is not None:
        try:
            current = int(limit_node.expression.this)
            if current > max_rows:
                ast.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        except (ValueError, AttributeError):
            ast.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    else:
        ast.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
