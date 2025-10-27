#!/usr/bin/env python3

from datetime import datetime
from shared.logger import get_logger

logger = get_logger(__name__)

async def generate_multi_cluster_report(consolidated_result, timestamp, output_paths):
    dataset_name = consolidated_result['dataset_name']
    report_filename = output_paths['reports'] / f"multi_cluster_report_{dataset_name}_{timestamp}.md"

    report_content = f"""# Multi-Cluster Hierarchical Analysis Report

**Dataset:** {dataset_name}
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Total Messages:** {consolidated_result['total_messages']:,}
**Cluster Ranges Tested:** {', '.join(consolidated_result['cluster_ranges'])}

## Clustering Results Summary

| Cluster Count | Themes Found | Avg People/Cluster | Top Themes |
|---------------|--------------|---------------------|------------|
"""

    for cluster_count in consolidated_result['cluster_ranges']:
        result = consolidated_result['cluster_results'][cluster_count]
        analyzed_clusters_count = len(result['analyzed_clusters'])

        analyzed_clusters = result['analyzed_clusters']
        total_unique_people = sum(
            getattr(cluster, 'unique_respondents', 0) if hasattr(cluster, 'unique_respondents')
            else cluster.get('unique_respondents', 0)
            for cluster in analyzed_clusters
        )
        avg_people_per_cluster = total_unique_people / analyzed_clusters_count if analyzed_clusters_count > 0 else 0

        if analyzed_clusters:
            top_themes = sorted(
                analyzed_clusters,
                key=lambda x: getattr(x, 'unique_respondents', 0) if hasattr(x, 'unique_respondents') else (x.get('unique_respondents', 0) if isinstance(x, dict) else 0),
                reverse=True
            )[:3]
        else:
            top_themes = []

        top_theme_names = []
        for theme in top_themes:
            if hasattr(theme, 'theme_analysis'):
                name = theme.theme_analysis.theme
                people_count = getattr(theme, 'unique_respondents', 0)
            elif isinstance(theme, dict):
                name = theme.get('theme', f"Cluster {theme.get('cluster_id', '?')}")
                people_count = theme.get('unique_respondents', 0)
            else:
                name = "Unknown"
                people_count = 0
            top_theme_names.append(f"{name} ({people_count} people)")

        report_content += f"| {cluster_count} | {analyzed_clusters_count} | {avg_people_per_cluster:.1f} | {', '.join(top_theme_names)} |\n"

    report_content += f"""
## Cost Summary

**Total Cost:** ${consolidated_result['pipeline_state'].total_cost:.4f}
**API Calls:** {consolidated_result['pipeline_state'].api_calls:,}

Generated with Multi-Cluster Hierarchical Discovery Pipeline
"""

    with open(report_filename, 'w') as f:
        f.write(report_content)

    logger.info(f"Multi-cluster report generated: {report_filename}")

async def generate_summary_report(pipeline_result, config, output_paths):
    try:
        report_file = output_paths['reports'] / f"hierarchical_discovery_report_{config.data_source}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

        total_duration = sum(pipeline_result.pipeline_state.stage_durations.values())

        report_content = f"""# Hierarchical Clustering Civic Message Discovery Report

**Pipeline ID:** {pipeline_result.pipeline_state.pipeline_id}
**Data Source:** {config.data_source}
**Clustering Algorithm:** Hierarchical (Agglomerative)
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Total Duration:** {total_duration:.1f} seconds

## Pipeline Overview

This report summarizes the results of analyzing civic engagement messages through our hierarchical clustering discovery pipeline, which processes raw messages through filtering, AI processing, embedding generation, hierarchical clustering optimization, and thematic analysis.

## Processing Statistics

| Stage | Messages | Duration (s) | Notes |
|-------|----------|--------------|-------|
| Raw Messages | {len(pipeline_result.raw_messages):,} | {pipeline_result.pipeline_state.stage_durations.get('data_loading', 0):.1f} | Initial dataset loaded |
| After Filtering | {len([m for m in pipeline_result.filtered_messages if m.filter_result.passed]):,} | {pipeline_result.pipeline_state.stage_durations.get('filtering', 0):.1f} | Non-substantive content removed |
| After Preprocessing | {len(pipeline_result.processed_messages):,} | {pipeline_result.pipeline_state.stage_durations.get('preprocessing', 0):.1f} | Text normalized and standardized |
| AI Processed Messages | {len(pipeline_result.atomic_messages):,} | {pipeline_result.pipeline_state.stage_durations.get('ai_processing', 0):.1f} | Messages processed and anonymized by AI |
| Embedded Messages | {len(pipeline_result.embedded_messages):,} | {pipeline_result.pipeline_state.stage_durations.get('embedding', 0):.1f} | Vector embeddings generated |
| Clustered Messages | {len(pipeline_result.clustered_messages):,} | {pipeline_result.pipeline_state.stage_durations.get('hierarchical_clustering', 0):.1f} | Messages grouped by hierarchical similarity |
| Analyzed Clusters | {len(pipeline_result.cluster_analyses)} | {pipeline_result.pipeline_state.stage_durations.get('analysis', 0):.1f} | Cluster themes identified |

## Hierarchical Clustering Results

- **Total Clusters:** {pipeline_result.pipeline_state.total_clusters}
- **Noise Points:** {pipeline_result.pipeline_state.noise_points:,} ({pipeline_result.pipeline_state.noise_points/len(pipeline_result.clustered_messages)*100:.1f}%)
- **Successfully Clustered:** {len(pipeline_result.clustered_messages) - pipeline_result.pipeline_state.noise_points:,} messages

## Top Civic Themes Discovered

"""

        for i, analysis in enumerate(pipeline_result.cluster_analyses[:15], 1):
            report_content += f"""### {i}. {analysis.theme_analysis.theme}

**Cluster ID:** {analysis.cluster_id} | **Size:** {analysis.size} messages | **Sentiment:** {analysis.theme_analysis.sentiment} | **Confidence:** {analysis.theme_analysis.confidence_score:.2f}

{analysis.theme_analysis.summary}

**Key Topics:** {', '.join(analysis.theme_analysis.key_topics)}

**Civic Relevance:** {analysis.theme_analysis.civic_relevance}

**Example Messages:**
"""
            for j, example in enumerate(analysis.example_messages[:3], 1):
                report_content += f"  {j}. \"{example}\"\n"

            report_content += "\n"

        hierarchical_config = getattr(config, 'hierarchical', {})
        embeddings_config = getattr(config, 'embeddings', {})
        ai_processing_config = getattr(config, 'ai_processing', {})

        report_content += f"""## Technical Details

### Configuration Used
- **Hierarchical Clustering Parameters:** Optimized using Optuna {'(enabled)' if hierarchical_config.get('optimization', {}).get('enabled', True) else '(disabled)'}
- **Linkage Method:** {hierarchical_config.get('linkage', 'ward')}
- **Distance Metric:** {hierarchical_config.get('affinity', 'euclidean')}
- **Embedding Model:** {embeddings_config.get('model', 'gemini')}
- **AI Processing:** {'Enabled' if ai_processing_config.get('enabled', True) else 'Disabled'}

### Performance
- **Total Processing Time:** {total_duration:.1f} seconds
- **Messages per Second:** {len(pipeline_result.raw_messages)/total_duration:.1f}
- **Slowest Stage:** {max(pipeline_result.pipeline_state.stage_durations.items(), key=lambda x: x[1])[0]} ({max(pipeline_result.pipeline_state.stage_durations.values()):.1f}s)

### Cost Analysis
- **Total Gemini API Cost:** ${pipeline_result.pipeline_state.total_cost:.4f}
- **Total API Calls:** {pipeline_result.pipeline_state.api_calls:,}
- **Cost per Message:** ${pipeline_result.pipeline_state.total_cost/len(pipeline_result.raw_messages):.6f}
- **Stage Breakdown:**"""

        for stage, cost in pipeline_result.pipeline_state.stage_costs.items():
            if cost > 0:
                report_content += f"\n  - {stage.title()}: ${cost:.4f}"

        report_content += f"""

### Data Quality
- **Filtering Pass Rate:** {len([m for m in pipeline_result.filtered_messages if m.filter_result.passed])/len(pipeline_result.raw_messages)*100:.1f}%
- **Average Cluster Confidence:** {sum(a.theme_analysis.confidence_score for a in pipeline_result.cluster_analyses)/len(pipeline_result.cluster_analyses):.3f}
- **Noise Ratio:** {pipeline_result.pipeline_state.noise_points/len(pipeline_result.clustered_messages)*100:.1f}%

### Hierarchical Clustering Benefits
- **Deterministic Results:** Unlike HDBSCAN, hierarchical clustering produces consistent results across runs
- **Interpretable Structure:** Dendrograms provide visual insight into cluster relationships and merge decisions
- **Flexible Cluster Count:** Can be configured to find optimal number of clusters using silhouette analysis
- **Full Connectivity:** Every point is assigned to a cluster (no inherent noise concept)

---

*Generated by Hierarchical Clustering Civic Message Discovery Pipeline v1.0*
"""

        with open(report_file, 'w') as f:
            f.write(report_content)

        logger.info(f"Summary report generated: {report_file}")

    except Exception as e:
        logger.error(f"Failed to generate summary report: {e}")
