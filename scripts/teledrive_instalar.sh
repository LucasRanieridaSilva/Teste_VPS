#!/usr/bin/env bash
# Instala o TeleDrive (app desktop) a partir de um .deb fixado em automacoes/teledrive.json,
# conferindo o SHA-256 antes de instalar. Nunca executa nada baixado sem essa conferencia.
set -euo pipefail
if [ -x /opt/teledrive/bin/TeleDrive ]; then
  echo 'TeleDrive ja instalado.'
  exit 0
fi
read -r url sha < <(python3 - <<'PY'
import json
import re
config = json.load(open('automacoes/teledrive.json', encoding='utf-8'))
url, sha = config.get('url', ''), config.get('sha256', '')
if not re.fullmatch(r'https://github\.com/Mahmud0808/TeleDrive/releases/download/v[0-9][0-9A-Za-z.]*/teledrive_[0-9][0-9A-Za-z.]*_amd64\.deb', url):
    raise SystemExit('URL invalida em automacoes/teledrive.json (so releases do Mahmud0808/TeleDrive).')
if not re.fullmatch(r'[0-9a-f]{64}', sha):
    raise SystemExit('sha256 invalido em automacoes/teledrive.json.')
print(url, sha)
PY
)
deb="${RUNNER_TEMP:-/tmp}/teledrive.deb"
curl -fsSL --retry 3 --max-time 600 -o "$deb" "$url"
echo "$sha  $deb" | sha256sum -c - >/dev/null || { echo '::error::Checksum do TeleDrive nao confere; instalacao abortada.'; rm -f "$deb"; exit 1; }
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$deb"
rm -f "$deb"
test -x /opt/teledrive/bin/TeleDrive
echo 'TeleDrive instalado.'
