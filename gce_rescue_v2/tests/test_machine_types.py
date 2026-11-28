"""Unit tests for the oom repair machine type sizing."""

import pytest

from gce_rescue_v2.core.machine_types import (
    machine_type_name, next_larger_machine_type,
)


def _mt(name, memory_mb, cpus=2, deprecated=None):
    item = {'name': name, 'memoryMb': memory_mb, 'guestCpus': cpus}
    if deprecated:
        item['deprecated'] = {'state': deprecated}
    return item


ZONE_TYPES = [
    _mt('e2-micro', 1024),
    _mt('e2-small', 2048),
    _mt('e2-medium', 4096),
    _mt('e2-standard-2', 8192),
    _mt('e2-standard-4', 16384, 4),
    _mt('e2-standard-32', 131072, 32),
    _mt('e2-highmem-2', 16384),
    _mt('e2-highcpu-2', 2048),
    _mt('e2-highcpu-4', 4096, 4),
    _mt('f1-micro', 614, 1),
    _mt('g1-small', 1740, 1),
    _mt('n1-standard-1', 3840, 1),
    _mt('n1-standard-2', 7680),
    _mt('n2-standard-2', 8192),
    _mt('n2-highmem-4', 32768, 4),
    _mt('n2-highmem-8', 65536, 8),
    _mt('c3-standard-4', 16384, 4),
    _mt('c3-standard-4-lssd', 16384, 4),
    _mt('c3-standard-8', 32768, 8),
    _mt('c3-standard-8-lssd', 32768, 8),
]


def _next(current, types=ZONE_TYPES):
    result = next_larger_machine_type(current, types)
    return result['name'] if result else None


class TestNextLargerMachineType:

    @pytest.mark.parametrize('current,expected', [
        ('e2-standard-2', 'e2-standard-4'),
        ('n2-highmem-4', 'n2-highmem-8'),
        ('e2-highcpu-2', 'e2-highcpu-4'),
        ('c3-standard-4', 'c3-standard-8'),
        ('c3-standard-4-lssd', 'c3-standard-8-lssd'),
    ])
    def test_same_series_and_shape(self, current, expected):
        assert _next(current) == expected

    @pytest.mark.parametrize('current,expected', [
        ('e2-micro', 'e2-small'),
        ('e2-small', 'e2-medium'),
        ('e2-medium', 'e2-standard-2'),
        ('f1-micro', 'g1-small'),
        ('g1-small', 'n1-standard-1'),
    ])
    def test_shared_core_moves_up_then_to_standard(self, current, expected):
        assert _next(current) == expected

    def test_shared_core_skips_highcpu_and_highmem(self):
        types = [mt for mt in ZONE_TYPES if mt['name'] != 'e2-medium']
        assert _next('e2-small', types) == 'e2-standard-2'

    def test_accepts_machine_type_url(self):
        url = ('https://www.googleapis.com/compute/v1/projects/p/zones/'
               'us-central1-a/machineTypes/e2-small')
        assert _next(url) == 'e2-medium'

    def test_largest_size_has_no_proposal(self):
        assert _next('e2-standard-32') is None
        assert _next('n2-highmem-8') is None

    def test_custom_type_has_no_proposal(self):
        types = ZONE_TYPES + [_mt('n2-custom-4-8192', 8192, 4)]
        assert _next('n2-custom-4-8192', types) is None

    def test_type_missing_from_zone_has_no_proposal(self):
        assert _next('n4-standard-2') is None

    def test_deprecated_types_are_skipped(self):
        types = [mt for mt in ZONE_TYPES if mt['name'] != 'e2-medium']
        types.append(_mt('e2-medium', 4096, deprecated='DEPRECATED'))
        assert _next('e2-small', types) == 'e2-standard-2'

    def test_returns_the_machine_type_item(self):
        result = next_larger_machine_type('e2-small', ZONE_TYPES)
        assert result == _mt('e2-medium', 4096)


class TestMachineTypeHelpers:

    def test_machine_type_name(self):
        assert machine_type_name('zones/us-central1-a/machineTypes/e2-micro') == 'e2-micro'
        assert machine_type_name('e2-micro') == 'e2-micro'
