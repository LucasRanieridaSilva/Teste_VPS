#!/usr/bin/env bash
# Sobe a GUI da sessao (mesma Central de Controle, modo sessao) e abre uma aba
# no navegador do desktop. A GUI le o config do momento do disparo e, se o
# RENOVAR_TOKEN permitir, sincroniza mudancas de volta para as variaveis do
# repositorio (mesma origem de verdade da GUI local do pen drive).
set -euo pipefail

CONFIG=${GUI_SESSAO_CONFIG:-/tmp/gui-sessao.json}

mkdir -p "$(dirname "$CONFIG")"
# JSON gerado pelo Python: aspas e quebras de linha nas instrucoes da IA nao
# quebram o arquivo de configuracao.
python3 - "$CONFIG" <<'EOF'
import json
import os
import sys

config = {
    'modo': 'sessao',
    'repositorio': os.environ.get('GITHUB_REPOSITORY', ''),
    'run_id': os.environ.get('GITHUB_RUN_ID', ''),
    'distro': os.environ.get('DISTRO', 'ubuntu'),
    'desktop_env': os.environ.get('DESKTOP_ENV', 'xfce'),
    'dotfiles_urls': os.environ.get('DOTFILES_URLS', ''),
    'instrucao_ia': os.environ.get('INSTRUCAO_IA', ''),
    'renovar': os.environ.get('RENOVAR', ''),
    'renovar_max_horas': os.environ.get('RENOVAR_MAX_HORAS', ''),
    'renovar_max_horas_mes': os.environ.get('RENOVAR_MAX_HORAS_MES', ''),
    'public_repo': os.environ.get('PUBLIC_REPO', ''),
    'tem_token': bool(os.environ.get('RENOVAR_TOKEN', '').strip()),
}
with open(sys.argv[1], 'w', encoding='utf-8') as arquivo:
    json.dump(config, arquivo, ensure_ascii=False)
EOF
chmod 600 "$CONFIG"

GUI_PORTA=${GUI_PORTA:-6081}
nohup python3 gui/control_center.py --modo sessao --porta "$GUI_PORTA" \
  >/tmp/gui-sessao.log 2>&1 &

# Aguarda a GUI responder.
pronto=false
for attempt in {1..20}; do
  if curl -fsS "http://127.0.0.1:$GUI_PORTA/" >/dev/null; then pronto=true; break; fi
  sleep 1
done
[[ "$pronto" == true ]] || { echo '::warning::GUI da sessao nao subiu (log em /tmp/gui-sessao.log).'; exit 0; }

# Abre a aba no navegador da sessao.
if [[ "${DISTRO:-ubuntu}" == arch ]] && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^webpc-arch$'; then
  if [[ "${DESKTOP_ENV:-}" == hyprland ]]; then
    docker exec -d webpc-arch bash -c \
      'WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/0 firefox http://host.docker.internal:'"$GUI_PORTA"
  else
    docker exec -d webpc-arch bash -c 'DISPLAY=:99 firefox http://host.docker.internal:'"$GUI_PORTA"
  fi
else
  DISPLAY=:99 google-chrome "http://127.0.0.1:$GUI_PORTA" >/dev/null 2>&1 || true
fi
echo "GUI da sessao disponivel em http://127.0.0.1:$GUI_PORTA (aba aberta no desktop)."
