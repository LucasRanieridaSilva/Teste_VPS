#!/usr/bin/env python3
"""Revezamento entre dois repositorios.

Quando faltam 30 minutos para a sessao acabar, dispara o MESMO workflow no
repositorio par (PEER_REPO). Ele faz o mesmo quando chegar a sua vez, fechando o
ciclo A -> B -> A -> ... Cada repositorio usa os seus proprios Secrets, entao o
link sai para o mesmo e-mail se os Secrets SMTP_* forem iguais nos dois.

Somente biblioteca padrao. O token (PEER_REPO_TOKEN) so viaja no cabecalho
Authorization e nunca e impresso.
"""
import calendar
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

API = 'https://api.github.com'
SESSION_SECONDS = 18000
LEAD_SECONDS = 1800
RETRY_SECONDS = 60
REPO_RE = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
WORKFLOW_RE = re.compile(r'[A-Za-z0-9_.-]+\.ya?ml')
MAX_INPUT_BYTES = 60000  # limite do GitHub para um input e margem de seguranca
STALE_SECONDS = 7 * 3600  # execucao 'ativa' mais velha que isso (job maximo: 6h) e considerada travada
PEER_WAIT_SECONDS = 2700  # quanto uma sessao nova espera a antiga terminar o backup


class HandoffError(RuntimeError):
    pass


def settings(env):
    """Retorna a configuracao do revezamento ou None se ele estiver desligado."""
    peer = env.get('PEER_REPO', '').strip()
    if not peer:
        return None
    workflow = env.get('PEER_WORKFLOW', '').strip() or 'linux-web-pc.yml'
    if not REPO_RE.fullmatch(peer):
        raise HandoffError('PEER_REPO deve ter o formato USUARIO/REPOSITORIO.')
    if not WORKFLOW_RE.fullmatch(workflow):
        raise HandoffError('PEER_WORKFLOW deve ser um nome de arquivo .yml/.yaml.')
    if peer.lower() == env.get('GITHUB_REPOSITORY', '').strip().lower():
        raise HandoffError('PEER_REPO nao pode ser o proprio repositorio.')
    token = env.get('PEER_REPO_TOKEN', '').strip()
    if not token:
        raise HandoffError('PEER_REPO esta definido, mas o Secret PEER_REPO_TOKEN esta vazio.')
    instruction = env.get('HANDOFF_STARTUP_INSTRUCTION', '')
    if len(instruction.encode('utf-8')) > MAX_INPUT_BYTES:
        raise HandoffError('startup_instruction e grande demais para repassar ao repositorio par.')
    return {'peer': peer, 'workflow': workflow, 'token': token, 'instruction': instruction}


def api(cfg, method, path, body=None, urlopen=urllib.request.urlopen):
    """Chamada a API do GitHub. Retorna (status HTTP, JSON). Erros de rede viram HandoffError."""
    data = None if body is None else json.dumps(body).encode('utf-8')
    headers = {'Authorization': 'Bearer ' + cfg['token'], 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'linux-web-pc-handoff'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b'{}')
        except ValueError:
            payload = {}
        return exc.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HandoffError(f'falha de rede ao falar com o GitHub ({type(exc).__name__})') from None


def explain(status, payload, what):
    """Mensagem curta e sem dados sensiveis para um status inesperado."""
    if status == 401:
        return f'{what}: o PEER_REPO_TOKEN foi recusado (invalido ou expirado).'
    if status in (403, 404):
        return (f'{what}: sem acesso (HTTP {status}). O token precisa enxergar o repositorio par '
                'com permissao Actions: leitura e escrita.')
    if status == 422:
        detail = payload.get('message', '') if isinstance(payload, dict) else ''
        return f'{what}: o GitHub recusou o pedido (HTTP 422). {detail}'.strip()
    return f'{what}: resposta inesperada do GitHub (HTTP {status}).'


def check(cfg, urlopen=urllib.request.urlopen):
    """Confere repositorio e workflow do par. Retorna a branch padrao do par."""
    peer, workflow = cfg['peer'], cfg['workflow']
    status, repo = api(cfg, 'GET', f'/repos/{peer}', urlopen=urlopen)
    if status != 200 or not isinstance(repo, dict) or not repo.get('default_branch'):
        raise HandoffError(explain(status, repo, 'Repositorio par'))
    path = f'/repos/{peer}/actions/workflows/{quote(workflow, safe="")}'
    status, info = api(cfg, 'GET', path, urlopen=urlopen)
    if status != 200 or not isinstance(info, dict):
        raise HandoffError(explain(status, info, f'Workflow {workflow} no repositorio par'))
    if info.get('state') != 'active':
        raise HandoffError(f'O workflow {workflow} do repositorio par nao esta ativo (habilite-o na aba Actions).')
    return repo['default_branch']


def parse_time(value):
    """ISO 8601 UTC do GitHub (2026-10-27T01:02:03Z) -> epoch; None se ausente/invalido."""
    try:
        return calendar.timegm(time.strptime(value, '%Y-%m-%dT%H:%M:%SZ'))
    except (TypeError, ValueError):
        return None


def list_runs(cfg, urlopen=urllib.request.urlopen):
    """Execucoes recentes do workflow em cfg['peer'] (o repositorio alvo)."""
    path = f'/repos/{cfg["peer"]}/actions/workflows/{quote(cfg["workflow"], safe="")}/runs?per_page=100'
    status, payload = api(cfg, 'GET', path, urlopen=urlopen)
    runs = payload.get('workflow_runs') if isinstance(payload, dict) else None
    if status != 200 or not isinstance(runs, list):
        raise HandoffError(explain(status, payload, 'Consulta de execucoes do repositorio'))
    for run in runs:
        if not isinstance(run, dict) or not run.get('status'):
            raise HandoffError('Execucao sem status; decisao adiada por seguranca.')
    return runs


def is_active(run, now):
    """Em andamento ou na fila, ignorando execucoes travadas ha mais de STALE_SECONDS."""
    if run['status'] == 'completed':
        return False
    created = parse_time(run.get('created_at'))
    return created is None or now - created <= STALE_SECONDS


def peer_busy(cfg, urlopen=urllib.request.urlopen, now=time.time):
    """True se o alvo ja tem execucao em andamento ou na fila (evita sessoes duplicadas)."""
    current = now()
    return any(is_active(run, current) for run in list_runs(cfg, urlopen))


def peer_older_active(cfg, my_started, urlopen=urllib.request.urlopen, now=time.time):
    """True se o par tem uma sessao ativa que comecou ANTES da minha."""
    current = now()
    for run in list_runs(cfg, urlopen):
        created = parse_time(run.get('created_at'))
        if is_active(run, current) and created is not None and created < my_started - 60:
            return True
    return False


def shared_profile(env):
    """Perfil de backup comum aos dois repositorios do revezamento (None se desligado).

    Os dois lados calculam o mesmo valor, porque os nomes entram ordenados. Assim A e B
    compartilham o indice incremental e reaproveitam o que o outro ja enviou.
    """
    own = env.get('GITHUB_REPOSITORY', '').strip().lower()
    peer = env.get('PEER_REPO', '').strip().lower()
    if not own or not REPO_RE.fullmatch(own) or not REPO_RE.fullmatch(peer) or own == peer:
        return None
    return hashlib.sha256('\n'.join(sorted([own, peer])).encode()).hexdigest()[:16]


def wait_for_older_peer(env=None, max_wait=PEER_WAIT_SECONDS, poll=60, now=time.time, sleep=time.sleep,
                        urlopen=urllib.request.urlopen):
    """Antes de gravar no indice compartilhado, espera a sessao MAIS ANTIGA do par terminar.

    Retorna 'off', 'idle', 'timeout' ou 'unknown'. Nunca bloqueia o backup indefinidamente
    nem por falha da API: nesses casos segue com um aviso.
    """
    env = os.environ if env is None else env
    try:
        cfg = settings(env)
        started = float(env.get('SESSION_STARTED', ''))
    except (HandoffError, ValueError):
        return 'off'
    if cfg is None:
        return 'off'
    deadline, errors, announced = now() + max_wait, 0, False
    while True:
        try:
            busy = peer_older_active(cfg, started, urlopen, now)
        except HandoffError as exc:
            errors += 1
            if errors >= 3:
                print(f'::warning::Nao foi possivel conferir a sessao anterior ({exc}); backup segue sem esperar.', flush=True)
                return 'unknown'
            sleep(10)
            continue
        if not busy:
            return 'idle'
        if not announced:
            print('Aguardando a sessao anterior terminar o backup antes de gravar no indice compartilhado.', flush=True)
            announced = True
        if now() >= deadline:
            print('::warning::A sessao anterior continuou ativa; backup segue sem esperar mais.', flush=True)
            return 'timeout'
        sleep(min(poll, max(1, deadline - now())))


def make_notifier(env):
    """Funcao notify(assunto, texto) que usa os Secrets SMTP; silenciosa se nao configurados."""
    def notify(subject, text):
        try:
            import enviar_link
            repo, run_id = env.get('GITHUB_REPOSITORY', ''), env.get('GITHUB_RUN_ID', '')
            if REPO_RE.fullmatch(repo) and run_id.isdigit():
                text += f'\n\nExecucao: https://github.com/{repo}/actions/runs/{run_id}\n'
            enviar_link.notify(env, subject, text)
        except Exception as exc:  # o aviso nunca pode derrubar a sessao
            print(f'::warning::Aviso por e-mail do revezamento falhou ({type(exc).__name__}).', flush=True)
    return notify


def dispatch(cfg, branch, urlopen=urllib.request.urlopen):
    body = {'ref': branch}
    if cfg['instruction'].strip():
        # Repassa o pedido manual desta execucao; sem ele, o par usa os proprios Secrets/arquivos.
        body['inputs'] = {'startup_instruction': cfg['instruction']}
    path = f'/repos/{cfg["peer"]}/actions/workflows/{quote(cfg["workflow"], safe="")}/dispatches'
    status, payload = api(cfg, 'POST', path, body, urlopen=urlopen)
    if status != 204:
        raise HandoffError(explain(status, payload, 'Disparo no repositorio par'))


def run_handoff(cfg, deadline, now=time.time, sleep=time.sleep, urlopen=urllib.request.urlopen,
                retry=RETRY_SECONDS, notify=None):
    """Tenta disparar o par ate `deadline`. Retorna 'dispatched', 'busy' ou 'failed'."""
    branch, last, outcome = None, None, 'failed'
    while now() < deadline:
        try:
            branch = branch or check(cfg, urlopen)
            if peer_busy(cfg, urlopen):
                outcome, message = 'busy', 'O repositorio par ja tem uma execucao ativa; aguardando ela terminar.'
            else:
                dispatch(cfg, branch, urlopen)
                print('Revezamento: nova sessao solicitada no repositorio par. O link seguira por e-mail.', flush=True)
                return 'dispatched'
        except HandoffError as exc:
            outcome, message = 'failed', str(exc)
        if message != last:
            print(f'::warning::Revezamento: {message}', flush=True)
            last = message
        sleep(min(retry, max(1, deadline - now())))
    if outcome == 'busy':
        print('::warning::Revezamento: o par continuou ativo ate o fim desta sessao e nenhum disparo foi feito. '
              'Se ele tambem tiver o revezamento ligado, o ciclo continua por ele.', flush=True)
    else:
        print('::error::Revezamento nao concluido: a proxima sessao NAO foi aberta. Confira PEER_REPO_TOKEN, '
              'o repositorio par e o workflow; inicie uma nova execucao manualmente.', flush=True)
        if notify:
            notify('Linux Web PC: revezamento falhou',
                   'A proxima sessao nao foi aberta em ' + cfg['peer'] + '.\n\nMotivo: ' + (last or 'desconhecido') +
                   '\n\nSe o vigia estiver ligado, ele abre uma nova sessao em ate cerca de uma hora. '
                   'Senao, inicie uma execucao manualmente. Confira tambem a validade do PEER_REPO_TOKEN.')
    return outcome


def _worker(cfg, started, duration, lead, now, sleep, urlopen=urllib.request.urlopen, notify=None):
    trigger = started + duration - lead
    while now() < trigger:
        sleep(min(30, max(1, trigger - now())))
    run_handoff(cfg, started + duration, now=now, sleep=sleep, urlopen=urlopen, notify=notify)


def start_background(started, env=None, duration=SESSION_SECONDS, lead=LEAD_SECONDS,
                     now=time.time, sleep=time.sleep):
    """Inicia a thread do revezamento. Retorna a thread, ou None se desligado/ignorado."""
    env = os.environ if env is None else env
    try:
        cfg = settings(env)
    except HandoffError as exc:
        print(f'::error::Revezamento desativado nesta sessao: {exc}', flush=True)
        return None
    if cfg is None:
        return None
    if env.get('NEEDLE_PREVIEW_ONLY') == '1':
        print('Revezamento ignorado: execucao em modo somente planejamento (needle_preview_only).', flush=True)
        return None
    when = time.strftime('%H:%M UTC', time.gmtime(started + duration - lead))
    print(f'Revezamento ativo: a proxima sessao sera solicitada em {cfg["peer"]} as {when} '
          f'({lead // 60} min antes do fim).', flush=True)
    thread = threading.Thread(target=_worker, args=(cfg, started, duration, lead, now, sleep,
                                                    urllib.request.urlopen, make_notifier(env)),
                              name='handoff', daemon=True)
    thread.start()
    return thread


def preflight(env=None, urlopen=urllib.request.urlopen, sleep=time.sleep, attempts=3):
    """Valida o revezamento no inicio da sessao, para falhar cedo e nao as 4h30."""
    env = os.environ if env is None else env
    cfg = settings(env)
    if cfg is None:
        print('Revezamento desligado (PEER_REPO nao definido).')
        return 0
    for attempt in range(attempts):
        try:
            check(cfg, urlopen)
            print(f'Revezamento pronto: repositorio par {cfg["peer"]} e workflow {cfg["workflow"]} acessiveis.')
            return 0
        except HandoffError as exc:
            if 'falha de rede' not in str(exc) or attempt == attempts - 1:
                raise
            sleep(5)
    return 1


if __name__ == '__main__':
    if sys.argv[1:] != ['check']:
        sys.exit('Uso: handoff.py check')
    try:
        sys.exit(preflight())
    except HandoffError as exc:
        print(f'::error::Revezamento: {exc} Corrija, ou remova a variavel PEER_REPO para desligar o revezamento.')
        sys.exit(1)
