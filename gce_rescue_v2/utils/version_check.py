"""
GCE Rescue - PyPI Version Check Utility

Asynchronous check for newer versions of gce-rescue on PyPI with caching
and opt-out mechanisms.
"""

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Any

from ..core.config import VERSION

logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/gce-rescue/json"
OPT_OUT_ENV_VAR = "GCE_RESCUE_DISABLE_VERSION_CHECK"
CACHE_TTL_SECONDS = 86400  # 24 hours


def get_cache_path() -> Path:
    """Return the path to the cached version check file."""
    custom_path = os.getenv("GCE_RESCUE_VERSION_CACHE_PATH")
    if custom_path:
        return Path(custom_path)
    return Path.home() / ".config" / "gce-rescue" / "version-check.json"


def is_version_check_disabled(args: Optional[Any] = None) -> bool:
    """Check if version checking is disabled via env var or command line args.

    Disabled when:
    1. Opt-out environment variable GCE_RESCUE_DISABLE_VERSION_CHECK is truthy
    2. --quiet flag is passed in args
    3. --format is set to non-text formats (json, yaml, csv, none, or value(...))
    """
    # Check opt-out environment variable
    env_val = os.getenv(OPT_OUT_ENV_VAR, "").strip().lower()
    if env_val and env_val not in ("0", "false", "no", "off"):
        return True

    if args:
        # Check --quiet flag
        if getattr(args, "quiet", False):
            return True

        # Check --format flag
        fmt = str(getattr(args, "format", "disable")).lower()
        if fmt in ("json", "yaml", "csv", "none") or fmt.startswith("value"):
            return True

    return False


def is_newer_version(latest_str: str, current_str: str = VERSION) -> bool:
    """Compare version strings to check if latest_str is newer than current_str."""
    if not latest_str or not current_str or latest_str == current_str:
        return False

    try:
        from packaging.version import Version
        return Version(latest_str) > Version(current_str)
    except Exception:
        pass

    # Fallback stdlib semver comparison
    try:
        def to_parts(val: str) -> list:
            clean_val = val.lstrip("v").split("-")[0]
            parts = []
            for component in clean_val.split("."):
                num_str = ""
                for char in component:
                    if char.isdigit():
                        num_str += char
                    else:
                        break
                parts.append(int(num_str) if num_str else 0)
            return parts

        return to_parts(latest_str) > to_parts(current_str)
    except Exception:
        return False


def get_cached_version(cache_path: Optional[Path] = None,
                       ttl: int = CACHE_TTL_SECONDS) -> Optional[str]:
    """Retrieve valid cached version if within TTL."""
    path = cache_path or get_cache_path()
    try:
        if not path.is_file():
            return None
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        last_checked = data.get("last_checked", 0)
        latest_version = data.get("latest_version")

        if (time.time() - float(last_checked) < ttl and
                isinstance(latest_version, str) and latest_version):
            return latest_version
    except Exception as e:
        logger.debug("Failed to read version check cache: %s", e)
    return None


def save_cached_version(version: str, cache_path: Optional[Path] = None) -> None:
    """Save latest version to cache file with current timestamp."""
    path = cache_path or get_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "latest_version": version,
            "last_checked": time.time(),
        }
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        logger.debug("Failed to write version check cache: %s", e)


def fetch_pypi_version(url: str = PYPI_URL, timeout: float = 3.0) -> Optional[str]:
    """Fetch the latest release version of gce-rescue from PyPI using urllib."""
    try:
        headers = {"User-Agent": f"gce-rescue-version-check/{VERSION}"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                version = data.get("info", {}).get("version")
                if isinstance(version, str) and version:
                    return version
    except Exception as e:
        # Silently ignore network failures, PyPI downtime, timeouts, and parse errors
        logger.debug("Failed to fetch version from PyPI: %s", e)
    return None


def get_latest_version(cache_path: Optional[Path] = None) -> Optional[str]:
    """Get the latest version from cache or PyPI."""
    path = cache_path or get_cache_path()
    cached = get_cached_version(path)
    if cached is not None:
        return cached

    fetched = fetch_pypi_version()
    if fetched is not None:
        save_cached_version(fetched, path)
        return fetched

    return None


class VersionChecker:
    """Background version checking to eliminate perceived latency."""

    def __init__(self, args: Optional[Any] = None,
                 cache_path: Optional[Path] = None):
        self.args = args
        self.cache_path = cache_path or get_cache_path()
        self.result: Optional[str] = None
        self.thread: Optional[threading.Thread] = None
        self.started = False

    def start(self) -> None:
        """Start the background check thread if checking is enabled."""
        if is_version_check_disabled(self.args):
            return
        self.started = True
        self.thread = threading.Thread(target=self._run_check, daemon=True)
        self.thread.start()

    def _run_check(self) -> None:
        """Background worker method."""
        try:
            self.result = get_latest_version(self.cache_path)
        except Exception as e:
            logger.debug("Unhandled exception in version check thread: %s", e)
            self.result = None

    def display_notice(self, stream: Optional[Any] = None) -> None:
        """Display an upgrade notice if a newer version is found."""
        if not self.started or is_version_check_disabled(self.args):
            return

        if stream is None:
            stream = sys.stderr

        if self.thread and self.thread.is_alive():
            # Join with a short timeout to prevent delaying CLI termination
            self.thread.join(timeout=1.0)

        if not self.result:
            return

        if is_newer_version(self.result, VERSION):
            print(
                f"\nA new version of gce-rescue ({self.result}) is available."
                f" You are running {VERSION}.\n"
                "To upgrade: pip install --upgrade gce-rescue",
                file=stream
            )
