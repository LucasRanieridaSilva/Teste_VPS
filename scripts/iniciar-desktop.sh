#!/usr/bin/env bash
# Orquestrador do ambiente: escolhe a "ISO" (imagem de sistema) e o desktop.
#
# Variaveis de entrada:
#   DISTRO         ubuntu (padrao) | arch
#   DESKTOP_ENV    xfce (padrao) | kde | gnome | hyprland  (kde/gnome/hyprland: arch)
#   DOTFILES_URLS  URLs de repositorios git de dotfiles, separadas por espaco/quebra de linha
#   VNC_PASSWORD   senha VNC (8 caracteres ASCII)
#   PUBLIC_REPO    true esconde o link do Summary e do log
#
# Ubuntu roda direto no runner (caminho classico). Arch roda num container
# Docker archlinux:base com o desktop escolhido; o noVNC publica na porta 6080
# e o Cloudflare Tunnel, na maquina hospedeira, expoe o mesmo endereco de sempre.
set -euo pipefail

export DISTRO="${DISTRO:-ubuntu}"
export DESKTOP_ENV="${DESKTOP_ENV:-xfce}"

logdir="$RUNNER_TEMP/linux-web-pc"

case "$DISTRO:$DESKTOP_ENV" in
  ubuntu:xfce)
    bash scripts/start-desktop.sh
    ;;
  arch:kde|arch:gnome|arch:hyprland)
    bash scripts/desktops/arch.sh
    ;;
  *)
    echo "Combinacao invalida: DISTRO=$DISTRO DESKTOP_ENV=$DESKTOP_ENV." >&2
    echo 'Disponiveis: ubuntu:xfce, arch:kde, arch:gnome, arch:hyprland.' >&2
    exit 1
    ;;
esac

# Dotfiles do GitHub, se configurados (aplica na home da sessao correspondente).
if [[ -n "${DOTFILES_URLS:-}" ]]; then
  bash scripts/aplicar-dotfiles.sh || echo '::warning::Falha ao aplicar dotfiles; a sessao continua.'
else
  echo 'Sem dotfiles para aplicar (DOTFILES_URLS vazio).'
fi
