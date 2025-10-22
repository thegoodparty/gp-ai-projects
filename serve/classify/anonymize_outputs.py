#!/usr/bin/env python3

import re
import glob
import os
from pathlib import Path

def anonymize_file(file_path: str, replacements: dict) -> None:
    """Anonymize a file by replacing specific terms"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    original_content = content

    # Apply replacements (case insensitive)
    for old_term, new_term in replacements.items():
        # Use word boundaries to avoid partial matches
        pattern = r'\b' + re.escape(old_term) + r'\b'
        content = re.sub(pattern, new_term, content, flags=re.IGNORECASE)

    # Only write if content changed
    if content != original_content:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✅ Anonymized: {file_path}")
    else:
        print(f"⏭️  No changes needed: {file_path}")

def main():
    # Define replacements for each campaign
    anonymization_rules = {
        'josh': {
            'minooka': 'the local area',
            'Minooka': 'the local area'
        },
        'cara': {
            'burnsville': 'the local area',
            'Burnsville': 'the local area'
        },
        'berkley': {
            'berkley': 'the local area',
            'Berkley': 'the local area'
        }
    }

    # Find output directories
    output_dirs = [
        '/Users/collinpark/work/gp-ai-projects/serve/output',
        '/Users/collinpark/work/gp-ai-projects/serve/classify/output'
    ]

    for output_dir in output_dirs:
        if not os.path.exists(output_dir):
            continue

        print(f"\n🔍 Processing directory: {output_dir}")

        for campaign, replacements in anonymization_rules.items():
            # Find files for this campaign
            pattern = os.path.join(output_dir, f"{campaign}_*")
            files = glob.glob(pattern)

            if not files:
                print(f"⚠️  No files found for {campaign} in {output_dir}")
                continue

            print(f"\n📁 Anonymizing {len(files)} files for {campaign}...")
            print(f"   Replacements: {replacements}")

            for file_path in files:
                try:
                    anonymize_file(file_path, replacements)
                except Exception as e:
                    print(f"❌ Error processing {file_path}: {e}")

if __name__ == "__main__":
    main()