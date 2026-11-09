"""Tests for the shared chroot helper used by fix scripts (#210).

Covers:
  - startup_scripts/fixes/lib/chroot.sh, run in bash against a fake root
    filesystem: mount, umount, mountpoint, chroot and timeout are replaced by
    shell functions that record what they were asked to do.
  - compose.py: fix scripts that declare "# gce-rescue-requires: chroot" get
    the library inlined once, before the fix bodies; others get nothing.
  - Packaging: the library ships in the wheel.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gce_rescue_v2.orchestration import compose
from gce_rescue_v2.orchestration.compose import (
    compose_startup_script, load_fix_lib, required_fix_libs,
)

FIXES = Path(__file__).parent.parent / 'startup_scripts' / 'fixes'
LIB = FIXES / 'lib' / 'chroot.sh'
HEADER = ('# gce-rescue: temporary DNS settings used during repair; '
          'the original is restored afterwards')

needs_bash = pytest.mark.skipif(
    sys.platform != 'linux' or shutil.which('bash') is None,
    reason='needs Linux bash')


# ===========================================================================
# chroot.sh: static checks
# ===========================================================================

class TestLibStatic:

    def test_exists(self):
        assert LIB.is_file()

    @needs_bash
    def test_bash_syntax(self):
        subprocess.run(['bash', '-n', str(LIB)], check=True)

    def test_functions_are_namespaced(self):
        names = re.findall(r'^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{', LIB.read_text(),
                           re.MULTILINE)
        assert names
        for name in names:
            assert name.startswith('chroot_'), name

    def test_never_calls_exit(self):
        for line in LIB.read_text().splitlines():
            code = line.split('#', 1)[0]
            assert not re.search(r'(^|[;&|{]\s*)exit\b', code.strip()), line

    def test_globals_are_namespaced(self):
        for name in re.findall(r'^([A-Z_][A-Z0-9_]*)=', LIB.read_text(), re.MULTILINE):
            assert name.startswith('CHROOT_'), name

    def test_is_not_a_fix(self):
        """The library never reports a repair result itself."""
        text = LIB.read_text()
        assert 'GCE-REPAIR-RESULT' not in text
        assert 'repair_result' not in text


# ===========================================================================
# chroot.sh: behaviour against a fake root
# ===========================================================================

HARNESS = r'''
export LOGFILE="$WORK/log"
# The base mount script and every fix define log(); the library uses it.
log() { echo "$1" >> "$LOGFILE"; }
source "$LIB"
SYSROOT="$ROOT"
# Mount state lives in a file so subshells see it too.
: > "$WORK/mounted"
for m in $PREMOUNTED; do echo "$ROOT/$m" >> "$WORK/mounted"; done
mountpoint() {
    [ "$2" = "$ROOT" ] && return 0
    grep -qxF "$2" "$WORK/mounted"
}
mount() {
    echo "mount $*" >> "$WORK/cmds"
    [ -n "${FAKE_MOUNT_FAIL:-}" ] && return 1
    echo "${!#}" >> "$WORK/mounted"
}
umount() {
    echo "umount $*" >> "$WORK/cmds"
    local target="${!#}"
    grep -vxF "$target" "$WORK/mounted" > "$WORK/mounted.new"; mv "$WORK/mounted.new" "$WORK/mounted"
}
timeout() { echo "timeout $1" >> "$WORK/cmds"; shift; "$@"; }
# Fake chroot. It re-parses the exact "chroot ROOT /usr/bin/env -i VAR=..."
# shape that chroot_exec/chroot_run build; test_exec_passes_output_and_exit_code
# asserts that shape on purpose, so change both together.
chroot() {
    echo "chroot $*" >> "$WORK/cmds"
    local root="$1"; shift
    # Skip "/usr/bin/env -i VAR=... " to reach the command.
    [ "$1" = /usr/bin/env ] && shift 2
    while [ $# -gt 0 ] && [ "${1#*=}" != "$1" ]; do shift; done
    case "$1" in
        /bin/sh)
            local want="$5"
            case " $FAKE_TOOLS " in *" $want "*) return 0 ;; esac
            return 1 ;;
        fake-ok) echo "out:$2"; return 0 ;;
        fake-fail) echo "err" >&2; return 7 ;;
    esac
    return 0
}
'''


class FakeRoot:

    def __init__(self, tmp_path):
        self.root = tmp_path / 'root'
        self.work = tmp_path / 'work'
        self.work.mkdir()
        for d in ('etc', 'proc', 'sys', 'dev/pts', 'run'):
            (self.root / d).mkdir(parents=True)
        self.host_resolv = self.work / 'host-resolv.conf'
        self.host_resolv.write_text('nameserver 169.254.169.254\nsearch c.internal\n')

    @property
    def conf(self):
        return self.root / 'etc/resolv.conf'

    def install(self, tools):
        """Create empty executables named in `tools` in the target's /usr/bin."""
        (self.root / 'usr/bin').mkdir(parents=True, exist_ok=True)
        for tool in tools.split():
            (self.root / 'usr/bin' / tool).write_text('')

    @property
    def backup(self):
        return self.root / 'etc/resolv.conf.gce-rescue-chroot-backup'

    def run(self, body, premounted='proc sys dev', tools='', **env):
        full_env = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'LIB': str(LIB), 'ROOT': str(self.root), 'WORK': str(self.work),
            'PREMOUNTED': premounted, 'FAKE_TOOLS': tools,
            'CHROOT_HOST_RESOLV': str(self.host_resolv),
        }
        full_env.update({k: str(v) for k, v in env.items()})
        proc = subprocess.run(['bash', '-c', HARNESS + '\n' + body], env=full_env,
                              capture_output=True, text=True, timeout=60)
        return proc

    def cmds(self):
        f = self.work / 'cmds'
        return f.read_text().splitlines() if f.exists() else []

    def mounted(self):
        return [line for line in (self.work / 'mounted').read_text().splitlines() if line]


@needs_bash
class TestEnterLeaveMounts:

    def test_enter_binds_only_missing_mounts_in_order(self, tmp_path):
        r = FakeRoot(tmp_path)
        p = r.run('chroot_enter --no-dns; echo "rc=$?"; echo "M=$CHROOT_MOUNTED"')
        assert 'rc=0' in p.stdout
        mounts = [c for c in r.cmds() if c.startswith('mount ')]
        assert mounts == [f'mount -o bind /dev/pts {r.root}/dev/pts',
                          f'mount -o bind /run {r.root}/run']
        assert 'M=run dev/pts' in p.stdout

    def test_leave_unmounts_only_what_enter_mounted_in_reverse(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.run('chroot_enter --no-dns; chroot_leave')
        umounts = [c for c in r.cmds() if c.startswith('umount ')]
        assert umounts == [f'umount {r.root}/run', f'umount {r.root}/dev/pts']
        # The base script's proc/sys/dev mounts are left alone.
        assert r.mounted() == [f'{r.root}/proc', f'{r.root}/sys', f'{r.root}/dev']

    def test_enter_mounts_everything_when_nothing_is_mounted(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.run('chroot_enter --no-dns; chroot_leave', premounted='')
        mounts = [c.split()[-1] for c in r.cmds() if c.startswith('mount ')]
        umounts = [c.split()[-1] for c in r.cmds() if c.startswith('umount ')]
        expected = [f'{r.root}/{d}' for d in ('proc', 'sys', 'dev', 'dev/pts', 'run')]
        assert mounts == expected
        assert umounts == list(reversed(expected))

    def test_failed_bind_is_not_unmounted_later(self, tmp_path):
        r = FakeRoot(tmp_path)
        p = r.run('chroot_enter --no-dns; echo "rc=$?"; chroot_leave', FAKE_MOUNT_FAIL=1)
        assert 'rc=0' in p.stdout
        assert not [c for c in r.cmds() if c.startswith('umount ')]

    def test_enter_refuses_unmounted_root(self, tmp_path):
        r = FakeRoot(tmp_path)
        p = r.run('SYSROOT="$ROOT/elsewhere"; chroot_enter; echo "rc=$?"')
        assert 'rc=1' in p.stdout
        assert not r.cmds()

    def test_leave_without_enter_is_a_noop(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.conf.write_text('nameserver 10.0.0.2\n')
        p = r.run('chroot_leave; echo "rc=$?"')
        assert 'rc=0' in p.stdout
        assert not r.cmds()
        assert r.conf.read_text() == 'nameserver 10.0.0.2\n'

    def test_leave_twice_is_safe(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.run('chroot_enter; chroot_leave; chroot_leave')
        assert len([c for c in r.cmds() if c.startswith('umount ')]) == 2

    def test_skips_missing_mount_points(self, tmp_path):
        r = FakeRoot(tmp_path)
        shutil.rmtree(r.root / 'run')
        r.run('chroot_enter --no-dns')
        assert not [c for c in r.cmds() if c.endswith('/run')]

    def test_failed_bind_is_logged(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.run('chroot_enter --no-dns', FAKE_MOUNT_FAIL=1)
        assert 'WARNING: chroot: could not bind-mount /run' in (r.work / 'log').read_text()

    def test_enter_logs_what_it_did(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.run('chroot_enter; chroot_leave')
        log = (r.work / 'log').read_text()
        assert f'chroot: entered {r.root} (added mounts: run dev/pts; DNS: rescue OS)' in log
        (tmp_path / 'b').mkdir()
        r2 = FakeRoot(tmp_path / 'b')
        r2.run('chroot_enter --no-dns', premounted='proc sys dev dev/pts run')
        assert 'added mounts: none; DNS: unchanged' in (r2.work / 'log').read_text()


@needs_bash
class TestDns:

    def test_regular_file_replaced_then_restored_verbatim(self, tmp_path):
        r = FakeRoot(tmp_path)
        original = 'nameserver 10.1.2.3\noptions edns0\n'
        r.conf.write_text(original)
        p = r.run('chroot_enter; cat "$ROOT/etc/resolv.conf" > "$WORK/inside"; chroot_leave')
        inside = (r.work / 'inside').read_text()
        assert inside.splitlines()[0] == HEADER
        assert 'nameserver 169.254.169.254' in inside
        assert r.conf.read_text() == original
        assert not r.backup.exists()
        assert 'restored the original resolv.conf' in (r.work / 'log').read_text(), p.stderr

    def test_symlink_is_kept_as_symlink_and_never_followed(self, tmp_path):
        """systemd-resolved: resolv.conf -> ../run/systemd/resolve/stub-resolv.conf."""
        r = FakeRoot(tmp_path)
        stub = r.root / 'run/systemd/resolve/stub-resolv.conf'
        stub.parent.mkdir(parents=True)
        stub.write_text('nameserver 127.0.0.53\n')
        os.symlink('../run/systemd/resolve/stub-resolv.conf', r.conf)
        r.run('chroot_enter; [ -L "$ROOT/etc/resolv.conf" ] && echo LINK > "$WORK/inside"; chroot_leave')
        assert not (r.work / 'inside').exists()  # a regular file while inside
        assert stub.read_text() == 'nameserver 127.0.0.53\n'  # target untouched
        assert r.conf.is_symlink()
        assert os.readlink(r.conf) == '../run/systemd/resolve/stub-resolv.conf'
        assert not r.backup.exists() and not r.backup.is_symlink()

    def test_dangling_symlink_restored(self, tmp_path):
        r = FakeRoot(tmp_path)
        os.symlink('/run/does-not-exist', r.conf)
        r.run('chroot_enter; chroot_leave')
        assert r.conf.is_symlink() and os.readlink(r.conf) == '/run/does-not-exist'

    def test_absent_file_created_then_removed(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.run('chroot_enter; [ -f "$ROOT/etc/resolv.conf" ] && echo yes > "$WORK/inside"; chroot_leave')
        assert (r.work / 'inside').read_text().strip() == 'yes'
        assert not r.conf.exists() and not r.conf.is_symlink()

    def test_no_dns_leaves_resolv_conf_untouched(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.conf.write_text('nameserver 10.1.2.3\n')
        r.run('chroot_enter --no-dns; cat "$ROOT/etc/resolv.conf" > "$WORK/inside"; chroot_leave')
        assert (r.work / 'inside').read_text() == 'nameserver 10.1.2.3\n'
        assert r.conf.read_text() == 'nameserver 10.1.2.3\n'

    def test_interrupted_session_backup_is_the_original(self, tmp_path):
        """A crash left our copy plus the backup: the backup is restored."""
        r = FakeRoot(tmp_path)
        r.backup.write_text('nameserver 10.9.9.9\n')
        r.conf.write_text(HEADER + '\nnameserver 169.254.169.254\n')
        r.run('chroot_enter; chroot_leave')
        assert r.conf.read_text() == 'nameserver 10.9.9.9\n'
        assert not r.backup.exists()

    def test_interrupted_session_without_copy_restores_backup(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.backup.write_text('nameserver 10.9.9.9\n')
        r.run('chroot_enter; chroot_leave')
        assert r.conf.read_text() == 'nameserver 10.9.9.9\n'

    def test_stale_backup_is_kept_but_not_restored(self, tmp_path):
        """The system changed resolv.conf since: its current file wins."""
        r = FakeRoot(tmp_path)
        r.backup.write_text('nameserver 10.9.9.9\n')
        r.conf.write_text('nameserver 10.5.5.5\n')
        r.run('chroot_enter; chroot_leave')
        assert r.conf.read_text() == 'nameserver 10.5.5.5\n'
        stale = r.root / 'etc/resolv.conf.gce-rescue-chroot-backup.stale'
        assert stale.read_text() == 'nameserver 10.9.9.9\n'

    def test_stale_backup_never_overwritten_when_it_cannot_be_renamed(self, tmp_path):
        """If the old backup cannot move aside, DNS is left alone so the backup is never overwritten."""
        if os.geteuid() == 0:
            pytest.skip('root ignores directory permissions')
        r = FakeRoot(tmp_path)
        r.backup.write_text('nameserver 10.9.9.9\n')
        r.conf.write_text('nameserver 10.5.5.5\n')
        blocker = r.root / 'etc/resolv.conf.gce-rescue-chroot-backup.stale'
        blocker.mkdir()
        blocker.chmod(0o555)  # mv backup -> backup.stale fails; /etc stays writable
        try:
            r.run('chroot_enter; chroot_leave')
        finally:
            blocker.chmod(0o755)
        assert r.backup.read_text() == 'nameserver 10.9.9.9\n'
        assert r.conf.read_text() == 'nameserver 10.5.5.5\n'
        assert 'leaving DNS unchanged' in (r.work / 'log').read_text()

    def test_exit_trap_restores_dns_when_fix_never_leaves(self, tmp_path):
        """Script stopped (or a fix forgets chroot_leave): the EXIT trap restores resolv.conf."""
        r = FakeRoot(tmp_path)
        r.conf.write_text('nameserver 10.1.2.3\n')
        r.run('chroot_enter; head -1 "$ROOT/etc/resolv.conf" > "$WORK/inside"')
        assert (r.work / 'inside').read_text().strip() == HEADER
        assert r.conf.read_text() == 'nameserver 10.1.2.3\n'
        assert not r.backup.exists()

    def test_exit_trap_runs_on_sigterm(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.conf.write_text('nameserver 10.1.2.3\n')
        # No background job: an orphan holding stdout would make subprocess
        # wait for it and slow the suite down.
        r.run('chroot_enter; kill -TERM $$; echo not-reached')
        assert r.conf.read_text() == 'nameserver 10.1.2.3\n'

    def test_existing_exit_trap_is_never_replaced(self, tmp_path):
        """A custom --fix-script's own EXIT trap wins; the library only logs."""
        r = FakeRoot(tmp_path)
        p = r.run('trap "echo mine" EXIT; chroot_enter; chroot_leave; trap -p EXIT')
        assert "trap -- 'echo mine' EXIT" in p.stdout
        assert 'an EXIT trap is already set' in (r.work / 'log').read_text()

    def test_no_dns_sets_no_trap(self, tmp_path):
        r = FakeRoot(tmp_path)
        p = r.run('chroot_enter --no-dns; trap -p EXIT')
        assert p.stdout.strip() == ''

    def test_enter_twice_keeps_first_backup(self, tmp_path):
        r = FakeRoot(tmp_path)
        r.conf.write_text('nameserver 10.1.2.3\n')
        r.run('chroot_enter; chroot_enter; chroot_leave')
        assert r.conf.read_text() == 'nameserver 10.1.2.3\n'

    def test_unwritable_etc_does_not_abort(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip('root ignores directory permissions')
        r = FakeRoot(tmp_path)
        r.conf.write_text('nameserver 10.1.2.3\n')
        (r.root / 'etc').chmod(0o555)
        try:
            p = r.run('chroot_enter; echo "rc=$?"; chroot_leave; echo done')
        finally:
            (r.root / 'etc').chmod(0o755)
        assert 'rc=0' in p.stdout and 'done' in p.stdout
        assert r.conf.read_text() == 'nameserver 10.1.2.3\n'
        assert 'WARNING' in (r.work / 'log').read_text()


@needs_bash
class TestRunAndExec:

    def test_exec_passes_output_and_exit_code(self, tmp_path):
        r = FakeRoot(tmp_path)
        p = r.run('chroot_exec fake-ok hi; echo "rc=$?"; chroot_exec fake-fail; echo "rc=$?"')
        assert 'out:hi' in p.stdout
        assert 'rc=0' in p.stdout and 'rc=7' in p.stdout
        call = [c for c in r.cmds() if c.startswith('chroot ')][0]
        assert call.startswith(f'chroot {r.root} /usr/bin/env -i PATH=')
        assert 'LC_ALL=C HOME=/root TERM=dumb fake-ok hi' in call

    def test_run_logs_uses_timeout_and_returns_exit_code(self, tmp_path):
        r = FakeRoot(tmp_path)
        p = r.run('chroot_run 42 fake-fail; echo "rc=$?"; chroot_run 5 fake-ok x; echo "rc=$?"')
        assert 'rc=7' in p.stdout and 'rc=0' in p.stdout
        assert 'out:x' not in p.stdout  # output goes to the log only
        log = (r.work / 'log').read_text()
        assert 'chroot: fake-fail' in log and 'out:x' in log
        assert 'timeout 42' in r.cmds()

    def test_has_cmd(self, tmp_path):
        r = FakeRoot(tmp_path)
        p = r.run('chroot_has_cmd dnf && echo has-dnf; chroot_has_cmd zypper || echo no-zypper',
                  tools='dnf rpm')
        assert 'has-dnf' in p.stdout and 'no-zypper' in p.stdout


@needs_bash
class TestPkgMgr:

    @pytest.mark.parametrize('flavor,tools,expected', [
        ('debian-12-bookworm', 'apt-get dpkg', 'apt-get'),
        ('ubuntu-2204-lts', 'apt-get', 'apt-get'),
        ('rhel-9-server', 'dnf yum', 'dnf'),
        ('rhel-8-server', 'dnf yum', 'dnf'),
        ('rocky-linux-9', 'dnf', 'dnf'),
        ('almalinux-9', 'dnf', 'dnf'),
        ('centos-stream-9', 'dnf', 'dnf'),
        ('rhel-7-server', 'yum', 'yum'),
        ('centos-7', 'yum', 'yum'),
        ('sles-15', 'zypper', 'zypper'),
        ('opensuse-leap', 'zypper', 'zypper'),
        # Flavor guess not installed in the target: ask the target.
        ('rhel-9-server', 'yum', 'yum'),
        # No license (disk name) or unknown: ask the target.
        ('my-custom-disk', 'zypper', 'zypper'),
        ('unknown', 'apt-get', 'apt-get'),
        ('', 'dnf yum', 'dnf'),
    ])
    def test_detection(self, tmp_path, flavor, tools, expected):
        r = FakeRoot(tmp_path)
        r.install(tools)
        p = r.run('chroot_pkg_mgr; echo "rc=$?"', OS_FLAVOR=flavor)
        assert p.stdout.splitlines() == [expected, 'rc=0']

    def test_none_found(self, tmp_path):
        r = FakeRoot(tmp_path)
        p = r.run('chroot_pkg_mgr; echo "rc=$?"', OS_FLAVOR='debian-12')
        assert p.stdout.splitlines() == ['rc=1']

    def test_works_without_running_anything_in_the_target(self, tmp_path):
        """Broken libc or /bin/sh in the target (#201): detection is a file check."""
        r = FakeRoot(tmp_path)
        r.install('dnf')
        p = r.run('chroot() { echo "chroot $*" >> "$WORK/cmds"; return 127; }\n'
                  'chroot_pkg_mgr; echo "rc=$?"', OS_FLAVOR='rhel-9-server')
        assert p.stdout.splitlines() == ['dnf', 'rc=0']
        assert not [c for c in r.cmds() if c.startswith('chroot ')]

    def test_found_in_sbin_and_as_symlink(self, tmp_path):
        r = FakeRoot(tmp_path)
        (r.root / 'usr/sbin').mkdir(parents=True)
        os.symlink('zypper-real', r.root / 'usr/sbin/zypper')  # dangling is fine
        p = r.run('chroot_pkg_mgr; echo "rc=$?"', OS_FLAVOR='sles-15')
        assert p.stdout.splitlines() == ['zypper', 'rc=0']

    def test_absolute_merged_usr_symlink_not_followed_to_the_rescue_os(self, tmp_path):
        """bin -> /elsewhere must not find tools that only exist outside the target."""
        r = FakeRoot(tmp_path)
        outside = tmp_path / 'outside-bin'
        outside.mkdir()
        (outside / 'apt-get').write_text('')
        os.symlink(str(outside), r.root / 'bin')
        p = r.run('chroot_pkg_mgr; echo "rc=$?"', OS_FLAVOR='debian-12')
        assert p.stdout.splitlines() == ['rc=1']


# ===========================================================================
# compose.py: library declaration and inlining
# ===========================================================================

BASE = (
    '#!/bin/bash\n'
    'log() { echo "$1"; }\n'
    'signal_complete() { echo GCE-RESCUE-COMPLETE; }\n'
    'log "Detecting filesystem type..."\n'
    'signal_complete\n'
    'log "=== Startup script completed successfully ==="\n'
)
NEEDS = '# gce-rescue-requires: chroot\nchroot_enter\necho fix-a\nchroot_leave\n'
PLAIN = 'echo fix-plain\n'


class TestRequiredLibs:

    def test_none(self):
        assert required_fix_libs([PLAIN]) == []

    def test_declared_once_across_fixes(self):
        assert required_fix_libs([NEEDS, PLAIN, NEEDS]) == ['chroot']

    def test_indented_and_spaced_declaration(self):
        assert required_fix_libs(['  #  gce-rescue-requires:   chroot  \n']) == ['chroot']

    @pytest.mark.parametrize('line,expected', [
        ('# gce-rescue-requires: chroot, extra_lib\n', ['chroot', 'extra_lib']),
        ('# gce-rescue-requires: chroot extra_lib\n', ['chroot', 'extra_lib']),
        ('# GCE-Rescue-Requires: chroot\n', ['chroot']),
        ('#gce-rescue-requires:chroot\n', ['chroot']),
        ('# gce-rescue-requires: chroot,\n', ['chroot']),
    ])
    def test_accepted_forms(self, line, expected):
        assert required_fix_libs([line]) == expected

    @pytest.mark.parametrize('line', [
        '# gce-rescue-requires: chroot   # for apt\n',   # trailing comment
        '# gce-rescue-requires:\n',                      # no name
        '# gce-rescue-requires:   \n',                   # only spaces
        '# gce-rescue-requires chroot\n',                # missing colon
        '# gce-rescue-requires: ,\n',                    # only a separator
    ])
    def test_malformed_declaration_fails_loudly(self, line):
        """An ignored declaration would only fail later, on the rescue VM."""
        with pytest.raises(ValueError, match='Malformed "gce-rescue-requires" line'):
            required_fix_libs([line])

    @pytest.mark.parametrize('line', [
        '# gce-rescue-requires: chroot.sh\n',
        '# gce-rescue-requires: Chroot\n',
        '# gce-rescue-requires: chroot, ../x\n',
    ])
    def test_invalid_names_fail(self, line):
        with pytest.raises(ValueError, match='invalid library name'):
            required_fix_libs([line])

    def test_mention_in_prose_is_not_a_declaration(self):
        assert required_fix_libs(['# see gce-rescue-requires: chroot in docs\n']) == []

    def test_invalid_name_rejected(self):
        with pytest.raises(ValueError, match='invalid library name'):
            required_fix_libs(['# gce-rescue-requires: ../etc/passwd\n'])

    def test_missing_library_rejected(self):
        with pytest.raises(ValueError, match="requires library 'nosuch'"):
            load_fix_lib('nosuch')

    def test_load_strips_shebang(self):
        body = load_fix_lib('chroot')
        assert not body.startswith('#!')
        assert 'chroot_enter()' in body


class TestComposeInlining:

    def test_inlined_once_before_first_fix_and_after_variables(self):
        script = compose_startup_script(BASE, [NEEDS, PLAIN, NEEDS], repair_targets=[],
                                        failing_kernel='', os_flavor='debian-12-bookworm',
                                        boot_mode='uefi')
        assert script.count('chroot_enter() {') == 1
        assert script.count('# === GCE Repair Fix Library: chroot ===') == 1
        lib_at = script.index('# === GCE Repair Fix Library: chroot ===')
        assert script.index('export OS_FLAVOR=') < lib_at
        assert script.index('REPAIR_TARGETS=') < lib_at
        assert script.index('GCE_FAILING_KERNEL=') < lib_at
        assert lib_at < script.index('echo fix-a') < script.index('echo fix-plain')
        assert script.rstrip().endswith('log "=== Startup script completed successfully ==="')

    def test_not_inlined_when_not_declared(self):
        script = compose_startup_script(BASE, [PLAIN], repair_targets=[])
        assert 'chroot_enter() {' not in script
        assert 'GCE Repair Fix Library' not in script

    def test_custom_script_can_declare(self):
        """--fix-script scripts (repair_targets=None) get the library too."""
        script = compose_startup_script(BASE, [NEEDS])
        assert script.count('chroot_enter() {') == 1

    def test_missing_library_fails_composition(self, monkeypatch, tmp_path):
        monkeypatch.setattr(compose, 'FIX_LIB_DIR', tmp_path)
        with pytest.raises(ValueError, match="requires library 'chroot'"):
            compose_startup_script(BASE, [NEEDS], repair_targets=[])

    @needs_bash
    def test_composed_script_is_valid_bash(self, tmp_path):
        script = compose_startup_script(BASE, [NEEDS, PLAIN], repair_targets=[])
        path = tmp_path / 'composed.sh'
        path.write_text(script)
        subprocess.run(['bash', '-n', str(path)], check=True)

    def test_initramfs_fix_declares_the_library(self):
        assert required_fix_libs([(FIXES / 'initramfs_fix.sh').read_text()]) == ['chroot']

    def test_other_shipped_fixes_do_not_pull_the_library(self):
        for name in ('fstab_fix.sh', 'grub_fix.sh', 'disk_full_fix.sh'):
            assert required_fix_libs([(FIXES / name).read_text()]) == [], name

    def test_initramfs_repair_script_contains_library_once(self):
        """With the real mount script and the fix scripts as repair loads them."""
        from gce_rescue_v2.orchestration.repair import RepairOrchestrator
        orch = RepairOrchestrator(compute=None, project='p', zone='z', vm_name='vm')
        base = (FIXES.parent / 'rescue_mount.sh').read_text().replace(
            'DISK_NAME_PLACEHOLDER', 'disk-1')
        fixes = [orch._get_fix_script('fstab'), orch._get_fix_script('initramfs')]
        script = compose_startup_script(base, fixes, repair_targets=[], failing_kernel='')
        assert script.count('chroot_enter() {') == 1
        assert script.index('chroot_enter() {') < script.index('=== initramfs repair started ===')


class TestCustomScriptPreflight:
    """--fix-script library declarations are checked before any VM change.

    Custom scripts are composed inside the rescue flow, after the VM has been
    stopped and its boot disk swapped, so read_fix_script() (called before the
    orchestrator runs) must reject a bad declaration.
    """

    def test_valid_declaration_accepted(self, tmp_path):
        from gce_rescue_v2 import cli
        script = tmp_path / 'fix.sh'
        script.write_text(NEEDS)
        assert cli.read_fix_script(str(script)) == NEEDS

    def test_unknown_library_rejected(self, tmp_path):
        from gce_rescue_v2 import cli
        script = tmp_path / 'fix.sh'
        script.write_text('# gce-rescue-requires: nosuch\necho hi\n')
        with pytest.raises(ValueError, match=r"--fix-script: .*requires library 'nosuch'"):
            cli.read_fix_script(str(script))

    def test_invalid_name_rejected(self, tmp_path):
        """A common slip: naming the file instead of the library."""
        from gce_rescue_v2 import cli
        script = tmp_path / 'fix.sh'
        script.write_text('# gce-rescue-requires: chroot.sh\necho hi\n')
        with pytest.raises(ValueError, match=r'--fix-script: .*invalid library name'):
            cli.read_fix_script(str(script))

    def test_rejected_in_args_to_config(self, tmp_path):
        """The CLI handlers build the config before the orchestrator runs."""
        from gce_rescue_v2 import cli
        script = tmp_path / 'fix.sh'
        script.write_text('# gce-rescue-requires: nosuch\n')
        args = cli.create_parser().parse_args(
            ['rescue', 'vm-1', '--zone', 'us-central1-a', '--fix-script', str(script)])
        with pytest.raises(ValueError, match='nosuch'):
            cli.args_to_rescue_config(args)

    def test_declared_libraries_count_against_the_size_budget(self, tmp_path, monkeypatch):
        from gce_rescue_v2 import cli
        script = tmp_path / 'fix.sh'
        script.write_text(NEEDS)
        lib_size = len(load_fix_lib('chroot').encode('utf-8'))
        monkeypatch.setattr(cli, 'MAX_FIX_SCRIPT_BYTES', len(NEEDS) + lib_size - 1)
        with pytest.raises(ValueError, match='plus .* bytes of declared libraries'):
            cli.read_fix_script(str(script))
        monkeypatch.setattr(cli, 'MAX_FIX_SCRIPT_BYTES', len(NEEDS) + lib_size)
        assert cli.read_fix_script(str(script)) == NEEDS

    def test_malformed_declaration_rejected_at_preflight(self, tmp_path):
        from gce_rescue_v2 import cli
        script = tmp_path / 'fix.sh'
        script.write_text('# gce-rescue-requires: chroot  # needs apt\n')
        with pytest.raises(ValueError, match='--fix-script: Malformed'):
            cli.read_fix_script(str(script))


class TestPackaging:

    def test_library_ships_in_wheel(self):
        repo = Path(compose.__file__).resolve().parents[2]
        setup_py = (repo / 'setup.py').read_text(encoding='utf-8')
        pyproject = (repo / 'gce_rescue_v2' / 'pyproject.toml').read_text(encoding='utf-8')
        assert 'startup_scripts/fixes/lib/*.sh' in setup_py
        assert 'startup_scripts/fixes/lib/*.sh' in pyproject
