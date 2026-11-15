#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import json
from pathlib import Path

home = Path.home().resolve()
folders = json.loads(Path('automacoes/pastas.json').read_text(encoding='utf-8'))
if not isinstance(folders, list) or not all(isinstance(x, str) and x for x in folders):
    raise SystemExit('Use uma lista JSON de caminhos relativos a sua home em automacoes/pastas.json.')
for name in folders:
    relative = Path(name)
    folder = (home / relative).resolve()
    if relative.is_absolute() or folder == home or home not in folder.parents:
        raise SystemExit('As pastas devem ficar dentro da home do usuario.')
    folder.mkdir(parents=True, exist_ok=True)
print('Pastas preparadas.')
PY
