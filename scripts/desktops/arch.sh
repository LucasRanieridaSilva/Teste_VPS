#!/usr/bin/env bash
# Arch Linux em container Docker com KDE Plasma, GNOME ou Hyprland.
#
# A "ISO" do Arch e a imagem oficial archlinux:base. O desktop escolhido roda
# dentro do container e o noVNC publica na porta 6080; o tunel Cloudflare, na
# maquina hospedeira, expoe o mesmo endereco das outras distros.
#
# A home do usuario do container (/root) e o volume /home/runner/sessao no
# hospedeiro: o backup incremental (que roda no hospedeiro) enxerga tudo se
# BACKUP_PATHS_JSON apontar para /home/runner/sessao (recomendado).
#
# Limitacoes honestas: o app TeleDrive (pacote .deb) nao abre dentro do Arch;
# o backup e a restauracao continuam funcionando normalmente pelo hospedeiro.
set -euo pipefail

: "${DESKTOP_ENV:?Use kde, gnome ou hyprland}"
: "${VNC_PASSWORD:?Senha VNC ausente}"

logdir="$RUNNER_TEMP/linux-web-pc"
mkdir -p "$logdir" /home/runner/sessao
chmod 700 "$logdir"

VOLUME_SESSAO=/home/runner/sessao
CONTAINER=webpc-arch

# Limpa container de tentativa anterior, se existir.
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

docker run -d --name "$CONTAINER" \
  --shm-size=1g \
  --add-host=host.docker.internal:host-gateway \
  -p 127.0.0.1:6080:6080 \
  -v "$VOLUME_SESSAO":/root \
  archlinux:base sleep infinity

echo 'Atualizando o sistema dentro do container...'
docker exec "$CONTAINER" pacman -Syu --noconfirm --needed base >/dev/null

echo "Instalando pacotes do ambiente ($DESKTOP_ENV)..."

comuns=(xorg-server-xvfb x11vnc python-pip dbus firefox git ttf-liberation)
case "$DESKTOP_ENV" in
  kde)
    docker exec "$CONTAINER" pacman -Syu --noconfirm --needed \
      plasma-desktop plasma-workspace konsole "${comuns[@]}"
    ;;
  gnome)
    docker exec "$CONTAINER" pacman -Syu --noconfirm --needed \
      gnome-shell gnome-session gnome-console "${comuns[@]}"
    ;;
  hyprland)
    docker exec "$CONTAINER" pacman -Syu --noconfirm --needed \
      hyprland wayvnc kitty "${comuns[@]}"
    ;;
esac

# websockify via pip e noVNC (estatico) do GitHub: versoes fixadas.
docker exec "$CONTAINER" pip install --break-system-packages --no-cache-dir websockify==0.11.0
docker exec "$CONTAINER" bash -c '
  curl -fsSL --retry 3 -o /tmp/novnc.tar.gz https://github.com/novnc/noVNC/archive/refs/tags/v1.4.0.tar.gz
  tar -xzf /tmp/novnc.tar.gz -C /opt && mv /opt/noVNC-1.4.0 /opt/novnc
  ln -sf /opt/novnc/vnc.html /opt/novnc/index.html
'

# Scripts de inicializacao dentro do container (reiniciam o desktop se cair).
docker exec "$CONTAINER" bash -c 'cat > /usr/local/bin/iniciar-sessao.sh' <<'EOS'
#!/usr/bin/env bash
set -u
export DISPLAY=:99
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1600x900x24 >/root/.sessao-xvfb.log 2>&1 &
sleep 2
case "$1" in
  kde)
    while true; do
      QT_QUICK_BACKEND=software dbus-run-session startplasma-x11 >>/root/.sessao-de.log 2>&1
      sleep 5
    done
    ;;
  gnome)
    while true; do
      XDG_SESSION_TYPE=x11 dbus-run-session gnome-session --session=gnome >>/root/.sessao-de.log 2>&1
      sleep 5
    done
    ;;
  hyprland)
    while true; do
      WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 \
        dbus-run-session Hyprland >>/root/.sessao-de.log 2>&1
      sleep 5
    done
    ;;
esac
EOS
docker exec "$CONTAINER" chmod 700 /usr/local/bin/iniciar-sessao.sh

# Servico de tela: VNC + noVNC (Wayland usa wayvnc; X11 usa x11vnc).
if [[ "$DESKTOP_ENV" == hyprland ]]; then
  # wayvnc autentica por arquivo de config (senha em /tmp, fora do volume de backup).
  docker exec "$CONTAINER" bash -c 'cat > /tmp/wayvnc.ini' <<EOS
[auth]
enable=true
username=vnc
password=$VNC_PASSWORD
EOS
  docker exec "$CONTAINER" chmod 600 /tmp/wayvnc.ini
  docker exec -d "$CONTAINER" bash -c '
    while true; do
      WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 wayvnc \
        --config=/tmp/wayvnc.ini 0.0.0.0 5900 >>/root/.sessao-vnc.log 2>&1
      sleep 5
    done
  '
else
  # Senha VNC fora do volume de backup, em arquivo temporario do container.
  docker exec -i "$CONTAINER" bash -c 'base64 -d > /tmp/vnc.pass.b64' <<<"$(printf %s "$VNC_PASSWORD" | base64)"
  docker exec "$CONTAINER" bash -c '
    base64 -d /tmp/vnc.pass.b64 > /tmp/vnc.pass.txt
    x11vnc -storepasswd "$(cat /tmp/vnc.pass.txt)" /tmp/vnc.pass >/dev/null 2>&1
    rm -f /tmp/vnc.pass.b64 /tmp/vnc.pass.txt
    chmod 600 /tmp/vnc.pass
    while true; do
      x11vnc -display :99 -localhost -forever -shared -rfbport 5900 \
        -rfbauth /tmp/vnc.pass -quiet >>/root/.sessao-vnc.log 2>&1
      sleep 5
    done
  ' &
fi

docker exec -d "$CONTAINER" websockify --web=/opt/novnc 6080 127.0.0.1:5900
docker exec -d "$CONTAINER" /usr/local/bin/iniciar-sessao.sh "$DESKTOP_ENV"

# Firefox maximizado na sessao.
if [[ "$DESKTOP_ENV" == hyprland ]]; then
  sleep 12
  docker exec -d "$CONTAINER" bash -c 'sleep 8; WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/0 firefox'
else
  sleep 8
  docker exec -d "$CONTAINER" bash -c 'sleep 8; DISPLAY=:99 firefox'
fi

# Aguarda o noVNC responder e sobe o tunel no hospedeiro (mesmo comportamento
# do caminho Ubuntu: com PUBLIC_REPO=true o link sai so por e-mail e artifact).
ready=false
for attempt in {1..30}; do
  if curl -fsS http://127.0.0.1:6080/vnc.html >/dev/null; then ready=true; break; fi
  sleep 2
done
if [[ "$ready" != true ]]; then echo 'Falha ao iniciar noVNC no container.'; exit 1; fi

unset VNC_PASSWORD
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
  echo 'Desktop pronto. Link enviado por e-mail (repositorio publico: URL oculta).'
else
  {
    echo "## Linux Web PC (Arch, $DESKTOP_ENV)"
    echo
    echo "[Abrir desktop]($access_url)"
    echo
    echo 'Use a senha VNC_PASSWORD configurada nos Secrets.'
    echo 'O backup comeca cinco horas depois do inicio do job. A URL muda em cada sessao.'
    echo 'Salve seu trabalho e feche os programas antes do backup.'
  } >> "$GITHUB_STEP_SUMMARY"
  echo "Desktop: $access_url"
fi
