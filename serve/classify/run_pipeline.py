#!/usr/bin/env python3

import asyncio
import argparse
import sys
from pathlib import Path
import time
import yaml
from typing import Dict, Any, Optional

from shared.logger import get_logger

from serve.classify.data_loader import DataLoader
from serve.classify.data_cleaner import SmartDataCleaner
from serve.classify.batch_processor import BatchProcessor, BatchProcessingConfig
from serve.classify.smart_aggregator import SmartAggregator
from serve.classify.validator import ClassificationValidator

logger = get_logger(__name__)


class ClassificationPipeline:
    """
    Main orchestrator for the world-class civic message classification pipeline
    """

    def __init__(self, config_file: Optional[str] = None):
        self.config = self._load_config(config_file)

        # Always use classify/output directory regardless of where script is run
        script_dir = Path(__file__).parent  # classify directory
        configured_output = self.config.get("output", {}).get("directory", "./output")

        # If configured output is relative, make it relative to classify dir
        if not Path(configured_output).is_absolute():
            self.output_dir = script_dir / configured_output
        else:
            self.output_dir = Path(configured_output)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Initialize components
        # Data directory should be relative to serve folder (parent of classify)
        configured_data = self.config.get("data", {}).get("directory", "./data")
        if not Path(configured_data).is_absolute():
            serve_dir = script_dir.parent  # serve directory
            data_dir = serve_dir / configured_data.lstrip('./')
        else:
            data_dir = Path(configured_data)

        self.data_loader = DataLoader(str(data_dir))
        self.data_cleaner = SmartDataCleaner(
            min_length=self.config.get("cleaning", {}).get("min_length", 10),
            remove_duplicates=self.config.get("cleaning", {}).get("remove_duplicates", True)
        )

        # Configure batch processor
        batch_config = BatchProcessingConfig(
            batch_size=self.config.get("processing", {}).get("batch_size", 200),
            max_parallel_batches=self.config.get("processing", {}).get("max_parallel", 50),
            enable_validation=self.config.get("processing", {}).get("enable_validation", False),
            temperature=self.config.get("processing", {}).get("temperature", 0.0),
            ultra_fast_mode=True  # Enable all speed optimizations
        )
        self.batch_processor = BatchProcessor(batch_config, str(self.output_dir / "checkpoints"))

        self.aggregator = SmartAggregator(self.config)
        self.validator = ClassificationValidator()

        # Set up progress tracking
        self.batch_processor.set_progress_callback(self._progress_callback)

    def _load_config(self, config_file: Optional[str]) -> Dict[str, Any]:
        """Load configuration from YAML file or use defaults"""
        default_config = {
            "data": {
                "directory": "./data",
                "source": "josh",
                "inbound_only": True
            },
            "cleaning": {
                "min_length": 10,
                "remove_duplicates": True,
                "remove_stop_messages": True
            },
            "processing": {
                "batch_size": 200,
                "max_parallel": 50,
                "enable_validation": False,  # Disable for ultra-fast mode
                "temperature": 0.0
            },
            "output": {
                "directory": "./output",
                "formats": ["csv", "json", "markdown"],
                "include_reports": True
            }
        }

        if config_file and Path(config_file).exists():
            try:
                with open(config_file, 'r') as f:
                    file_config = yaml.safe_load(f)

                # Merge with defaults
                config = default_config.copy()
                config.update(file_config)
                logger.info(f"Configuration loaded from {config_file}")
                return config
            except Exception as e:
                logger.warning(f"Failed to load config file {config_file}: {e}")
                logger.info("Using default configuration")

        return default_config

    def _progress_callback(self, stats):
        """Progress callback for batch processing"""
        progress = stats.processed_messages / stats.total_messages if stats.total_messages > 0 else 0
        logger.info(f"Processing progress: {stats.processed_messages}/{stats.total_messages} "
                   f"({progress:.1%}) - {stats.messages_per_second:.1f} msgs/sec")

    async def run_full_pipeline(self, data_source: str = None, return_data: bool = False) -> Dict[str, Any]:
        """Run the complete classification pipeline"""
        pipeline_start = time.time()

        # Use config default if no data source specified
        data_source = data_source or self.config["data"]["source"]

        logger.info("🚀 Starting World-Class Civic Message Classification Pipeline")
        logger.info(f"Data source: {data_source}")
        logger.info(f"Output directory: {self.output_dir}")

        results = {}

        try:
            # Step 1: Load Data
            logger.info("📂 Step 1: Loading data...")
            messages, data_summary = self.data_loader.load_for_classification(
                source=data_source,
                inbound_only=self.config["data"]["inbound_only"]
            )

            logger.info(f"Loaded {len(messages)} messages")
            results["data_loading"] = {
                "total_messages": len(messages),
                "data_summary": data_summary
            }

            # Step 2: Clean Data
            logger.info("🧹 Step 2: Cleaning data...")
            cleaned_messages, cleaning_stats = self.data_cleaner.clean_messages(messages)

            logger.info(f"Cleaned to {len(cleaned_messages)} substantive messages "
                       f"({len(cleaned_messages)/len(messages):.1%} retention rate)")

            results["data_cleaning"] = {
                "original_count": len(messages),
                "cleaned_count": len(cleaned_messages),
                "retention_rate": len(cleaned_messages)/len(messages),
                "cleaning_stats": cleaning_stats
            }

            # Step 3: Classify Messages
            logger.info("🤖 Step 3: Classifying messages...")
            classified_messages = await self.batch_processor.process_all_messages_production_pattern(cleaned_messages)

            logger.info(f"Successfully classified {len(classified_messages)} messages")
            results["classification"] = {
                "input_count": len(cleaned_messages),
                "output_count": len(classified_messages),
                "processing_stats": self.batch_processor.stats,
                "processing_report": self.batch_processor.generate_processing_report()
            }

            # Include classified messages data objects if requested
            if return_data:
                results["classified_messages"] = classified_messages

            # Step 4: Generate Insights
            logger.info("📊 Step 4: Generating insights...")
            insights = self.aggregator.generate_insights(classified_messages)

            logger.info(f"Generated insights from {insights.substantive_messages} substantive messages")
            results["insights"] = {
                "campaign_insights": insights,
                "insights_report": self.aggregator.generate_insights_report(insights)
            }

            # Step 5: Export Results
            logger.info("💾 Step 5: Exporting results...")
            export_results = await self._export_results(classified_messages, insights, results, data_source)
            results["exports"] = export_results

            # Pipeline Summary
            pipeline_duration = time.time() - pipeline_start
            results["pipeline_summary"] = {
                "total_duration": pipeline_duration,
                "messages_processed": len(classified_messages),
                "processing_speed": len(classified_messages) / pipeline_duration,
                "success": True
            }

            logger.info(f"✅ Pipeline completed successfully in {pipeline_duration:.1f} seconds")
            logger.info(f"Processed {len(classified_messages)} messages at {len(classified_messages)/pipeline_duration:.1f} msgs/sec")

            return results

        except Exception as e:
            logger.error(f"❌ Pipeline failed: {e}")
            results["pipeline_summary"] = {
                "total_duration": time.time() - pipeline_start,
                "error": str(e),
                "success": False
            }
            raise

    async def _export_results(self, messages, insights, pipeline_results, data_source: str) -> Dict[str, Any]:
        """Export results in multiple formats"""
        export_results = {}

        # Create data source prefix for filenames
        source_prefix = f"{data_source}_" if data_source != "all" else "all_campaigns_"

        # Export CSV with classifications
        if "csv" in self.config["output"]["formats"]:
            csv_file = self.output_dir / f"{source_prefix}classified_messages.csv"
            await self._export_csv(messages, csv_file)
            export_results["csv_file"] = str(csv_file)
            logger.info(f"Exported CSV: {csv_file}")

        # Export JSON with full data
        if "json" in self.config["output"]["formats"]:
            json_file = self.output_dir / f"{source_prefix}classification_results.json"
            await self._export_json(pipeline_results, json_file)
            export_results["json_file"] = str(json_file)
            logger.info(f"Exported JSON: {json_file}")

        # Export Markdown reports
        if "markdown" in self.config["output"]["formats"] and self.config["output"]["include_reports"]:
            # Insights report
            insights_file = self.output_dir / f"{source_prefix}insights_report.md"
            insights_report = self.aggregator.generate_insights_report(insights)
            with open(insights_file, 'w') as f:
                f.write(insights_report)
            export_results["insights_report"] = str(insights_file)

            # Processing report
            processing_file = self.output_dir / f"{source_prefix}processing_report.md"
            with open(processing_file, 'w') as f:
                f.write(pipeline_results["classification"]["processing_report"])
            export_results["processing_report"] = str(processing_file)

            # Data cleaning report
            cleaning_file = self.output_dir / f"{source_prefix}cleaning_report.md"
            cleaning_report = self.data_cleaner.generate_cleaning_report(
                pipeline_results["data_cleaning"]["cleaning_stats"]
            )
            with open(cleaning_file, 'w') as f:
                f.write(cleaning_report)
            export_results["cleaning_report"] = str(cleaning_file)

            logger.info("Exported markdown reports")

        return export_results

    async def _export_csv(self, messages, csv_file):
        """Export messages to CSV format"""
        import csv

        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            if not messages:
                return

            # Use the first message to get field names
            sample_row = messages[0].to_csv_row()
            fieldnames = list(sample_row.keys())

            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for message in messages:
                try:
                    row = message.to_csv_row()
                    writer.writerow(row)
                except Exception as e:
                    logger.warning(f"Failed to export message to CSV: {e}")

    async def _export_json(self, results, json_file):
        """Export results to JSON format"""
        import json
        from datetime import datetime

        # Prepare JSON-serializable data
        export_data = {
            "export_timestamp": datetime.now().isoformat(),
            "pipeline_config": self.config,
            "data_loading": results.get("data_loading", {}),
            "data_cleaning": results.get("data_cleaning", {}),
            "classification": {
                "input_count": results.get("classification", {}).get("input_count"),
                "output_count": results.get("classification", {}).get("output_count"),
                "processing_stats": {
                    "total_messages": results.get("classification", {}).get("processing_stats").total_messages,
                    "processed_messages": results.get("classification", {}).get("processing_stats").processed_messages,
                    "success_rate": results.get("classification", {}).get("processing_stats").success_rate,
                    "duration": results.get("classification", {}).get("processing_stats").duration,
                    "messages_per_second": results.get("classification", {}).get("processing_stats").messages_per_second
                } if results.get("classification", {}).get("processing_stats") else {}
            },
            "insights": self._export_insights_data(results.get("insights", {})),
            "pipeline_summary": results.get("pipeline_summary", {})
        }

        with open(json_file, 'w') as f:
            json.dump(export_data, f, indent=2, default=str)

    def _export_insights_data(self, insights_data):
        """Convert insights data to JSON-serializable format"""
        if not insights_data or "campaign_insights" not in insights_data:
            return {}

        insights = insights_data["campaign_insights"]

        return {
            "total_messages": insights.total_messages,
            "substantive_messages": insights.substantive_messages,
            "uncategorized_messages": insights.uncategorized_messages,
            "hierarchical_category_counts": {
                "primary_counts": insights.hierarchical_category_counts.primary_counts,
                "secondary_counts": insights.hierarchical_category_counts.secondary_counts,
                "total_categorized": insights.hierarchical_category_counts.total_categorized,
                "category_percentages": {
                    primary: round(count / insights.hierarchical_category_counts.total_categorized * 100, 1)
                    for primary, count in insights.hierarchical_category_counts.primary_counts.items()
                } if insights.hierarchical_category_counts.total_categorized > 0 else {}
            },
            "top_issues_summary": [
                {
                    "category": f"{issue.primary_category}/{issue.secondary_category}",
                    "total_mentions": issue.total_mentions,
                    "urgency_score": round(issue.urgency_score, 1),
                    "stance_distribution": issue.stance_distribution
                }
                for issue in insights.top_issues[:10]
            ],
            "sentiment_analysis": {
                "overall_distribution": insights.overall_sentiment_distribution,
                "quality_distribution": insights.message_quality_distribution,
                "content_type_distribution": insights.content_type_distribution
            }
        }

    def print_summary(self, results: Dict[str, Any]):
        """Print a summary of the pipeline results"""
        print("\n" + "="*60)
        print("🏆 WORLD-CLASS CLASSIFICATION PIPELINE SUMMARY")
        print("="*60)

        summary = results.get("pipeline_summary", {})
        if summary.get("success"):
            print(f"✅ Status: SUCCESS")
            print(f"⏱️  Duration: {summary['total_duration']:.1f} seconds")
            print(f"📊 Messages Processed: {summary['messages_processed']:,}")
            print(f"🚀 Processing Speed: {summary['processing_speed']:.1f} msgs/sec")
        else:
            print(f"❌ Status: FAILED")
            print(f"❗ Error: {summary.get('error', 'Unknown error')}")

        # Data flow summary
        if "data_cleaning" in results:
            cleaning = results["data_cleaning"]
            print(f"🧹 Data Cleaning: {cleaning['original_count']:,} → {cleaning['cleaned_count']:,} "
                  f"({cleaning['retention_rate']:.1%} retention)")

        # Classification summary
        if "classification" in results:
            classification = results["classification"]
            stats = classification.get("processing_stats")
            if stats:
                print(f"🤖 Classification: {stats.success_rate:.1%} success rate")

        # Top insights
        if "insights" in results:
            insights = results["insights"]["campaign_insights"]
            print(f"📈 Top Issues: {len(insights.top_issues)} identified")
            if insights.top_issues:
                for i, issue in enumerate(insights.top_issues[:3], 1):
                    issue_name = f"{issue.primary_category.replace('_', ' ').title()}: {issue.secondary_category.replace('_', ' ').title()}"
                    print(f"   {i}. {issue_name} ({issue.total_mentions} mentions)")

        # Export files
        if "exports" in results:
            exports = results["exports"]
            print("📁 Exported Files:")
            for export_type, file_path in exports.items():
                print(f"   - {export_type}: {file_path}")

        print("="*60)


async def main():
    """Main CLI interface"""
    parser = argparse.ArgumentParser(
        description="World-Class Civic Message Classification Pipeline"
    )
    parser.add_argument(
        "--data-source",
        default="josh",
        choices=["josh", "cara", "berkley", "heather", "japjeet", "joanna", "jonathan", "all"],
        help="Data source to process (default: josh)"
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to configuration YAML file"
    )
    parser.add_argument(
        "--quick-test",
        action="store_true",
        help="Run with minimal data for quick testing"
    )

    args = parser.parse_args()

    try:
        # Initialize pipeline
        pipeline = ClassificationPipeline(args.config)

        # Adjust for quick test
        if args.quick_test:
            logger.info("🧪 Quick test mode enabled")
            pipeline.config["processing"]["batch_size"] = 10

        # Run pipeline
        results = await pipeline.run_full_pipeline(args.data_source)

        # Print summary
        pipeline.print_summary(results)

        return 0

    except KeyboardInterrupt:
        logger.info("Pipeline interrupted by user")
        return 1
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)