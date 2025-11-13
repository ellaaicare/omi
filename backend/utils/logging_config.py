"""
Structured logging configuration using structlog.

This module provides a centralized logging configuration for the backend.
It uses structlog for structured JSON logging in production and human-readable
output for development.
"""

import logging
import os
import sys
from typing import Any, Dict

import structlog


def get_environment() -> str:
    """Determine the current environment (production, development, etc.)."""
    return os.getenv("ENVIRONMENT", "development")


def configure_logging() -> None:
    """
    Configure structlog for the application.

    In production: JSON output for log aggregation systems
    In development: Human-readable console output
    """
    environment = get_environment()
    is_production = environment == "production"

    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO if is_production else logging.DEBUG,
    )

    # Shared processors for all environments
    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Production processors (JSON output)
    production_processors = shared_processors + [
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer(),
    ]

    # Development processors (console output)
    development_processors = shared_processors + [
        structlog.dev.ConsoleRenderer(),
    ]

    # Choose processors based on environment
    processors = production_processors if is_production else development_processors

    # Configure structlog
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Get a configured logger instance.

    Args:
        name: The name of the logger (typically __name__)

    Returns:
        A configured structlog logger instance

    Example:
        >>> from utils.logging_config import get_logger
        >>> logger = get_logger(__name__)
        >>> logger.info("user_action", uid="123", action="login")
    """
    return structlog.get_logger(name)


# Configure logging on module import
configure_logging()
