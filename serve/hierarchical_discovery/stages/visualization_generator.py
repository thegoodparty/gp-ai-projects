#!/usr/bin/env python3

import json
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from shared.logger import get_logger
from ..models import ClusteredMessage, ClusterAnalysis, PipelineConfig

logger = get_logger(__name__)

class VisualizationGenerator:
    """Generate interactive HTML visualizations for cluster analysis results"""

    def __init__(self, config: PipelineConfig, viz_dir: Path):
        self.config = config
        self.output_config = config.output
        self.viz_dir = Path(viz_dir)

        logger.info(f"VisualizationGenerator initialized - output: {self.viz_dir}")

    def _load_from_accountability_csv(self, csv_file: Path, result_data: Dict[str, Any]) -> List:
        """Load clustered messages from accountability CSV with original and processed text"""
        import pandas as pd
        from serve.hierarchical_discovery.models import ClusteredMessage, ClusterAssignment, EmbeddingData

        df = pd.read_csv(csv_file)
        logger.info(f"CSV total rows: {len(df)}")

        # Filter to only clustered messages (not noise)
        # Handle various noise representations: False, "False", 0, 0.0
        clustered_df = df[
            (df['cluster_id'] >= 0) &
            (df['is_noise'].astype(str).str.lower() == 'false')
        ]
        logger.info(f"CSV clustered messages (not noise): {len(clustered_df)}")

        json_messages = result_data.get('clustered_messages', [])
        logger.info(f"JSON clustered messages: {len(json_messages)}")

        clustered_messages = []
        coord_match_count = 0
        no_coord_count = 0

        for i, row in clustered_df.iterrows():
            # Create embedding data with coordinates from JSON if available
            json_msg = next((m for m in json_messages
                           if m.get('text') == row['ai_summary']), None)

            if json_msg and 'coordinates' in json_msg:
                coords = json_msg['coordinates']
                coord_match_count += 1
                embedding_data = EmbeddingData(
                    embedding_3072d=np.zeros(3072),
                    
                    embedding_3d=np.array([coords.get('x', 0.0), coords.get('y', 0.0), coords.get('z', 0.0)])
                )
            else:
                no_coord_count += 1
                if i < 3:
                    logger.warning(f"No coordinate match for CSV row {i}: '{row['ai_summary'][:50]}...'")
                    if json_messages:
                        sample_json_text = json_messages[0].get('text', 'NO TEXT')[:50]
                        logger.warning(f"Sample JSON text: '{sample_json_text}...'")

                embedding_data = EmbeddingData(
                    embedding_3072d=np.zeros(3072),
                    
                    embedding_3d=np.array([0.0, 0.0, 0.0])
                )

            cluster_assignment = ClusterAssignment(
                cluster_id=int(row['cluster_id']),
                cluster_confidence=float(row['cluster_confidence']),
                is_noise=row['is_noise'],
                distance_to_centroid=float(row.get('distance_to_centroid', 0.0))
            )

            clustered_message = ClusteredMessage(
                id=str(row['csv_row_index']),  # Use row index as ID
                embedded_message_id=str(row['csv_row_index']),
                csv_file=row['csv_file'],
                csv_row_index=int(row['csv_row_index']),
                text=row['ai_summary'],  # Processed text
                original_text=row['original_text'],  # Original text
                campaign_source=row['campaign_source'],
                cluster_assignment=cluster_assignment,
                embeddings=embedding_data,
                created_at=datetime.now()
            )
            clustered_messages.append(clustered_message)

        logger.info(f"Coordinate matches: {coord_match_count}, No coordinates: {no_coord_count}")
        logger.info(f"Successfully loaded {len(clustered_messages)} messages from accountability CSV")
        return clustered_messages

    def load_pipeline_result(self, result_file: Path) -> Dict[str, Any]:
        """Load pipeline result from JSON file"""
        try:
            with open(result_file, 'r') as f:
                result_data = json.load(f)
            logger.info(f"Loaded pipeline result: {result_data.get('data_source', 'unknown')} - {result_data.get('cluster_statistics', {}).get('total_clusters', 0)} clusters")
            return result_data
        except Exception as e:
            logger.error(f"Failed to load pipeline result: {e}")
            return {}

    def create_cluster_3d_plot(self, clustered_messages: List[ClusteredMessage], cluster_analyses: List[ClusterAnalysis]) -> go.Figure:
        """Create 3D cluster visualization using UMAP coordinates"""
        if not clustered_messages:
            logger.warning("No clustered messages for visualization")
            return go.Figure()

        # Extract data for plotting
        x_coords = []
        y_coords = []
        z_coords = []
        cluster_ids = []
        cluster_labels = []
        message_texts = []
        confidences = []

        # Create cluster theme mapping
        cluster_themes = {}
        for analysis in cluster_analyses:
            cluster_themes[analysis.cluster_id] = analysis.theme_analysis.theme

        for msg in clustered_messages:
            if msg.embeddings and msg.embeddings.embedding_3d is not None:
                coords = msg.embeddings.embedding_3d
                if len(coords) >= 3:
                    x_coords.append(float(coords[0]))
                    y_coords.append(float(coords[1]))
                    z_coords.append(float(coords[2]))

                    cluster_id = msg.cluster_assignment.cluster_id
                    cluster_ids.append(cluster_id)

                    # Get cluster theme or mark as noise
                    if msg.cluster_assignment.is_noise:
                        cluster_labels.append("Noise")
                    else:
                        # Handle hierarchical sub-clusters (ID format: parent_id * 1000 + sub_id)
                        if cluster_id >= 1000:
                            parent_id = cluster_id // 1000
                            sub_id = cluster_id % 1000
                            # Try to get sub-cluster theme first, fallback to parent theme
                            sub_theme = cluster_themes.get(cluster_id, f"Sub-cluster {sub_id}")
                            parent_theme = cluster_themes.get(parent_id, f"Cluster {parent_id}")
                            cluster_labels.append(f"Sub-cluster {parent_id}.{sub_id}: {sub_theme}")
                        else:
                            theme = cluster_themes.get(cluster_id, f"Cluster {cluster_id}")
                            cluster_labels.append(f"Cluster {cluster_id}: {theme}")

                    # Store both processed and original text for hover display
                    # Truncate text to reasonable lengths for hover display
                    processed_text = msg.text[:300] + "..." if len(msg.text) > 300 else msg.text
                    original_text = msg.original_text[:400] + "..." if len(msg.original_text) > 400 else msg.original_text

                    # Add line breaks for better readability in long text
                    def add_line_breaks(text, max_line_length=80):
                        words = text.split()
                        lines = []
                        current_line = []
                        current_length = 0

                        for word in words:
                            if current_length + len(word) + 1 > max_line_length and current_line:
                                lines.append(' '.join(current_line))
                                current_line = [word]
                                current_length = len(word)
                            else:
                                current_line.append(word)
                                current_length += len(word) + 1

                        if current_line:
                            lines.append(' '.join(current_line))

                        return '<br>'.join(lines)

                    processed_formatted = add_line_breaks(processed_text)
                    original_formatted = add_line_breaks(original_text)

                    message_texts.append({
                        'processed': processed_formatted,
                        'original': original_formatted
                    })
                    confidences.append(msg.cluster_assignment.cluster_confidence)

        if not x_coords:
            logger.warning("No valid coordinates found for visualization")
            return go.Figure()

        # Create scatter plot
        fig = go.Figure()

        # Get unique cluster labels and sort by size (largest first)
        unique_labels = list(set(cluster_labels))

        # Calculate cluster sizes for sorting
        cluster_sizes = {}
        for label in unique_labels:
            cluster_sizes[label] = sum(1 for cl in cluster_labels if cl == label)

        # Sort clusters by size (largest first) for legend ordering
        unique_labels_sorted = sorted(unique_labels, key=lambda x: cluster_sizes[x], reverse=True)

        colors = px.colors.qualitative.Set3[:len(unique_labels_sorted)]

        # Separate main clusters and sub-clusters for toggle functionality
        # Use the label text itself to determine type (since labels are prefixed correctly)
        main_cluster_labels = [label for label in unique_labels_sorted
                              if not label.startswith("Sub-cluster")]
        sub_cluster_labels = [label for label in unique_labels_sorted
                             if label.startswith("Sub-cluster")]

        logger.info(f"Visualization clusters: {len(main_cluster_labels)} main, {len(sub_cluster_labels)} sub")
        logger.info(f"Main cluster labels: {main_cluster_labels[:3]}")
        logger.info(f"Sub-cluster labels: {sub_cluster_labels[:3]}")

        # Plot main clusters first
        for i, label in enumerate(main_cluster_labels):
            mask = [cl == label for cl in cluster_labels]

            if not any(mask):
                continue

            x_cluster = [x for x, m in zip(x_coords, mask) if m]
            y_cluster = [y for y, m in zip(y_coords, mask) if m]
            z_cluster = [z for z, m in zip(z_coords, mask) if m]
            texts_cluster = [t for t, m in zip(message_texts, mask) if m]
            customdata_cluster = [[t['processed'], t['original']] for t in texts_cluster]
            conf_cluster = [c for c, m in zip(confidences, mask) if m]

            # Marker size based on confidence
            sizes = [max(5, min(15, c * 20)) for c in conf_cluster]

            fig.add_trace(go.Scatter3d(
                x=x_cluster,
                y=y_cluster,
                z=z_cluster,
                mode='markers',
                name=f"{label} ({cluster_sizes[label]} msgs)",
                marker=dict(
                    size=sizes,
                    color=colors[i % len(colors)],
                    opacity=0.8,
                    symbol='circle',
                    line=dict(width=1, color='rgba(0,0,0,0.3)')
                ),
                visible=True,  # Main clusters visible by default
                customdata=customdata_cluster,
                hovertemplate='<b style="color: black !important;">Processed Text:</b><br>' +
                            '<span style="word-wrap: break-word; white-space: normal; max-width: 400px; display: inline-block; color: black !important;">%{customdata[0]}</span><br><br>' +
                            '<b style="color: black !important;">Original Message:</b><br>' +
                            '<span style="word-wrap: break-word; white-space: normal; max-width: 400px; display: inline-block; color: black !important;">%{customdata[1]}</span><br><br>' +
                            '<span style="color: black !important;">Coordinates: (%{x:.2f}, %{y:.2f}, %{z:.2f})<br>' +
                            'Confidence: %{marker.size:.2f}</span><br>' +
                            '<extra><span style="color: black !important;">%{fullData.name}</span></extra>',
                showlegend=True
            ))

        # Plot sub-clusters with different styling
        for i, label in enumerate(sub_cluster_labels):
            mask = [cl == label for cl in cluster_labels]

            if not any(mask):
                continue

            x_cluster = [x for x, m in zip(x_coords, mask) if m]
            y_cluster = [y for y, m in zip(y_coords, mask) if m]
            z_cluster = [z for z, m in zip(z_coords, mask) if m]
            texts_cluster = [t for t, m in zip(message_texts, mask) if m]
            customdata_cluster = [[t['processed'], t['original']] for t in texts_cluster]
            conf_cluster = [c for c, m in zip(confidences, mask) if m]

            # Marker size based on confidence
            sizes = [max(5, min(15, c * 20)) for c in conf_cluster]

            # Use offset color index to continue color sequence
            color_idx = len(main_cluster_labels) + i

            fig.add_trace(go.Scatter3d(
                x=x_cluster,
                y=y_cluster,
                z=z_cluster,
                mode='markers',
                name=f"{label} ({cluster_sizes[label]} msgs)",
                marker=dict(
                    size=sizes,
                    color=colors[color_idx % len(colors)],
                    opacity=0.6,
                    symbol='diamond',
                    line=dict(width=2, color='rgba(0,0,0,0.6)')
                ),
                visible=False,  # Sub-clusters hidden by default
                customdata=customdata_cluster,
                hovertemplate='<b style="color: black !important;">Processed Text:</b><br>' +
                            '<span style="word-wrap: break-word; white-space: normal; max-width: 400px; display: inline-block; color: black !important;">%{customdata[0]}</span><br><br>' +
                            '<b style="color: black !important;">Original Message:</b><br>' +
                            '<span style="word-wrap: break-word; white-space: normal; max-width: 400px; display: inline-block; color: black !important;">%{customdata[1]}</span><br><br>' +
                            '<span style="color: black !important;">Coordinates: (%{x:.2f}, %{y:.2f}, %{z:.2f})<br>' +
                            'Confidence: %{marker.size:.2f}</span><br>' +
                            '<extra><span style="color: black !important;">%{fullData.name}</span></extra>',
                showlegend=True
            ))

        # Update layout
        fig.update_layout(
            title={
                'text': f'3D Hierarchical Cluster Visualization - {self.config.data_source.title()} Data<br><sub style="font-size: 12px;">Use buttons above to toggle between Main Clusters (circles) and Sub-clusters (diamonds)</sub>',
                'x': 0.5,
                'xanchor': 'center',
                'font': {'size': 20}
            },
            scene=dict(
                xaxis_title='Dimension 1 (8d UMAP)',
                yaxis_title='Dimension 2 (8d UMAP)',
                zaxis_title='Dimension 3 (8d UMAP)',
                camera=dict(
                    eye=dict(x=1.5, y=1.5, z=1.5)
                )
            ),
            legend=dict(
                yanchor="top",
                y=0.99,
                xanchor="left",
                x=0.01
            ),
            updatemenus=[
                dict(
                    type="buttons",
                    direction="left",
                    x=0.7,
                    y=1.05,
                    showactive=True,
                    buttons=list([
                        dict(
                            label="Main Clusters",
                            method="update",
                            args=[{"visible": [True] * len(main_cluster_labels) + [False] * len(sub_cluster_labels)}]
                        ),
                        dict(
                            label="Sub-clusters",
                            method="update",
                            args=[{"visible": [False] * len(main_cluster_labels) + [True] * len(sub_cluster_labels)}]
                        ),
                        dict(
                            label="Both",
                            method="update",
                            args=[{"visible": [True] * (len(main_cluster_labels) + len(sub_cluster_labels))}]
                        )
                    ]),
                ),
            ],
            width=1200,
            height=800,
            margin=dict(l=0, r=0, t=50, b=0),
            hoverlabel=dict(
                bgcolor="white",
                bordercolor="black",
                font_size=12,
                font_family="Arial",
                font_color="black",
                align="left"
            )
        )

        return fig

    def create_cluster_summary_table(self, cluster_analyses: List[ClusterAnalysis]) -> go.Figure:
        """Create summary table of cluster analyses"""
        if not cluster_analyses:
            return go.Figure()

        # Prepare table data
        headers = ['Cluster ID', 'Size', 'Theme']

        cluster_ids = []
        sizes = []
        themes = []

        for analysis in sorted(cluster_analyses, key=lambda x: x.size, reverse=True):
            # Handle hierarchical sub-clusters in table display
            if analysis.cluster_id >= 1000:
                parent_id = analysis.cluster_id // 1000
                sub_id = analysis.cluster_id % 1000
                cluster_ids.append(f"Sub-cluster {parent_id}.{sub_id}")
            else:
                cluster_ids.append(f"Cluster {analysis.cluster_id}")
            sizes.append(analysis.size)
            themes.append(analysis.theme_analysis.theme)

        # Create table
        fig = go.Figure(data=[go.Table(
            header=dict(
                values=headers,
                fill_color='lightblue',
                align='left',
                font=dict(size=12, color='black')
            ),
            cells=dict(
                values=[cluster_ids, sizes, themes],
                fill_color='white',
                align='left',
                font=dict(size=11),
                height=30
            )
        )])

        fig.update_layout(
            title={
                'text': 'Cluster Analysis Summary',
                'x': 0.5,
                'xanchor': 'center',
                'font': {'size': 16}
            },
            width=1000,
            height=min(400, len(cluster_analyses) * 40 + 100)
        )

        return fig

    def create_statistics_charts(self, result_data: Dict[str, Any]) -> go.Figure:
        """Create charts showing pipeline statistics"""
        # Extract statistics
        message_counts = result_data.get('message_counts', {})
        cluster_stats = result_data.get('cluster_statistics', {})
        stage_durations = result_data.get('stage_durations', {})

        # Create subplots
        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=('Message Flow', 'Cluster Distribution', 'Processing Time by Stage', 'Data Source'),
            specs=[[{"type": "bar"}, {"type": "pie"}],
                   [{"type": "bar"}, {"type": "pie"}]]
        )

        # Message flow chart
        stages = ['Raw', 'Filtered', 'Processed', 'Atomic', 'Embedded', 'Clustered']
        counts = [
            message_counts.get('raw_messages', 0),
            message_counts.get('filtered_messages', 0),
            message_counts.get('processed_messages', 0),
            message_counts.get('atomic_messages', 0),
            message_counts.get('embedded_messages', 0),
            message_counts.get('clustered_messages', 0)
        ]

        fig.add_trace(go.Bar(
            x=stages,
            y=counts,
            name='Message Count',
            marker_color='skyblue'
        ), row=1, col=1)

        # Cluster distribution pie chart
        total_clusters = cluster_stats.get('total_clusters', 0)
        noise_points = cluster_stats.get('noise_points', 0)
        clustered_points = message_counts.get('clustered_messages', 0) - noise_points

        fig.add_trace(go.Pie(
            labels=['Clustered Messages', 'Noise Points'],
            values=[clustered_points, noise_points],
            name='Cluster Distribution'
        ), row=1, col=2)

        # Processing time chart
        stage_names = list(stage_durations.keys())
        durations = list(stage_durations.values())

        fig.add_trace(go.Bar(
            x=stage_names,
            y=durations,
            name='Duration (seconds)',
            marker_color='lightcoral'
        ), row=2, col=1)

        # Data source info
        data_source = result_data.get('data_source', 'Unknown')
        total_duration = result_data.get('total_duration', 0)

        fig.add_trace(go.Pie(
            labels=['Processing Time', 'Total Pipeline'],
            values=[total_duration, 100],  # Normalize for display
            name='Pipeline Info'
        ), row=2, col=2)

        # Update layout
        fig.update_layout(
            title={
                'text': f'Pipeline Statistics - {data_source.title()}',
                'x': 0.5,
                'xanchor': 'center'
            },
            height=800,
            showlegend=False
        )

        return fig

    def find_dendrogram_images(self, result_data: Dict[str, Any]) -> Dict[str, str]:
        """Find dendrogram images for the current data source"""
        dendrograms = {}
        data_source = result_data.get('data_source', 'unknown')

        # Search for dendrogram files in the output directory (from config)
        base_dir = self.output_config.get('base_dir', 'output')
        subdirs = self.output_config.get('subdirs', {})
        dendrogram_subdir = subdirs.get('dendrograms', 'dendrograms')
        dendrogram_dir = Path(f"{base_dir}/{dendrogram_subdir}")
        if dendrogram_dir.exists():
            # Look for the most recent dendrograms for this data source
            for pattern in ['dendrogram_basic_*.png', 'dendrogram_colored_*.png', 'dendrogram_cluster_sizes_*.png']:
                files = list(dendrogram_dir.glob(pattern))
                if files:
                    # Get the most recent file
                    latest_file = max(files, key=lambda x: x.stat().st_mtime)

                    # Convert to base64 for embedding in HTML
                    import base64
                    try:
                        with open(latest_file, 'rb') as f:
                            img_data = base64.b64encode(f.read()).decode('utf-8')

                        if 'basic' in pattern:
                            dendrograms['basic'] = f"data:image/png;base64,{img_data}"
                        elif 'colored' in pattern:
                            dendrograms['colored'] = f"data:image/png;base64,{img_data}"
                        elif 'cluster_sizes' in pattern:
                            dendrograms['cluster_sizes'] = f"data:image/png;base64,{img_data}"

                        logger.info(f"Found dendrogram: {latest_file.name}")
                    except Exception as e:
                        logger.warning(f"Failed to load dendrogram {latest_file}: {e}")

        return dendrograms

    def _generate_dendrogram_section(self, dendrograms: Dict[str, str]) -> str:
        """Generate HTML section for dendrogram visualizations"""
        if not dendrograms:
            return """
    <div class="section">
        <h2>🌳 Hierarchical Dendrograms</h2>
        <p style="color: #888; font-style: italic;">No dendrogram images found for this dataset.</p>
    </div>"""

        # Create tabs for different dendrogram types
        dendrogram_tabs = []
        dendrogram_content = []

        if 'basic' in dendrograms:
            dendrogram_tabs.append('<button class="tab-button active" onclick="showDendrogram(\'basic\')">Basic</button>')
            dendrogram_content.append(f'''
                <div id="basic-dendrogram" class="dendrogram-content active">
                    <h3>Basic Dendrogram</h3>
                    <p>Shows the hierarchical clustering structure with distance thresholds.</p>
                    <img src="{dendrograms['basic']}" alt="Basic Dendrogram" style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 5px;"/>
                </div>''')

        if 'colored' in dendrograms:
            active_class = '' if 'basic' in dendrograms else 'active'
            dendrogram_tabs.append(f'<button class="tab-button {active_class}" onclick="showDendrogram(\'colored\')">Colored</button>')
            dendrogram_content.append(f'''
                <div id="colored-dendrogram" class="dendrogram-content {active_class}">
                    <h3>Colored Dendrogram</h3>
                    <p>Shows clusters highlighted in different colors based on the clustering threshold.</p>
                    <img src="{dendrograms['colored']}" alt="Colored Dendrogram" style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 5px;"/>
                </div>''')

        if 'cluster_sizes' in dendrograms:
            active_class = '' if dendrograms else 'active'
            dendrogram_tabs.append(f'<button class="tab-button {active_class}" onclick="showDendrogram(\'cluster_sizes\')">Cluster Sizes</button>')
            dendrogram_content.append(f'''
                <div id="cluster_sizes-dendrogram" class="dendrogram-content {active_class}">
                    <h3>Dendrogram with Cluster Size Analysis</h3>
                    <p>Shows the dendrogram alongside a distribution of cluster sizes.</p>
                    <img src="{dendrograms['cluster_sizes']}" alt="Cluster Sizes Dendrogram" style="max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 5px;"/>
                </div>''')

        tabs_html = ''.join(dendrogram_tabs)
        content_html = ''.join(dendrogram_content)

        return f"""
    <div class="section">
        <h2>🌳 Hierarchical Dendrograms</h2>
        <p>Tree-like diagrams showing how messages are hierarchically clustered. Each branching point represents a merge between clusters at a specific distance threshold.</p>

        <div class="dendrogram-tabs">
            {tabs_html}
        </div>

        <div class="dendrogram-container">
            {content_html}
        </div>

        <style>
            .dendrogram-tabs {{
                margin-bottom: 20px;
                border-bottom: 2px solid #667eea;
            }}

            .tab-button {{
                background: #f8f9fa;
                border: none;
                padding: 10px 20px;
                margin-right: 5px;
                cursor: pointer;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                transition: background-color 0.3s;
            }}

            .tab-button:hover {{
                background: #e9ecef;
            }}

            .tab-button.active {{
                background: #667eea;
                color: white;
            }}

            .dendrogram-content {{
                display: none;
                padding: 20px;
                background: #f8f9fa;
                border-radius: 0 5px 5px 5px;
            }}

            .dendrogram-content.active {{
                display: block;
            }}

            .dendrogram-content h3 {{
                margin-top: 0;
                color: #333;
            }}
        </style>

        <script>
            function showDendrogram(type) {{
                // Hide all dendrogram content
                var contents = document.querySelectorAll('.dendrogram-content');
                contents.forEach(function(content) {{
                    content.classList.remove('active');
                }});

                // Remove active class from all tabs
                var tabs = document.querySelectorAll('.tab-button');
                tabs.forEach(function(tab) {{
                    tab.classList.remove('active');
                }});

                // Show selected dendrogram content
                document.getElementById(type + '-dendrogram').classList.add('active');

                // Add active class to clicked tab
                event.target.classList.add('active');
            }}
        </script>
    </div>"""

    def generate_html_report(self, clustered_messages: List[ClusteredMessage],
                           cluster_analyses: List[ClusterAnalysis],
                           result_data: Dict[str, Any]) -> str:
        """Generate complete HTML report with all visualizations"""

        # Create individual plots
        cluster_3d_fig = self.create_cluster_3d_plot(clustered_messages, cluster_analyses)
        summary_table_fig = self.create_cluster_summary_table(cluster_analyses)
        stats_charts_fig = self.create_statistics_charts(result_data)

        # Find dendrogram images
        dendrograms = self.find_dendrogram_images(result_data)

        # Convert to HTML
        cluster_3d_html = cluster_3d_fig.to_html(include_plotlyjs='cdn', div_id="cluster-3d")
        summary_table_html = summary_table_fig.to_html(include_plotlyjs=False, div_id="summary-table")
        stats_charts_html = stats_charts_fig.to_html(include_plotlyjs=False, div_id="stats-charts")

        # Create complete HTML document
        completion_time = result_data.get('completion_time', datetime.now().isoformat())
        data_source = result_data.get('data_source', 'Unknown')

        html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Civic Message Discovery - {data_source.title()} Analysis</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .header {{
            text-align: center;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 30px;
        }}
        .section {{
            background: white;
            border-radius: 10px;
            padding: 20px;
            margin-bottom: 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .section h2 {{
            color: #333;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }}
        .stat-card {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            text-align: center;
        }}
        .stat-value {{
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
        }}
        .stat-label {{
            color: #666;
            margin-top: 5px;
        }}
        .completion-time {{
            text-align: center;
            color: #888;
            font-style: italic;
            margin-top: 20px;
        }}
        /* Force all Plotly hover tooltips to have black text */
        .hoverlayer .hovertext {{
            color: black !important;
        }}
        .hoverlayer .hovertext * {{
            color: black !important;
        }}
        /* Force hover tooltip headers/titles to be black */
        .hoverlayer .hovertext .name {{
            color: black !important;
        }}
        /* Force legend hover text to be black */
        .legend .traces .legendtext {{
            color: black !important;
        }}
        /* Additional Plotly hover element targeting */
        g.hovertext {{
            color: black !important;
        }}
        g.hovertext text {{
            fill: black !important;
        }}
        .hoverlayer g text {{
            fill: black !important;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Civic Message Discovery Pipeline</h1>
        <h2>{data_source.title()} Data Analysis Report</h2>
        <p>Interactive cluster visualization and analysis results</p>
    </div>

    <div class="section">
        <h2>📊 Pipeline Summary</h2>
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value">{result_data.get('message_counts', {}).get('raw_messages', 0)}</div>
                <div class="stat-label">Raw Messages</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{result_data.get('cluster_statistics', {}).get('total_clusters', 0)}</div>
                <div class="stat-label">Clusters Found</div>
            </div>"""

        # Add merger stats card if merging occurred
        cluster_stats = result_data.get('cluster_statistics', {})
        pre_merge = cluster_stats.get('pre_merge_count', 0)
        post_merge = cluster_stats.get('post_merge_count', 0)

        if pre_merge > 0 and post_merge > 0 and pre_merge != post_merge:
            html_content += f"""
            <div class="stat-card">
                <div class="stat-value">{post_merge}</div>
                <div class="stat-label">Final Themes<br><small style="color: #888;">(reduced from {pre_merge})</small></div>
            </div>"""

        html_content += f"""
            <div class="stat-card">
                <div class="stat-value">{result_data.get('total_duration', 0):.1f}s</div>
                <div class="stat-label">Processing Time</div>
            </div>
        </div>
    </div>

    <div class="section">
        <h2>🎯 3D Cluster Visualization</h2>
        <p>Interactive 3D plot showing message clusters in UMAP-reduced embedding space. Hover over points to see message content.</p>
        {cluster_3d_html}
    </div>

    <div class="section">
        <h2>📋 Cluster Analysis Summary</h2>
        <p>Detailed analysis of each cluster including themes identified by AI.</p>
        {summary_table_html}
    </div>

    <div class="completion-time">
        Generated on {completion_time}
    </div>
</body>
</html>
"""
        return html_content

    def save_html_visualization(self, result_file: Path) -> Path:
        """Generate and save HTML visualization from pipeline result"""
        # Load pipeline result
        result_data = self.load_pipeline_result(result_file)
        if not result_data:
            raise ValueError(f"Could not load pipeline result from {result_file}")

        # Extract cluster analyses and actual clustered messages
        cluster_analyses = []
        clustered_messages = []

        # Load cluster analyses
        analyses_data = result_data.get('cluster_analyses', [])
        for analysis_data in analyses_data:
            from serve.hierarchical_discovery.models import ClusterTheme, ClusterAnalysis

            theme = ClusterTheme(
                theme=analysis_data.get('theme', 'Unknown'),
                summary=analysis_data.get('summary', '')
            )

            analysis = ClusterAnalysis(
                cluster_id=analysis_data.get('cluster_id', -1),
                size=analysis_data.get('size', 0),
                theme_analysis=theme,
                example_messages=analysis_data.get('example_messages', []),
                message_ids=[],
                analysis_model="gemini-flash",
                analysis_timestamp=datetime.now(),
                cost_estimate=0.0
            )
            cluster_analyses.append(analysis)

        # Load accountability CSV for original vs processed text comparison
        # Extract timestamp from pipeline_result_josh_20250918_111100.json
        timestamp = '_'.join(result_file.stem.split('_')[-2:])  # Gets "20250918_111100"
        accountability_file = result_file.parent / f"accountability_{result_data.get('data_source', 'unknown')}_{timestamp}.csv"

        logger.info(f"Looking for accountability file: {accountability_file}")
        logger.info(f"File exists: {accountability_file.exists()}")

        if accountability_file.exists():
            logger.info("Attempting to load from accountability CSV...")
            try:
                clustered_messages = self._load_from_accountability_csv(accountability_file, result_data)
                logger.info(f"Successfully loaded {len(clustered_messages)} messages from accountability CSV")
            except Exception as e:
                logger.error(f"Failed to load from accountability CSV: {e}")
                logger.info("Falling back to JSON data due to CSV loading error")
                # Fallback to JSON data if CSV loading fails
                clustered_messages_data = result_data.get('clustered_messages', [])
                clustered_messages = []
                for msg_data in clustered_messages_data:
                    from serve.hierarchical_discovery.models import ClusteredMessage, ClusterAssignment, EmbeddingData

                    coordinates = msg_data.get('coordinates', {})
                    embedding_data = EmbeddingData(
                        embedding_3072d=np.zeros(3072),
                        
                        embedding_3d=np.array([
                            coordinates.get('x', 0.0),
                            coordinates.get('y', 0.0),
                            coordinates.get('z', 0.0)
                        ])
                    )

                    cluster_assignment = ClusterAssignment(
                        cluster_id=msg_data.get('cluster_id', -1),
                        cluster_confidence=msg_data.get('cluster_confidence', 0.0),
                        is_noise=str(msg_data.get('is_noise', False)).lower() == 'true',
                        distance_to_centroid=0.0
                    )

                    clustered_message = ClusteredMessage(
                        id=msg_data.get('id', ''),
                        embedded_message_id=msg_data.get('id', ''),
                        csv_file="",
                        csv_row_index=0,
                        text=msg_data.get('text', ''),
                        original_text=msg_data.get('text', ''),
                        embeddings=embedding_data,
                        cluster_assignment=cluster_assignment,
                        campaign_source=result_data.get('data_source', 'unknown'),
                        created_at=datetime.now()
                    )
                    clustered_messages.append(clustered_message)
        else:
            # Fallback to JSON data if CSV not available
            clustered_messages_data = result_data.get('clustered_messages', [])
            clustered_messages = []
            for msg_data in clustered_messages_data:
                from serve.hierarchical_discovery.models import ClusteredMessage, ClusterAssignment, EmbeddingData

                coordinates = msg_data.get('coordinates', {})
                embedding_data = EmbeddingData(
                    embedding_3072d=np.zeros(3072),
                    
                    embedding_3d=np.array([
                        coordinates.get('x', 0.0),
                        coordinates.get('y', 0.0),
                        coordinates.get('z', 0.0)
                    ]),
                    embedding_model="gemini",
                    generation_timestamp=datetime.now()
                )

                cluster_assignment = ClusterAssignment(
                    cluster_id=msg_data.get('cluster_id', -1),
                    cluster_confidence=msg_data.get('cluster_confidence', 0.0),
                    is_noise=str(msg_data.get('is_noise', False)).lower() == 'true',
                    distance_to_centroid=0.0
                )

                clustered_message = ClusteredMessage(
                    id=msg_data.get('id', ''),
                    embedded_message_id=msg_data.get('id', ''),
                    csv_file="",
                    csv_row_index=0,
                    text=msg_data.get('text', ''),
                    original_text=msg_data.get('text', ''),
                    embeddings=embedding_data,
                    cluster_assignment=cluster_assignment,
                    campaign_source=result_data.get('data_source', 'unknown'),
                    created_at=datetime.now()
                )
                clustered_messages.append(clustered_message)

        # Generate HTML report
        html_content = self.generate_html_report(clustered_messages, cluster_analyses, result_data)

        # Save HTML file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        data_source = result_data.get('data_source', 'unknown')
        html_file = self.viz_dir / f"cluster_visualization_{data_source}_{timestamp}.html"

        with open(html_file, 'w', encoding='utf-8') as f:
            f.write(html_content)

        logger.info(f"HTML visualization saved: {html_file}")
        return html_file

def visualization_generator_stage(result_file: Path, config: PipelineConfig, viz_dir: Path) -> Path:
    """Main entry point for visualization generation stage"""
    logger.info("=== VISUALIZATION GENERATION STAGE ===")

    try:
        generator = VisualizationGenerator(config, viz_dir)
        html_file = generator.save_html_visualization(result_file)
        logger.info(f"Visualization complete: {html_file}")
        return html_file

    except Exception as e:
        logger.error(f"Visualization generation failed: {e}")
        raise