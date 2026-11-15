#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import json
import os
from pathlib import Path
import shutil
import subprocess

programs = json.loads(Path('automacoes/programas.json').read_text(encoding='utf-8'))
if not isinstance(programs, list) or len(programs) > 20:
    raise SystemExit('Use uma lista JSON com ate 20 listas de argumentos.')
logs = Path(os.environ['RUNNER_TEMP']) / 'startup-programs'
logs.mkdir(parents=True, exist_ok=True, mode=0o700)
for index, args in enumerate(programs, 1):
    if not isinstance(args, list) or not args or not all(isinstance(x, str) and '\x00' not in x for x in args):
        raise SystemExit('Cada programa deve ser uma lista como ["xfce4-terminal"].')
    if not shutil.which(args[0]):
        raise SystemExit('Um programa da lista nao esta instalado. Confira automacoes/programas.json.')
    env = dict(os.environ, DISPLAY=':99')
    with (logs / f'program-{index}.log').open('ab') as log:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=log, env=env, start_new_session=True)
    try:
        code = process.wait(timeout=2)
        if code:
            raise SystemExit('Um programa falhou ao iniciar. Confira os logs em RUNNER_TEMP/startup-programs.')
    except subprocess.TimeoutExpired:
        pass
print('Lista de programas processada.')
PY
