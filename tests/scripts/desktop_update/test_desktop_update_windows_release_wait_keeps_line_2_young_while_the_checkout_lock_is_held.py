"""Real Windows behavior, isolated within the unchanged per-file deadline."""
from __future__ import annotations
from tests.scripts.desktop_update.test_desktop_update_windows_marker_release_ops import (HARNESS, MARKER, MARKER_PS1, POWERSHELL, Path, _CHECKOUT_HOLDER, pytest, subprocess, sys, time)

@pytest.mark.platforms('windows')
def test_release_wait_keeps_line_2_young_while_the_checkout_lock_is_held(tmp_path: Path) -> None:
    """R6 wait + old packaged Desktops: while the release waits on a survivor's checkout lock
    (up to hours), the heartbeat keeps line 2 young, or an old Desktop age-deletes the marker."""
    install = tmp_path / 'hermes-agent'
    install.mkdir()
    marker = tmp_path / MARKER
    release = tmp_path / 'release-holder'
    holder = subprocess.Popen([sys.executable, '-c', _CHECKOUT_HOLDER, str(install / '.hermes-update.lock'), str(release)])
    harness = tmp_path / 'harness.ps1'
    harness.write_text(HARNESS, encoding='utf-8')
    started = int(time.time()) - 900
    proc = None
    try:
        deadline = time.monotonic() + 30
        while not Path(str(release) + '.ready').exists():
            assert time.monotonic() < deadline and holder.poll() is None
            time.sleep(0.05)
        proc = subprocess.Popen([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness),
                                 '-MarkerPs1', str(MARKER_PS1), '-Marker', str(marker), '-Body', f'{{self}}\n{started}\nct:{{selfct}}\n',
                                 '-Action', 'release', '-InstallRoot', str(install), '-Heartbeat', '1'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
            time.sleep(0.05)
        seen = []
        for _ in range(2):
            time.sleep(4)
            assert proc.poll() is None, proc.communicate()
            lines = marker.read_bytes().decode().split('\n')
            assert time.time() - int(lines[1]) <= 3, lines
            seen.append(int(lines[1]))
        assert seen[1] > seen[0]
    finally:
        release.touch()
        holder.wait(timeout=30)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, out + err
    assert not marker.exists()
