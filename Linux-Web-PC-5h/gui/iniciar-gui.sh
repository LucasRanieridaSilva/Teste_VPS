#!/usr/bin/env bash
# Central de Controle do Linux Web PC - lance pelo pen drive (Linux/macOS).
# Requer apenas o Python 3. Nenhuma biblioteca extra.
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo 'Python 3 nao encontrado. Instale-o (apt install python3 / brew install python3).'
  exit 1
fi
echo 'Central de Controle: http://127.0.0.1:8787'
exec python3 control_center.py
