#!/usr/bin/env python3

import csv
import os
from typing import List, Optional, Dict, Any
from pathlib import Path
import pandas as pd
import asyncio
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from functools import partial
from shared.logger import get_logger

from .models import MessageData, EnrichedMessage

logger = get_logger(__name__)

class DataLoader:
    """Load and parse civic message CSV files with complete metadata tracking"""

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        if not self.data_dir.exists():
            raise FileNotFoundError(f"Data directory not found: {data_dir}")

    def get_available_datasets(self) -> Dict[str, List[str]]:
        """Get list of available datasets organized by campaign"""
        datasets = {
            "josh": [],
            "cara": [],
            "berkley": [],
            "heather": [],
            "japjeet": [],
            "joanna": [],
            "jonathan": [],
            "consolidated": []
        }

        for file_path in self.data_dir.glob("*.csv"):
            filename = file_path.name.lower()

            if "josh" in filename:
                datasets["josh"].append(str(file_path))
            elif "cara" in filename:
                datasets["cara"].append(str(file_path))
            elif "berkley" in filename:
                datasets["berkley"].append(str(file_path))
            elif "heather" in filename:
                datasets["heather"].append(str(file_path))
            elif "japjeet" in filename:
                datasets["japjeet"].append(str(file_path))
            elif "joanna" in filename:
                datasets["joanna"].append(str(file_path))
            elif "jonathan" in filename:
                datasets["jonathan"].append(str(file_path))
            elif "consolidated" in filename:
                datasets["consolidated"].append(str(file_path))

        logger.info(f"Found datasets: {sum(len(v) for v in datasets.values())} files total")
        for campaign, files in datasets.items():
            if files:
                logger.info(f"  {campaign}: {len(files)} files")

        return datasets

    def load_csv_file(self, file_path: str, ultra_fast_mode: bool = True) -> List[EnrichedMessage]:
        """
        Load a single CSV file and convert to EnrichedMessage objects
        Ultra-fast mode disables verbose logging for speed
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"CSV file not found: {file_path}")

        if not ultra_fast_mode:
            logger.info(f"Loading CSV file: {file_path.name}")

        messages = []

        try:
            # Use pandas for robust CSV parsing with optimized settings
            df = pd.read_csv(
                file_path,
                encoding='utf-8',
                quoting=csv.QUOTE_ALL,
                engine='c',  # Use C engine for speed
                low_memory=False  # Read entire file into memory for speed
            )

            # Rename columns to match our model (handle variations in naming)
            column_mapping = {
                'Campaign ID': 'campaign_id',
                'Campaign Name': 'campaign_name',
                'Contact Phone Number': 'contact_phone_number',
                'Carrier': 'carrier',
                'Campaign Number': 'campaign_number',
                'Is Automatic Reply?': 'is_automatic_reply',
                'Send Direction': 'send_direction',
                'Send Status': 'send_status',
                'Error Code': 'error_code',
                'Sent At': 'sent_at',
                'Message Text': 'message_text',
                'Texter Name': 'texter_name',
                'Message Type': 'message_type',
                'MMS Attachments': 'mms_attachments'
            }

            df = df.rename(columns=column_mapping)

            # Process each row
            for idx, row in df.iterrows():
                try:
                    # Convert pandas row to dict and handle NaN values
                    row_dict = row.to_dict()
                    for key, value in row_dict.items():
                        if pd.isna(value):
                            row_dict[key] = None
                        elif key == 'is_automatic_reply':
                            # Convert to boolean
                            row_dict[key] = str(value).upper() == 'TRUE'
                        else:
                            row_dict[key] = str(value) if value is not None else None

                    # Create MessageData object
                    message_data = MessageData(**row_dict)

                    # Create EnrichedMessage with metadata
                    enriched_message = EnrichedMessage(
                        original_data=message_data,
                        is_substantive=False,  # Will be determined during cleaning
                        original_csv_row=idx + 2,  # +2 because pandas is 0-indexed and CSV has header
                        original_csv_file=file_path.name
                    )

                    messages.append(enriched_message)

                except Exception as e:
                    if not ultra_fast_mode:
                        logger.warning(f"Skipping invalid row {idx + 2} in {file_path.name}: {str(e)}")
                    continue

        except Exception as e:
            logger.error(f"Error loading CSV file {file_path}: {e}")
            raise

        if not ultra_fast_mode:
            logger.info(f"Loaded {len(messages)} messages from {file_path.name}")
        return messages

    async def load_by_source_parallel(self, source: str, max_workers: int = 10) -> List[EnrichedMessage]:
        """
        Load messages from a specific source with parallel file processing
        """
        datasets = self.get_available_datasets()

        files_to_load = []
        if source.lower() == "all":
            for campaign_files in datasets.values():
                files_to_load.extend(campaign_files)
        elif source.lower() in datasets:
            files_to_load = datasets[source.lower()]
        else:
            consolidated_files = [
                str(f) for f in self.data_dir.glob("*.csv")
                if source.lower() in f.name.lower()
            ]
            files_to_load = consolidated_files

        if not files_to_load:
            raise ValueError(f"Unknown data source: {source}. Available: {list(datasets.keys()) + ['all']}")

        # Process files in parallel using ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Create partial function with ultra_fast_mode=True
            load_func = partial(self.load_csv_file, ultra_fast_mode=True)

            # Submit all file loading tasks
            loop = asyncio.get_event_loop()
            tasks = [
                loop.run_in_executor(executor, load_func, file_path)
                for file_path in files_to_load
            ]

            # Wait for all files to load in parallel
            file_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Combine results
        messages = []
        for i, result in enumerate(file_results):
            if isinstance(result, Exception):
                logger.error(f"Failed to load {files_to_load[i]}: {result}")
            else:
                messages.extend(result)

        logger.info(f"Total loaded messages for '{source}': {len(messages)} (parallel mode)")
        return messages

    def load_by_source(self, source: str) -> List[EnrichedMessage]:
        """
        Load messages from a specific source (josh, cara, berkley, or all)
        """
        datasets = self.get_available_datasets()
        messages = []

        if source.lower() == "all":
            # Load all datasets
            for campaign_files in datasets.values():
                for file_path in campaign_files:
                    messages.extend(self.load_csv_file(file_path, ultra_fast_mode=True))
        elif source.lower() in datasets:
            # Load specific campaign
            for file_path in datasets[source.lower()]:
                messages.extend(self.load_csv_file(file_path, ultra_fast_mode=True))
        else:
            # Try to find consolidated file
            consolidated_files = [
                f for f in self.data_dir.glob("*.csv")
                if source.lower() in f.name.lower()
            ]

            if consolidated_files:
                for file_path in consolidated_files:
                    messages.extend(self.load_csv_file(str(file_path), ultra_fast_mode=True))
            else:
                raise ValueError(f"Unknown data source: {source}. Available: {list(datasets.keys()) + ['all']}")

        logger.info(f"Total loaded messages for '{source}': {len(messages)}")
        return messages

    def filter_inbound_messages(self, messages: List[EnrichedMessage]) -> List[EnrichedMessage]:
        """Filter to only INBOUND messages (citizen responses)"""
        inbound_messages = [
            msg for msg in messages
            if msg.original_data.send_direction and msg.original_data.send_direction.upper() == "INBOUND"
        ]

        logger.info(f"Filtered to {len(inbound_messages)} INBOUND messages from {len(messages)} total")
        return inbound_messages

    def get_data_summary(self, messages: List[EnrichedMessage]) -> Dict[str, Any]:
        """Get summary statistics about the loaded data"""
        if not messages:
            return {"total_messages": 0}

        campaigns = {}
        directions = {}
        message_types = {}
        carriers = {}
        total_length = 0

        for msg in messages:
            # Campaign stats
            campaign = msg.original_data.campaign_name or "Unknown"
            campaigns[campaign] = campaigns.get(campaign, 0) + 1

            # Direction stats
            direction = msg.original_data.send_direction or "Unknown"
            directions[direction] = directions.get(direction, 0) + 1

            # Message type stats
            msg_type = msg.original_data.message_type or "Unknown"
            message_types[msg_type] = message_types.get(msg_type, 0) + 1

            # Carrier stats
            carrier = msg.original_data.carrier or "Unknown"
            carriers[carrier] = carriers.get(carrier, 0) + 1

            # Message length
            if msg.original_data.message_text:
                total_length += len(msg.original_data.message_text)

        avg_length = total_length / len(messages) if messages else 0

        return {
            "total_messages": len(messages),
            "campaigns": campaigns,
            "directions": directions,
            "message_types": message_types,
            "carriers": carriers,
            "average_message_length": round(avg_length, 1)
        }

    async def load_for_classification_parallel(self, source: str = "josh", inbound_only: bool = True, max_workers: int = 10) -> tuple[List[EnrichedMessage], Dict[str, Any]]:
        """
        Ultra-high-speed data loading with parallel file processing
        """
        logger.info(f"Loading data for classification (parallel): source='{source}', inbound_only={inbound_only}")

        # Load messages in parallel
        messages = await self.load_by_source_parallel(source, max_workers)

        # Filter to inbound only if requested (this is fast, no need to parallelize)
        if inbound_only:
            messages = self.filter_inbound_messages(messages)

        # Get summary stats
        summary = self.get_data_summary(messages)

        logger.info(f"Data loading complete (parallel): {len(messages)} messages ready for classification")
        return messages, summary

    def load_for_classification(self, source: str = "josh", inbound_only: bool = True) -> tuple[List[EnrichedMessage], Dict[str, Any]]:
        """
        Main method to load data ready for classification pipeline
        """
        logger.info(f"Loading data for classification: source='{source}', inbound_only={inbound_only}")

        # Load messages
        messages = self.load_by_source(source)

        # Filter to inbound only if requested
        if inbound_only:
            messages = self.filter_inbound_messages(messages)

        # Get summary stats
        summary = self.get_data_summary(messages)

        logger.info(f"Data loading complete: {len(messages)} messages ready for classification")
        return messages, summary


def main():
    """Test the data loader"""
    loader = DataLoader()

    # Test loading josh data
    messages, summary = loader.load_for_classification("josh")

    print(f"Loaded {len(messages)} messages")
    print("Summary:", summary)

    # Print a few sample messages
    for i, msg in enumerate(messages[:3]):
        print(f"\nMessage {i+1}:")
        print(f"  From: {msg.original_data.contact_phone_number}")
        print(f"  Text: {msg.original_data.message_text[:100]}...")
        print(f"  CSV Row: {msg.original_csv_row}")
        print(f"  CSV File: {msg.original_csv_file}")


if __name__ == "__main__":
    main()