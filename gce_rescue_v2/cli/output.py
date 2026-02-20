"""Output formatting and progress indicators for CLI."""

import json
import sys
import threading
import time
import yaml
from typing import Dict, Any


class OutputFormatter:
    """
    Handle output formatting similar to gcloud.

    Supports: json, yaml, table
    """

    @staticmethod
    def format_output(data: Dict[str, Any], format_type: str = 'table'):
        """Format output based on format type."""
        if format_type == 'json':
            return json.dumps(data, indent=2)
        elif format_type == 'yaml':
            return yaml.dump(data, default_flow_style=False)
        elif format_type == 'table':
            return OutputFormatter._format_table(data)
        elif format_type == 'csv':
            return OutputFormatter._format_csv(data)
        elif format_type.startswith('value('):
            # Extract specific field: value(vmName)
            field = format_type[6:-1]
            return str(data.get(field, ''))
        else:
            return str(data)

    @staticmethod
    def _format_table(data: Dict[str, Any]) -> str:
        """Format as table (ASCII-safe for Windows compatibility)."""
        lines = []
        lines.append("+-" + "-" * 50 + "-+")
        for key, value in data.items():
            lines.append(f"| {key:20} | {str(value):27} |")
        lines.append("+-" + "-" * 50 + "-+")
        return "\n".join(lines)

    @staticmethod
    def _format_csv(data: Dict[str, Any]) -> str:
        """Format as CSV."""
        keys = ",".join(data.keys())
        values = ",".join(str(v) for v in data.values())
        return f"{keys}\n{values}"


class _Spinner:
    """Simple inline spinner for short-lived operations."""

    def __init__(self, message: str):
        self._message = message
        self._stop = False
        self._thread = None

    def start(self):
        self._stop = False
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self, clear: bool = True):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=0.5)
        if clear:
            sys.stdout.write(f"\r{' ' * (len(self._message) + 10)}\r")
            sys.stdout.flush()

    def _spin(self):
        dots = ['.  ', '.. ', '...']
        idx = 0
        while not self._stop:
            sys.stdout.write(f"\r{self._message}{dots[idx]}")
            sys.stdout.flush()
            idx = (idx + 1) % len(dots)
            time.sleep(0.4)


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string (e.g. '1m 42s')."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    secs = total % 60
    if secs == 0:
        return f"{minutes}m"
    return f"{minutes}m {secs}s"
