"""
GCE Rescue - Logging Setup

This module sets up logging for GCE Rescue operations.
Provides better logging than simple print() statements.

Logging Strategy:
- INFO (default): High-level progress for end users
- DEBUG (--debug): Detailed technical info for developers
- WARNING: Recoverable issues
- ERROR: Problems that stop operations
- CRITICAL: Serious issues requiring manual intervention
"""

import logging
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Dict


class CleanFormatter(logging.Formatter):
    """
    Custom formatter for clean user-facing logs.

    - INFO: Just the message (clean)
    - WARNING/ERROR/CRITICAL: Show level with color/emphasis
    """

    def format(self, record):
        """Format log record based on level."""

        # INFO: Clean message only
        if record.levelno == logging.INFO:
            return record.getMessage()

        # WARNING: Show with prefix
        elif record.levelno == logging.WARNING:
            return f"[!]  WARNING: {record.getMessage()}"

        # ERROR: Show with prefix
        elif record.levelno == logging.ERROR:
            return f"[X] ERROR: {record.getMessage()}"

        # CRITICAL: Show with prefix and emphasis
        elif record.levelno == logging.CRITICAL:
            return f"🚨 CRITICAL: {record.getMessage()}"

        # DEBUG: Include timestamp (shouldn't happen in normal mode, but just in case)
        else:
            return f"[DEBUG] {record.getMessage()}"


def setup_logging(level='INFO', log_file=None, debug=False):
    """
    Setup logging for GCE Rescue.

    Configures logging to:
    1. Output to console (stdout)
    2. Optionally write to log file
    3. Use clear, readable format
    4. Different formats for normal vs debug mode

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional path to log file. If None, only logs to console.
        debug: If True, use DEBUG level and detailed format

    Returns:
        logging.Logger: Configured logger instance

    Example:
        # Normal mode
        logger = setup_logging('INFO')
        logger.info("Starting rescue operation...")

        # Debug mode
        logger = setup_logging(debug=True)
        logger.debug("API call: compute.instances().get(...)")
    """

    # If debug=True, override level to DEBUG
    if debug:
        level = 'DEBUG'

    # Convert string level to logging level
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Create logger
    logger = logging.getLogger('gce_rescue')
    logger.setLevel(numeric_level)

    # Remove existing handlers (in case setup_logging called multiple times)
    logger.handlers.clear()

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)

    # Create formatter based on debug mode
    if debug:
        # Debug mode: Include timestamp with milliseconds, level, function, line number
        # Format: [2025-11-02 10:30:45.123] DEBUG [stop_vm:45]: API call: instances.stop(...)
        console_format = logging.Formatter(
            '[%(asctime)s] %(levelname)s [%(funcName)s:%(lineno)d]: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    else:
        # Normal mode: Clean format, just timestamp and message
        # INFO messages don't show level (clean for users)
        # WARNING/ERROR/CRITICAL show level (important to highlight)
        console_format = CleanFormatter(
            '%(message)s',  # Default format (for INFO)
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    console_handler.setFormatter(console_format)

    # Add console handler
    logger.addHandler(console_handler)

    # Add file handler if log_file specified
    if log_file:
        # Create log directory if it doesn't exist
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Create file handler
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(numeric_level)

        # File format includes more details
        file_format = logging.Formatter(
            '[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_format)

        # Add file handler
        logger.addHandler(file_handler)

        logger.debug(f"Logging to file: {log_file}")

    return logger


def get_logger():
    """
    Get the GCE Rescue logger instance.

    Returns:
        logging.Logger: Logger instance

    Example:
        from utils.logger import get_logger
        logger = get_logger()
        logger.info("Hello!")
    """
    return logging.getLogger('gce_rescue')


# Create a simple print-like wrapper for backwards compatibility
class LoggerWrapper:
    """
    Wrapper around logger that provides print-like interface.

    This allows gradual migration from print() to logger.
    """

    def __init__(self, logger):
        self.logger = logger

    def __call__(self, message):
        """Allow logger() to work like print()."""
        self.logger.info(message)

    def info(self, message):
        """Info level message."""
        self.logger.info(message)

    def warning(self, message):
        """Warning level message."""
        self.logger.warning(message)

    def error(self, message):
        """Error level message."""
        self.logger.error(message)

    def debug(self, message):
        """Debug level message."""
        self.logger.debug(message)

    def critical(self, message):
        """Critical level message."""
        self.logger.critical(message)


# Debug logging helpers

def log_api_call(logger, method_name: str, **params):
    """
    Log an API call (DEBUG level).

    Args:
        logger: Logger instance
        method_name: Name of the API method (e.g., 'instances.get')
        **params: API call parameters

    Example:
        log_api_call(logger, 'instances.get', project='my-project', zone='us-central1-a', instance='my-vm')
        # Output: API call: instances.get(project=my-project, zone=us-central1-a, instance=my-vm)
    """
    param_str = ', '.join(f'{k}={v}' for k, v in params.items())
    logger.debug(f"API call: {method_name}({param_str})")


def log_api_response(logger, response: Any, truncate: int = 200):
    """
    Log an API response (DEBUG level).

    Args:
        logger: Logger instance
        response: API response (dict or other)
        truncate: Max characters to log (default: 200)

    Example:
        log_api_response(logger, vm_info)
        # Output: API response: {'id': '123', 'name': 'my-vm', ...}
    """
    response_str = str(response)
    if len(response_str) > truncate:
        response_str = response_str[:truncate] + '...'
    logger.debug(f"API response: {response_str}")


def log_operation_start(logger, operation_name: str):
    """
    Log the start of an operation (DEBUG level).

    Args:
        logger: Logger instance
        operation_name: Name of the operation

    Returns:
        float: Start time (for use with log_operation_end)

    Example:
        start_time = log_operation_start(logger, 'Stop VM')
        # ... do operation ...
        log_operation_end(logger, 'Stop VM', start_time)
    """
    logger.debug(f"Starting operation: {operation_name}")
    return time.time()


def log_operation_end(logger, operation_name: str, start_time: float):
    """
    Log the end of an operation with timing (DEBUG level).

    Args:
        logger: Logger instance
        operation_name: Name of the operation
        start_time: Start time from log_operation_start()

    Example:
        start_time = log_operation_start(logger, 'Stop VM')
        # ... do operation ...
        log_operation_end(logger, 'Stop VM', start_time)
        # Output: Operation completed: Stop VM (took 10.5s)
    """
    duration = time.time() - start_time
    logger.debug(f"Operation completed: {operation_name} (took {duration:.2f}s)")


def log_state_change(logger, resource: str, old_state: str, new_state: str):
    """
    Log a state change (DEBUG level).

    Args:
        logger: Logger instance
        resource: Resource name (e.g., 'VM my-instance')
        old_state: Previous state
        new_state: New state

    Example:
        log_state_change(logger, 'VM my-instance', 'RUNNING', 'TERMINATED')
        # Output: State change: VM my-instance: RUNNING → TERMINATED
    """
    logger.debug(f"State change: {resource}: {old_state} → {new_state}")


def log_rollback_data(logger, operation_name: str, rollback_data: Dict):
    """
    Log rollback data (DEBUG level).

    Args:
        logger: Logger instance
        operation_name: Name of the operation
        rollback_data: Data needed for rollback

    Example:
        log_rollback_data(logger, 'Stop VM', {'vm_name': 'my-instance', 'original_state': 'RUNNING'})
        # Output: Rollback data for Stop VM: {'vm_name': 'my-instance', 'original_state': 'RUNNING'}
    """
    logger.debug(f"Rollback data for {operation_name}: {rollback_data}")


def print_separator(logger, char='=', length=60):
    """
    Print a separator line (INFO level).

    Args:
        logger: Logger instance
        char: Character to use for separator
        length: Length of separator

    Example:
        print_separator(logger)
        # Output: ============================================================
    """
    logger.info(char * length)


def print_header(logger, title: str, char='=', length=60):
    """
    Print a formatted header (INFO level).

    Args:
        logger: Logger instance
        title: Header title
        char: Character for border
        length: Total width

    Example:
        print_header(logger, 'GCE Rescue V2 - Rescue Operation')
        # Output:
        # ============================================================
        # GCE Rescue V2 - Rescue Operation
        # ============================================================
    """
    logger.info(char * length)
    logger.info(title)
    logger.info(char * length)
