"""
Tests for the shared logging module.
"""

import logging
from unittest.mock import patch

import pytest

from shared.logger import get_logger, set_debug_mode, set_production_mode


def test_get_logger_returns_logger():
    """Test that get_logger returns a logging.Logger instance."""
    logger = get_logger("test_module")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "test_module"


def test_get_logger_same_name_returns_same_instance():
    """Test that getting a logger with the same name returns the same instance."""
    logger1 = get_logger("same_name")
    logger2 = get_logger("same_name")
    assert logger1 is logger2


def test_set_debug_mode():
    """Test that debug mode configuration works."""
    with patch("shared.logger.logger_manager") as mock_manager:
        set_debug_mode()
        mock_manager.configure_for_debug.assert_called_once()


def test_set_production_mode():
    """Test that production mode configuration works."""
    with patch("shared.logger.logger_manager") as mock_manager:
        set_production_mode()
        mock_manager.configure_for_production.assert_called_once()


class TestLoggerBasicFunctionality:
    """Test basic logger functionality."""

    def test_logger_can_log_messages(self):
        """Test that loggers can log messages at different levels."""
        logger = get_logger("test_logging")
        
        # These should not raise exceptions
        logger.debug("Debug message")
        logger.info("Info message") 
        logger.warning("Warning message")
        logger.error("Error message")
        logger.critical("Critical message")

    def test_logger_has_proper_level(self):
        """Test that logger has appropriate level set."""
        logger = get_logger("test_level")
        # Logger should be configured with some level
        assert hasattr(logger, "level")
        assert isinstance(logger.level, int)
