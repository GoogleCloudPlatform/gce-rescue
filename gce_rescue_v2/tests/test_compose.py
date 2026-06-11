"""Tests for the startup-script composer (orchestration/compose.py)."""

from gce_rescue_v2.orchestration.compose import (
    RESCUE_COMPLETE_MARKER,
    compose_startup_script,
    strip_shebang,
)


# Minimal stand-in for rescue_mount.sh: mount logic + completion marker
BASE_SCRIPT = (
    'log "mounting disk"\n'
    'mount /dev/sdb1 /mnt/sysroot\n'
    f'echo "{RESCUE_COMPLETE_MARKER}" >&2\n'
    'log "startup done"\n'
)


class TestStripShebang:
    """strip_shebang removes a leading shebang line only."""

    def test_strips_bash_shebang(self):
        assert strip_shebang('#!/bin/bash\necho hi\n') == 'echo hi\n'

    def test_strips_sh_shebang(self):
        assert strip_shebang('#!/bin/sh\necho hi\n') == 'echo hi\n'

    def test_no_shebang_unchanged(self):
        assert strip_shebang('echo hi\n') == 'echo hi\n'

    def test_shebang_only_returns_empty(self):
        assert strip_shebang('#!/bin/bash') == ''

    def test_comment_not_stripped(self):
        """A normal comment line is not a shebang."""
        script = '# not a shebang\necho hi\n'
        assert strip_shebang(script) == script


class TestComposeStartupScript:
    """compose_startup_script combines mount + fix with marker relocation."""

    def test_marker_relocated_after_fix(self):
        """The active completion marker must come AFTER the fix body."""
        fix = 'sed -i "s/bad/good/" /mnt/sysroot/etc/fstab'
        script = compose_startup_script(BASE_SCRIPT, [fix], repair_targets=None)
        marker_pos = script.rindex(f'echo "{RESCUE_COMPLETE_MARKER}"')
        assert marker_pos > script.index(fix), \
            "completion marker must fire only after the fix has run"

    def test_original_marker_commented(self):
        """The base script's marker line is replaced with a comment."""
        script = compose_startup_script(BASE_SCRIPT, ['fix'], repair_targets=None)
        assert 'marker moved to end' in script
        # Exactly one ACTIVE echo of the marker remains (the relocated one)
        assert script.count(f'echo "{RESCUE_COMPLETE_MARKER}"') == 1

    def test_mount_logic_preserved(self):
        """Base mount logic is kept intact ahead of the fix."""
        fix = 'fix body'
        script = compose_startup_script(BASE_SCRIPT, [fix], repair_targets=None)
        assert 'mount /dev/sdb1 /mnt/sysroot' in script
        assert script.index('mount /dev/sdb1') < script.index(fix)

    def test_none_targets_omits_repair_targets(self):
        """Custom (self-contained) scripts get no REPAIR_TARGETS variable."""
        script = compose_startup_script(BASE_SCRIPT, ['fix'], repair_targets=None)
        assert 'REPAIR_TARGETS' not in script

    def test_empty_targets_injects_empty_variable(self):
        """Diagnosis mode with no extracted targets injects REPAIR_TARGETS=""."""
        script = compose_startup_script(BASE_SCRIPT, ['fix'], repair_targets=[])
        assert 'REPAIR_TARGETS=""' in script

    def test_targets_injected_newline_separated(self):
        """Diagnosis-extracted targets are newline-joined into the variable."""
        script = compose_startup_script(
            BASE_SCRIPT, ['fix'], repair_targets=['uuid-1', '/dev/sdb1']
        )
        assert 'REPAIR_TARGETS="uuid-1\n/dev/sdb1"' in script

    def test_multiple_fix_scripts_in_order(self):
        """Multiple fix bodies are appended in the given order."""
        script = compose_startup_script(
            BASE_SCRIPT, ['first fix', 'second fix'], repair_targets=[]
        )
        assert script.index('first fix') < script.index('second fix')
        # Marker still after the LAST fix
        assert script.rindex(f'echo "{RESCUE_COMPLETE_MARKER}"') > \
            script.index('second fix')
