"""
Shared test fixtures for the campaign plan generator project.
"""

import pytest
from pathlib import Path
from unittest.mock import Mock

from shared.logger import get_logger


@pytest.fixture
def temp_directory(tmp_path: Path) -> Path:
    """Provide a temporary directory for tests."""
    return tmp_path


@pytest.fixture
def mock_logger():
    """Provide a mock logger for testing."""
    return Mock(spec=get_logger("test"))


@pytest.fixture
def sample_campaign_data() -> dict[str, any]:
    """Provide sample campaign data for testing."""
    return {
        "candidate_name": "Test Candidate",
        "election_date": "2025-11-05",
        "office_and_jurisdiction": "Test Office, Test City, State",
        "incumbent_status": "N/A",
        "race_type": "Nonpartisan",
        "seats_available": 1,
        "number_of_opponents": 2,
        "win_number": 1000,
        "total_likely_voters": 5000,
        "available_cell_phones": 1000,
        "available_landlines": 200,
    }
