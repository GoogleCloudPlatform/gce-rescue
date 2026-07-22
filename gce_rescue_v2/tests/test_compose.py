"""Tests for the startup-script composer (orchestration/compose.py)."""

import pytest

from gce_rescue_v2.orchestration.compose import (
    PREMOUNT_ANCHOR,
    PREMOUNT_BEGIN_MARKER,
    PREMOUNT_END_MARKER,
    RESCUE_COMPLETE_MARKER,
    compose_startup_script,
    compose_startup_script_windows,
    extract_premount_blocks,
    strip_shebang,
)


# Minimal stand-in for rescue_mount.sh: mount logic + completion call.
# The real script calls signal_complete (emits the serial marker AND sets the
# completion guest attribute); compose relocates that call after the fixes.
BASE_SCRIPT = (
    'log "mounting disk"\n'
    'mount /dev/sdb1 /mnt/sysroot\n'
    'signal_complete\n'
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
        """The active completion signal must come AFTER the fix body."""
        fix = 'sed -i "s/bad/good/" /mnt/sysroot/etc/fstab'
        script = compose_startup_script(BASE_SCRIPT, [fix], repair_targets=None)
        signal_pos = script.rindex('signal_complete')
        assert signal_pos > script.index(fix), \
            "completion signal must fire only after the fix has run"

    def test_original_marker_commented(self):
        """The base script's completion call is replaced with a comment."""
        script = compose_startup_script(BASE_SCRIPT, ['fix'], repair_targets=None)
        assert 'moved to end' in script
        # Exactly one ACTIVE completion call remains (the relocated one)
        assert script.count('signal_complete') == 1

    def test_mount_logic_preserved(self):
        """Base mount logic is kept intact ahead of the fix."""
        fix = 'fix body'
        script = compose_startup_script(BASE_SCRIPT, [fix], repair_targets=None)
        assert 'mount /dev/sdb1 /mnt/sysroot' in script
        assert script.index('mount /dev/sdb1') < script.index(fix)

    def test_trailing_success_log_relocated_with_signal(self):
        """The base script's 'completed successfully' log must not print
        before the fixes run (it moves to the end with the signal)."""
        base = (
            'log "mounting disk"\n'
            'signal_complete\n'
            'log "=== Startup script completed successfully ==="\n'
        )
        fix = 'fix body'
        script = compose_startup_script(base, [fix], repair_targets=None)
        # The only ACTIVE success log (compose appends its own) comes after
        # the fix body; the base's copy is neutralized into a comment.
        active = script.rindex('log "=== Startup script completed successfully ==="')
        assert active > script.index(fix)
        assert script.count('log "=== Startup script completed successfully ==="') == 1

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
        # Completion signal still after the LAST fix
        assert script.rindex('signal_complete') > script.index('second fix')


class TestRepairLogArchival:
    """compose appends a one-time snippet that archives the previous session's
    on-disk repair log before this session's fixes overwrite it."""

    def test_snippet_present_exactly_once(self):
        """One archival snippet per session, regardless of fix count."""
        script = compose_startup_script(
            BASE_SCRIPT, ['fix one', 'fix two'], repair_targets=[]
        )
        assert script.count('gce-repair-history.log') == 1

    def test_snippet_after_mount_before_first_fix(self):
        """Archival must run after the mount but before any fix can
        overwrite /var/log/gce-repair.log on the customer disk."""
        script = compose_startup_script(
            BASE_SCRIPT, ['first fix body'], repair_targets=[]
        )
        archive_pos = script.index('gce-repair-history.log')
        assert script.index('mount /dev/sdb1') < archive_pos
        assert archive_pos < script.index('first fix body')

    def test_snippet_guarded_against_unmounted_sysroot(self):
        """Unmounted sysroot or missing log must be a harmless no-op."""
        script = compose_startup_script(BASE_SCRIPT, ['fix'], repair_targets=[])
        assert 'mountpoint -q /mnt/sysroot' in script
        assert '[ -f /mnt/sysroot/var/log/gce-repair.log ]' in script
        assert '|| true' in script

    def test_snippet_absent_from_windows_composition(self):
        """The Linux log-archival snippet must not leak into PowerShell."""
        script = compose_startup_script_windows(BASE_SCRIPT_WINDOWS, ['fix'])
        assert 'gce-repair-history.log' not in script


# Stand-in for rescue_mount.sh including the pre-mount anchor line: the
# disk-wait loop has finished ($disk set, device present) but nothing is
# mounted yet when the anchor line runs.
BASE_SCRIPT_WITH_ANCHOR = (
    'log "Waiting for disk to appear..."\n'
    'disk=my-disk\n'
    f'{PREMOUNT_ANCHOR}\n'
    'mount /dev/sdb1 /mnt/sysroot\n'
    'signal_complete\n'
    'log "startup done"\n'
)


def _premount_fix(premount_body: str, post_body: str) -> str:
    """Build a fix script with a pre-mount block followed by a normal body."""
    return (
        f'{PREMOUNT_BEGIN_MARKER}\n'
        f'{premount_body}\n'
        f'{PREMOUNT_END_MARKER}\n'
        f'{post_body}\n'
    )


class TestExtractPremountBlocks:
    """extract_premount_blocks splits pre-mount blocks from the remainder."""

    def test_no_block_returns_script_unchanged(self):
        script = 'log "fixing"\nfsck_stuff\n'
        blocks, remainder = extract_premount_blocks(script)
        assert blocks == []
        assert remainder == script

    def test_single_block_extracted(self):
        script = _premount_fix('fsck -y "$dev"', 'log "post-mount fix"')
        blocks, remainder = extract_premount_blocks(script)
        assert blocks == ['fsck -y "$dev"']
        assert 'log "post-mount fix"' in remainder
        # Marker lines and block body are removed from the remainder
        assert PREMOUNT_BEGIN_MARKER not in remainder
        assert PREMOUNT_END_MARKER not in remainder
        assert 'fsck -y' not in remainder

    def test_multiple_blocks_in_one_script(self):
        script = (
            _premount_fix('first block', 'middle body')
            + _premount_fix('second block', 'tail body')
        )
        blocks, remainder = extract_premount_blocks(script)
        assert blocks == ['first block', 'second block']
        assert 'middle body' in remainder
        assert 'tail body' in remainder

    def test_indented_markers_recognized(self):
        """Marker lines are matched ignoring surrounding whitespace."""
        script = (
            f'    {PREMOUNT_BEGIN_MARKER}\n'
            'fsck_line\n'
            f'    {PREMOUNT_END_MARKER}\n'
            'body\n'
        )
        blocks, remainder = extract_premount_blocks(script)
        assert blocks == ['fsck_line']
        assert 'body' in remainder

    def test_unterminated_block_raises(self):
        script = f'{PREMOUNT_BEGIN_MARKER}\nfsck_line\nbody\n'
        with pytest.raises(ValueError, match='without a closing end marker'):
            extract_premount_blocks(script)

    def test_end_without_begin_raises(self):
        script = f'body\n{PREMOUNT_END_MARKER}\n'
        with pytest.raises(ValueError, match='without a preceding begin'):
            extract_premount_blocks(script)


class TestComposePremountInjection:
    """compose_startup_script injects pre-mount blocks before the mount."""

    def test_premount_block_injected_before_mount(self):
        """The block must run before the anchor line (and thus the mount)."""
        fix = _premount_fix('fsck -y /dev/sdb1', 'log "editing fstab"')
        script = compose_startup_script(
            BASE_SCRIPT_WITH_ANCHOR, [fix], repair_targets=[]
        )
        assert script.index('fsck -y /dev/sdb1') < script.index(PREMOUNT_ANCHOR)
        assert script.index(PREMOUNT_ANCHOR) < script.index('mount /dev/sdb1')

    def test_premount_block_runs_after_disk_wait(self):
        """The block is injected after the disk-wait loop, not before it."""
        fix = _premount_fix('fsck -y /dev/sdb1', 'post body')
        script = compose_startup_script(
            BASE_SCRIPT_WITH_ANCHOR, [fix], repair_targets=[]
        )
        assert script.index('Waiting for disk to appear') < \
            script.index('fsck -y /dev/sdb1')

    def test_remainder_appended_at_end(self):
        """Everything outside the block stays in the fix section at the end."""
        fix = _premount_fix('fsck -y /dev/sdb1', 'log "editing fstab"')
        script = compose_startup_script(
            BASE_SCRIPT_WITH_ANCHOR, [fix], repair_targets=[]
        )
        assert script.index('mount /dev/sdb1') < \
            script.index('log "editing fstab"')
        # Pre-mount body must not be duplicated into the fix section
        assert script.count('fsck -y /dev/sdb1') == 1
        # Marker lines are consumed by extraction
        assert PREMOUNT_BEGIN_MARKER not in script
        assert PREMOUNT_END_MARKER not in script

    def test_no_block_behavior_unchanged(self):
        """A fix script without a block composes exactly as before."""
        script = compose_startup_script(
            BASE_SCRIPT_WITH_ANCHOR, ['plain fix body'], repair_targets=[]
        )
        assert 'Pre-Mount Fixes' not in script
        assert script.index(PREMOUNT_ANCHOR) < script.index('plain fix body')

    def test_no_block_with_anchorless_base_still_composes(self):
        """Scripts without blocks must not require the anchor (old base)."""
        script = compose_startup_script(BASE_SCRIPT, ['fix'], repair_targets=[])
        assert 'fix' in script

    def test_multiple_scripts_blocks_injected_in_order(self):
        """Blocks from multiple fix scripts are injected in script order."""
        fs_fix = _premount_fix('fsck part', 'fs post body')
        fstab_fix = 'fstab body only'
        script = compose_startup_script(
            BASE_SCRIPT_WITH_ANCHOR, [fs_fix, fstab_fix], repair_targets=[]
        )
        anchor_pos = script.index(PREMOUNT_ANCHOR)
        assert script.index('fsck part') < anchor_pos
        # Post-mount bodies keep their order in the fix section
        assert script.index('fs post body') > anchor_pos
        assert script.index('fs post body') < script.index('fstab body only')

    def test_blocks_from_two_scripts_ordered(self):
        first = _premount_fix('block one', 'body one')
        second = _premount_fix('block two', 'body two')
        script = compose_startup_script(
            BASE_SCRIPT_WITH_ANCHOR, [first, second], repair_targets=[]
        )
        assert script.index('block one') < script.index('block two')
        assert script.index('block two') < script.index(PREMOUNT_ANCHOR)

    def test_anchor_missing_raises(self):
        """A block with no anchor in the base script must fail loudly."""
        fix = _premount_fix('fsck -y /dev/sdb1', 'post body')
        with pytest.raises(ValueError, match='anchor line'):
            compose_startup_script(BASE_SCRIPT, [fix], repair_targets=[])

    def test_completion_signal_still_last(self):
        """Marker relocation is unaffected by pre-mount injection."""
        fix = _premount_fix('fsck -y /dev/sdb1', 'post body')
        script = compose_startup_script(
            BASE_SCRIPT_WITH_ANCHOR, [fix], repair_targets=[]
        )
        assert script.rindex('signal_complete') > script.index('post body')
        assert script.count('signal_complete') == 1


# Minimal stand-in for rescue_mount_windows.ps1: mount + marker + trailing
# content (RDP credentials) that must be preserved after composition.
BASE_SCRIPT_WINDOWS = (
    'Write-Log "mounting disk"\n'
    'Set-Partition -NewDriveLetter D\n'
    f'Write-Log "{RESCUE_COMPLETE_MARKER}"\n'
    'Write-Log "RDP CONNECTION CREDENTIALS"\n'
    'Write-Log "Password: hunter2"\n'
)


class TestComposeStartupScriptWindows:
    """Windows composition: fix inserted BEFORE the marker, tail preserved."""

    def test_fix_inserted_before_marker(self):
        fix = 'Remove-Item D:\\Windows\\bad-driver.sys'
        script = compose_startup_script_windows(BASE_SCRIPT_WINDOWS, [fix])
        marker_pos = script.index(f'Write-Log "{RESCUE_COMPLETE_MARKER}"')
        assert script.index(fix) < marker_pos

    def test_mount_logic_runs_before_fix(self):
        fix = 'Write-Log "fixing"'
        script = compose_startup_script_windows(BASE_SCRIPT_WINDOWS, [fix])
        assert script.index('Set-Partition') < script.index(fix)

    def test_post_marker_content_preserved(self):
        """RDP credentials etc. after the marker must survive composition."""
        script = compose_startup_script_windows(BASE_SCRIPT_WINDOWS, ['fix'])
        marker_pos = script.index(f'Write-Log "{RESCUE_COMPLETE_MARKER}"')
        assert 'RDP CONNECTION CREDENTIALS' in script
        assert script.index('RDP CONNECTION CREDENTIALS') > marker_pos

    def test_single_marker_emission(self):
        """The marker is not duplicated by composition."""
        script = compose_startup_script_windows(BASE_SCRIPT_WINDOWS, ['fix'])
        assert script.count(f'Write-Log "{RESCUE_COMPLETE_MARKER}"') == 1

    def test_marker_missing_falls_back_to_append(self):
        """If the base has no marker, fixes + marker are appended at the end."""
        base = 'Write-Log "mount only"\n'
        script = compose_startup_script_windows(base, ['fix body'])
        assert 'fix body' in script
        assert script.rindex(f'Write-Log "{RESCUE_COMPLETE_MARKER}"') > \
            script.index('fix body')
