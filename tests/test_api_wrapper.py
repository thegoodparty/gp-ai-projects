"""
Tests for the API wrapper functionality.
"""

import pytest
from unittest.mock import Mock, patch

from api_wrapper import parse_timeline_tasks, parse_voter_contact_tasks


class TestTaskParsing:
    """Test task parsing functionality."""

    def test_parse_timeline_tasks_empty_content(self):
        """Test parsing empty timeline content."""
        result = parse_timeline_tasks("")
        assert result == []

    def test_parse_timeline_tasks_valid_format(self):
        """Test parsing valid timeline format."""
        content = """
        - July 15 | Campaign Launch Event | Official campaign announcement
        - August 1 | Fundraising Deadline | Monthly fundraising goal
        """
        result = parse_timeline_tasks(content)
        
        assert len(result) == 2
        assert result[0]["date"] == "July 15"
        assert result[0]["title"] == "Campaign Launch Event"
        assert result[0]["description"] == "Official campaign announcement"
        assert result[0]["type"] == "timeline"

    def test_parse_timeline_tasks_invalid_format(self):
        """Test parsing invalid timeline format."""
        content = """
        Invalid line format
        - Missing separator
        """
        result = parse_timeline_tasks(content)
        assert result == []

    def test_parse_voter_contact_tasks_empty_content(self):
        """Test parsing empty voter contact content."""
        result = parse_voter_contact_tasks("")
        assert result == []

    def test_parse_voter_contact_tasks_valid_format(self):
        """Test parsing valid voter contact format."""
        content = """
        - [JULY 15] – P2P Text #1: Candidate intro message
        - [AUGUST 1] – Robocall #1: Voting reminder
        """
        result = parse_voter_contact_tasks(content)
        
        assert len(result) == 2
        assert result[0]["date"] == "JULY 15"
        assert result[0]["title"] == "P2P Text #1"
        assert result[0]["description"] == "Candidate intro message"
        assert result[0]["type"] == "voter_contact"

    def test_parse_voter_contact_tasks_invalid_format(self):
        """Test parsing invalid voter contact format."""
        content = """
        Invalid line format
        - [JULY 15] Missing dash separator
        """
        result = parse_voter_contact_tasks(content)
        assert len(result) == 0  # Only valid formats should be parsed
