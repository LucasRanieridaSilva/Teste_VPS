#!/usr/bin/env python3
"""Renovacao automatica em um unico repositorio.

No fim de cada sessao (modo `renovar`, chamado pelo workflow principal) solicita
a proxima execucao do MESMO workflow neste repositorio, repassando a mesma
instrucao manual quando existe. O workflow `renovar.yml` (modo `vigiar`) roda ao
fim de cada sessao e de 6 em 6 horas como rede de seguranca: se nao houver
nenhuma sessao ativa, reabre o ciclo. Em ambos os casos o link do desktop segue
por e-mail quando a nova sessao estiver pronta.

Protecoes (ver README, "Renovacao automatica"):
- Limite diario (RENOVAR_MAX_HORAS) e mensal (RENOVAR_MAX_HORAS_MES) por janela
  movel, para nao queimar os minutos do GitHub Actions.
- Disjuntor: 3 execucoes seguidas com falha pausam a renovacao por 3 horas.
- Nao renova em modo de planejamento (needle_preview_only) e recusa rodar junto
  com o revezamento (PEER_REPO): use um ou outro.

Somente biblioteca padrao. O token (RENOVAR_TOKEN) so via no cabecalho
Authorization e nunca e impresso.
"""
import os
import sys
import time
import urllib.request

import handoff

DAY_SECONDS = 24 * 3600
MONTH_SECONDS = 30 * 24 * 3600
FAILURES = ('failure', 'timed_out', 'startup_failure')
BREAKER_RUNS = 3
BREAKER_PAUSE_SECONDS = 3 * 3600
FRESH_SECONDS = 35 * 60  # avisa uma vez por evento: o vigia roda ao fim de cada sessao
MODES = ('renovar', 'vigiar')


class RenovarError(RuntimeError):
    pass


def parse_horas(value, name, default):
    """Converte '8', '7,5' ou vazio em horas (float). 0 desliga o limite."""
    text = (value or '').strip()
    if not text:
        return default
    try:
        horas = float(text.replace(',', '.'))
    except ValueError:
        raise RenovarError(f'{name} deve ser um numero de horas, como 20 ou 7,5 (vazio usa o padrao; 0 desliga).')
    if horas < 0 or horas > 1000:
        raise RenovarError(f'{name} deve estar entre 0 e 1000 horas.')
    return horas


def settings(env):
    """Retorna a configuracao da renovacao ou None se ela estiver desligada."""
    if env.get('RENOVAR', '').strip().lower() != 'true':
        return None
    if env.get('PEER_REPO', '').strip():
        raise RenovarError('PEER_REPO (revezamento) esta definido: use o revezamento OU a renovacao, nunca os dois.')
    repo = env.get('GITHUB_REPOSITORY', '').strip()
    if not handoff.REPO_RE.fullmatch(repo):
        raise RenovarError('GITHUB_REPOSITORY ausente ou invalido.')
    workflow = env.get('RENOVAR_WORKFLOW', '').strip() or 'linux-web-pc.yml'
    if not handoff.WORKFLOW_RE.fullmatch(workflow):
        raise RenovarError('RENOVAR_WORKFLOW deve ser um nome de arquivo .yml/.yaml.')
    token = env.get('RENOVAR_TOKEN', '').strip()
    if not token:
        raise RenovarError('RENOVAR=true, mas o Secret RENOVAR_TOKEN esta vazio. Crie um token com permissao Actions (leitura e escrita) e guarde-o como Secret.')
    instruction = env.get('HANDOFF_STARTUP_INSTRUCTION', '')
    if len(instruction.encode('utf-8')) > handoff.MAX_INPUT_BYTES:
        raise RenovarError('startup_instruction e grande demais para repassar a proxima sessao.')
    return {'peer': repo, 'workflow': workflow, 'token': token, 'instruction': instruction,
            'max_day': parse_horas(env.get('RENOVAR_MAX_HORAS'), 'RENOVAR_MAX_HORAS', 20.0),
            'max_month': parse_horas(env.get('RENOVAR_MAX_HORAS_MES'), 'RENOVAR_MAX_HORAS_MES', 0.0)}


def duracao(run, now):
    """Segundos de execucao: concluidos usam updated_at; ativos contam ate agora."""
    created = handoff.parse_time(run.get('created_at'))
    if created is None:
        return 0
    if run['status'] == 'completed':
        ended = handoff.parse_time(run.get('updated_at'))
    else:
        ended = None
    if ended is None or ended < created:
        ended = now
    return ended - created


def uso(runs, now, window):
    """Total de segundos usados pelas execucoes criadas nos ultimos `window` segundos."""
    total = 0
    for run in runs:
        created = handoff.parse_time(run.get('created_at'))
        if created is None or created < now - window:
            continue
        total += max(0, duracao(run, now))
    return total


def decide(runs, own_id, now, max_day, max_month):
    """-> ('active'|'cap_day'|'cap_month'|'paused'|'start', detalhe).

    `own_id` e a propria execucao (modo renovar): ela nao conta como sessao
    ativa, mas entra no calculo de uso de minutos.
    """
    for run in runs:
        if own_id is not None and run.get('id') == own_id:
            continue
        if handoff.is_active(run, now):
            return 'active', None
    if max_day:
        usado = uso(runs, now, DAY_SECONDS)
        if usado >= max_day * 3600:
            return 'cap_day', usado
    if max_month:
        usado = uso(runs, now, MONTH_SECONDS)
        if usado >= max_month * 3600:
            return 'cap_month', usado
    finished = [run for run in runs if run['status'] == 'completed']
    finished.sort(key=lambda run: run.get('created_at') or '', reverse=True)
    recent = finished[:BREAKER_RUNS]
    if len(recent) == BREAKER_RUNS and all(run.get('conclusion') in FAILURES for run in recent):
        ended = handoff.parse_time(recent[0].get('updated_at'))
        if ended is not None and now - ended < BREAKER_PAUSE_SECONDS:
            return 'paused', ended
    return 'start', None


def recente(runs, now):
    """True se alguma execucao concluida terminou ha pouco (avisa cada evento uma vez)."""
    for run in runs:
        if run['status'] != 'completed':
            continue
        ended = handoff.parse_time(run.get('updated_at'))
        if ended is not None and now - ended <= FRESH_SECONDS:
            return True
    return False


def horas_texto(segundos):
    return f'{segundos / 3600:.1f} h'


def avisos_cap(verdict, usado, cap, janela, mode, runs, now, notify):
    prefixo = 'limite diario' if verdict == 'cap_day' else 'limite mensal'
    print(f'::warning::Renovacao pausada pelo {prefixo}: {horas_texto(usado)} usadas nas ultimas {janela}, '
          f'limite de {cap:g} h. O ciclo volta sozinho quando a janela aliviar.')
    if mode == 'renovar' or recente(runs, now):
        notify('Linux Web PC: renovacao pausada pelo limite de uso',
               f'A nova sessao nao foi aberta: o {prefixo} de {cap:g} horas foi atingido '
               f'({horas_texto(usado)} usadas nas ultimas {janela}).\n\n'
               'O ciclo volta sozinho quando a janela de tempo aliviar. Para gastar menos minutos, '
               'reduza RENOVAR_MAX_HORAS ou defina RENOVAR_MAX_HORAS_MES no repositorio.')


def main(argv, env=None, urlopen=urllib.request.urlopen, now=time.time):
    if argv[1:] != ['renovar'] and argv[1:] != ['vigiar']:
        print('Uso: renovar_sessao.py renovar|vigiar', file=sys.stderr)
        return 2
    mode = argv[1]
    env = os.environ if env is None else env
    notify = handoff.make_notifier(env)
    try:
        cfg = settings(env)
        if cfg is None:
            print('Renovacao desligada (RENOVAR != true).')
            return 0
        if mode == 'renovar' and env.get('NEEDLE_PREVIEW_ONLY') == '1':
            print('Renovacao ignorada: execucao em modo somente planejamento (needle_preview_only).')
            return 0
        runs = handoff.list_runs(cfg, urlopen)
        own_id = None
        if mode == 'renovar' and env.get('GITHUB_RUN_ID', '').strip().isdigit():
            own_id = int(env['GITHUB_RUN_ID'].strip())
        momento = now()
        verdict, detalhe = decide(runs, own_id, momento, cfg['max_day'], cfg['max_month'])
        if verdict == 'active':
            print('Ja existe sessao ativa ou na fila; nada a fazer.')
            return 0
        if verdict == 'cap_day':
            avisos_cap('cap_day', detalhe, cfg['max_day'], '24 horas', mode, runs, momento, notify)
            return 0
        if verdict == 'cap_month':
            avisos_cap('cap_month', detalhe, cfg['max_month'], '30 dias', mode, runs, momento, notify)
            return 0
        if verdict == 'paused':
            print('::warning::Renovacao em pausa: as 3 ultimas execucoes falharam. Nova tentativa em ate 3 horas.')
            if mode == 'renovar' or (detalhe is not None and momento - detalhe <= FRESH_SECONDS):
                notify('Linux Web PC: renovacao em pausa',
                       'As 3 ultimas execucoes falharam, entao a renovacao nao vai abrir uma nova sessao agora. '
                       'Ela tenta de novo em ate 3 horas. Confira os logs das execucoes (aba Actions).')
            return 0
        branch = handoff.check(cfg, urlopen)
        handoff.dispatch(cfg, branch, urlopen)
        print('Renovacao: nova sessao solicitada neste repositorio. O link seguira por e-mail quando o desktop estiver pronto.', flush=True)
        notify('Linux Web PC: nova sessao solicitada',
               'A sessao anterior terminou e uma nova foi solicitada automaticamente neste repositorio.\n'
               'O link do desktop chega por e-mail assim que ele estiver pronto; use a mesma senha VNC.\n'
               'Para parar o ciclo, mude a variavel RENOVAR para algo diferente de true (ou desabilite o workflow) e cancele a execucao ativa.')
        return 0
    except handoff.HandoffError as exc:
        mensagem = str(exc).replace('PEER_REPO_TOKEN', 'RENOVAR_TOKEN')
        print(f'::error::Renovacao: {mensagem}', flush=True)
        notify('Linux Web PC: renovacao falhou',
               'A proxima sessao NAO foi aberta.\n\nMotivo: ' + mensagem +
               '\n\nConfira o Secret RENOVAR_TOKEN (validade e permissao Actions: leitura e escrita) '
               'e se o workflow esta ativo. O vigia (renovar.yml) tenta reabrir o ciclo de 6 em 6 horas; '
               'se nao estiver ligado, inicie uma execucao manualmente.')
        return 1
    except RenovarError as exc:
        print(f'::error::Renovacao: {exc}', flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
