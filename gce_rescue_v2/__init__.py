"""GCE Rescue V2 - Clean implementation of GCE VM rescue operations.

This is a complete rewrite of the original gce-rescue tool with focus on:
- Simplicity: Clear, readable code
- Reliability: Proper error handling
- Maintainability: Easy to understand and modify
- Testability: Pure functions, easy to test

Core functionality:
- Enter rescue mode: Boot VM with rescue OS, mount original disk
- Exit rescue mode: Restore original boot disk, delete rescue disk

Example usage:
    >>> from gce_rescue_v2 import enter_rescue_mode, exit_rescue_mode
    >>> enter_rescue_mode('my-project', 'us-central1-a', 'my-vm')
    >>> exit_rescue_mode('my-project', 'us-central1-a', 'my-vm')
"""

from core.config import VERSION

__version__ = VERSION
__author__ = "GCE Rescue Team"

# Main public API will be added as we build each module
# from .rescue import enter_rescue_mode, exit_rescue_mode, get_rescue_status
# from .vm import get_vm_info
# from .auth import get_compute_client

__all__ = []  # Will populate as we add functions
