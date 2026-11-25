"""Make untrusted text safe to print to a terminal.

VM metadata and serial console output can be written by people other
than the operator running gce-rescue. Printing that text as-is would let
it carry terminal control sequences (cursor moves, screen clears, title
or hyperlink escapes) into the operator's terminal. These helpers replace
every control character with a visible escape such as ``\\x1b``, so the
text stays readable but can no longer drive the terminal.
"""

import re
from typing import Any, Optional

# C0 controls, DEL, C1 controls, and the Unicode bidirectional marks and
# overrides that can make text display in a different order than stored.
_BIDI = '؜‎‏‪-‮⁦-⁩'
_UNSAFE = re.compile(f'[\x00-\x1f\x7f-\x9f{_BIDI}]')
_UNSAFE_KEEP_TABS = re.compile(f'[\x00-\x08\x0a-\x1f\x7f-\x9f{_BIDI}]')
_PARTIAL_ESCAPE = re.compile(r'\\(?:x[0-9a-f]?|u[0-9a-f]{0,3})?$')


def _escape(match: 're.Match[str]') -> str:
    code = ord(match.group(0))
    if code <= 0xff:
        return f'\\x{code:02x}'
    return f'\\u{code:04x}'


def safe_text(value: Any, max_len: Optional[int] = None,
              keep_tabs: bool = False) -> str:
    """Return ``value`` as a single line with control characters escaped.

    Args:
        value: Text (or any value, converted with ``str``) from an
            untrusted source.
        max_len: If set, cut the result to this many characters and add
            ``...``. An escape is never cut in half.
        keep_tabs: Leave tab characters as they are.

    Returns:
        str: Text that is safe to print. Newlines are escaped too, so the
        result is always one line.
    """
    text = '' if value is None else str(value)
    pattern = _UNSAFE_KEEP_TABS if keep_tabs else _UNSAFE
    text = pattern.sub(_escape, text)
    if max_len is not None and len(text) > max_len:
        text = text[:max_len]
        partial = _PARTIAL_ESCAPE.search(text)
        if partial:
            text = text[:partial.start()]
        text += '...'
    return text
