"""Tests for org-policy image pre-flight (issue #122, Layer 1)."""

from unittest.mock import Mock

from gce_rescue_v2.cli import preflight


def _vm(os_type='linux', arch='x86_64'):
    """Minimal vm_info for OS/arch detection."""
    if os_type == 'windows':
        return {'disks': [{'boot': True,
                           'licenses': ['projects/windows-cloud/global/licenses/windows-server-2022']}]}
    disks = [{'boot': True, 'licenses': ['projects/debian-cloud/global/licenses/debian-12']}]
    vm = {'disks': disks}
    if arch == 'arm64':
        vm['disks'][0]['architecture'] = 'ARM64'
    return vm


class TestResolveRescueImageProject:
    def test_linux_default(self):
        assert preflight.resolve_rescue_image_project(_vm('linux')) == 'debian-cloud'

    def test_windows_default(self):
        assert preflight.resolve_rescue_image_project(_vm('windows')) == 'windows-cloud'

    def test_custom_image_url_parsed(self):
        url = 'projects/my-proj/global/images/family/my-fam'
        assert preflight.resolve_rescue_image_project(_vm(), rescue_image_url=url) == 'my-proj'

    def test_unparseable_url_returns_empty(self):
        assert preflight.resolve_rescue_image_project(_vm(), rescue_image_url='garbage') == ''


class TestCheckImageOrgPolicy:
    def _patch_policy(self, monkeypatch, policy):
        """Make the policy fetch return `policy` (None simulates unreadable)."""
        monkeypatch.setattr(preflight, '_fetch_trusted_image_policy',
                            lambda compute, project: policy)

    def test_blocked_when_not_in_allowed(self, monkeypatch):
        self._patch_policy(monkeypatch, {'listPolicy': {'allowedValues': ['projects/gokulr']}})
        err = preflight.check_image_org_policy(Mock(), 'gokulr', 'debian-cloud', command='rescue')
        assert err is not None
        assert '--rescue-image' in err
        assert 'debian-cloud' in err

    def test_example_uses_first_allowed_project(self, monkeypatch):
        # The suggested --rescue-image example should reference a real allowed
        # project, not the generic PROJECT placeholder.
        self._patch_policy(monkeypatch, {'listPolicy': {'allowedValues': ['projects/my-approved']}})
        err = preflight.check_image_org_policy(Mock(), 'gokulr', 'debian-cloud')
        assert 'projects/my-approved/global/images/IMAGE' in err

    def test_allowed_when_in_allowed(self, monkeypatch):
        self._patch_policy(monkeypatch, {'listPolicy': {'allowedValues': ['projects/debian-cloud']}})
        assert preflight.check_image_org_policy(Mock(), 'gokulr', 'debian-cloud') is None

    def test_all_values_allow(self, monkeypatch):
        self._patch_policy(monkeypatch, {'listPolicy': {'allValues': 'ALLOW'}})
        assert preflight.check_image_org_policy(Mock(), 'gokulr', 'debian-cloud') is None

    def test_denied_values(self, monkeypatch):
        self._patch_policy(monkeypatch, {'listPolicy': {'deniedValues': ['projects/debian-cloud']}})
        err = preflight.check_image_org_policy(Mock(), 'gokulr', 'debian-cloud')
        assert err is not None

    def test_fail_open_on_read_error(self, monkeypatch):
        # _fetch returns None when the policy can't be read -> must NOT block
        self._patch_policy(monkeypatch, None)
        assert preflight.check_image_org_policy(Mock(), 'gokulr', 'debian-cloud') is None

    def test_empty_image_project_skips(self):
        assert preflight.check_image_org_policy(Mock(), 'gokulr', '') is None
