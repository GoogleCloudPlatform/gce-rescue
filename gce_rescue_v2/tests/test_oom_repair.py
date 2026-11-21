"""Unit tests for the oom machine type repair (issue #204)."""

from gce_rescue_v2.core.diagnosis import latest_boot_output, oom_kills_only_memcg

KERNEL = '6.1.0-28-cloud-amd64'
GLOBAL_KILL = (
    '[  12.345678] java invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE)\n'
    '[  12.345900] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,'
    'mems_allowed=0,global_oom,task_memcg=/system.slice/app.service,task=java,pid=812\n'
    '[  12.346000] Out of memory: Killed process 812 (java) total-vm:2097152kB\n'
)
MEMCG_KILL = (
    '[  40.100000] python3 invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL)\n'
    '[  40.100200] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,'
    'mems_allowed=0,oom_memcg=/kubepods/pod1,task_memcg=/kubepods/pod1,task=python3,pid=901\n'
    '[  40.100300] Memory cgroup out of memory: Killed process 901 (python3)\n'
)


class TestLatestBootOutput:

    def test_keeps_only_the_last_boot(self):
        serial = (f'Linux version {KERNEL} (x) #1\n{GLOBAL_KILL}'
                  f'Linux version {KERNEL} (x) #1\nStartup finished in 5s\n')

        latest = latest_boot_output(serial)

        assert latest.startswith('Linux version')
        assert 'Out of memory' not in latest
        assert 'Startup finished' in latest

    def test_no_banner_returns_everything(self):
        assert latest_boot_output(GLOBAL_KILL) == GLOBAL_KILL

    def test_empty(self):
        assert latest_boot_output('') == ''
        assert latest_boot_output(None) == ''


class TestOomKillsOnlyMemcg:

    def test_global_kill(self):
        assert oom_kills_only_memcg(GLOBAL_KILL) is False

    def test_memcg_kill(self):
        assert oom_kills_only_memcg(MEMCG_KILL) is True

    def test_mixed_kills(self):
        assert oom_kills_only_memcg(MEMCG_KILL + GLOBAL_KILL) is False

    def test_old_kernel_wording(self):
        memcg = 'Memory cgroup out of memory: Kill process 901 (python3) score 900\n'
        global_kill = 'Out of memory: Kill process 812 (java) score 900 or sacrifice child\n'
        assert oom_kills_only_memcg(memcg) is True
        assert oom_kills_only_memcg(memcg + global_kill) is False

    def test_memcg_summary_line_only(self):
        serial = 'oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/\n'
        assert oom_kills_only_memcg(serial) is True

    def test_no_kill(self):
        assert oom_kills_only_memcg('Startup finished in 5s\n') is False
        assert oom_kills_only_memcg('') is False
