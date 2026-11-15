#!/usr/bin/env python3
"""Dispatch only after every previous run of this exact workflow has finished.

The old job exits normally after its backup. Do not cancel an upload in progress.
GitHub workflow concurrency remains a second guard for manually started runs.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from urllib.parse import quote


def gh(*args):
    result = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError('GitHub CLI falhou ao consultar/disparar a execucao. Confira login e permissoes Actions.')
    return result.stdout


def active_runs(repo, workflow):
    # Paginacao completa para nao ignorar uma execucao antiga alem das 100 recentes.
    endpoint = f'repos/{repo}/actions/workflows/{quote(workflow, safe="")}/runs?per_page=100'
    pages = json.loads(gh('api', '--method', 'GET', '--paginate', '--slurp', endpoint))
    if not isinstance(pages, list):
        raise ValueError('Resposta inesperada do GitHub; nova execucao bloqueada.')
    active = {}
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get('workflow_runs'), list):
            raise ValueError('Resposta inesperada do GitHub; nova execucao bloqueada.')
        for run in page['workflow_runs']:
            if not run.get('status') or not isinstance(run.get('id'), int):
                raise ValueError('Execucao sem status/ID; nova execucao bloqueada.')
            if run['status'] != 'completed':
                active[run['id']] = run['status']
    return active


def switch(repo, workflow, branch, wait_seconds=3600, poll_seconds=30,
           clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + wait_seconds
    last = None
    while True:
        active = active_runs(repo, workflow)
        if not active:
            break
        state = tuple(sorted(active.items()))
        if state != last:
            print('Aguardando encerramento da execucao antiga e seu backup: ' +
                  ', '.join(f'{run_id} ({status})' for run_id, status in state), flush=True)
            last = state
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError('A antiga continua ativa. Nenhuma nova execucao foi aberta; o agendador tentara novamente.')
        sleep(min(poll_seconds, remaining))
    # Nenhuma nova execucao e criada enquanto houver uma anterior ativa ou na fila.
    print('Execucoes anteriores encerradas. Solicitando uma nova.', flush=True)
    gh('workflow', 'run', workflow, '--repo', repo, '--ref', branch)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--workflow', default='linux-web-pc.yml')
    parser.add_argument('--branch', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        parser.error('Use --repo USUARIO/REPOSITORIO')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml', args.workflow):
        parser.error('Nome de workflow invalido.')
    try:
        switch(args.repo, args.workflow, args.branch)
    except (RuntimeError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
