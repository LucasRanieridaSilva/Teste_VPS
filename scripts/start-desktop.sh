#!/usr/bin/env bash
set -euo pipefail
export DISPLAY=:99
logdir="$RUNNER_TEMP/linux-web-pc"
mkdir -p "$logdir"
chmod 700 "$logdir"
Xvfb :99 -screen 0 1600x900x24 >"$logdir/xvfb.log" 2>&1 &
sleep 2
dbus-launch --exit-with-session startxfce4 >"$logdir/xfce.log" 2>&1 &
sleep 5
google-chrome --disable-dev-shm-usage --start-maximized >"$logdir/chrome.log" 2>&1 &

# A senha nao aparece nos argumentos do servidor nem nos logs.
x11vnc -storepasswd "$VNC_PASSWORD" "$logdir/vnc.pass" >/dev/null 2>&1
unset VNC_PASSWORD
x11vnc -display :99 -localhost -forever -shared -rfbport 5900 \
  -rfbauth "$logdir/vnc.pass" -quiet >"$logdir/vnc.log" 2>&1 &
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 >"$logdir/novnc.log" 2>&1 &

ready=false
for attempt in {1..30}; do
  if curl -fsS http://127.0.0.1:6080/vnc.html >/dev/null; then ready=true; break; fi
  sleep 2
done
if [[ "$ready" != true ]]; then echo 'Falha ao iniciar noVNC.'; exit 1; fi

"$RUNNER_TEMP/cloudflared" tunnel --url http://127.0.0.1:6080 --no-autoupdate \
  >"$logdir/tunnel.log" 2>&1 &
url=''
for attempt in {1..60}; do
  url=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$logdir/tunnel.log" | head -n 1 || true)
  [[ -n "$url" ]] && break
  sleep 2
done
if [[ -z "$url" ]]; then echo 'Falha ao obter endereco do tunel.'; exit 1; fi
access_url="$url/vnc.html?autoconnect=true&resize=scale"
printf '%s\n' "$access_url" > "$logdir/access-url.txt"
chmod 600 "$logdir/access-url.txt"
if [[ "${PUBLIC_REPO:-}" == "true" ]]; then
  # Repositorio publico: o link sai SOMENTE por e-mail e artifact da GUI.
  # Nada no Summary nem no log, ambos publicos.
  echo 'Desktop pronto. Link enviado por e-mail (repositorio publico: URL oculta).'
else
  {
    echo '## Linux Web PC'
    echo
    echo "[Abrir desktop]($access_url)"
    echo
    echo 'Use a senha VNC_PASSWORD configurada nos Secrets.'
    echo 'O backup comeca cinco horas depois do inicio do job. A URL muda em cada sessao.'
    echo 'Salve seu trabalho e feche os programas antes do backup.'
  } >> "$GITHUB_STEP_SUMMARY"
  echo "Desktop: $access_url"
fi
