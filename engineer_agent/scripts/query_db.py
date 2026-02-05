#!/usr/bin/env python3
import argparse
import os
import sys

from shared.databricks_client import DatabricksClient
from shared.logger import get_logger

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Query Databricks database (READ-ONLY)",
        epilog="""
Examples:
  # Run a SELECT query
  python query_db.py --query "SELECT * FROM goodparty_data_catalog.dbt.campaigns LIMIT 10"

  # List tables in a schema
  python query_db.py --list-tables

  # Describe a table
  python query_db.py --describe campaigns

  # Get row count
  python query_db.py --count campaigns

IMPORTANT: Only SELECT queries are allowed. No INSERT, UPDATE, DELETE, or DDL.
        """
    )

    parser.add_argument(
        "--query",
        type=str,
        help="SQL query to execute (SELECT only)"
    )
    parser.add_argument(
        "--list-tables",
        action="store_true",
        help="List all tables in the default schema"
    )
    parser.add_argument(
        "--describe",
        type=str,
        metavar="TABLE",
        help="Describe a table's schema"
    )
    parser.add_argument(
        "--count",
        type=str,
        metavar="TABLE",
        help="Get row count for a table"
    )
    parser.add_argument(
        "--sample",
        type=str,
        metavar="TABLE",
        help="Get sample rows from a table"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of rows for sample (default: 10)"
    )
    parser.add_argument(
        "--output",
        choices=["table", "json", "csv"],
        default="table",
        help="Output format (default: table)"
    )

    args = parser.parse_args()

    catalog = os.environ.get("DATABRICKS_CATALOG", "goodparty_data_catalog")
    schema = os.environ.get("DATABRICKS_SCHEMA", "dbt")

    if args.query:
        query_upper = args.query.upper().strip()
        forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE", "GRANT", "REVOKE"]
        for word in forbidden:
            if query_upper.startswith(word) or f" {word} " in query_upper:
                logger.error(f"READ-ONLY: {word} operations are not allowed")
                sys.exit(1)

    try:
        with DatabricksClient() as client:
            if args.list_tables:
                tables = client.list_tables(catalog, schema)
                print(f"\nTables in {catalog}.{schema}:")
                print("-" * 40)
                for table in sorted(tables):
                    print(f"  {table}")
                print(f"\nTotal: {len(tables)} tables")

            elif args.describe:
                table = args.describe
                df = client.get_table_schema(catalog, schema, table)
                print(f"\nSchema for {catalog}.{schema}.{table}:")
                print("-" * 60)
                print(df.to_string(index=False))

            elif args.count:
                table = args.count
                count = client.get_table_count(catalog, schema, table)
                print(f"\n{catalog}.{schema}.{table}: {count:,} rows")

            elif args.sample:
                table = args.sample
                df = client.get_table_sample(catalog, schema, table, args.limit)
                print(f"\nSample from {catalog}.{schema}.{table} ({len(df)} rows):")
                print("-" * 80)
                output_df(df, args.output)

            elif args.query:
                df = client.execute_query(args.query)
                print(f"\nQuery returned {len(df)} rows:")
                print("-" * 80)
                output_df(df, args.output)

            else:
                parser.print_help()
                sys.exit(1)

    except Exception as e:
        logger.error(f"Database error: {e}")
        sys.exit(1)


def output_df(df, format):
    if format == "json":
        print(df.to_json(orient="records", indent=2))
    elif format == "csv":
        print(df.to_csv(index=False))
    else:
        print(df.to_string(index=False))


if __name__ == "__main__":
    main()
