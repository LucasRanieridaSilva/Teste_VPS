#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
from pathlib import Path
import re
import subprocess

packages = []
for line in Path('automacoes/pacotes.txt').read_text(encoding='utf-8').splitlines():
    name = line.split('#', 1)[0].strip()
    if not name:
        continue
    if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?', name):
        raise SystemExit('Pacote invalido em automacoes/pacotes.txt.')
    if name not in packages:
        packages.append(name)
if packages:
    subprocess.run(['sudo', '-n', 'apt-get', 'update'], check=True, timeout=300)
    subprocess.run(['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive',
                    'apt-get', 'install', '-y', '--no-install-recommends',
                    '--', *packages], check=True, timeout=540)
else:
    print('Lista de pacotes vazia.')
PY
