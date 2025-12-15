#!/usr/bin/env python3

import json
import pandas as pd
from datetime import datetime
from shared.logger import get_logger

logger = get_logger(__name__)

async def export_multi_cluster_results(consolidated_result, output_paths):
    logger.info("= Starting multi-cluster CSV export...")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dataset_name = consolidated_result['dataset_name']

    csv_filename = output_paths['exports'] / f"multi_cluster_results_{dataset_name}_{timestamp}.csv"

    logger.info(f"= CSV file will be: {csv_filename}")
    logger.info(f"= Consolidated result contains {len(consolidated_result.get('messages', []))} messages")

    csv_data = []
    for msg in consolidated_result['messages']:
        row = {
            'message_id': msg['message_id'],
            'csv_row_index': msg['csv_row_index'],
            'phone_number': msg['phone_number'],
            'sent_at': msg['sent_at'],
            'original_text': msg['original_text'],
            'processed_text': msg['processed_text'],
            'campaign_source': msg['campaign_source']
        }

        for cluster_count in consolidated_result['cluster_ranges']:
            suffix = "merged" if len(consolidated_result['cluster_ranges']) == 1 else cluster_count

            row[f'cluster_{suffix}'] = msg['cluster_assignments'].get(cluster_count, '')
            row[f'theme_{suffix}'] = msg['cluster_themes'].get(cluster_count, '')
            row[f'issues_summary_{suffix}'] = msg['cluster_issues_summaries'].get(cluster_count, '')
            row[f'detailed_analysis_{suffix}'] = msg['cluster_detailed_analyses'].get(cluster_count, '')

            quotes = msg['cluster_verbatim_quotes'].get(cluster_count, [])
            row[f'verbatim_quotes_{suffix}'] = ' | '.join(quotes) if isinstance(quotes, list) else quotes

            quotes_with_phones = msg['cluster_quotes'].get(cluster_count, [])
            if quotes_with_phones:
                row[f'quotes_{suffix}'] = json.dumps(quotes_with_phones)
            else:
                row[f'quotes_{suffix}'] = ''

            row[f'unique_respondents_{suffix}'] = msg['cluster_unique_respondents'].get(cluster_count, 0)
            row[f'total_mentions_{suffix}'] = msg['cluster_total_mentions'].get(cluster_count, 0)
            row[f'avg_mentions_per_respondent_{suffix}'] = msg['cluster_avg_mentions_per_respondent'].get(cluster_count, 0.0)
            row[f'respondent_coverage_pct_{suffix}'] = msg['cluster_respondent_coverage_pct'].get(cluster_count, 0.0)

            merger_stats = consolidated_result['cluster_results'][cluster_count].get('merger_stats', {'pre_merge_count': 0, 'post_merge_count': 0})
            row[f'pre_merge_count_{suffix}'] = merger_stats.get('pre_merge_count', 0)
            row[f'post_merge_count_{suffix}'] = merger_stats.get('post_merge_count', 0)

        csv_data.append(row)

    logger.info(f"= Generated {len(csv_data)} rows for CSV export")

    if csv_data:
        df = pd.DataFrame(csv_data)
        logger.info(f"= DataFrame columns: {list(df.columns)}")
        df.to_csv(csv_filename, index=False)
        logger.info(f" Multi-cluster results exported to: {csv_filename}")
    else:
        logger.warning("L No CSV data to export - csv_data is empty")

    return csv_filename
