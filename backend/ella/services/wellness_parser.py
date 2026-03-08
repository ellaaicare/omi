"""
Parse wellness flags from conversation overview text.

The caregiver-enhanced structured summary prompt appends a [WELLNESS_FLAGS]
block to the overview field. This parser extracts it into a structured dict.
"""

import re
from typing import Optional, Dict, Any


WELLNESS_PATTERN = re.compile(
    r'\[WELLNESS_FLAGS\]\s*\n'
    r'cognitive:\s*(true|false)\s*\n'
    r'medication:\s*(true|false)\s*\n'
    r'emotional:\s*(true|false)\s*\n'
    r'physical:\s*(true|false)\s*\n'
    r'social:\s*(true|false)\s*\n'
    r'routine:\s*(true|false)\s*\n'
    r'details:\s*(.+?)\s*\n'
    r'urgency:\s*(low|medium|high|critical)\s*\n'
    r'\[/WELLNESS_FLAGS\]',
    re.DOTALL
)


def parse_wellness_flags(overview: str) -> Optional[Dict[str, Any]]:
    """Extract wellness flags from overview text."""
    if not overview or '[WELLNESS_FLAGS]' not in overview:
        return None

    match = WELLNESS_PATTERN.search(overview)
    if not match:
        return None

    return {
        'cognitive': match.group(1) == 'true',
        'medication': match.group(2) == 'true',
        'emotional': match.group(3) == 'true',
        'physical': match.group(4) == 'true',
        'social': match.group(5) == 'true',
        'routine': match.group(6) == 'true',
        'details': match.group(7).strip(),
        'urgency': match.group(8),
        'has_flags': True,
    }


def strip_wellness_flags(overview: str) -> str:
    """Remove wellness flags block from overview text."""
    if not overview or '[WELLNESS_FLAGS]' not in overview:
        return overview
    return WELLNESS_PATTERN.sub('', overview).strip()


def overview_has_flags(overview: str) -> bool:
    """Quick check if overview contains wellness flags."""
    return overview is not None and '[WELLNESS_FLAGS]' in overview
