#!/usr/bin/env python3

import json
import csv
from pathlib import Path
from collections import defaultdict

output_dir = Path("serve/analyze_texts/output")

campaigns = [
    "berkley",
    "cara-burnsville",
    "heather-ghaps",
    "japjeet-livingston",
    "joanna-missouri-city",
    "jonathan-north-las-vegas",
    "josh-minooka"
]

print("=" * 80)
print("ANALYZE_TEXTS PIPELINE RESULTS SUMMARY")
print("=" * 80)
print()

total_messages = 0
total_categories = 0
total_clusters = 0

for campaign in campaigns:
    campaign_dir = output_dir / campaign

    if not campaign_dir.exists():
        print(f"⚠️  {campaign}: Not yet processed")
        continue

    enriched_csv = campaign_dir / f"{campaign}_enriched.csv"
    refined_json = campaign_dir / f"{campaign}_refined_summaries.json"
    stats_json = campaign_dir / f"{campaign}_pipeline_stats.json"

    if not enriched_csv.exists() or not refined_json.exists():
        print(f"⚠️  {campaign}: Incomplete results")
        continue

    with open(enriched_csv, 'r') as f:
        reader = csv.DictReader(f)
        message_count = sum(1 for _ in reader)

    with open(refined_json, 'r') as f:
        refined_data = json.load(f)
        category_count = len(refined_data)
        cluster_count = sum(len(cat['cluster_analyses']) for cat in refined_data)

    stage_6_time = 0
    if stats_json.exists():
        with open(stats_json, 'r') as f:
            stats = json.load(f)
            stage_6_time = stats.get('stage_timings', {}).get('stage_6_hierarchical_reanalysis', 0)

    total_messages += message_count
    total_categories += category_count
    total_clusters += cluster_count

    print(f"✅ {campaign:30s} | {message_count:4d} msgs | {category_count:2d} cats | {cluster_count:3d} clusters | Stage 6: {stage_6_time:.1f}s")

print()
print("=" * 80)
print(f"TOTALS: {total_messages:,} messages | {total_categories} categories | {total_clusters} clusters")
print("=" * 80)
