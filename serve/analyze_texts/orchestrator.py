import time
import json
import sys
from pathlib import Path
from typing import Dict, List
import yaml

sys.path.append(str(Path(__file__).parent.parent.parent))
from shared.logger import get_logger
from serve.analyze_texts.models import PipelineStats, CategorySummary, ClassifiedMessage, EnrichedMessageExport, RefinedCategorySummary
from serve.analyze_texts.stages.stage_0_loader import load_data_stage
from serve.analyze_texts.stages.stage_1_cleaner import clean_data_stage
from serve.analyze_texts.stages.stage_2_filter import filter_data_stage
from serve.analyze_texts.stages.stage_3_atomizer import atomize_data_stage
from serve.analyze_texts.stages.stage_4_classifier import classify_data_stage
from serve.analyze_texts.stages.stage_5_synthesizer import synthesize_data_stage
from serve.analyze_texts.stages.stage_6_hierarchical_reanalysis import hierarchical_reanalysis_stage

logger = get_logger(__name__)


class AnalyzeTextsOrchestrator:
    def __init__(self, config_path: str = "serve/analyze_texts/config.yaml"):
        self.config_path = Path(config_path)

        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(self.config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        self.stage_timings = {}
        self.stage_outputs = {}

        logger.info(f"AnalyzeTextsOrchestrator initialized with config: {config_path}")

    def run(self, campaign: str = None) -> Dict:
        if campaign:
            self.config['pipeline']['campaign'] = campaign

        campaign_name = self.config['pipeline']['campaign']

        logger.info(f"=" * 80)
        logger.info(f"ANALYZE TEXTS PIPELINE - Campaign: {campaign_name}")
        logger.info(f"=" * 80)

        start_time = time.time()

        stage_0_start = time.time()
        messages = load_data_stage(campaign_name, self.config)
        self.stage_timings['stage_0_loader'] = time.time() - stage_0_start
        self.stage_outputs['loaded_messages'] = len(messages)

        stage_1_start = time.time()
        cleaned_messages = clean_data_stage(messages, self.config)
        self.stage_timings['stage_1_cleaner'] = time.time() - stage_1_start
        self.stage_outputs['cleaned_messages'] = len(cleaned_messages)

        stage_2_start = time.time()
        filtered_messages, filter_stats = filter_data_stage(cleaned_messages, self.config)
        self.stage_timings['stage_2_filter'] = time.time() - stage_2_start
        self.stage_outputs['filtered_messages'] = len(filtered_messages)
        self.stage_outputs['filter_stats'] = filter_stats

        stage_3_start = time.time()
        atomized_messages = atomize_data_stage(filtered_messages, self.config)
        self.stage_timings['stage_3_atomizer'] = time.time() - stage_3_start
        self.stage_outputs['atomized_messages'] = len(atomized_messages)

        stage_4_start = time.time()
        classified_messages = classify_data_stage(atomized_messages, self.config)
        self.stage_timings['stage_4_classifier'] = time.time() - stage_4_start
        self.stage_outputs['classified_messages'] = len(classified_messages)

        stage_5_start = time.time()
        category_summaries = synthesize_data_stage(classified_messages, self.config)
        self.stage_timings['stage_5_synthesizer'] = time.time() - stage_5_start
        self.stage_outputs['category_summaries'] = len(category_summaries)

        stage_6_start = time.time()
        refined_summaries = hierarchical_reanalysis_stage(classified_messages, category_summaries, self.config)
        self.stage_timings['stage_6_hierarchical_reanalysis'] = time.time() - stage_6_start
        self.stage_outputs['refined_summaries'] = len(refined_summaries)

        export_start = time.time()
        self.export_results(campaign_name, classified_messages, refined_summaries)
        self.stage_timings['export'] = time.time() - export_start

        total_time = time.time() - start_time

        logger.info(f"\n" + "=" * 80)
        logger.info(f"PIPELINE COMPLETE - Total Time: {total_time:.2f}s")
        logger.info(f"=" * 80)

        self.print_summary()

        return {
            "campaign": campaign_name,
            "classified_messages": classified_messages,
            "category_summaries": category_summaries,
            "stats": self.stage_outputs,
            "timings": self.stage_timings
        }

    def export_results(self, campaign: str, classified_messages: List[ClassifiedMessage], refined_summaries: List[RefinedCategorySummary]):
        logger.info("=== EXPORTING RESULTS ===")

        output_dir = Path(self.config.get('exporter', {}).get('output_dir', 'serve/analyze_texts/output'))
        campaign_dir = output_dir / campaign
        campaign_dir.mkdir(parents=True, exist_ok=True)

        atomized_csv_path = campaign_dir / f"{campaign}_atomized.csv"
        self._export_atomized_csv(classified_messages, atomized_csv_path)

        enriched_csv_path = campaign_dir / f"{campaign}_enriched.csv"
        self._export_enriched_csv(classified_messages, refined_summaries, enriched_csv_path)

        summaries_json_path = campaign_dir / f"{campaign}_refined_summaries.json"
        self._export_refined_summaries_json(refined_summaries, summaries_json_path)

        report_path = campaign_dir / f"{campaign}_analysis_report.md"
        self._export_report(campaign, classified_messages, refined_summaries, report_path)

        stats_path = campaign_dir / f"{campaign}_pipeline_stats.json"
        self._export_stats(campaign, stats_path)

        logger.info(f"Exported all results to {campaign_dir}")

    def _export_atomized_csv(self, classified_messages: List[ClassifiedMessage], output_path: Path):
        import csv

        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)

            writer.writerow([
                'original_row_idx', 'atomic_idx', 'phone_number', 'message_text',
                'campaign_source', 'round', 'age_group', 'voters_gender',
                'voting_performance_category', 'location', 'ward',
                'primary_category', 'secondary_category', 'stance', 'specific_concern'
            ])

            for msg in classified_messages:
                writer.writerow([
                    msg.message.original_row_idx,
                    msg.message.atomic_idx,
                    msg.message.phone_number,
                    msg.message.message_text,
                    msg.message.campaign_source,
                    msg.message.round,
                    msg.message.age_group,
                    msg.message.voters_gender or '',
                    msg.message.voting_performance_category,
                    msg.message.location,
                    msg.message.ward,
                    msg.classification.primary_category,
                    msg.classification.secondary_category,
                    msg.classification.stance,
                    msg.classification.specific_concern
                ])

        logger.info(f"Exported atomized CSV: {output_path}")

    def _export_enriched_csv(self, classified_messages: List[ClassifiedMessage], refined_summaries: List[RefinedCategorySummary], output_path: Path):
        import csv

        refined_map = {
            (s.primary_category, s.secondary_category): s
            for s in refined_summaries
        }

        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)

            writer.writerow([
                'poll_id', 'message', 'atomic_message', 'record_id', 'phone_number',
                'theme', 'summary', 'analysis', 'quotes',
                'category', 'sentiment', 'cluster_analysis',
                'age', 'business_owner', 'education_level', 'families_with_children',
                'homeowner', 'income', 'location'
            ])

            for msg in classified_messages:
                category_key = (msg.classification.primary_category, msg.classification.secondary_category)
                refined_summary = refined_map.get(category_key)

                theme = refined_summary.refined_theme if refined_summary else ''
                summary = refined_summary.refined_summary if refined_summary else ''
                analysis = refined_summary.refined_analysis if refined_summary else msg.classification.specific_concern

                quotes_json = json.dumps(refined_summary.refined_quotes) if refined_summary else '[]'

                cluster_analysis_json = '[]'
                if refined_summary and refined_summary.cluster_analyses:
                    cluster_analysis_data = [
                        {
                            "cluster_id": ca.cluster_id,
                            "theme": ca.theme,
                            "summary": ca.summary,
                            "analysis": ca.analysis,
                            "quotes": ca.quotes,
                            "sentiment": ca.sentiment,
                            "message_count": ca.message_count
                        }
                        for ca in refined_summary.cluster_analyses
                    ]
                    cluster_analysis_json = json.dumps(cluster_analysis_data)

                writer.writerow([
                    msg.message.poll_id,
                    msg.message.original_message_text,
                    msg.message.message_text,
                    msg.message.record_id,
                    msg.message.phone_number,
                    theme,
                    summary,
                    analysis,
                    quotes_json,
                    f"{msg.classification.primary_category}/{msg.classification.secondary_category}",
                    msg.classification.stance,
                    cluster_analysis_json,
                    msg.message.voters_age if msg.message.voters_age else '',
                    msg.message.business_owner,
                    msg.message.education_level,
                    msg.message.has_children_under_18,
                    msg.message.homeowner_status,
                    msg.message.income_level,
                    msg.message.location
                ])

        logger.info(f"Exported enriched CSV: {output_path}")

    def _export_refined_summaries_json(self, refined_summaries: List[RefinedCategorySummary], output_path: Path):
        summaries_data = [s.model_dump() for s in refined_summaries]

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(summaries_data, f, indent=2)

        logger.info(f"Exported refined summaries JSON: {output_path}")

    def _export_report(self, campaign: str, classified_messages: List[ClassifiedMessage], refined_summaries: List[RefinedCategorySummary], output_path: Path):
        total_messages = len(classified_messages)
        unique_respondents = len(set(msg.message.phone_number for msg in classified_messages))

        report_lines = [
            f"# {campaign.title()} Campaign Analysis Report",
            "",
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## Executive Summary",
            "",
            f"- **Total Messages Analyzed**: {total_messages:,}",
            f"- **Unique Respondents**: {unique_respondents:,}",
            f"- **Categories Identified**: {len(refined_summaries)}",
            "",
            "## Category Summaries (Bottom-Up Analysis)",
            ""
        ]

        for summary in refined_summaries:
            report_lines.extend([
                f"### {summary.primary_category} / {summary.secondary_category}",
                "",
                f"**Messages**: {summary.message_count} | **Respondents**: {summary.unique_respondents} | **Clusters**: {len(summary.cluster_analyses)}",
                "",
                f"**Theme**: {summary.refined_theme}",
                "",
                f"**Summary**: {summary.refined_summary}",
                "",
                f"**Analysis**: {summary.refined_analysis}",
                ""
            ])

            if summary.cluster_analyses:
                report_lines.append("**Cluster Breakdown**:")
                for cluster in summary.cluster_analyses:
                    report_lines.append(f"- *{cluster.theme}* ({cluster.message_count} msgs): {cluster.summary}")
                report_lines.append("")

            if summary.refined_quotes:
                report_lines.append("**Key Quotes**:")
                for quote_obj in summary.refined_quotes[:5]:
                    quote_text = quote_obj.get("quote", "")
                    report_lines.append(f"- \"{quote_text}\"")
                report_lines.append("")

            if summary.sentiment_distribution:
                sentiment_str = ", ".join([f"{k}: {v}" for k, v in summary.sentiment_distribution.items()])
                report_lines.append(f"**Sentiment**: {sentiment_str}")
                report_lines.append("")

            report_lines.append("---")
            report_lines.append("")

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))

        logger.info(f"Exported analysis report: {output_path}")

    def _export_stats(self, campaign: str, output_path: Path):
        stats = {
            "campaign": campaign,
            "stage_timings": self.stage_timings,
            "stage_outputs": {
                k: v.model_dump() if hasattr(v, 'model_dump') else v
                for k, v in self.stage_outputs.items()
            }
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2)

        logger.info(f"Exported pipeline stats: {output_path}")

    def print_summary(self):
        logger.info("\n" + "=" * 80)
        logger.info("PIPELINE SUMMARY")
        logger.info("=" * 80)

        logger.info("\nStage Timings:")
        for stage, duration in self.stage_timings.items():
            logger.info(f"  {stage:25s}: {duration:6.2f}s")

        logger.info("\nMessage Flow:")
        logger.info(f"  Loaded:      {self.stage_outputs.get('loaded_messages', 0):6d} messages")
        logger.info(f"  Cleaned:     {self.stage_outputs.get('cleaned_messages', 0):6d} messages")
        logger.info(f"  Filtered:    {self.stage_outputs.get('filtered_messages', 0):6d} messages")
        logger.info(f"  Atomized:    {self.stage_outputs.get('atomized_messages', 0):6d} messages")
        logger.info(f"  Classified:  {self.stage_outputs.get('classified_messages', 0):6d} messages")
        logger.info(f"  Categories:  {self.stage_outputs.get('category_summaries', 0):6d} summaries")

        logger.info("=" * 80)
