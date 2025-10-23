#!/usr/bin/env python3

from shared.logger import get_logger

logger = get_logger(__name__)

def create_single_message_output(analyzed_message, pipeline_state, data_source):
    metadata = getattr(analyzed_message, 'metadata', {})
    phone_number = metadata.get("Contact Phone Number", "")
    sent_at = metadata.get("Sent At", "")
    theme = analyzed_message.single_message_theme

    # Handle both AtomicMessage and EmbeddedMessage
    message_text = getattr(analyzed_message, 'atomic_text', None) or getattr(analyzed_message, 'text', '')
    original_text = getattr(analyzed_message, 'original_text', message_text)

    msg_data = {
        'message_id': analyzed_message.id,
        'csv_row_index': analyzed_message.csv_row_index,
        'phone_number': phone_number,
        'sent_at': sent_at,
        'message': original_text,
        'atomic_message': message_text,
        'original_text': original_text,
        'processed_text': message_text,
        'campaign_source': analyzed_message.campaign_source,
        'cluster_assignments': {'0': 0},
        'cluster_themes': {'0': theme.theme},
        'cluster_categories': {'0': theme.category},
        'cluster_issues_summaries': {'0': theme.issues_summary},
        'cluster_detailed_analyses': {'0': theme.detailed_analysis},
        'cluster_verbatim_quotes': {'0': theme.verbatim_quotes},
        'cluster_quotes': {'0': [
            {
                'quote': message_text,
                'phone_number': phone_number,
                'original_message': original_text,
                'atomic_message': message_text
            }
        ]},
        'cluster_action_items': {'0': theme.action_items},
        'cluster_key_topics': {'0': theme.key_topics},
        'cluster_sentiments': {'0': theme.sentiment},
        'cluster_civic_relevance': {'0': theme.civic_relevance},
        'cluster_confidence_scores': {'0': theme.confidence_score},
        'cluster_metadata': {},
        'cluster_unique_respondents': {'0': 1},
        'cluster_total_mentions': {'0': 1},
        'cluster_avg_mentions_per_respondent': {'0': 1.0},
        'cluster_respondent_coverage_pct': {'0': 100.0}
    }

    logger.info(f"Created single-message output - Theme: {theme.theme}, Category: {theme.category}")

    return {
        'messages': [msg_data],
        'cluster_results': {
            '0': {
                'clustered_messages': [analyzed_message],
                'analyzed_clusters': [],
                'n_clusters': 1,
                'cluster_count': 1,
                'merger_stats': {'pre_merge_count': 1, 'post_merge_count': 1}
            }
        },
        'pipeline_state': pipeline_state,
        'dataset_name': data_source,
        'cluster_ranges': ['0'],
        'total_messages': 1
    }

def create_multi_cluster_output(multi_results, embedded_messages, pipeline_state, data_source):
    consolidated_messages = []

    for embedded_msg in embedded_messages:
        metadata = getattr(embedded_msg, 'metadata', {})
        phone_number = metadata.get("Contact Phone Number", "")
        sent_at = metadata.get("Sent At", "")

        if len(consolidated_messages) < 5:
            logger.info(f"ORCHESTRATOR CSV_EXPORT MESSAGE {len(consolidated_messages)} (csv_row={embedded_msg.csv_row_index}):")
            logger.info(f"  phone='{phone_number}', sent_at='{sent_at}'")
            logger.info(f"  text_snippet='{embedded_msg.text[:50]}...'")
            logger.info(f"  embedded_msg has metadata attr: {hasattr(embedded_msg, 'metadata')}")
            logger.info(f"  metadata_keys={list(metadata.keys())}")
            logger.info(f"  full_metadata={metadata}")

        msg_data = {
            'message_id': embedded_msg.id,
            'csv_row_index': embedded_msg.csv_row_index,
            'phone_number': phone_number,
            'sent_at': sent_at,
            'message': embedded_msg.original_text,
            'atomic_message': embedded_msg.text,
            'original_text': embedded_msg.original_text,
            'processed_text': embedded_msg.text,
            'campaign_source': embedded_msg.campaign_source,
            'cluster_assignments': {},
            'cluster_themes': {},
            'cluster_categories': {},
            'cluster_issues_summaries': {},
            'cluster_detailed_analyses': {},
            'cluster_verbatim_quotes': {},
            'cluster_quotes': {},
            'cluster_action_items': {},
            'cluster_key_topics': {},
            'cluster_sentiments': {},
            'cluster_civic_relevance': {},
            'cluster_confidence_scores': {},
            'cluster_metadata': {},
            'cluster_unique_respondents': {},
            'cluster_total_mentions': {},
            'cluster_avg_mentions_per_respondent': {},
            'cluster_respondent_coverage_pct': {}
        }

        for n_clusters_str, result in multi_results.items():
            n_clusters = int(n_clusters_str)

            clustered_msg = next(
                (cm for cm in result['clustered_messages'] if cm.id == embedded_msg.id),
                None
            )

            if clustered_msg:
                cluster_id = clustered_msg.cluster_assignment.cluster_id

                theme = f"Cluster {cluster_id}"

                if len(result['analyzed_clusters']) > 0:
                    if n_clusters == 15:
                        logger.info(f"Looking for cluster_id {cluster_id} in {len(result['analyzed_clusters'])} analyzed clusters")

                    for cluster in result['analyzed_clusters']:
                        if hasattr(cluster, 'cluster_id'):
                            if cluster.cluster_id == cluster_id:
                                if hasattr(cluster, 'theme_analysis'):
                                    ta = cluster.theme_analysis
                                    theme = getattr(ta, 'theme', f"Cluster {cluster_id}")

                                    msg_data['cluster_categories'][n_clusters_str] = getattr(ta, 'category', '')
                                    msg_data['cluster_issues_summaries'][n_clusters_str] = getattr(ta, 'issues_summary', '')
                                    msg_data['cluster_detailed_analyses'][n_clusters_str] = getattr(ta, 'detailed_analysis', '')
                                    msg_data['cluster_verbatim_quotes'][n_clusters_str] = getattr(ta, 'verbatim_quotes', [])
                                    msg_data['cluster_quotes'][n_clusters_str] = getattr(ta, 'quotes', [])
                                    msg_data['cluster_action_items'][n_clusters_str] = getattr(ta, 'action_items', [])
                                    msg_data['cluster_key_topics'][n_clusters_str] = getattr(ta, 'key_topics', [])
                                    msg_data['cluster_sentiments'][n_clusters_str] = getattr(ta, 'sentiment', '')
                                    msg_data['cluster_civic_relevance'][n_clusters_str] = getattr(ta, 'civic_relevance', '')
                                    msg_data['cluster_confidence_scores'][n_clusters_str] = getattr(ta, 'confidence_score', 0.0)

                                    msg_data['cluster_unique_respondents'][n_clusters_str] = getattr(cluster, 'unique_respondents', 0)
                                    msg_data['cluster_total_mentions'][n_clusters_str] = getattr(cluster, 'total_mentions', 0)
                                    msg_data['cluster_avg_mentions_per_respondent'][n_clusters_str] = getattr(cluster, 'avg_mentions_per_respondent', 0.0)
                                    msg_data['cluster_respondent_coverage_pct'][n_clusters_str] = getattr(cluster, 'respondent_coverage_pct', 0.0)
                                else:
                                    theme = f"Cluster {cluster_id}"
                                    msg_data['cluster_categories'][n_clusters_str] = ''
                                    msg_data['cluster_issues_summaries'][n_clusters_str] = ''
                                    msg_data['cluster_detailed_analyses'][n_clusters_str] = ''
                                    msg_data['cluster_verbatim_quotes'][n_clusters_str] = []
                                    msg_data['cluster_action_items'][n_clusters_str] = []
                                    msg_data['cluster_key_topics'][n_clusters_str] = []
                                    msg_data['cluster_sentiments'][n_clusters_str] = ''
                                    msg_data['cluster_civic_relevance'][n_clusters_str] = ''
                                    msg_data['cluster_confidence_scores'][n_clusters_str] = 0.0

                                    msg_data['cluster_unique_respondents'][n_clusters_str] = 0
                                    msg_data['cluster_total_mentions'][n_clusters_str] = 0
                                    msg_data['cluster_avg_mentions_per_respondent'][n_clusters_str] = 0.0
                                    msg_data['cluster_respondent_coverage_pct'][n_clusters_str] = 0.0
                                break
                        elif isinstance(cluster, dict) and cluster.get('cluster_id') == cluster_id:
                            theme = cluster.get('theme', f"Cluster {cluster_id}")
                            msg_data['cluster_categories'][n_clusters_str] = cluster.get('category', '')
                            msg_data['cluster_issues_summaries'][n_clusters_str] = cluster.get('issues_summary', '')
                            msg_data['cluster_detailed_analyses'][n_clusters_str] = cluster.get('detailed_analysis', '')
                            msg_data['cluster_verbatim_quotes'][n_clusters_str] = cluster.get('verbatim_quotes', [])
                            msg_data['cluster_quotes'][n_clusters_str] = cluster.get('quotes', [])
                            msg_data['cluster_action_items'][n_clusters_str] = cluster.get('action_items', [])
                            msg_data['cluster_key_topics'][n_clusters_str] = cluster.get('key_topics', [])
                            msg_data['cluster_sentiments'][n_clusters_str] = cluster.get('sentiment', '')
                            msg_data['cluster_civic_relevance'][n_clusters_str] = cluster.get('civic_relevance', '')
                            msg_data['cluster_confidence_scores'][n_clusters_str] = cluster.get('confidence_score', 0.0)

                            msg_data['cluster_unique_respondents'][n_clusters_str] = cluster.get('unique_respondents', 0)
                            msg_data['cluster_total_mentions'][n_clusters_str] = cluster.get('total_mentions', 0)
                            msg_data['cluster_avg_mentions_per_respondent'][n_clusters_str] = cluster.get('avg_mentions_per_respondent', 0.0)
                            msg_data['cluster_respondent_coverage_pct'][n_clusters_str] = cluster.get('respondent_coverage_pct', 0.0)
                            break

                    if n_clusters == 15 and cluster_id == 0:
                        logger.info(f"Mapped cluster_id {cluster_id} to theme: {theme}")
                else:
                    logger.warning(f"No cluster analysis available for {n_clusters} clusters - using fallback theme 'Cluster {cluster_id}'")

                msg_data['cluster_assignments'][n_clusters_str] = cluster_id
                msg_data['cluster_themes'][n_clusters_str] = theme

        consolidated_messages.append(msg_data)

    return {
        'messages': consolidated_messages,
        'cluster_results': multi_results,
        'pipeline_state': pipeline_state,
        'dataset_name': data_source,
        'cluster_ranges': list(multi_results.keys()),
        'total_messages': len(consolidated_messages)
    }
