import pandas as pd
import sys
import uuid
from pathlib import Path
from typing import List

sys.path.append(str(Path(__file__).parent.parent.parent.parent))
from shared.logger import get_logger
from serve.analyze_texts.models import MessageRecord

logger = get_logger(__name__)


class DataLoader:
    def __init__(self, input_dir: str = "serve/data"):
        self.input_dir = Path(input_dir)
        if not self.input_dir.exists():
            raise FileNotFoundError(f"Input directory not found: {input_dir}")

    def load_campaign_data(self, campaign_name: str) -> List[MessageRecord]:
        csv_path = self.input_dir / f"{campaign_name}_consolidated.csv"

        if not csv_path.exists():
            raise FileNotFoundError(f"Consolidated CSV not found: {csv_path}")

        logger.info(f"Loading data from {csv_path}")

        df = pd.read_csv(csv_path)
        logger.info(f"Loaded {len(df)} rows from CSV")

        messages = []
        for idx, row in df.iterrows():
            message_text = str(row.get("Message Text", "")).strip()

            if not message_text:
                continue

            voters_age_val = None
            if pd.notna(row.get("voters_age")):
                try:
                    voters_age_val = float(row.get("voters_age"))
                except (ValueError, TypeError):
                    voters_age_val = None

            message = MessageRecord(
                original_row_idx=int(idx),
                atomic_idx=0,
                phone_number=str(row.get("Contact Phone Number", "")),
                message_text=message_text,
                original_message_text=message_text,
                poll_id=str(row.get("Campaign ID", "")),
                record_id=str(uuid.uuid4()),
                campaign_source=campaign_name,
                round=str(row.get("round", "Unknown")),
                voters_age=voters_age_val,
                voters_gender=str(row.get("voters_gender", "")) if pd.notna(row.get("voters_gender")) else None,
                age_group=str(row.get("age_group", "Unknown")),
                voting_performance_category=str(row.get("voting_performance_category", "Unknown")),
                location=str(row.get("location", "Unknown")),
                ward=str(row.get("ward", "Unknown")),
                income_level=str(row.get("income_level", "Unknown")),
                education_level=str(row.get("education_level", "Unknown")),
                homeowner_status=str(row.get("homeowner_status", "Unknown")),
                business_owner=str(row.get("business_owner", "Unknown")),
                has_children_under_18=str(row.get("has_children_under_18", "Unknown"))
            )

            messages.append(message)

        logger.info(f"Created {len(messages)} message records (filtered {len(df) - len(messages)} empty messages)")

        return messages


def load_data_stage(campaign: str, config: dict) -> List[MessageRecord]:
    logger.info("=== STAGE 0: DATA LOADING ===")

    input_dir = config.get("loader", {}).get("input_dir", "serve/data")
    loader = DataLoader(input_dir=input_dir)

    messages = loader.load_campaign_data(campaign)

    logger.info(f"Loaded {len(messages)} messages from {campaign} campaign")

    return messages
