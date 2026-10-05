"""ccm_core.secure_tmp_root, the Python twin of lib/ccm_tmp_root.sh (same
cases as tests/test_tmp_root.bats, plus a foreign owner)."""
import os
import stat

import pytest

import ccm_core


def test_creates_a_root_closed_to_others(tmp_path):
    root = tmp_path / 'ccm-1'
    assert ccm_core.secure_tmp_root(str(root))
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_closes_an_open_root(tmp_path):
    root = tmp_path / 'ccm-1'
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    assert ccm_core.secure_tmp_root(str(root))
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_refuses_symlinks_files_and_foreign_owners(tmp_path, monkeypatch):
    (tmp_path / 'elsewhere').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'elsewhere')
    (tmp_path / 'file').write_text('')
    assert not ccm_core.secure_tmp_root(str(tmp_path / 'link'))
    assert not ccm_core.secure_tmp_root(str(tmp_path / 'file'))
    mine = tmp_path / 'mine'
    mine.mkdir()
    other = os.getuid() + 1
    monkeypatch.setattr(ccm_core.os, 'getuid', lambda: other)
    assert not ccm_core.secure_tmp_root(str(mine))


def test_the_codex_hook_writes_nothing_under_an_untrusted_root(tmp_path, monkeypatch):
    import ccm_sidekick_notify
    received = []
    monkeypatch.setattr(ccm_core, 'secure_tmp_root', lambda *a: False)
    monkeypatch.setattr(ccm_sidekick_notify, 'receive', received.append)
    ccm_sidekick_notify.hook_main()
    assert received == []


def test_a_root_of_ours_others_could_write_is_moved_aside_and_made_afresh(tmp_path):
    for mode in (0o777, 0o775, 0o722):
        root = tmp_path / f'ccm-{mode:o}'
        root.mkdir()
        os.chmod(root, mode)
        (root / 'planted').symlink_to(tmp_path / 'elsewhere')
        assert ccm_core.secure_tmp_root(str(root))
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert list(root.iterdir()) == []
        aside = list(tmp_path.glob(f'ccm-{mode:o}.untrusted-*'))
        assert len(aside) == 1 and (aside[0] / 'planted').is_symlink()


def test_a_trailing_slash_or_dot_does_not_hide_a_symlink(tmp_path):
    (tmp_path / 'elsewhere').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'elsewhere')
    for suffix in ('/', '//', '/.', '/./'):
        assert not ccm_core.secure_tmp_root(str(tmp_path / 'link') + suffix)
    assert ccm_core.secure_tmp_root(str(tmp_path / 'ccm-1') + '/')


def test_the_dock_lock_is_not_opened_in_a_root_others_could_write(tmp_path, monkeypatch):
    import ccm_dock
    root = tmp_path / 'ccm-1'
    root.mkdir()
    os.chmod(root, 0o777)
    victim = tmp_path / 'victim'
    victim.write_text('KEEP')
    (root / 'dock.lock').symlink_to(victim)
    monkeypatch.setattr(ccm_core, 'CCM_TMP_DIR', str(root))
    with ccm_dock._serialized():
        pass
    assert (root / 'dock.lock').is_file() and not (root / 'dock.lock').is_symlink()
    assert victim.read_text() == 'KEEP'


@pytest.mark.parametrize('enter', ['inject_status', 'dashboard', 'init_dirs'])
def test_entry_points_stop_before_writing_under_an_untrusted_root(enter, tmp_path, monkeypatch):
    tmp_path = tmp_path / 'root'
    tmp_path.mkdir()
    monkeypatch.setattr(ccm_core, 'secure_tmp_root', lambda *a: False)
    if enter == 'inject_status':
        import inject_status
        monkeypatch.setattr(inject_status, 'CCM_TMP_DIR', str(tmp_path))
        call = inject_status.acquire_lockfile
    elif enter == 'dashboard':
        import dashboard
        monkeypatch.setattr(dashboard, 'CCM_TMP_DIR', str(tmp_path))
        call = lambda: dashboard.acquire_pidfile('dashboard.pid')
    else:
        call = ccm_core.init_dirs
    with pytest.raises(SystemExit):
        call()
    assert list(tmp_path.iterdir()) == []
