import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any
from enum import Enum


class LogLevel(Enum):
    """Log level enumeration for easy configuration"""
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR
    CRITICAL = logging.CRITICAL


class Logger:
    """
    Centralized logger class with support for both debug and production environments.
    
    Features:
    - Environment-based configuration (DEBUG/PRODUCTION)
    - File rotation
    - Console and file output
    - Structured formatting
    - Context-aware logging
    """
    
    _instance: Optional['Logger'] = None
    _loggers: Dict[str, logging.Logger] = {}
    
    def __new__(cls) -> 'Logger':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        
        self._initialized = True
        self.log_dir = Path("logs")
        self.log_dir.mkdir(exist_ok=True)
        
        # Determine environment
        self.environment = os.getenv('ENVIRONMENT', 'development').lower()
        self.is_production = self.environment == 'production'
        
        # Set default log level based on environment
        if self.is_production:
            self.default_level = LogLevel.INFO
        else:
            self.default_level = LogLevel.DEBUG
    
    def get_logger(self, name: str, level: Optional[LogLevel] = None) -> logging.Logger:
        """
        Get or create a logger with the specified name and configuration.
        
        Args:
            name: Logger name (typically __name__ of the calling module)
            level: Optional log level override
            
        Returns:
            Configured logger instance
        """
        if name in self._loggers:
            return self._loggers[name]
        
        logger = logging.getLogger(name)
        
        # Clear any existing handlers to avoid duplicates
        logger.handlers.clear()
        
        # Set log level
        log_level = level.value if level else self.default_level.value
        logger.setLevel(log_level)
        
        # Create formatters
        detailed_formatter = logging.Formatter(
            fmt='%(asctime)s | %(name)s | %(levelname)s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        simple_formatter = logging.Formatter(
            fmt='%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        if self.is_production:
            console_handler.setFormatter(simple_formatter)
            console_handler.setLevel(logging.INFO)
        else:
            console_handler.setFormatter(detailed_formatter)
            console_handler.setLevel(logging.DEBUG)
        
        logger.addHandler(console_handler)
        
        self._add_file_handlers(logger, name, detailed_formatter)
        
        logger.propagate = False
        
        self._loggers[name] = logger
        return logger
    
    def _add_file_handlers(self, logger: logging.Logger, name: str, formatter: logging.Formatter):
        """Add rotating file handlers for different log levels"""
        
        general_handler = logging.handlers.RotatingFileHandler(
            filename=self.log_dir / f"{name.replace('.', '_')}.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5
        )
        general_handler.setFormatter(formatter)
        general_handler.setLevel(self.default_level.value)
        logger.addHandler(general_handler)
        
        error_handler = logging.handlers.RotatingFileHandler(
            filename=self.log_dir / f"{name.replace('.', '_')}_errors.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5
        )
        error_handler.setFormatter(formatter)
        error_handler.setLevel(logging.WARNING)
        logger.addHandler(error_handler)
        
        if not self.is_production:
            debug_handler = logging.handlers.RotatingFileHandler(
                filename=self.log_dir / f"{name.replace('.', '_')}_debug.log",
                maxBytes=5 * 1024 * 1024,  # 5MB
                backupCount=3
            )
            debug_handler.setFormatter(formatter)
            debug_handler.setLevel(logging.DEBUG)
            logger.addHandler(debug_handler)
    
    def set_level(self, name: str, level: LogLevel):
        """Set log level for a specific logger"""
        if name in self._loggers:
            self._loggers[name].setLevel(level.value)
    
    def configure_for_production(self):
        """Configure all loggers for production environment"""
        self.is_production = True
        self.default_level = LogLevel.INFO
        
        for logger in self._loggers.values():
            logger.setLevel(logging.INFO)
            for handler in logger.handlers:
                if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                    handler.setLevel(logging.INFO)
    
    def configure_for_debug(self):
        """Configure all loggers for debug environment"""
        self.is_production = False
        self.default_level = LogLevel.DEBUG
        
        for logger in self._loggers.values():
            logger.setLevel(logging.DEBUG)
            for handler in logger.handlers:
                if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                    handler.setLevel(logging.DEBUG)


logger_manager = Logger()


def get_logger(name: str, level: Optional[LogLevel] = None) -> logging.Logger:
    """
    Convenience function to get a logger instance.
    
    Usage:
        from shared.logger import get_logger
        logger = get_logger(__name__)
        logger.info("This is an info message")
        logger.debug("Debug information")
        logger.error("An error occurred")
    
    Args:
        name: Logger name (typically __name__)
        level: Optional log level override
        
    Returns:
        Configured logger instance
    """
    return logger_manager.get_logger(name, level)


def set_production_mode():
    """Set all loggers to production mode"""
    logger_manager.configure_for_production()


def set_debug_mode():
    """Set all loggers to debug mode"""
    logger_manager.configure_for_debug()


class LogLevelContext:
    """Context manager for temporarily changing log levels"""
    
    def __init__(self, logger_name: str, level: LogLevel):
        self.logger_name = logger_name
        self.new_level = level
        self.original_level = None
    
    def __enter__(self):
        if self.logger_name in logger_manager._loggers:
            logger = logger_manager._loggers[self.logger_name]
            self.original_level = logger.level
            logger.setLevel(self.new_level.value)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.original_level is not None and self.logger_name in logger_manager._loggers:
            logger_manager._loggers[self.logger_name].setLevel(self.original_level)


def with_log_level(logger_name: str, level: LogLevel):
    """
    Context manager for temporarily changing log level.
    
    Usage:
        with with_log_level('my_module', LogLevel.DEBUG):
            logger.debug("This will be logged even in production")
    """
    return LogLevelContext(logger_name, level)
