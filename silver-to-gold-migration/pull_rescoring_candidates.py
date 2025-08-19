#!/usr/bin/env python3

import os
import sys
import pandas as pd
from pathlib import Path

# Add the parent directory to path to import shared modules
sys.path.append(str(Path(__file__).parent.parent))

from shared.databricks_client import DatabricksClient
from shared.logger import get_logger

class RescoringCandidatesPuller:
    """Pull silver tier candidates that need rescoring to gold tier"""
    
    def __init__(self):
        self.logger = get_logger(__name__)
        self.databricks = DatabricksClient()
        
        # Table configuration
        self.catalog = "goodparty_data_catalog"
        self.schema = "sandbox"
        self.table = "gp_candidates_needing_rescoring"
        self.table_path = f"{self.catalog}.{self.schema}.{self.table}"
        
        # Output directory
        self.output_dir = Path(__file__).parent / "data"
        self.output_dir.mkdir(exist_ok=True)
        
    def check_table_exists(self) -> bool:
        """Check if the rescoring candidates table exists"""
        try:
            schema_df = self.databricks.get_table_schema(self.catalog, self.schema, self.table)
            self.logger.info(f"✅ Table {self.table_path} exists with {len(schema_df)} columns")
            return True
        except Exception as e:
            self.logger.error(f"❌ Table {self.table_path} does not exist or is not accessible: {e}")
            return False
    
    def analyze_table_structure(self) -> pd.DataFrame:
        """Analyze the structure of the rescoring candidates table"""
        self.logger.info(f"📊 Analyzing table structure: {self.table_path}")
        
        try:
            # Get schema
            schema_df = self.databricks.get_table_schema(self.catalog, self.schema, self.table)
            self.logger.info(f"Table schema:")
            for _, row in schema_df.iterrows():
                self.logger.info(f"  - {row['col_name']}: {row['data_type']}")
            
            # Get row count
            count = self.databricks.get_table_count(self.catalog, self.schema, self.table)
            self.logger.info(f"Total rows: {count:,}")
            
            # Get sample data
            sample_df = self.databricks.get_table_sample(self.catalog, self.schema, self.table, limit=5)
            self.logger.info(f"Sample data (first 5 rows):")
            self.logger.info(f"\n{sample_df.to_string()}")
            
            return schema_df
            
        except Exception as e:
            self.logger.error(f"❌ Failed to analyze table structure: {e}")
            raise
    
    def pull_rescoring_candidates(self, limit: int = None, output_format: str = "parquet") -> str:
        """
        Pull candidates needing rescoring from Databricks
        
        Args:
            limit: Optional limit on number of rows to pull
            output_format: Output format ('parquet', 'csv', 'tsv')
        
        Returns:
            Path to the output file
        """
        self.logger.info(f"🔄 Pulling candidates needing rescoring from {self.table_path}")
        
        # Build query
        limit_clause = f"LIMIT {limit}" if limit else ""
        query = f"""
        SELECT *
        FROM {self.table_path}
        {limit_clause}
        """
        
        try:
            # Execute query
            df = self.databricks.execute_query(query)
            
            if df.empty:
                self.logger.warning("⚠️ No candidates found needing rescoring")
                return ""
            
            self.logger.info(f"📊 Retrieved {len(df):,} candidates needing rescoring")
            
            # Generate output filename
            if limit:
                filename = f"gp_candidates_needing_rescoring_sample_{limit}.{output_format}"
            else:
                filename = f"gp_candidates_needing_rescoring_full.{output_format}"
            
            output_path = self.output_dir / filename
            
            # Save data
            if output_format == "parquet":
                df.to_parquet(output_path, index=False)
            elif output_format == "csv":
                df.to_csv(output_path, index=False)
            elif output_format == "tsv":
                df.to_csv(output_path, index=False, sep='\t')
            else:
                raise ValueError(f"Unsupported output format: {output_format}")
            
            self.logger.info(f"💾 Data saved to: {output_path}")
            
            # Print summary
            self.print_data_summary(df)
            
            return str(output_path)
            
        except Exception as e:
            self.logger.error(f"❌ Failed to pull rescoring candidates: {e}")
            raise
    
    def print_data_summary(self, df: pd.DataFrame):
        """Print summary of the pulled data"""
        self.logger.info(f"\n📊 DATA SUMMARY")
        self.logger.info(f"{'='*50}")
        self.logger.info(f"Total candidates: {len(df):,}")
        self.logger.info(f"Columns: {len(df.columns)}")
        
        # Show column names
        self.logger.info(f"\nColumns:")
        for col in df.columns:
            self.logger.info(f"  - {col}")
        
        # Show data types
        self.logger.info(f"\nData types:")
        for col, dtype in df.dtypes.items():
            self.logger.info(f"  - {col}: {dtype}")
        
        # Show sample data
        self.logger.info(f"\nFirst 3 rows:")
        self.logger.info(f"\n{df.head(3).to_string()}")
    
    def get_candidates_by_state(self, state: str = None) -> pd.DataFrame:
        """Get candidates filtered by state"""
        if state:
            state = state.upper()
            query = f"""
            SELECT *
            FROM {self.table_path}
            WHERE UPPER(state) = '{state}'
            """
            self.logger.info(f"🎯 Pulling candidates for state: {state}")
        else:
            query = f"SELECT * FROM {self.table_path}"
            self.logger.info(f"🎯 Pulling all candidates")
        
        try:
            df = self.databricks.execute_query(query)
            self.logger.info(f"📊 Found {len(df):,} candidates" + (f" in {state}" if state else ""))
            return df
        except Exception as e:
            self.logger.error(f"❌ Failed to get candidates by state: {e}")
            raise


def main():
    """Main entry point for the script"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Pull GP candidates needing rescoring from Databricks")
    parser.add_argument('--limit', '-l', type=int, help='Limit number of rows to pull')
    parser.add_argument('--state', '-s', type=str, help='Filter by state (e.g., CA, NY, TX)')
    parser.add_argument('--format', '-f', choices=['parquet', 'csv', 'tsv'], default='parquet',
                       help='Output format (default: parquet)')
    parser.add_argument('--analyze', '-a', action='store_true',
                       help='Analyze table structure only (no data pull)')
    
    args = parser.parse_args()
    
    puller = RescoringCandidatesPuller()
    
    # Test connection first
    if not puller.databricks.test_connection():
        print("❌ Failed to connect to Databricks. Check your credentials.")
        return
    
    # Check if table exists
    if not puller.check_table_exists():
        print(f"❌ Table {puller.table_path} not found. Please verify the table name.")
        return
    
    # Analyze table structure if requested
    if args.analyze:
        puller.analyze_table_structure()
        return
    
    try:
        if args.state:
            # Get candidates by state
            df = puller.get_candidates_by_state(args.state)
            
            # Save state-specific data
            filename = f"gp_candidates_needing_rescoring_{args.state.lower()}.{args.format}"
            output_path = puller.output_dir / filename
            
            if args.format == "parquet":
                df.to_parquet(output_path, index=False)
            elif args.format == "csv":
                df.to_csv(output_path, index=False)
            elif args.format == "tsv":
                df.to_csv(output_path, index=False, sep='\t')
            
            puller.logger.info(f"💾 State data saved to: {output_path}")
            puller.print_data_summary(df)
        else:
            # Pull all data or limited data
            output_path = puller.pull_rescoring_candidates(limit=args.limit, output_format=args.format)
            
        print(f"\n✅ Successfully pulled rescoring candidates data")
        print(f"📁 Output file: {output_path}")
        
        print(f"\n💡 Next steps:")
        print(f"1. Review the data: pandas df = pd.read_parquet('{output_path}')")
        print(f"2. Run the rescoring model on these candidates")
        print(f"3. Update their tier from silver to gold")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())