"""
Color and terminal utilities for CLI output.

Provides simple ANSI color support with TTY detection.
Colors are automatically disabled when output is piped.
"""

import sys

# ANSI color codes
RED = '\033[91m'
GREEN = '\033[92m'
YELLOW = '\033[93m'
BOLD = '\033[1m'
DIM = '\033[2m'
RESET = '\033[0m'


def _is_tty() -> bool:
    """Check if stdout is a terminal (not piped)."""
    return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()


def red(text: str) -> str:
    """Return text in red if TTY, otherwise plain text."""
    if _is_tty():
        return f"{RED}{text}{RESET}"
    return text


def yellow(text: str) -> str:
    """Return text in yellow if TTY, otherwise plain text."""
    if _is_tty():
        return f"{YELLOW}{text}{RESET}"
    return text


def green(text: str) -> str:
    """Return text in green if TTY, otherwise plain text."""
    if _is_tty():
        return f"{GREEN}{text}{RESET}"
    return text


def bold(text: str) -> str:
    """Return text in bold if TTY, otherwise plain text."""
    if _is_tty():
        return f"{BOLD}{text}{RESET}"
    return text


def dim(text: str) -> str:
    """Return text in dim/faint if TTY, otherwise plain text."""
    if _is_tty():
        return f"{DIM}{text}{RESET}"
    return text


def error_prefix() -> str:
    """Return colored 'ERROR:' prefix."""
    return red("ERROR:")


def warning_prefix() -> str:
    """Return colored 'WARNING:' prefix."""
    return yellow("WARNING:")


def note_prefix() -> str:
    """Return colored 'Note:' prefix."""
    return yellow("Note:")


def clear_lines(num_lines: int) -> None:
    """Clear the specified number of lines above cursor."""
    if _is_tty() and num_lines > 0:
        # Move cursor up and clear each line
        for _ in range(num_lines):
            sys.stdout.write('\033[A')  # Move up
            sys.stdout.write('\033[2K')  # Clear line
        sys.stdout.flush()
