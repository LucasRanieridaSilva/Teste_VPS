#!/usr/bin/env python3
"""Vigia do revezamento: reabre o ciclo se nao houver nenhuma sessao ativa.

Roda de tempos em tempos por um workflow agendado (vigia.yml) em UM dos dois
repositorios. Se existir sessao ativa ou na fila em qualquer um deles, nao faz
nada. Se nao existir, abre uma nova no proprio repositorio onde roda, e o
revezamento continua a partir dela. Como o proprio GitHub agenda o vigia, o
seu computador nao precisa ficar ligado.

Disjuntor: se as 3 execucoes mais recentes falharam, o vigia espera 3 horas
antes de tentar de novo, para nao gastar minutos reabrindo uma sessao que ja
nasce quebrada.
"""
import os
import sys
import time
import urllib.request

import handoff

FAILURES = ('failure', 'timed_out', 'startup_failure')
BREAKER_RUNS = 3
BREAKER_PAUSE_SECONDS = 3 * 3600
FRESH_FAILURE_SECONDS = 35 * 60  # avisa uma vez por falha: o vigia roda a cada ~30 min


def decide(runs_by_repo, now):
    """-> ('active', repo) | ('paused', avisar_agora) | ('start', None)."""
    for repo, runs in runs_by_repo.items():
        if any(handoff.is_active(run, now) for run in runs):
            return 'active', repo
    finished = [run for runs in runs_by_repo.values() for run in runs if run['status'] == 'completed']
    finished.sort(key=lambda run: run.get('created_at') or '', reverse=True)
    recent = finished[:BREAKER_RUNS]
    if len(recent) == BREAKER_RUNS and all(run.get('conclusion') in FAILURES for run in recent):
        ended = handoff.parse_time(recent[0].get('updated_at'))
        if ended is not None and now - ended < BREAKER_PAUSE_SECONDS:
            return 'paused', now - ended < FRESH_FAILURE_SECONDS
    return 'start', None


def main(env=None, urlopen=urllib.request.urlopen, now=time.time):
    env = os.environ if env is None else env
    notify = handoff.make_notifier(env)
    try:
        base = handoff.settings(env)
        if base is None:
            print('Vigia inativo: PEER_REPO nao esta definido.')
            return 0
        own = env.get('GITHUB_REPOSITORY', '').strip()
        if not handoff.REPO_RE.fullmatch(own):
            raise handoff.HandoffError('GITHUB_REPOSITORY ausente ou invalido.')
        targets = {own: dict(base, peer=own), base['peer']: base}
        runs = {repo: handoff.list_runs(cfg, urlopen) for repo, cfg in targets.items()}
        verdict, detail = decide(runs, now())
        if verdict == 'active':
            print('Ha sessao ativa ou na fila; nada a fazer.')
            return 0
        if verdict == 'paused':
            print('::warning::Vigia em pausa: as 3 ultimas execucoes falharam. Nova tentativa em ate 3 horas.')
            if detail:
                notify('Linux Web PC: vigia em pausa',
                       'As 3 ultimas execucoes falharam, entao o vigia nao vai reabrir o ciclo agora. '
                       'Ele tenta de novo em ate 3 horas. Confira os logs das execucoes.')
            return 0
        branch = handoff.check(targets[own], urlopen)
        handoff.dispatch(targets[own], branch, urlopen)
        print(f'Nenhuma sessao ativa em {own} nem em {base["peer"]}: nova sessao solicitada em {own}.')
        notify('Linux Web PC: o vigia reabriu o ciclo',
               f'Nao havia sessao ativa em {own} nem em {base["peer"]}. Abri uma nova em {own}; '
               'o link chega por e-mail quando o desktop estiver pronto.')
        return 0
    except handoff.HandoffError as exc:
        print(f'::error::Vigia: {exc}')
        notify('Linux Web PC: o vigia falhou', f'Motivo: {exc}\n\nConfira o PEER_REPO_TOKEN (validade e permissao Actions).')
        return 1


if __name__ == '__main__':
    sys.exit(main())
