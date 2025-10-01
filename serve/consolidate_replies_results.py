#!/usr/bin/env python3

import pandas as pd
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from datetime import datetime
import re

sys.path.append('/Users/collinpark/work/gp-ai-projects')
from shared.logger import get_logger

logger = get_logger(__name__)

class RepliesResultsConsolidator:
    """Consolidate replies and results files with demographic enrichment"""

    def __init__(self, input_dir: str = "./input", output_dir: str = "./output"):
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)

        if not self.input_dir.exists():
            raise FileNotFoundError(f"Input directory not found: {input_dir}")

    def discover_files(self) -> Dict[str, Dict[str, List[Path]]]:
        """Discover and organize all replies and results files"""
        campaigns = {}

        for file_path in self.input_dir.glob("*.csv"):
            filename = file_path.name

            # Extract campaign and round info
            match = re.search(r'--([^-]+)--R(\d+)-(replies|results)', filename)
            if not match:
                logger.warning(f"Skipping file with unexpected format: {filename}")
                continue

            campaign = match.group(1).lower()
            round_num = f"R{match.group(2)}"
            file_type = match.group(3)

            if campaign not in campaigns:
                campaigns[campaign] = {"replies": [], "results": []}

            campaigns[campaign][file_type].append({
                "path": file_path,
                "round": round_num,
                "filename": filename
            })

        logger.info(f"Discovered campaigns: {list(campaigns.keys())}")
        for campaign, files in campaigns.items():
            logger.info(f"  {campaign}: {len(files['replies'])} replies, {len(files['results'])} results")

        return campaigns

    def load_replies(self, files: List[Dict]) -> pd.DataFrame:
        """Load and combine all replies files"""
        all_replies = []

        for file_info in files:
            try:
                df = pd.read_csv(file_info["path"])
                df["round"] = file_info["round"]
                df["source_file"] = file_info["filename"]
                all_replies.append(df)
                logger.info(f"Loaded {len(df)} replies from {file_info['round']}")
            except Exception as e:
                logger.error(f"Error loading {file_info['filename']}: {e}")
                continue

        if not all_replies:
            return pd.DataFrame()

        combined = pd.concat(all_replies, ignore_index=True)

        # Clean and standardize phone numbers
        combined["Contact Phone Number"] = combined["Contact Phone Number"].astype(str).str.replace(r"[^\d]", "", regex=True)

        # Remove STOP messages (opt-outs)
        before_filter = len(combined)
        combined = combined[~combined["Message Text"].astype(str).str.upper().str.strip().isin(["STOP", "STOP "])]
        after_filter = len(combined)
        logger.info(f"Filtered out {before_filter - after_filter} STOP messages")

        return combined

    def load_results(self, files: List[Dict]) -> pd.DataFrame:
        """Load and combine all results files"""
        all_results = []

        for file_info in files:
            try:
                df = pd.read_csv(file_info["path"])
                df["round"] = file_info["round"]
                df["source_file"] = file_info["filename"]
                all_results.append(df)
                logger.info(f"Loaded {len(df)} results from {file_info['round']}")
            except Exception as e:
                logger.error(f"Error loading {file_info['filename']}: {e}")
                continue

        if not all_results:
            return pd.DataFrame()

        combined = pd.concat(all_results, ignore_index=True)

        # Clean and standardize phone numbers
        combined["Contact Phone Number"] = combined["Contact Phone Number"].astype(str).str.replace(r"[^\d]", "", regex=True)

        # Remove duplicates (same person contacted in multiple rounds)
        before_dedup = len(combined)
        combined = combined.drop_duplicates(subset=["Contact Phone Number"], keep="first")
        after_dedup = len(combined)
        logger.info(f"Removed {before_dedup - after_dedup} duplicate contacts")

        return combined

    def enrich_with_demographics(self, replies_df: pd.DataFrame, results_df: pd.DataFrame) -> pd.DataFrame:
        """Join replies with demographic data from results"""

        # Perform left join to preserve all replies
        enriched = replies_df.merge(
            results_df,
            on="Contact Phone Number",
            how="left",
            suffixes=("_reply", "_result")
        )

        # Calculate join statistics
        total_replies = len(replies_df)
        matched_replies = len(enriched[enriched["voters_age"].notna()])
        match_rate = (matched_replies / total_replies) * 100 if total_replies > 0 else 0

        logger.info(f"Demographic enrichment: {matched_replies}/{total_replies} replies matched ({match_rate:.1f}%)")

        # Clean up duplicate columns
        enriched = self._cleanup_duplicate_columns(enriched)

        # Add derived demographic fields
        enriched = self._add_derived_demographics(enriched)

        # Add placeholder columns for future demographics
        enriched = self._add_placeholder_demographics(enriched)

        return enriched

    def _cleanup_duplicate_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Clean up duplicate columns from merge"""
        # Keep reply version for message-related fields, result version for demographics
        columns_to_keep = []

        for col in df.columns:
            if col.endswith("_reply"):
                base_col = col.replace("_reply", "")
                if base_col in ["Campaign ID", "Campaign Name", "Contact Phone Number"]:
                    columns_to_keep.append(col)
                    df[base_col] = df[col]
            elif col.endswith("_result"):
                base_col = col.replace("_result", "")
                if base_col not in ["round", "source_file"]:
                    continue  # Keep original demographic columns
            else:
                columns_to_keep.append(col)

        # Remove duplicate suffix columns
        df = df[[col for col in df.columns if not (col.endswith("_reply") or col.endswith("_result"))]]

        return df

    def _add_derived_demographics(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add derived demographic fields"""

        # Age groups
        def categorize_age(age):
            if pd.isna(age):
                return "Unknown"
            age = int(age)
            if age < 30:
                return "18-29"
            elif age < 45:
                return "30-44"
            elif age < 65:
                return "45-64"
            else:
                return "65+"

        df["age_group"] = df["voters_age"].apply(categorize_age)

        # Voting performance categories
        def categorize_voting_performance(general_perf, minor_perf):
            if pd.isna(general_perf) or general_perf in ["Not Eligible", ""]:
                return "Unknown"

            try:
                if isinstance(general_perf, str):
                    general = float(general_perf.replace("%", ""))
                else:
                    general = float(general_perf)
            except (ValueError, AttributeError):
                return "Unknown"

            if general >= 80:
                return "High Turnout"
            elif general >= 50:
                return "Moderate Turnout"
            else:
                return "Low Turnout"

        df["voting_performance_category"] = df.apply(
            lambda row: categorize_voting_performance(
                row.get("votingperformanceevenyeargeneral"),
                row.get("votingperformanceminorelection")
            ), axis=1
        )

        # Location categorization
        df["location"] = df["residence_addresses_city"].fillna("Unknown")
        df["ward"] = df["city_ward"].fillna("Unknown")

        return df

    def _add_placeholder_demographics(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add placeholder columns for future demographic data"""
        placeholder_columns = {
            "income_level": "Unknown",
            "education_level": "Unknown",
            "homeowner_status": "Unknown",
            "business_owner": "Unknown",
            "has_children_under_18": "Unknown"
        }

        for col, default_value in placeholder_columns.items():
            df[col] = default_value

        return df

    def generate_demographic_analysis(self, df: pd.DataFrame) -> Dict:
        """Generate comprehensive demographic analysis"""

        total_responses = len(df)
        unique_respondents = df["Contact Phone Number"].nunique()

        analysis = {
            "summary": {
                "total_messages": total_responses,
                "unique_respondents": unique_respondents,
                "messages_per_respondent": round(total_responses / unique_respondents, 2) if unique_respondents > 0 else 0,
                "demographic_match_rate": round((df["voters_age"].notna().sum() / total_responses) * 100, 2)
            },
            "by_round": {},
            "demographics": {}
        }

        # Round-by-round breakdown
        if "round_reply" in df.columns:
            round_col = "round_reply"
        elif "round" in df.columns:
            round_col = "round"
        else:
            round_col = None

        if round_col:
            for round_name in df[round_col].unique():
                if pd.notna(round_name):
                    round_data = df[df[round_col] == round_name]
                    analysis["by_round"][round_name] = {
                        "total_messages": len(round_data),
                        "unique_respondents": round_data["Contact Phone Number"].nunique()
                    }

        # Age group analysis
        age_analysis = df.groupby("age_group").agg({
            "Contact Phone Number": "nunique",
            "Message Text": "count"
        }).to_dict()

        analysis["demographics"]["age_groups"] = {
            "respondent_counts": age_analysis["Contact Phone Number"],
            "message_counts": age_analysis["Message Text"]
        }

        # Gender analysis
        gender_analysis = df.groupby("voters_gender").agg({
            "Contact Phone Number": "nunique",
            "Message Text": "count"
        }).to_dict()

        analysis["demographics"]["gender"] = {
            "respondent_counts": gender_analysis["Contact Phone Number"],
            "message_counts": gender_analysis["Message Text"]
        }

        # Voting performance analysis
        voting_analysis = df.groupby("voting_performance_category").agg({
            "Contact Phone Number": "nunique",
            "Message Text": "count"
        }).to_dict()

        analysis["demographics"]["voting_performance"] = {
            "respondent_counts": voting_analysis["Contact Phone Number"],
            "message_counts": voting_analysis["Message Text"]
        }

        # Location analysis
        location_analysis = df.groupby("ward").agg({
            "Contact Phone Number": "nunique",
            "Message Text": "count"
        }).to_dict()

        analysis["demographics"]["ward"] = {
            "respondent_counts": location_analysis["Contact Phone Number"],
            "message_counts": location_analysis["Message Text"]
        }

        return analysis

    def process_campaign(self, campaign_name: str, files: Dict) -> Tuple[pd.DataFrame, Dict]:
        """Process a single campaign's data"""
        logger.info(f"🎯 Processing {campaign_name.upper()} campaign...")

        # Load replies and results
        replies_df = self.load_replies(files["replies"])
        results_df = self.load_results(files["results"])

        if replies_df.empty:
            logger.error(f"No replies data found for {campaign_name}")
            return pd.DataFrame(), {}

        if results_df.empty:
            logger.warning(f"No results data found for {campaign_name} - proceeding without demographics")
            enriched_df = replies_df.copy()
            enriched_df = self._add_placeholder_demographics(enriched_df)
        else:
            # Enrich replies with demographic data
            enriched_df = self.enrich_with_demographics(replies_df, results_df)

        # Generate analysis
        analysis = self.generate_demographic_analysis(enriched_df)

        # Save outputs
        self._save_outputs(campaign_name, enriched_df, analysis)

        return enriched_df, analysis

    def _save_outputs(self, campaign_name: str, df: pd.DataFrame, analysis: Dict):
        """Save consolidated data and analysis"""

        # Save enriched CSV
        csv_path = self.output_dir / f"{campaign_name}_consolidated.csv"
        df.to_csv(csv_path, index=False)
        logger.info(f"💾 Saved consolidated data: {csv_path}")

        # Save analysis JSON
        json_path = self.output_dir / f"{campaign_name}_demographics.json"
        with open(json_path, 'w') as f:
            json.dump(analysis, f, indent=2, default=str)
        logger.info(f"📊 Saved demographic analysis: {json_path}")

        # Save analysis report
        report_path = self.output_dir / f"{campaign_name}_analysis_report.md"
        self._generate_analysis_report(campaign_name, analysis, report_path)
        logger.info(f"📄 Saved analysis report: {report_path}")

    def _generate_analysis_report(self, campaign_name: str, analysis: Dict, output_path: Path):
        """Generate markdown analysis report"""

        report = f"""# {campaign_name.title()} Campaign Analysis Report

Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## Executive Summary

- **Total Messages**: {analysis['summary']['total_messages']:,}
- **Unique Respondents**: {analysis['summary']['unique_respondents']:,}
- **Avg Messages/Respondent**: {analysis['summary']['messages_per_respondent']}
- **Demographic Match Rate**: {analysis['summary']['demographic_match_rate']}%

## Round-by-Round Breakdown

"""

        for round_name, data in analysis["by_round"].items():
            report += f"### {round_name}\n"
            report += f"- Messages: {data['total_messages']:,}\n"
            report += f"- Unique Respondents: {data['unique_respondents']:,}\n\n"

        report += "## Demographic Breakdowns\n\n"

        # Age groups
        if "age_groups" in analysis["demographics"]:
            report += "### Age Groups\n\n"
            for age_group, count in analysis["demographics"]["age_groups"]["respondent_counts"].items():
                msg_count = analysis["demographics"]["age_groups"]["message_counts"].get(age_group, 0)
                report += f"- **{age_group}**: {count} respondents, {msg_count} messages\n"
            report += "\n"

        # Gender
        if "gender" in analysis["demographics"]:
            report += "### Gender\n\n"
            for gender, count in analysis["demographics"]["gender"]["respondent_counts"].items():
                msg_count = analysis["demographics"]["gender"]["message_counts"].get(gender, 0)
                report += f"- **{gender}**: {count} respondents, {msg_count} messages\n"
            report += "\n"

        # Voting performance
        if "voting_performance" in analysis["demographics"]:
            report += "### Voting Performance\n\n"
            for category, count in analysis["demographics"]["voting_performance"]["respondent_counts"].items():
                msg_count = analysis["demographics"]["voting_performance"]["message_counts"].get(category, 0)
                report += f"- **{category}**: {count} respondents, {msg_count} messages\n"
            report += "\n"

        # Location
        if "ward" in analysis["demographics"]:
            report += "### By Ward\n\n"
            for ward, count in analysis["demographics"]["ward"]["respondent_counts"].items():
                msg_count = analysis["demographics"]["ward"]["message_counts"].get(ward, 0)
                report += f"- **Ward {ward}**: {count} respondents, {msg_count} messages\n"
            report += "\n"

        report += """## Future Enhancements

This report includes placeholder columns for additional demographics that will be populated when available:

- Income Level
- Education Level
- Homeowner Status
- Business Owner Status
- Families with Children Under 18

## Usage for Dashboard

The consolidated CSV contains all message data enriched with available demographics, ready for:
- Theme clustering analysis
- Sentiment analysis by demographic segment
- Response expandable sections with constituent attribution
- Interactive demographic filtering
"""

        with open(output_path, 'w') as f:
            f.write(report)

    def consolidate_all(self):
        """Consolidate all discovered campaigns"""
        campaigns = self.discover_files()

        if not campaigns:
            logger.error("No campaigns discovered!")
            return

        all_results = {}

        for campaign_name, files in campaigns.items():
            try:
                df, analysis = self.process_campaign(campaign_name, files)
                all_results[campaign_name] = {
                    "dataframe": df,
                    "analysis": analysis
                }
                logger.info(f"✅ Successfully processed {campaign_name}")
            except Exception as e:
                logger.error(f"❌ Failed to process {campaign_name}: {e}")
                continue

        logger.info(f"🏆 Consolidation complete! Processed {len(all_results)} campaigns")
        logger.info(f"📂 Output directory: {self.output_dir}")

        return all_results


def main():
    """Run the consolidation process"""
    input_dir = "/Users/collinpark/work/gp-ai-projects/serve/input"
    output_dir = "/Users/collinpark/work/gp-ai-projects/serve/output"

    consolidator = RepliesResultsConsolidator(input_dir, output_dir)
    results = consolidator.consolidate_all()

    return results


if __name__ == "__main__":
    main()