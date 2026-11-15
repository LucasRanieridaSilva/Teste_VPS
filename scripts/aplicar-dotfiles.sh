#!/usr/bin/env bash
# Aplica dotfiles de repositorios do GitHub na home da sessao.
#
# DOTFILES_URLS: URLs https de repositorios git, separadas por espaco ou quebra
# de linha. Cada repositorio e clonado e seu conteudo (exceto .git e README) e
# copiado para a home do usuario da sessao, preservando a hierarquia.
#
# Ubuntu: home do runner (/home/runner). Arch: /root do container (volume).
set -euo pipefail

[[ -n "${DOTFILES_URLS:-}" ]] || { echo 'Nenhuma URL de dotfiles informada.'; exit 0; }

url_re='^https://[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/?$'
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [[ "${DISTRO:-ubuntu}" == arch && "$(docker ps --format '{{.Names}}' 2>/dev/null | grep -c '^webpc-arch$')" -gt 0 ]]; then
  HOME_SESSAO=/root
  COPIAR() {
    docker cp "$1" "webpc-arch:$2"
  }
else
  HOME_SESSAO="$HOME"
  COPIAR() {
    # rsync nao esta garantido; cp preserva a hierarquia com --parents.
    dirname_dest="$(dirname "$2")"
    mkdir -p "$dirname_dest"
    cp -a "$1" "$2"
  }
fi

aplicar_um_repo() {
  local url="$1" idx=0
  if [[ ! ($url =~ $url_re) ]]; then
    echo "::warning::URL de dotfiles ignorada (formato invalido): $url"
    return 0
  fi
  idx=$((idx + 1))
  local destino="$tmp/dotfiles-$RANDOM"
  if ! git clone --depth 1 "$url.git" "$destino" 2>>"$tmp/clone.err"; then
    echo "::warning::Falha ao clonar $url; conferindo o acesso ao repositorio."
    return 0
  fi
  local count=0
  while IFS= read -r -d '' item; do
    local rel="${item#"$destino"/}"
    [[ "$rel" == .git* || "$rel" == .git ]] && continue
    case "$(basename "$rel")" in
      README|README.md|README.txt|LICENSE|LICENSE.md) continue ;;
    esac
    COPIAR "$item" "$HOME_SESSAO/$rel"
    count=$((count + 1))
  done < <(find "$destino" -type f -print0)
  echo "Dotfiles de $url aplicados ($count arquivo(s))."
}

# Quebra por espaco ou quebra de linha.
while IFS= read -r url; do
  [[ -n "$url" ]] && aplicar_um_repo "$url"
done < <(printf '%s\n' "$DOTFILES_URLS" | tr ' ' '\n')
