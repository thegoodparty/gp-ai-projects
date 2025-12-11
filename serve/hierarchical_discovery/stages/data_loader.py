#!/usr/bin/env python3

import pandas as pd
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
import uuid

from shared.logger import get_logger
from ..models import RawMessage, PipelineConfig

logger = get_logger(__name__)

class DataLoader:
    """Load CSV data with complete row tracking and metadata preservation"""

    def __init__(self, config: PipelineConfig):
        self.config = config
        # Try to get data_files from config, fallback to defaults
        self.data_files = getattr(config, 'data_files', {
            "josh": "serve/data/josh-minooka_consolidated.csv",
            "cara": "serve/data/cara-burnsville_consolidated.csv",
            "berkeley": "serve/data/berkley_consolidated.csv",
            "heather": "serve/data/heather-ghaps_consolidated.csv",
            "japjeet": "serve/data/japjeet-livingston_consolidated.csv",
            "joanna": "serve/data/joanna-missouri-city_consolidated.csv",
            "jonathan": "serve/data/jonathan-north-las-vegas_consolidated.csv",
            "test_1msg": "serve/hierarchical_discovery/test_data/test_1_message.csv",
            "test_2msg": "serve/hierarchical_discovery/test_data/test_2_messages.csv",
            "test_3msg": "serve/hierarchical_discovery/test_data/test_3_messages.csv"
        })

    def _find_data_file(self, filename: str) -> Path:
        """Find data file with relative path fallbacks from project root"""
        possible_paths = [
            Path(filename),
            Path("../../..") / filename,
        ]

        for path in possible_paths:
            if path.exists():
                return path

        raise FileNotFoundError(
            f"Could not find data file: {filename}. "
            f"Tried paths: {[str(p) for p in possible_paths]}"
        )

    def _identify_text_column(self, df: pd.DataFrame) -> Optional[str]:
        """Identify the column containing message text"""
        possible_columns = [
            'Message Text', 'text', 'message_text', 'Message',
            'message', 'content', 'body', 'description'
        ]

        for col in possible_columns:
            if col in df.columns:
                # Check if column has substantial non-null text content
                non_null_count = df[col].dropna().shape[0]
                if non_null_count > 0:
                    return col

        # If no exact match, look for columns with "text" or "message" in name
        for col in df.columns:
            if any(keyword in col.lower() for keyword in ['text', 'message', 'content']):
                non_null_count = df[col].dropna().shape[0]
                if non_null_count > 0:
                    return col

        return None

    def _extract_metadata(self, row: pd.Series, exclude_text_col: str) -> Dict[str, Any]:
        """Extract metadata from CSV row, excluding the text column"""
        metadata = {}

        for col, value in row.items():
            if col == exclude_text_col:
                continue

            # Convert to serializable format
            if pd.isna(value):
                metadata[col] = None
            elif isinstance(value, (int, float, str, bool)):
                metadata[col] = value
            elif isinstance(value, datetime):
                metadata[col] = value.isoformat()
            else:
                metadata[col] = str(value)

        return metadata

    def _parse_timestamp(self, row: pd.Series) -> Optional[datetime]:
        """Try to parse timestamp from various possible columns"""
        timestamp_columns = [
            'timestamp', 'created_at', 'date', 'time',
            'created', 'sent_at', 'received_at'
        ]

        for col in timestamp_columns:
            if col in row:
                try:
                    if pd.notna(row[col]):
                        timestamp_str = str(row[col])
                        timestamp_str = timestamp_str.replace('.000Z', 'Z').replace('..', '.')
                        return pd.to_datetime(timestamp_str)
                except:
                    continue

        return None

    def load_single_campaign(self, campaign: str) -> List[RawMessage]:
        """Load messages from a single campaign CSV"""
        if campaign not in self.data_files:
            raise ValueError(f"Unknown campaign: {campaign}. Available: {list(self.data_files.keys())}")

        data_file = self.data_files[campaign]
        file_path = self._find_data_file(data_file)

        logger.info(f"Loading {campaign} data from: {file_path}")

        try:
            df = pd.read_csv(file_path)
        except Exception as e:
            logger.error(f"Failed to load CSV file {file_path}: {e}")
            raise

        text_column = self._identify_text_column(df)
        if not text_column:
            logger.warning(f"No text column found in {file_path}")
            return []

        logger.info(f"Found text column '{text_column}' with {df[text_column].dropna().shape[0]} non-null messages")

        raw_messages = []

        for row_index, row in df.iterrows():
            # Skip rows with no text content
            if pd.isna(row.get(text_column, '')):
                continue

            text_content = str(row[text_column]).strip()
            if not text_content:
                continue

            # Extract metadata and debug log for first few messages
            metadata = self._extract_metadata(row, text_column)

            # Create RawMessage with complete tracking
            # Use actual CSV line number (row_index + 2 to account for 0-based index + header row)
            actual_csv_line_number = int(row_index) + 2
            raw_message = RawMessage(
                id=str(uuid.uuid4()),
                csv_file=str(file_path),
                csv_row_index=actual_csv_line_number,
                original_text=text_content,
                timestamp=self._parse_timestamp(row),
                campaign_source=campaign,
                metadata=metadata,
                created_at=datetime.now()
            )

            # Debug first few messages with comprehensive metadata logging
            if len(raw_messages) < 5:
                phone = metadata.get("Contact Phone Number", "NOT_FOUND")
                sent_at = metadata.get("Sent At", "NOT_FOUND")
                logger.info(f"RAW MESSAGE {len(raw_messages)} (csv_row={actual_csv_line_number}):")
                logger.info(f"  phone='{phone}', sent_at='{sent_at}'")
                logger.info(f"  text_snippet='{text_content[:50]}...'")
                logger.info(f"  metadata_keys={list(metadata.keys())}")
                logger.info(f"  full_metadata={metadata}")

            raw_messages.append(raw_message)

        logger.info(f"Loaded {len(raw_messages)} messages from {campaign}")
        return raw_messages

    def load_multiple_campaigns(self, campaigns: List[str]) -> List[RawMessage]:
        """Load messages from multiple campaigns"""
        all_messages = []

        for campaign in campaigns:
            try:
                messages = self.load_single_campaign(campaign)
                all_messages.extend(messages)
                logger.info(f"Added {len(messages)} messages from {campaign}")
            except Exception as e:
                logger.error(f"Failed to load {campaign}: {e}")
                if not self.config.error_handling.get("continue_on_error", False):
                    raise

        logger.info(f"Total messages loaded: {len(all_messages)}")
        return all_messages

    def load_data(self) -> List[RawMessage]:
        """Load data according to configuration"""
        data_source = self.config.data_source

        if data_source == "all":
            campaigns = list(self.data_files.keys())
            logger.info(f"Loading all campaigns: {campaigns}")
            return self.load_multiple_campaigns(campaigns)
        elif data_source in self.data_files:
            logger.info(f"Loading single campaign: {data_source}")
            return self.load_single_campaign(data_source)
        else:
            # Handle comma-separated list
            if "," in data_source:
                campaigns = [c.strip() for c in data_source.split(",")]
                logger.info(f"Loading specified campaigns: {campaigns}")
                return self.load_multiple_campaigns(campaigns)
            else:
                # Try to load as a file path or UUID filename
                return self._load_from_path(data_source)

    def _load_from_path(self, data_source: str) -> List[RawMessage]:
        """Load data from a file path or UUID filename"""
        # Check if it's a direct path
        path = Path(data_source)
        if path.exists():
            logger.info(f"Loading from direct path: {path}")
            return self._load_csv_file(path, data_source)

        # Check if it's a filename in serve/data/
        serve_data_path = Path("serve/data") / f"{data_source}.csv"
        if serve_data_path.exists():
            logger.info(f"Loading from serve/data: {serve_data_path}")
            return self._load_csv_file(serve_data_path, data_source)

        # Check without .csv extension
        serve_data_path_no_ext = Path("serve/data") / data_source
        if serve_data_path_no_ext.exists():
            logger.info(f"Loading from serve/data: {serve_data_path_no_ext}")
            return self._load_csv_file(serve_data_path_no_ext, data_source)

        # Try with .csv extension on the original path
        path_with_csv = Path(f"{data_source}.csv")
        if path_with_csv.exists():
            logger.info(f"Loading from path with .csv: {path_with_csv}")
            return self._load_csv_file(path_with_csv, data_source)

        raise ValueError(
            f"Unknown data source: {data_source}. "
            f"Not found as predefined campaign, file path, or in serve/data/. "
            f"Available predefined: {list(self.data_files.keys())}"
        )

    def _load_csv_file(self, file_path: Path, campaign_name: str) -> List[RawMessage]:
        """Load messages from a CSV file path"""
        logger.info(f"Loading data from: {file_path}")

        try:
            df = pd.read_csv(file_path)
        except Exception as e:
            logger.error(f"Failed to load CSV file {file_path}: {e}")
            raise

        text_column = self._identify_text_column(df)
        if not text_column:
            logger.warning(f"No text column found in {file_path}")
            return []

        logger.info(f"Found text column '{text_column}' with {df[text_column].dropna().shape[0]} non-null messages")

        raw_messages = []

        for row_index, row in df.iterrows():
            # Skip rows with no text content
            if pd.isna(row.get(text_column, '')):
                continue

            text_content = str(row[text_column]).strip()
            if not text_content:
                continue

            # Extract metadata and debug log for first few messages
            metadata = self._extract_metadata(row, text_column)

            # Create RawMessage with complete tracking
            # Use actual CSV line number (row_index + 2 to account for 0-based index + header row)
            actual_csv_line_number = int(row_index) + 2
            raw_message = RawMessage(
                id=str(uuid.uuid4()),
                csv_file=str(file_path),
                csv_row_index=actual_csv_line_number,
                original_text=text_content,
                timestamp=self._parse_timestamp(row),
                campaign_source=campaign_name,
                metadata=metadata,
                created_at=datetime.now()
            )

            # Debug first few messages with comprehensive metadata logging
            if len(raw_messages) < 5:
                phone = metadata.get("Contact Phone Number", "NOT_FOUND")
                sent_at = metadata.get("Sent At", "NOT_FOUND")
                logger.info(f"RAW MESSAGE {len(raw_messages)} (csv_row={actual_csv_line_number}):")
                logger.info(f"  phone='{phone}', sent_at='{sent_at}'")
                logger.info(f"  text_snippet='{text_content[:50]}...'")
                logger.info(f"  metadata_keys={list(metadata.keys())}")
                logger.info(f"  full_metadata={metadata}")

            raw_messages.append(raw_message)

        logger.info(f"Loaded {len(raw_messages)} messages from {file_path}")
        return raw_messages

    def validate_data(self, messages: List[RawMessage]) -> Dict[str, Any]:
        """Validate loaded data and return statistics"""
        if not messages:
            return {"valid": False, "error": "No messages loaded"}

        stats = {
            "valid": True,
            "total_messages": len(messages),
            "campaigns": {},
            "csv_files": set(),
            "text_length_stats": {},
            "timestamp_coverage": 0,
            "metadata_fields": set()
        }

        # Count by campaign
        for message in messages:
            campaign = message.campaign_source
            if campaign not in stats["campaigns"]:
                stats["campaigns"][campaign] = 0
            stats["campaigns"][campaign] += 1
            stats["csv_files"].add(message.csv_file)
            stats["metadata_fields"].update(message.metadata.keys())

        # Text length statistics
        text_lengths = [len(message.original_text) for message in messages]
        stats["text_length_stats"] = {
            "min": min(text_lengths),
            "max": max(text_lengths),
            "mean": sum(text_lengths) / len(text_lengths),
            "median": sorted(text_lengths)[len(text_lengths) // 2]
        }

        # Timestamp coverage
        with_timestamps = sum(1 for m in messages if m.timestamp is not None)
        stats["timestamp_coverage"] = with_timestamps / len(messages)

        # Convert sets to lists for JSON serialization
        stats["csv_files"] = list(stats["csv_files"])
        stats["metadata_fields"] = list(stats["metadata_fields"])

        logger.info(f"Data validation complete: {stats['total_messages']} messages from {len(stats['campaigns'])} campaigns")
        return stats

    def get_data_summary(self, messages: List[RawMessage]) -> str:
        """Generate human-readable data summary"""
        if not messages:
            return "No messages loaded"

        stats = self.validate_data(messages)

        summary_lines = [
            f"Data Summary:",
            f"  Total Messages: {stats['total_messages']:,}",
            f"  Campaigns: {', '.join(stats['campaigns'].keys())}",
            f"  CSV Files: {len(stats['csv_files'])}",
            f"",
            f"Message Distribution:"
        ]

        for campaign, count in stats["campaigns"].items():
            percentage = (count / stats['total_messages']) * 100
            summary_lines.append(f"  {campaign}: {count:,} ({percentage:.1f}%)")

        summary_lines.extend([
            f"",
            f"Text Length Statistics:",
            f"  Min: {stats['text_length_stats']['min']} chars",
            f"  Max: {stats['text_length_stats']['max']} chars",
            f"  Mean: {stats['text_length_stats']['mean']:.1f} chars",
            f"  Median: {stats['text_length_stats']['median']} chars",
            f"",
            f"Timestamp Coverage: {stats['timestamp_coverage']:.1%}",
            f"Metadata Fields: {len(stats['metadata_fields'])} fields available"
        ])

        return "\n".join(summary_lines)

def load_data_stage(config: PipelineConfig) -> List[RawMessage]:
    """Main entry point for data loading stage"""
    logger.info("=== DATA LOADING STAGE ===")

    loader = DataLoader(config)

    try:
        # Load the data
        messages = loader.load_data()

        # Validate and log summary
        validation_stats = loader.validate_data(messages)
        if not validation_stats["valid"]:
            raise ValueError(f"Data validation failed: {validation_stats.get('error')}")

        summary = loader.get_data_summary(messages)
        logger.info(f"\n{summary}")

        return messages

    except Exception as e:
        logger.error(f"Data loading failed: {e}")
        raise