#!/usr/bin/env python3
"""
Test script for the new JSON endpoints of the Campaign Plan Generator API.

This script tests the JSON endpoints functionality in a development context.
It uses logging instead of print statements and follows the project's style guidelines.
"""

from typing import Any, Dict
import sys

import requests

from shared.logger import get_logger

# Constants
HTTP_OK = 200
DEFAULT_TIMEOUT = 120

logger = get_logger(__name__)

# Test data
test_campaign_data: Dict[str, Any] = {
    "candidate_name": "Jane Smith",
    "primary_date": None,
    "election_date": "2025-11-05",
    "office_and_jurisdiction": "City Council, District 3, Boston, MA",
    "incumbent_status": "N/A",
    "race_type": "Nonpartisan",
    "seats_available": 1,
    "number_of_opponents": 3,
    "win_number": 8000,
    "total_likely_voters": 50000,
    "available_cell_phones": 5000,
    "available_landlines": 500,
    "additional_race_context": "Focus on neighborhood safety and small business support",
}


def test_generate_campaign_plan_json() -> bool:
    """Test the /generate-campaign-plan endpoint with format=json."""
    logger.info("Testing /generate-campaign-plan?format=json...")

    try:
        response = requests.post(
            "http://localhost:8000/generate-campaign-plan?format=json",
            json=test_campaign_data,
            timeout=DEFAULT_TIMEOUT,
        )

        if response.status_code == HTTP_OK:
            json_data = response.json()
            logger.info("✅ JSON endpoint successful!")
            logger.info(f"Response keys: {list(json_data.keys())}")

            if "tasks" in json_data:
                timeline_tasks = json_data["tasks"]["timeline"]
                voter_contact_tasks = json_data["tasks"]["voter_contact"]
                logger.info(f"Timeline tasks found: {len(timeline_tasks)}")
                logger.info(f"Voter contact tasks found: {len(voter_contact_tasks)}")

                if timeline_tasks:
                    logger.debug(f"Sample timeline task: {timeline_tasks[0]}")
                if voter_contact_tasks:
                    logger.debug(f"Sample voter contact task: {voter_contact_tasks[0]}")

            return True
        else:
            logger.error(f"❌ Error: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        logger.error(f"❌ Exception: {e!s}")
        return False


def test_generate_campaign_plan_pdf() -> bool:
    """Test the /generate-campaign-plan endpoint with format=pdf (default)."""
    logger.info("Testing /generate-campaign-plan (PDF format)...")

    try:
        response = requests.post(
            "http://localhost:8000/generate-campaign-plan",
            json=test_campaign_data,
            timeout=DEFAULT_TIMEOUT,
        )

        if response.status_code == HTTP_OK:
            content_type = response.headers.get("content-type", "")
            if "application/pdf" in content_type:
                logger.info("✅ PDF endpoint successful!")
                logger.info(f"PDF size: {len(response.content)} bytes")
                return True
            else:
                logger.error(f"❌ Unexpected content type: {content_type}")
                return False
        else:
            logger.error(f"❌ Error: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        logger.error(f"❌ Exception: {e!s}")
        return False


def test_generate_campaign_plan_json_only() -> bool:
    """Test the dedicated /generate-campaign-plan-json endpoint."""
    logger.info("Testing /generate-campaign-plan-json...")

    try:
        response = requests.post(
            "http://localhost:8000/generate-campaign-plan-json",
            json=test_campaign_data,
            timeout=DEFAULT_TIMEOUT,
        )

        if response.status_code == HTTP_OK:
            json_data = response.json()
            logger.info("✅ JSON-only endpoint successful!")
            logger.info(f"Response keys: {list(json_data.keys())}")
            return True
        else:
            logger.error(f"❌ Error: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        logger.error(f"❌ Exception: {e!s}")
        return False


def check_server_health() -> bool:
    """Check if the server is running and healthy."""
    try:
        response = requests.get("http://localhost:8000/health", timeout=5)
        if response.status_code != HTTP_OK:
            logger.error("❌ Server health check failed")
            return False
        logger.info("✅ Server is running")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Server not responding: {e!s}")
        logger.error("Make sure the server is running on port 8000")
        return False


def main() -> None:
    """Run all tests."""
    logger.info("🧪 Testing Campaign Plan JSON Endpoints")
    logger.info("=" * 50)

    # Check if server is running
    if not check_server_health():
        sys.exit(1)

    # Run tests
    tests = [
        test_generate_campaign_plan_json,
        test_generate_campaign_plan_pdf,
        test_generate_campaign_plan_json_only,
    ]

    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            logger.error(f"❌ Test failed with exception: {e!s}")
            results.append(False)

    # Summary
    logger.info("=" * 50)
    logger.info("📊 Test Summary:")
    passed = sum(results)
    total = len(results)
    logger.info(f"Passed: {passed}/{total}")

    if passed == total:
        logger.info("🎉 All tests passed!")
        sys.exit(0)
    else:
        logger.warning("⚠️  Some tests failed. Check the output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()