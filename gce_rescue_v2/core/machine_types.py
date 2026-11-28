"""Machine type sizing for the oom repair.

Picks the next larger machine type for a VM that runs out of memory. The
candidates come from machineTypes.list for the VM's zone, so only types
that exist there are proposed.
"""

import re
from typing import Any, Dict, List, Optional

# f1-micro and g1-small are the shared-core types of the N1 series.
_SHARED_CORE_SERIES = {'f1': 'n1', 'g1': 'n1'}

_VCPU_COUNT_RE = re.compile(r'-\d+')


def machine_type_name(machine_type: str) -> str:
    """Return the short name of a machine type given as a name or a URL."""
    return machine_type.rstrip('/').split('/')[-1]


def _series(name: str) -> str:
    series = name.split('-', 1)[0]
    return _SHARED_CORE_SERIES.get(series, series)


def _shape(name: str) -> Optional[str]:
    """Return the name with its vCPU count masked, or None for shared-core.

    e2-standard-4 -> e2-standard-#, c3-standard-4-lssd -> c3-standard-#-lssd.
    Two types with the same shape only differ in size.
    """
    if not _VCPU_COUNT_RE.search(name):
        return None
    return _VCPU_COUNT_RE.sub('-#', name, count=1)


def _is_available(machine_type: Dict[str, Any]) -> bool:
    return not machine_type.get('deprecated', {}).get('state')


def next_larger_machine_type(
    current: str, machine_types: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Return the smallest machine type with more memory than the current one.

    The proposal stays in the same series and shape (e2-standard-2 ->
    e2-standard-4, n2-highmem-4 -> n2-highmem-8). A shared-core type moves
    to the next shared-core type, then to the smallest standard type of its
    series (e2-medium -> e2-standard-2, g1-small -> n1-standard-1).

    Args:
        current: Current machine type, as a name or a URL.
        machine_types: Items of machineTypes.list for the VM's zone.

    Returns:
        The proposed machineTypes item, or None for types missing from the
        list (custom types are never listed) and types already at the
        largest size.
    """
    name = machine_type_name(current)
    by_name = {mt.get('name'): mt for mt in machine_types}
    current_type = by_name.get(name)
    if current_type is None:
        return None

    series = _series(name)
    shape = _shape(name)

    def same_family(candidate: str) -> bool:
        if _series(candidate) != series:
            return False
        candidate_shape = _shape(candidate)
        if shape is not None:
            return candidate_shape == shape
        return (candidate_shape is None
                or candidate_shape == f'{series}-standard-#')

    candidates = [
        mt for mt in machine_types
        if same_family(mt.get('name', ''))
        and _is_available(mt)
        and mt.get('memoryMb', 0) > current_type.get('memoryMb', 0)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda mt: mt['memoryMb'])
