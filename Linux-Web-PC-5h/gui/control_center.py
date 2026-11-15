#!/usr/bin/env python3
"""Central de Controle do Linux Web PC.

GUI web em arquivo unico, SOMENTE biblioteca padrao do Python (cabe em um pen
drive): configure o repositorio, tokens, senhas, e-mail, distro, desktop,
dotfiles e comandos para a IA; inicie e pare a sessao; e acompanhe o desktop
pelo noVNC embutido.

Dois modos:
- local (padrao): roda no seu computador (python3 gui/control_center.py).
  Abre em http://127.0.0.1:8787. O token fica em ~/.linux-web-pc-gui.json.
- sessao: roda DENTRO do desktop da sessao (scripts/gui_sessao.sh), le a
  configuracao do momento do disparo e sincroniza mudancas de volta para as
  variaveis do repositorio com o RENOVAR_TOKEN - a mesma origem de verdade da
  GUI local.

Seguranca: escuta apenas em 127.0.0.1; o token nunca e impresso nem gravado
no repositorio; secrets sao gravados via GitHub CLI (gh secret set), que
criptografa com a chave publica do repositorio.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = 'https://api.github.com'
REPO_RE = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
WORKFLOW = 'linux-web-pc.yml'
CONFIG_PATH = os.path.expanduser('~/.linux-web-pc-gui.json')
DISTROS = ('ubuntu', 'arch')
DESKTOPS = {'ubuntu': ('xfce',), 'arch': ('kde', 'gnome', 'hyprland')}
VARIAVEIS = ('DISTRO', 'DESKTOP_ENV', 'DOTFILES_URLS', 'INSTRUCAO_IA', 'RENOVAR',
             'RENOVAR_MAX_HORAS', 'RENOVAR_MAX_HORAS_MES', 'PUBLIC_REPO',
             'BACKUP_PATHS_JSON')
SECRETS_GUI = ('TG_API_ID', 'TG_API_HASH', 'TG_SESSION', 'TG_CHANNEL_ID',
               'TELEDRIVE_PASSPHRASE', 'BACKUP_PASSWORD', 'VNC_PASSWORD',
               'SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASSWORD',
               'EMAIL_FROM', 'EMAIL_TO', 'RENOVAR_TOKEN', 'STARTUP_SECRETS_JSON')
PORTA_LOCAL = 8787
PORTA_SESSAO = 6081


class ApiError(RuntimeError):
    pass


# ---------------------------------------------------------------- GitHub API

class SemAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Remove o Authorization ao seguir redirect (download de artifact)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        pedido = super().redirect_request(req, fp, code, msg, headers, newurl)
        if pedido is not None:
            pedido.headers.pop('Authorization', None)
            pedido.unredirected_hdrs.pop('Authorization', None)
        return pedido


def api_get(token, path, aceita_json=True):
    pedido = urllib.request.Request(API + path, headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'linux-web-pc-gui'})
    with _abrir(pedido) as resposta:
        dados = resposta.read()
    return json.loads(dados) if aceita_json and dados else ({} if aceita_json else dados)


def _abrir(pedido):
    abridor = urllib.request.build_opener(SemAuthRedirect)
    return abridor.open(pedido, timeout=30)


def api_send(token, path, metodo='POST', corpo=None):
    dados = None if corpo is None else json.dumps(corpo).encode('utf-8')
    cabecalhos = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                  'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'linux-web-pc-gui'}
    if dados is not None:
        cabecalhos['Content-Type'] = 'application/json'
    pedido = urllib.request.Request(API + path, data=dados, method=metodo, headers=cabecalhos)
    try:
        with _abrir(pedido) as resposta:
            resposta.read()
            return resposta.status
    except urllib.error.HTTPError as exc:
        exc.read()
        if exc.code in (401,):
            raise ApiError('Token recusado (invalido ou expirado).')
        if exc.code in (403, 404):
            raise ApiError('Sem permissao (HTTP %d). O token precisa das permissoes indicadas na aba Credenciais.' % exc.code)
        raise ApiError('GitHub recusou o pedido (HTTP %d).' % exc.code)


def ler_variaveis(token, repo):
    dados = api_get(token, f'/repos/{repo}/actions/vars')
    return {item['name']: item['value'] for item in dados.get('variables', [])}


def gravar_variavel(token, repo, nome, valor):
    atual = ler_variaveis(token, repo)
    if nome in atual:
        api_send(token, f'/repos/{repo}/actions/variables/{nome}', 'PATCH', {'name': nome, 'value': valor})
    else:
        api_send(token, f'/repos/{repo}/actions/variables', 'POST', {'name': nome, 'value': valor})


def runs_recentes(token, repo):
    dados = api_get(token, f'/repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page=6')
    lista = []
    for run in dados.get('workflow_runs', []):
        lista.append({'id': run['id'], 'status': run['status'], 'conclusion': run.get('conclusion'),
                      'criado': run.get('created_at', ''), 'url': run.get('html_url', '')})
    return lista


def buscar_link(token, repo):
    """Link do desktop no artifact da execucao mais recente (auth necessaria)."""
    runs = api_get(token, f'/repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page=5')
    for run in runs.get('workflow_runs', []):
        arts = api_get(token, f"/repos/{repo}/actions/runs/{run['id']}/artifacts")
        for art in arts.get('artifacts', []):
            if art['name'].startswith('link-sessao') and not art['expired']:
                pedido = urllib.request.Request(
                    f"{API}/repos/{repo}/actions/artifacts/{art['id']}/zip",
                    headers={'Authorization': 'Bearer ' + token,
                             'Accept': 'application/vnd.github+json',
                             'User-Agent': 'linux-web-pc-gui'})
                with _abrir(pedido) as resposta:
                    with zipfile.ZipFile(io.BytesIO(resposta.read())) as zipo:
                        for nome in zipo.namelist():
                            if nome.endswith('access-url.txt'):
                                return zipo.read(nome).decode('utf-8').strip()
    return None


def gh_disponivel():
    return shutil.which('gh') is not None


def gh_secret(repo, nome, valor):
    if not gh_disponivel():
        raise ApiError('GitHub CLI (gh) nao encontrado: instale-o para gravar Secrets '
                       '(winget install GitHub.cli no Windows).')
    resultado = subprocess.run(['gh', 'secret', 'set', nome, '--repo', repo],
                               input=valor, text=True, capture_output=True, timeout=60)
    if resultado.returncode:
        raise ApiError('gh secret set falhou; confira o login (gh auth status) e as permissoes.')


# ---------------------------------------------------------------- configuracao

def carregar_config():
    try:
        with open(CONFIG_PATH, encoding='utf-8') as arquivo:
            dados = json.load(arquivo)
    except (OSError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


def salvar_config(dados):
    atual = carregar_config()
    atual.update({k: v for k, v in dados.items() if v is not None})
    with open(CONFIG_PATH, 'w', encoding='utf-8') as arquivo:
        json.dump(atual, arquivo)
    os.chmod(CONFIG_PATH, 0o600)


def validar_variaveis(corpo):
    distro = corpo.get('DISTRO') or 'ubuntu'
    desktop = corpo.get('DESKTOP_ENV') or 'xfce'
    if distro not in DISTROS:
        raise ApiError('Distro invalida: use ubuntu ou arch.')
    if desktop not in DESKTOPS.get(distro, ()):
        raise ApiError(f'Desktop {desktop} nao disponivel para {distro}. '
                       f'Opcoes de {distro}: {", ".join(DESKTOPS[distro])}.')
    saida = {'DISTRO': distro, 'DESKTOP_ENV': desktop}
    dotfiles = corpo.get('DOTFILES_URLS') or ''
    for url in re.split(r'[\s,]+', dotfiles):
        if not url:
            continue
        if not re.fullmatch(r'https://[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/?', url):
            raise ApiError(f'URL de dotfiles invalida: {url}')
    saida['DOTFILES_URLS'] = dotfiles.strip()
    instrucao = corpo.get('INSTRUCAO_IA') or ''
    if len(instrucao) > 60000:
        raise ApiError('Instrucoes para a IA excederam 60000 caracteres.')
    saida['INSTRUCAO_IA'] = instrucao
    renovar = corpo.get('RENOVAR') or ''
    if renovar and renovar != 'true':
        raise ApiError('RENOVAR aceita true ou vazio (desligado).')
    saida['RENOVAR'] = renovar
    for campo in ('RENOVAR_MAX_HORAS', 'RENOVAR_MAX_HORAS_MES'):
        valor = (corpo.get(campo) or '').strip()
        if valor and not re.fullmatch(r'\d+([.,]\d+)?', valor):
            raise ApiError(f'{campo} deve ser um numero de horas (ex.: 20 ou 7,5) ou vazio.')
        saida[campo] = valor.replace(',', '.')
    publico = corpo.get('PUBLIC_REPO') or ''
    if publico and publico != 'true':
        raise ApiError('PUBLIC_REPO aceita true ou vazio.')
    saida['PUBLIC_REPO'] = publico
    caminhos = (corpo.get('BACKUP_PATHS_JSON') or '').strip()
    if caminhos:
        try:
            lista = json.loads(caminhos)
        except ValueError:
            raise ApiError('BACKUP_PATHS_JSON deve ser um JSON valido, como ["/home/runner/sessao"].')
        if not isinstance(lista, list) or not all(isinstance(p, str) and p.startswith('/') for p in lista):
            raise ApiError('BACKUP_PATHS_JSON deve ser uma lista de caminhos absolutos.')
    saida['BACKUP_PATHS_JSON'] = caminhos
    return saida


# ---------------------------------------------------------------- servidor

class Gui:
    def __init__(self, modo):
        self.modo = modo
        self.config_sessao = {}
        if modo == 'sessao':
            caminho = os.environ.get('GUI_SESSAO_CONFIG', '/tmp/gui-sessao.json')
            try:
                with open(caminho, encoding='utf-8') as arquivo:
                    self.config_sessao = json.load(arquivo)
            except (OSError, ValueError):
                self.config_sessao = {}

    def token(self):
        if self.modo == 'sessao':
            return os.environ.get('RENOVAR_TOKEN', '').strip()
        return (carregar_config().get('token') or '').strip()

    def repo(self):
        if self.modo == 'sessao':
            return self.config_sessao.get('repositorio', '')
        return (carregar_config().get('repositorio') or '').strip()

    def exigir(self):
        repo, token = self.repo(), self.token()
        if not REPO_RE.fullmatch(repo):
            raise ApiError('Informe o repositorio (USUARIO/REPOSITORIO) na aba Sessao.')
        if not token:
            raise ApiError('Informe o token de acesso na aba Sessao.')
        return repo, token

    # -- endpoints -----------------------------------------------------------

    def estado(self):
        repo = self.repo()
        return {'modo': self.modo,
                'repositorio': repo if REPO_RE.fullmatch(repo) else '',
                'tem_token': bool(self.token()),
                'gh_ok': gh_disponivel(),
                'sessao': self.config_sessao,
                'porta_local': PORTA_LOCAL}

    def variaveis_get(self):
        repo, token = self.exigir()
        variaveis = ler_variaveis(token, repo)
        return {nome: variaveis.get(nome, '') for nome in VARIAVEIS}

    def variaveis_post(self, corpo):
        repo, token = self.exigir()
        dados = validar_variaveis(corpo)
        for nome, valor in dados.items():
            gravar_variavel(token, repo, nome, valor)
        return {'ok': True, 'salvos': list(dados)}

    def config_post(self, corpo):
        repo = (corpo.get('repositorio') or '').strip()
        token = (corpo.get('token') or '').strip()
        if repo and not REPO_RE.fullmatch(repo):
            raise ApiError('Repositorio deve ter o formato USUARIO/REPOSITORIO.')
        salvar_config({'repositorio': repo or None, 'token': token or None})
        return self.estado()

    def secret_post(self, corpo):
        repo, _ = self.exigir()
        nome = corpo.get('nome') or ''
        valor = corpo.get('valor') or ''
        if nome not in SECRETS_GUI:
            raise ApiError(f'Secret {nome} nao e gerenciado pela GUI.')
        if not valor:
            raise ApiError('Informe o valor do Secret.')
        if nome == 'VNC_PASSWORD' and not re.fullmatch(r'[!-~]{8}', valor):
            raise ApiError('VNC_PASSWORD deve ter exatamente 8 caracteres ASCII imprimiveis.')
        gh_secret(repo, nome, valor)
        return {'ok': True, 'nome': nome}

    def sessao_iniciar(self):
        repo, token = self.exigir()
        info = api_get(token, f'/repos/{repo}')
        branch = info.get('default_branch')
        if not branch:
            raise ApiError('Nao foi possivel ler a branch padrao do repositorio.')
        api_send(token, f'/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches',
                 'POST', {'ref': branch})
        return {'ok': True, 'mensagem': 'Sessao solicitada. O link chega por e-mail quando o desktop estiver pronto (1 a 3 min).'}

    def sessao_parar(self):
        repo, token = self.exigir()
        paradas = []
        for run in runs_recentes(token, repo):
            if run['status'] != 'completed':
                api_send(token, f"/repos/{repo}/actions/runs/{run['id']}/cancel", 'POST')
                paradas.append(run['id'])
        if not paradas:
            raise ApiError('Nenhuma execucao ativa para parar.')
        return {'ok': True, 'paradas': paradas}

    def sessao_status(self):
        repo, token = self.exigir()
        return {'runs': runs_recentes(token, repo)}

    def sessao_link(self):
        repo, token = self.exigir()
        url = buscar_link(token, repo)
        if not url:
            raise ApiError('Link ainda nao disponivel: aguarde o desktop ficar pronto '
                           '(a execucao precisa passar do passo "Abrir desktop").')
        return {'url': url}


class Handler(BaseHTTPRequestHandler):
    gui = None

    def log_message(self, *args):
        pass

    def _json(self, dados, status=200):
        corpo = json.dumps(dados).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _erro(self, exc):
        self._json({'erro': str(exc)}, 400)

    def do_GET(self):
        gui = self.gui
        try:
            if self.path == '/':
                self._html()
            elif self.path == '/api/estado':
                self._json(gui.estado())
            elif self.path == '/api/variaveis':
                self._json(gui.variaveis_get())
            elif self.path == '/api/sessao/status':
                self._json(gui.sessao_status())
            elif self.path == '/api/sessao/link':
                self._json(gui.sessao_link())
            else:
                self._json({'erro': 'Rota desconhecida.'}, 404)
        except ApiError as exc:
            self._erro(exc)
        except Exception as exc:  # noqa: BLE001 - a GUI nunca deve derrubar o servidor
            self._json({'erro': f'Falha inesperada ({type(exc).__name__}).'}, 500)

    def do_POST(self):
        gui = self.gui
        try:
            tamanho = int(self.headers.get('Content-Length') or 0)
            if tamanho > 200000:
                raise ApiError('Corpo grande demais.')
            corpo = json.loads(self.rfile.read(tamanho) or b'{}')
            if self.path == '/api/config':
                self._json(gui.config_post(corpo))
            elif self.path == '/api/variaveis':
                self._json(gui.variaveis_post(corpo))
            elif self.path == '/api/secret':
                self._json(gui.secret_post(corpo))
            elif self.path == '/api/sessao/iniciar':
                self._json(gui.sessao_iniciar())
            elif self.path == '/api/sessao/parar':
                self._json(gui.sessao_parar())
            else:
                self._json({'erro': 'Rota desconhecida.'}, 404)
        except ApiError as exc:
            self._erro(exc)
        except ValueError:
            self._json({'erro': 'JSON invalido.'}, 400)
        except Exception as exc:  # noqa: BLE001
            self._json({'erro': f'Falha inesperada ({type(exc).__name__}).'}, 500)

    def _html(self):
        corpo = PAGINA.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)


PAGINA = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Linux Web PC - Central de Controle</title>
<style>
:root{--bg:#0f1115;--painel:#171a21;--borda:#2a2f3a;--texto:#e6e9ef;--suave:#9aa3b2;--azul:#4f8cff;--verde:#3fb96f;--verm:#e05555}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--texto)}
header{padding:14px 22px;border-bottom:1px solid var(--borda);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0}header .tag{font-size:11px;padding:3px 8px;border-radius:20px;background:var(--painel);border:1px solid var(--borda);color:var(--suave)}
main{max-width:1060px;margin:0 auto;padding:20px}
nav{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px}
nav button{background:var(--painel);color:var(--suave);border:1px solid var(--borda);padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px}
nav button.ativo{color:#fff;border-color:var(--azul);background:#1c2740}
section{display:none}section.ativo{display:block}
.cartao{background:var(--painel);border:1px solid var(--borda);border-radius:12px;padding:16px;margin-bottom:14px}
.cartao h2{margin:0 0 10px;font-size:14px;color:var(--suave);font-weight:600;text-transform:uppercase;letter-spacing:.4px}
label{display:block;font-size:13px;margin:10px 0 4px;color:var(--suave)}
input,select,textarea{width:100%;background:#0c0e12;color:var(--texto);border:1px solid var(--borda);border-radius:8px;padding:9px 10px;font-size:13px}
textarea{min-height:90px;resize:vertical;font-family:inherit}
.linha{display:grid;grid-template-columns:1fr 1fr;gap:12px}.linha3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
button.acao{background:var(--azul);color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:13px;cursor:pointer;margin-top:12px}
button.acao:disabled{opacity:.5;cursor:default}
button.perigo{background:var(--verm)}
button.ok{background:var(--verde)}
.msg{margin-top:10px;font-size:13px;min-height:18px}
.msg.erro{color:var(--verm)}.msg.bom{color:var(--verde)}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:8px 6px;border-bottom:1px solid var(--borda);text-align:left}
th{color:var(--suave);font-weight:600}
.pill{font-size:11px;padding:2px 8px;border-radius:12px}
.pill.ativo{background:#1c2740;color:var(--azul)}.pill.sucesso{background:#12271b;color:var(--verde)}
.pill.falha{background:#2b1515;color:var(--verm)}.pill.outro{background:var(--bg);color:var(--suave)}
#vnc{position:fixed;inset:0;background:#000;display:none;z-index:50}
#vnc iframe{width:100%;height:100%;border:0}
#vnc .fechar{position:absolute;top:10px;right:10px;z-index:60;background:var(--verm);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer}
.nota{font-size:12px;color:var(--suave);margin-top:6px;line-height:1.5}
code{background:#0c0e12;padding:1px 5px;border-radius:5px;font-size:12px}
</style></head><body>
<div id="vnc"><button class="fechar" onclick="fecharVnc()">&times; Fechar desktop</button><iframe id="vncFrame" allow="clipboard-read; clipboard-write"></iframe></div>
<header><h1>Linux Web PC</h1><span class="tag" id="tagModo">Central de Controle</span><span class="tag" id="tagRepo"></span></header>
<main>
<nav>
<button data-aba="sessao" class="ativo">Sessão</button>
<button data-aba="ambiente">Imagem &amp; Desktop</button>
<button data-aba="credenciais">Credenciais</button>
<button data-aba="ia">Automação IA</button>
<button data-aba="ajuda">Ajuda</button>
</nav>

<section id="aba-sessao" class="ativo">
<div class="cartao"><h2>Conexão com o GitHub</h2>
<label>Repositório (USUARIO/REPOSITORIO)</label><input id="repo" placeholder="seunome/linux-web-pc">
<label>Token de acesso (fine-grained PAT — fica só neste computador)</label><input id="token" type="password" placeholder="github_pat_...">
<div class="linha3">
<div><label>Limite diário (h)</label><input id="maxHoras" placeholder="20"></div>
<div><label>Limite mensal (h)</label><input id="maxHorasMes" placeholder="vazio = desligado"></div>
<div><label>Renovação automática</label><select id="renovar"><option value="">Desligada</option><option value="true">Ligada</option></select></div>
</div>
<button class="acao" onclick="salvarConfig()">Salvar conexão e renovação</button>
<div class="msg" id="msgConfig"></div>
<p class="nota">Permissões necessárias no token: <b>Actions</b> (leitura e escrita), <b>Variables</b> (leitura e escrita), <b>Secrets</b> (leitura e escrita) e <b>Metadata</b> (leitura). Sem o GitHub CLI instalado, a gravação de Secrets fica desativada.</p>
</div>
<div class="cartao"><h2>Sessão atual</h2>
<div class="linha">
<div><button class="acao ok" style="width:100%" onclick="iniciarSessao()">▶ Iniciar sessão</button></div>
<div><button class="acao perigo" style="width:100%" onclick="pararSessao()">■ Parar sessão</button></div>
</div>
<button class="acao" onclick="abrirDesktop()">Abrir desktop (noVNC na GUI)</button>
<div class="msg" id="msgSessao"></div>
<table><thead><tr><th>Execução</th><th>Estado</th><th>Criada em</th><th></th></tr></thead><tbody id="runs"></tbody></table>
</div>
</section>

<section id="aba-ambiente">
<div class="cartao"><h2>Imagem do sistema ("ISO")</h2>
<div class="linha">
<div><label>Distribuição</label><select id="distro" onchange="atualizarDesktops()"><option value="ubuntu">Ubuntu 24.04 LTS</option><option value="arch">Arch Linux</option></select></div>
<div><label>Ambiente de desktop</label><select id="desktop"></select></div>
</div>
<p class="nota">A sessão roda em um ambiente efêmero no GitHub Actions: a "ISO" é a imagem de sistema da distro escolhida. O Ubuntu abre direto no runner com o TeleDrive; o Arch roda em container e, no caso dele, aponte <code>BACKUP_PATHS_JSON</code> para <code>["/home/runner/sessao"]</code> na aba Credenciais (campo avançado) para o backup cobrir sua home.</p>
</div>
<div class="cartao"><h2>Dotfiles do GitHub</h2>
<label>URLs dos repositórios (uma por linha)</label><textarea id="dotfiles" placeholder="https://github.com/voce/dotfiles&#10;https://github.com/voce/outras-configs"></textarea>
<p class="nota">Aplicados na home da sessão a cada início. Arquivos <code>README</code>, <code>LICENSE</code> e a pasta <code>.git</code> são ignorados.</p>
<button class="acao" onclick="salvarAmbiente()">Salvar imagem e dotfiles</button>
<div class="msg" id="msgAmbiente"></div>
</div>
</section>

<section id="aba-credenciais">
<div class="cartao"><h2>Secrets do repositório (via GitHub CLI)</h2>
<div class="linha">
<div><label>Qual Secret</label><select id="secretNome"></select></div>
<div><label>Valor</label><input id="secretValor" type="password"></div>
</div>
<button class="acao" onclick="salvarSecret()">Gravar Secret</button>
<div class="msg" id="msgSecret"></div>
<p class="nota" id="notaGh"></p>
<p class="nota">O valor viaja direto para o GitHub criptografado com a chave do repositório — nunca é gravado pela GUI. <b>TG_SESSION</b> só pode ser gerado pelo configurador (login do Telegram); os demais campos você digita aqui.</p>
</div>
<div class="cartao"><h2>Avançado</h2>
<label>BACKUP_PATHS_JSON (caminhos do backup, JSON)</label><input id="backupPaths" placeholder='["/home/runner/sessao"]'>
<p class="nota">Recomendado para Arch. Deixe vazio para usar o padrão do <code>backup/escopo.json</code>.</p>
<label>PUBLIC_REPO (repositório público: oculta o link de logs)</label><select id="publicRepo"><option value="">Não (privado)</option><option value="true">Sim — link só por e-mail/GUI</option></select>
<button class="acao" onclick="salvarAvancado()">Salvar avançados</button><div class="msg" id="msgAvancado"></div>
</div>
</section>

<section id="aba-ia">
<div class="cartao"><h2>Comandos para a IA (Qwen + Needle)</h2>
<label>Instruções aplicadas a cada início de sessão</label>
<textarea id="instrucao" style="min-height:140px" placeholder="Ex.: Instale os programas da lista.&#10;Prepare minhas pastas de trabalho.&#10;Abra o editor de codigo."></textarea>
<p class="nota">Equivalente ao campo "tarefas" do disparo manual e ao <code>INICIALIZACAO.txt</code>. A renovação automática repassa essas instruções para todas as próximas sessões. Nada de senhas aqui — use <code>STARTUP_SECRETS_JSON</code> (aba Credenciais) para valores privados das rotinas.</p>
<button class="acao" onclick="salvarInstrucao()">Salvar instruções</button><div class="msg" id="msgIa"></div>
</div>
</section>

<section id="aba-ajuda">
<div class="cartao"><h2>Pen drive</h2><p class="nota">
Copie a pasta <code>gui/</code> (e, se quiser, o projeto inteiro) para o pen drive e rode <code>Iniciar-GUI.bat</code> (Windows) ou <code>./iniciar-gui.sh</code> (Linux/macOS). A GUI só precisa do Python 3 instalado — nenhuma biblioteca extra. No Windows, um Python portável (WinPython) na raiz do pen drive também funciona.
</p></div>
<div class="cartao"><h2>Ordem de configuração (primeira vez)</h2><p class="nota">
1. Aba <b>Sessão</b>: repositório + token (PAT com Actions, Variables, Secrets e Metadata).<br>
2. Aba <b>Credenciais</b>: grave os Secrets (o <code>RENOVAR_TOKEN</code> pode ser o mesmo PAT).<br>
3. Aba <b>Imagem &amp; Desktop</b>: escolha distro, desktop e dotfiles.<br>
4. Aba <b>Sessão</b> → <b>▶ Iniciar sessão</b>. O link chega por e-mail e pelo botão <b>Abrir desktop</b>.<br>
5. Ligue a renovação automática para o ciclo se manter sozinho.
</p></div>
<div class="cartao"><h2>Sincronização</h2><p class="nota">
A GUI local e a GUI dentro da sessão usam as <b>variáveis do repositório</b> como origem da verdade: o que você salvar aqui vale para as próximas sessões, e a aba da sessão mostra (e altera) exatamente a mesma configuração.
</p></div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);
let ESTADO={};
const NOMES_SECRET={'TG_API_ID':'Telegram API ID','TG_API_HASH':'Telegram API Hash','TG_SESSION':'Telegram Session (gerado pelo configurador)','TG_CHANNEL_ID':'Canal Telegram (ID)','TELEDRIVE_PASSPHRASE':'Frase TeleDrive','BACKUP_PASSWORD':'Senha do backup (12+)','VNC_PASSWORD':'Senha VNC (8 ASCII)','SMTP_HOST':'Servidor SMTP','SMTP_PORT':'Porta SMTP (465/587)','SMTP_USER':'Usuário SMTP','SMTP_PASSWORD':'Senha SMTP','EMAIL_FROM':'Remetente','EMAIL_TO':'Destinatário','RENOVAR_TOKEN':'Token da renovação','STARTUP_SECRETS_JSON':'Secrets das rotinas (JSON)'};

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('nav button').forEach(x=>x.classList.remove('ativo'));
 document.querySelectorAll('section').forEach(x=>x.classList.remove('ativo'));
 b.classList.add('ativo');$('aba-'+b.dataset.aba).classList.add('ativo');
 if(b.dataset.aba==='sessao')atualizarRuns();
});

async function api(caminho,corpo){try{
 const r=await fetch(caminho,corpo?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(corpo)}:undefined);
 const d=await r.json();if(!r.ok)throw new Error(d.erro||'Falha');return d;
}catch(e){throw new Error(e.message||'Sem conexao com a GUI');}}

function msg(id,texto,erro){const el=$(id);el.textContent=texto;el.className='msg '+(erro?'erro':'bom');setTimeout(()=>{if(el.textContent===texto)el.textContent=''},9000)}

async function carregarEstado(){
 try{ESTADO=await api('/api/estado')}catch(e){return}
 $('tagModo').textContent=ESTADO.modo==='sessao'?'GUI da sessão':'Central de Controle';
 $('tagRepo').textContent=ESTADO.repositorio||'sem repositório';
 if(ESTADO.modo==='sessao'){$('repo').value=ESTADO.sessao.repositorio||'';aplicarAmbienteSessao()}
 else{$('repo').value=ESTADO.repositorio||'';$('token').value=''}
 $('notaGh').textContent=ESTADO.gh_ok?'GitHub CLI detectado: gravação de Secrets disponível.':'GitHub CLI (gh) NÃO encontrado: instale (winget install GitHub.cli) para gravar Secrets.';
 const sel=$('secretNome');sel.innerHTML='';for(const[n,d]of Object.entries(NOMES_SECRET)){const o=document.createElement('option');o.value=n;o.textContent=d;sel.appendChild(o)}
 atualizarDesktops();
 if(ESTADO.repositorio&&ESTADO.tem_token)carregarVariaveis();
}

async function carregarVariaveis(){try{
 const v=await api('/api/variaveis');
 if(v.DISTRO){$('distro').value=v.DISTRO;atualizarDesktops()}
 if(v.DESKTOP_ENV)$('desktop').value=v.DESKTOP_ENV;
 $('dotfiles').value=v.DOTFILES_URLS||'';
 $('instrucao').value=v.INSTRUCAO_IA||'';
 $('renovar').value=v.RENOVAR==='true'?'true':'';
 $('maxHoras').value=v.RENOVAR_MAX_HORAS||'';$('maxHorasMes').value=v.RENOVAR_MAX_HORAS_MES||'';
 $('backupPaths').value=v.BACKUP_PATHS_JSON||'';
 $('publicRepo').value=v.PUBLIC_REPO==='true'?'true':'';
}catch(e){}}

function atualizarDesktops(){
 const d=$('distro').value,sel=$('desktop');const opcoes={ubuntu:['xfce'],arch:['kde','gnome','hyprland']}[d];
 sel.innerHTML='';opcoes.forEach(o=>{const e=document.createElement('option');e.value=o;e.textContent={xfce:'XFCE (leve)',kde:'KDE Plasma',gnome:'GNOME',hyprland:'Hyprland (Wayland)'}[o];sel.appendChild(e)});
}
function aplicarAmbienteSessao(){
 const s=ESTADO.sessao||{};
 if(s.distro){$('distro').value=s.distro;atualizarDesktops()}
 if(s.desktop_env)$('desktop').value=s.desktop_env;
 $('dotfiles').value=s.dotfiles_urls||'';$('instrucao').value=s.instrucao_ia||'';
 $('renovar').value=s.renovar==='true'?'true':'';
 $('maxHoras').value=s.renovar_max_horas||'';$('maxHorasMes').value=s.renovar_max_horas_mes||'';
 $('publicRepo').value=s.public_repo==='true'?'true':'';
}

async function salvarConfig(){try{
 const e=await api('/api/config',{repositorio:$('repo').value.trim(),token:$('token').value.trim()});
 ESTADO=e;$('tagRepo').textContent=e.repositorio||'sem repositório';
 await api('/api/variaveis',{DISTRO:$('distro').value,DESKTOP_ENV:$('desktop').value,DOTFILES_URLS:$('dotfiles').value,
  INSTRUCAO_IA:$('instrucao').value,RENOVAR:$('renovar').value,RENOVAR_MAX_HORAS:$('maxHoras').value,
  RENOVAR_MAX_HORAS_MES:$('maxHorasMes').value,PUBLIC_REPO:$('publicRepo').value});
 msg('msgConfig','Conexão e renovação salvas.');
}catch(e){msg('msgConfig',e.message,true)}}

async function salvarAmbiente(){try{
 await api('/api/variaveis',{DISTRO:$('distro').value,DESKTOP_ENV:$('desktop').value,DOTFILES_URLS:$('dotfiles').value});
 msg('msgAmbiente','Imagem e dotfiles salvos para as próximas sessões.');
}catch(e){msg('msgAmbiente',e.message,true)}}

async function salvarAvancado(){try{
 await api('/api/variaveis',{DISTRO:$('distro').value,DESKTOP_ENV:$('desktop').value,PUBLIC_REPO:$('publicRepo').value,BACKUP_PATHS_JSON:$('backupPaths').value});
 msg('msgAvancado','Configurações avançadas salvas.');
}catch(e){msg('msgAvancado',e.message,true)}}

async function salvarInstrucao(){try{
 await api('/api/variaveis',{DISTRO:$('distro').value,DESKTOP_ENV:$('desktop').value,INSTRUCAO_IA:$('instrucao').value});
 msg('msgIa','Instruções salvas.');
}catch(e){msg('msgIa',e.message,true)}}

async function salvarSecret(){try{
 await api('/api/secret',{nome:$('secretNome').value,valor:$('secretValor').value});
 $('secretValor').value='';msg('msgSecret','Secret gravado no repositório.');
}catch(e){msg('msgSecret',e.message,true)}}

async function iniciarSessao(){try{
 msg('msgSessao','Solicitando...');
 const r=await api('/api/sessao/iniciar');msg('msgSessao',r.mensagem);setTimeout(atualizarRuns,3000);
}catch(e){msg('msgSessao',e.message,true)}}

async function pararSessao(){try{
 const r=await api('/api/sessao/parar');msg('msgSessao','Execuções paradas: '+r.paradas.join(', '));atualizarRuns();
}catch(e){msg('msgSessao',e.message,true)}}

async function abrirDesktop(){try{
 msg('msgSessao','Buscando o link do desktop...');
 const r=await api('/api/sessao/link');
 $('vncFrame').src=r.url;$('vnc').style.display='block';
}catch(e){msg('msgSessao',e.message,true)}}
function fecharVnc(){$('vnc').style.display='none';$('vncFrame').src=''}

function pillRun(r){
 if(r.status!=='completed')return '<span class="pill ativo">'+(r.status==='queued'?'na fila':'ativa')+'</span>';
 const mapa={success:['sucesso','sucesso'],failure:['falha','falha'],timed_out:['falha','estourou'],startup_failure:['falha','falhou ao iniciar'],cancelled:['outro','cancelada']};
 const m=mapa[r.conclusion]||['outro',r.conclusion||'?'];return '<span class="pill '+m[0]+'">'+m[1]+'</span>';
}
async function atualizarRuns(){try{
 const d=await api('/api/sessao/status');
 $('runs').innerHTML=d.runs.map(r=>'<tr><td>'+r.id+'</td><td>'+pillRun(r)+'</td><td>'+(r.criado||'')+'</td><td><a style="color:var(--azul)" href="'+r.url+'" target="_blank">ver</a></td></tr>').join('')||'<tr><td colspan="4" style="color:var(--suave)">Nenhuma execução ainda.</td></tr>';
}catch(e){}}

carregarEstado();atualizarRuns();setInterval(atualizarRuns,20000);
</script></body></html>"""


def principal():
    modo = 'sessao' if '--modo' in sys.argv and 'sessao' in sys.argv else 'local'
    porta = PORTA_SESSAO if modo == 'sessao' else PORTA_LOCAL
    if '--porta' in sys.argv:
        porta = int(sys.argv[sys.argv.index('--porta') + 1])
    Handler.gui = Gui(modo)
    servidor = ThreadingHTTPServer(('127.0.0.1', porta), Handler)
    endereco = f'http://127.0.0.1:{porta}'
    print(f'Central de Controle ({modo}) em {endereco}')
    if modo == 'local':
        webbrowser.open(endereco)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    principal()
