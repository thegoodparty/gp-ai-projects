import logging
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from shared.logger import (
    Logger,
    LogLevel,
    Colors,
    ColoredFormatter,
    get_logger,
    set_production_mode,
    set_debug_mode,
    get_session_id,
    set_session_id,
    generate_new_session_id,
    with_log_level,
    logger_manager,
)


@pytest.fixture(autouse=True)
def reset_logger():
    Logger._instance = None
    Logger._loggers = {}
    yield
    Logger._instance = None
    Logger._loggers = {}


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)


@pytest.fixture
def production_env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")


@pytest.fixture
def development_env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")


class TestColors:
    def test_color_codes_defined(self):
        assert Colors.GREEN == '\033[92m'
        assert Colors.YELLOW == '\033[93m'
        assert Colors.RED == '\033[91m'
        assert Colors.DARK_RED == '\033[31m'
        assert Colors.RESET == '\033[0m'
        assert Colors.BOLD == '\033[1m'


class TestLogLevel:
    def test_log_level_values(self):
        assert LogLevel.DEBUG.value == logging.DEBUG
        assert LogLevel.INFO.value == logging.INFO
        assert LogLevel.WARNING.value == logging.WARNING
        assert LogLevel.ERROR.value == logging.ERROR
        assert LogLevel.CRITICAL.value == logging.CRITICAL


class TestColoredFormatter:
    def test_formats_info_level(self):
        formatter = ColoredFormatter('%(levelname)s - %(message)s')
        record = logging.LogRecord(
            name='test',
            level=logging.INFO,
            pathname='test.py',
            lineno=1,
            msg='Test message',
            args=(),
            exc_info=None
        )
        formatted = formatter.format(record)
        assert Colors.GREEN in formatted
        assert Colors.RESET in formatted
        assert 'Test message' in formatted

    def test_formats_warning_level(self):
        formatter = ColoredFormatter('%(levelname)s - %(message)s')
        record = logging.LogRecord(
            name='test',
            level=logging.WARNING,
            pathname='test.py',
            lineno=1,
            msg='Warning message',
            args=(),
            exc_info=None
        )
        formatted = formatter.format(record)
        assert Colors.YELLOW in formatted

    def test_formats_error_level(self):
        formatter = ColoredFormatter('%(levelname)s - %(message)s')
        record = logging.LogRecord(
            name='test',
            level=logging.ERROR,
            pathname='test.py',
            lineno=1,
            msg='Error message',
            args=(),
            exc_info=None
        )
        formatted = formatter.format(record)
        assert Colors.RED in formatted

    def test_formats_critical_level(self):
        formatter = ColoredFormatter('%(levelname)s - %(message)s')
        record = logging.LogRecord(
            name='test',
            level=logging.CRITICAL,
            pathname='test.py',
            lineno=1,
            msg='Critical message',
            args=(),
            exc_info=None
        )
        formatted = formatter.format(record)
        assert Colors.DARK_RED in formatted
        assert Colors.BOLD in formatted

    def test_preserves_original_levelname(self):
        formatter = ColoredFormatter('%(levelname)s')
        record = logging.LogRecord(
            name='test',
            level=logging.INFO,
            pathname='test.py',
            lineno=1,
            msg='Test',
            args=(),
            exc_info=None
        )
        original_levelname = record.levelname
        formatter.format(record)
        assert record.levelname == original_levelname


class TestLoggerSingleton:
    def test_singleton_pattern(self, clean_env):
        logger1 = Logger()
        logger2 = Logger()
        assert logger1 is logger2

    def test_generates_session_id(self, clean_env):
        logger_instance = Logger()
        assert logger_instance.session_id is not None
        assert len(logger_instance.session_id) == 8


class TestLoggerEnvironment:
    def test_production_environment(self, production_env):
        Logger._instance = None
        logger_instance = Logger()
        assert logger_instance.is_production is True
        assert logger_instance.default_level == LogLevel.INFO

    def test_development_environment(self, development_env):
        Logger._instance = None
        logger_instance = Logger()
        assert logger_instance.is_development is True
        assert logger_instance.default_level == LogLevel.DEBUG

    def test_default_environment(self, clean_env):
        Logger._instance = None
        logger_instance = Logger()
        assert logger_instance.is_production is False
        assert logger_instance.is_development is False
        assert logger_instance.default_level == LogLevel.WARNING

    def test_prod_alias(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "prod")
        Logger._instance = None
        logger_instance = Logger()
        assert logger_instance.is_production is True

    def test_dev_alias(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "dev")
        Logger._instance = None
        logger_instance = Logger()
        assert logger_instance.is_development is True

    def test_debug_alias(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "debug")
        Logger._instance = None
        logger_instance = Logger()
        assert logger_instance.is_development is True


class TestGetLogger:
    def test_returns_logger_instance(self, clean_env):
        logger = get_logger("test_module")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test_module"

    def test_caches_loggers(self, clean_env):
        logger1 = get_logger("test_module")
        logger2 = get_logger("test_module")
        assert logger1 is logger2

    def test_different_names_different_loggers(self, clean_env):
        logger1 = get_logger("module1")
        logger2 = get_logger("module2")
        assert logger1 is not logger2

    def test_custom_level_override(self, clean_env):
        logger = get_logger("test_custom", level=LogLevel.DEBUG)
        assert logger.level == logging.DEBUG

    def test_logger_has_handlers(self, clean_env):
        logger = get_logger("test_handlers")
        assert len(logger.handlers) > 0

    def test_logger_propagate_false(self, clean_env):
        logger = get_logger("test_propagate")
        assert logger.propagate is False


class TestSessionId:
    def test_get_session_id(self, clean_env):
        Logger._instance = None
        Logger()
        session_id = get_session_id()
        assert session_id is not None
        assert len(session_id) == 8

    def test_set_session_id(self, clean_env):
        Logger._instance = None
        Logger()
        set_session_id("custom123")
        assert get_session_id() == "custom123"

    def test_generate_new_session_id(self, clean_env):
        Logger._instance = None
        Logger()
        old_id = get_session_id()
        new_id = generate_new_session_id()
        assert new_id != old_id
        assert get_session_id() == new_id


class TestModeConfiguration:
    def test_set_production_mode(self, clean_env):
        Logger._instance = None
        Logger._loggers = {}
        get_logger("test_prod_mode")

        set_production_mode()

        assert logger_manager.is_production is True
        assert logger_manager.default_level == LogLevel.INFO

    def test_set_debug_mode(self, clean_env):
        Logger._instance = None
        Logger._loggers = {}
        get_logger("test_debug_mode")

        set_debug_mode()

        assert logger_manager.is_development is True
        assert logger_manager.default_level == LogLevel.DEBUG


class TestLogLevelContext:
    def test_temporarily_changes_level(self, clean_env):
        Logger._instance = None
        Logger()
        logger = get_logger("test_context")
        original_level = logger.level

        with with_log_level("test_context", LogLevel.DEBUG):
            assert logger.level == logging.DEBUG

        assert logger.level == original_level

    def test_restores_level_on_exception(self, clean_env):
        Logger._instance = None
        Logger()
        logger = get_logger("test_exception")
        original_level = logger.level

        try:
            with with_log_level("test_exception", LogLevel.DEBUG):
                raise ValueError("Test exception")
        except ValueError:
            pass

        assert logger.level == original_level

    def test_handles_nonexistent_logger(self, clean_env):
        Logger._instance = None
        Logger()

        with with_log_level("nonexistent_logger", LogLevel.DEBUG):
            pass


class TestSetLevel:
    def test_set_level_for_existing_logger(self, clean_env):
        Logger._instance = None
        logger_instance = Logger()
        logger = get_logger("test_set_level")

        logger_instance.set_level("test_set_level", LogLevel.ERROR)

        assert logger.level == logging.ERROR

    def test_set_level_ignores_nonexistent_logger(self, clean_env):
        Logger._instance = None
        logger_instance = Logger()

        logger_instance.set_level("nonexistent", LogLevel.ERROR)


class TestThirdPartyLoggers:
    def test_configures_httpcore_logger(self, clean_env):
        Logger._instance = None
        Logger()

        httpcore_logger = logging.getLogger('httpcore')
        assert httpcore_logger.level == logging.WARNING

    def test_configures_httpx_logger(self, clean_env):
        Logger._instance = None
        Logger()

        httpx_logger = logging.getLogger('httpx')
        assert httpx_logger.level == logging.WARNING

    def test_configures_asyncio_logger(self, clean_env):
        Logger._instance = None
        Logger()

        asyncio_logger = logging.getLogger('asyncio')
        assert asyncio_logger.level == logging.WARNING


class TestLoggingOutput:
    def test_can_log_info(self, clean_env, capsys):
        Logger._instance = None
        Logger._loggers = {}
        logger = get_logger("test_info_output")
        logger.setLevel(logging.INFO)

        logger.info("Test info message")
        captured = capsys.readouterr()

        assert "Test info message" in captured.out

    def test_can_log_debug(self, development_env, capsys):
        Logger._instance = None
        Logger._loggers = {}
        logger = get_logger("test_debug_output")

        logger.debug("Test debug message")
        captured = capsys.readouterr()

        assert "Test debug message" in captured.out

    def test_can_log_error(self, clean_env, capsys):
        Logger._instance = None
        Logger._loggers = {}
        logger = get_logger("test_error_output")
        logger.setLevel(logging.ERROR)

        logger.error("Test error message")
        captured = capsys.readouterr()

        assert "Test error message" in captured.out

    def test_can_log_warning(self, clean_env, capsys):
        Logger._instance = None
        Logger._loggers = {}
        logger = get_logger("test_warning_output")

        logger.warning("Test warning message")
        captured = capsys.readouterr()

        assert "Test warning message" in captured.out


class TestFileHandlers:
    def test_creates_log_directory(self, clean_env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        Logger._instance = None
        logger_instance = Logger()

        assert (tmp_path / "logs").exists()

    def test_adds_file_handlers(self, clean_env):
        Logger._instance = None
        Logger()
        logger = get_logger("test_file_handlers")

        file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) >= 2


class TestLoggerNameFormatting:
    def test_replaces_dots_in_filename(self, clean_env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        Logger._instance = None
        Logger()

        logger = get_logger("my.module.name")

        log_dir = tmp_path / "logs"
        log_files = list(log_dir.glob("my_module_name*.log"))
        assert len(log_files) > 0
