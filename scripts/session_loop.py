#!/usr/bin/env python3
"""Run a first scan, then non-overlapping backup scans every 30 minutes."""
import os
from pathlib import Path
import subprocess
import sys
import time


def save_teledrive_session(run=subprocess.run, python=sys.executable):
    """Guarda a sessao do TeleDrive se estiver no prazo. Isolado: uma falha aqui nao afeta o backup."""
    if os.environ.get('TELEDRIVE_AUTO') == 'false' or not Path('/opt/teledrive/bin/TeleDrive').exists():
        return
    script = Path(__file__).resolve().parent / 'teledrive_sessao.py'
    try:
        run([python, str(script), 'save', '--if-due'], timeout=600, check=False)
    except (subprocess.TimeoutExpired, OSError):
        print('::warning::Salvamento da sessao do TeleDrive nao terminou; tentara no proximo ciclo.', flush=True)


def loop(started, duration=18000, interval=1800, now=time.time, sleep=time.sleep, scan=None):
    if scan is None:
        import needle_startup as needle
        root = Path(__file__).resolve().parents[1]
        def scan():
            catalog, _ = needle.load_catalog(root)
            routine = catalog['backup_changed_files']
            # Deterministic invocation of a registered Needle tool; no model is
            # needed to decide whether a fixed 30-minute timer has elapsed.
            env = dict(os.environ, BACKUP_PYTHON=sys.executable)
            script = (root / routine['script']).resolve()
            try:
                needle.run_script(script, root, env, max(1, int(started + duration - now())))
                return 0
            except RuntimeError:
                return 1
    deadline = started + duration
    slot = now()  # Initial baseline starts as soon as the desktop is ready.
    while now() < deadline:
        if now() < slot:
            sleep(min(30, slot - now(), deadline - now()))
            continue
        try:
            status = scan()
        except subprocess.TimeoutExpired:
            status = 124
        save_teledrive_session()
        if status:
            print('::warning::Uma varredura ficou parcial ou falhou; o encerramento tentara novamente.', flush=True)
        # Anchor following slots to the workflow start; no overlapping scans.
        slot = started + (int(max(0, now() - started) // interval) + 1) * interval


if __name__ == '__main__':
    started = float(os.environ['SESSION_STARTED'])
    import handoff
    # Thread separada: o disparo as 4h30 nao pode esperar uma varredura de backup longa.
    handoff.start_background(started)
    loop(started)
