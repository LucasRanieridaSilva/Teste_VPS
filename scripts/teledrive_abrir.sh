#!/usr/bin/env bash
# Restaura a sessao salva do TeleDrive (se existir) e abre o app no desktop.
# Na primeira vez nao ha sessao salva: voce entra uma vez pelo desktop e ela passa a ser guardada.
set -uo pipefail
app=/opt/teledrive/bin/TeleDrive
if [ ! -x "$app" ]; then
  echo '::warning::TeleDrive nao esta instalado; nada a abrir.'
  exit 0
fi
py="${BACKUP_PYTHON:-${RUNNER_TEMP:-/tmp}/backup-venv/bin/python}"
"$py" scripts/teledrive_sessao.py restore
code=$?
case "$code" in
  0) echo 'Sessao do TeleDrive restaurada.' ;;
  3) echo 'Nenhuma sessao salva ainda: entre no TeleDrive pelo desktop (so na primeira vez).' ;;
  *) echo '::warning::Nao foi possivel restaurar a sessao do TeleDrive; o app abre sem login e a sessao nao sera sobrescrita.' ;;
esac
log="${RUNNER_TEMP:-/tmp}/teledrive.log"
DISPLAY=:99 setsid nohup "$app" >"$log" 2>&1 </dev/null &
sleep 6
if pgrep -f '^/opt/teledrive/bin/TeleDrive' >/dev/null; then
  echo 'TeleDrive aberto no desktop.'
else
  echo '::warning::O TeleDrive fechou logo apos abrir. O log ficou no runner (nao e exibido aqui).'
fi
exit 0
