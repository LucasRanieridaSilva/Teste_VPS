#!/usr/bin/env python3
"""Needle 3 chooses registered startup routines; this runner executes their scripts.

Natural-language text is never evaluated as shell code. Routines and their scripts
are versioned by the repository owner. The model receives no Secrets.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

PHASES = ('before_desktop', 'after_desktop', 'before_backup')
ROOT = Path(__file__).resolve().parents[1]


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.writing')
    with temp.open('w', encoding='utf-8') as output:
        os.chmod(temp, 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2)
    temp.replace(path)


def load_catalog(root):
    raw = (root / 'automacoes/rotinas.json').read_bytes()
    data = json.loads(raw)
    routines = data.get('routines') if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(routines, list) or not 1 <= len(routines) <= 32:
        raise ValueError('Catalogo invalido: use version 1 e entre 1 e 32 rotinas.')
    found = {}
    allowed = (root / 'automacoes/rotinas').resolve()
    if root.resolve() not in allowed.parents:
        raise ValueError('A pasta das rotinas deve ficar dentro do repositorio.')
    for item in routines:
        if not isinstance(item, dict):
            raise ValueError('Entrada de rotina invalida.')
        name = item.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name) or name in found:
            raise ValueError('Nomes das rotinas devem ser unicos e usar snake_case.')
        if not isinstance(item.get('description'), str) or not 1 <= len(item['description']) <= 1200:
            raise ValueError('Cada rotina precisa de uma descricao de ate 1200 caracteres.')
        if item.get('phase') not in PHASES:
            raise ValueError('Fase da rotina invalida.')
        timeout = item.get('timeout_seconds', 300)
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            raise ValueError('timeout_seconds deve ficar entre 1 e 1800.')
        triggers = item.get('triggers', [])
        if not isinstance(triggers, list) or len(triggers) > 8:
            raise ValueError('triggers deve ser uma lista com ate 8 expressoes regulares.')
        for pattern in triggers:
            if not isinstance(pattern, str) or not 1 <= len(pattern) <= 400:
                raise ValueError('Cada trigger deve conter entre 1 e 400 caracteres.')
            try:
                re.compile(pattern)
            except re.error:
                raise ValueError('Expressao regular invalida em triggers.') from None
        relative = item.get('script')
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError('O script deve ter um caminho relativo ao repositorio.')
        path = (root / relative).resolve()
        if allowed not in path.parents or not path.is_file() or path.suffix != '.sh':
            raise ValueError('Scripts de rotinas devem existir em automacoes/rotinas/ e terminar em .sh.')
        found[name] = dict(item, timeout_seconds=timeout,
                           sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    digest = hashlib.sha256(raw).hexdigest()
    return found, digest


def read_instructions(root, override):
    text = override if override.strip() else (root / 'INICIALIZACAO.txt').read_text(encoding='utf-8')
    if len(text) > 12000:
        raise ValueError('Instrucao grande demais: limite de 12000 caracteres.')
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if len(lines) > 20 or any(len(line) > 2000 for line in lines):
        raise ValueError('Use ate 20 instrucoes, com no maximo 2000 caracteres por linha.')
    return lines


def model_response(agent, text):
    agent.reset()
    return agent.complete(text, max_new_tokens=512)


def choose_routines(lines, catalog, agent, minimum=0.5):
    selected = []
    for number, line in enumerate(lines, 1):
        # Small models can miss negation. Reject common PT/EN negatives rather
        # than risk executing the opposite of the requested action.
        if re.search(r"\b(?:n[aã]o|nunca|jamais|not|never|don['’]t|dont)\b", line, re.IGNORECASE):
            raise ValueError(f'Instrucao {number} contem negacao. Escreva somente as acoes afirmativas que deseja executar.')
        response = model_response(agent, line)
        if not isinstance(response, dict) or response.get('success') is False or response.get('error'):
            raise ValueError(f'Needle nao concluiu a instrucao {number}. Nenhuma rotina sera executada.')
        calls = response.get('function_calls')
        validation = response.get('validation') or {}
        if response.get('suppressed_calls') or validation.get('ungrounded'):
            raise ValueError(f'Needle reteve parte da instrucao {number}. Reescreva com os nomes das rotinas.')
        if not isinstance(calls, list) or not calls:
            raise ValueError(f'Instrucao {number} nao corresponde a uma rotina cadastrada. Use o comando list para consultar as opcoes.')
        confidence = response.get('confidence')
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or confidence < minimum:
            raise ValueError(f'Confianca insuficiente na instrucao {number}. Reescreva a instrucao.')
        for call in calls:
            if not isinstance(call, dict) or call.get('name') not in catalog:
                raise ValueError('Needle selecionou uma rotina que nao esta no catalogo.')
            # The exposed routines have no model-generated parameters.
            if call.get('arguments') not in (None, {}):
                raise ValueError('Esta integracao aceita somente rotinas sem argumentos gerados pela IA.')
            if call['name'] not in selected:
                selected.append(call['name'])
    return selected


def build_agent(catalog, root, install_runtime):
    os.environ['NEEDLE_TELEMETRY'] = '0'
    os.environ['DO_NOT_TRACK'] = '1'
    try:
        import needle
    except ImportError:
        if not install_runtime:
            raise RuntimeError('Instale requirements-needle.txt no ambiente Python ou use plan --install-runtime.') from None
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                        '-q', '-r', str(root / 'requirements-needle.txt')], check=True, timeout=180)
        import needle
    schemas = [{'name': name, 'description': item['description'],
                'triggers': item.get('triggers', []),
                'parameters': {'type': 'object', 'properties': {}, 'required': [],
                               'additionalProperties': False}}
               for name, item in catalog.items()]
    return needle.Needle(tools=schemas, generation=3, system='locale: pt-BR; device: Linux server')


def report(message, env):
    print(message, flush=True)
    summary = env.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as output:
            output.write(message + '\n\n')


def plan(root, plan_file, override='', preview=False, install_runtime=False, agent_factory=None, env=None):
    env = os.environ if env is None else env
    plan_file.unlink(missing_ok=True)  # A failed plan must not leave an executable old plan.
    catalog, digest = load_catalog(root)
    lines = read_instructions(root, override)
    selected = []
    if lines:
        agent = (agent_factory or build_agent)(catalog, root, install_runtime)
        selected = choose_routines(lines, catalog, agent)
    record = {'version': 1, 'preview': bool(preview), 'catalog_sha256': digest,
              'routines': [catalog[name] for name in selected], 'completed': [],
              'created_at': int(time.time()), 'run_id': env.get('GITHUB_RUN_ID', 'local')}
    atomic_json(plan_file, record)
    labels = ', '.join(selected) or 'nenhuma (automacoes desativadas)'
    report(('Plano de teste' if preview else 'Plano de inicializacao') + ': ' + labels, env)
    return record


def secret_environment(env):
    raw = env.get('STARTUP_SECRETS_JSON', '').strip()
    values = json.loads(raw) if raw else {}
    if not isinstance(values, dict) or len(values) > 50:
        raise ValueError('STARTUP_SECRETS_JSON deve ser um objeto JSON com ate 50 variaveis.')
    blocked = {'PATH', 'HOME', 'SHELL', 'BASH_ENV', 'ENV', 'PYTHONPATH', 'PYTHONHOME',
               'SHELLOPTS', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'STARTUP_SECRETS_JSON'}
    child = dict(env)
    child.pop('STARTUP_SECRETS_JSON', None)
    for name, value in values.items():
        if (not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', name) or name in blocked
                or name.startswith(('GITHUB_', 'RUNNER_')) or not isinstance(value, str) or '\x00' in value):
            raise ValueError('Nome ou valor invalido em STARTUP_SECRETS_JSON.')
        if value and env.get('GITHUB_ACTIONS') == 'true':
            masked = value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            print('::add-mask::' + masked, flush=True)
        child[name] = value
    return child


def run_script(script, root, env, timeout):
    if os.name != 'posix':
        raise RuntimeError('A execucao das rotinas Bash e feita no Linux do workflow. No Windows, use list ou plan.')
    # A separate process group lets timeout stop the script's children as well.
    process = subprocess.Popen(['bash', str(script)], cwd=root, env=env, start_new_session=True)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise RuntimeError('Uma rotina ultrapassou o tempo configurado.') from None
    if code:
        raise RuntimeError('Uma rotina falhou. Confira o passo da automacao no Actions.')


def execute(root, plan_file, phase, env=None, runner=run_script):
    env = os.environ if env is None else env
    catalog, digest = load_catalog(root)
    record = json.loads(plan_file.read_text(encoding='utf-8'))
    if record.get('version') != 1 or record.get('catalog_sha256') != digest:
        raise ValueError('O catalogo mudou depois do planejamento. Execute plan novamente.')
    if record.get('run_id') != env.get('GITHUB_RUN_ID', 'local'):
        raise ValueError('Plano de outra execucao do GitHub.')
    if record.get('preview'):
        report(f'Planejamento apenas: nenhuma rotina aplicada em {phase}.', env)
        return
    child_env = secret_environment(env)
    for item in record['routines']:
        name = item.get('name')
        if name not in catalog or item != catalog[name]:
            raise ValueError('Uma rotina ou seu script mudou depois do planejamento.')
        if item['phase'] != phase or name in record['completed']:
            continue
        if phase == 'after_desktop':
            child_env['DISPLAY'] = ':99'
        report('Executando rotina: ' + name, env)
        runner((root / item['script']).resolve(), root, child_env, item['timeout_seconds'])
        record['completed'].append(name)
        atomic_json(plan_file, record)
        report('Rotina concluida: ' + name, env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['list', 'plan', 'execute'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--plan-file', type=Path)
    parser.add_argument('--phase', choices=PHASES)
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--install-runtime', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    plan_file = args.plan_file or Path(os.environ.get('RUNNER_TEMP', str(root))) / 'startup-plan.json'
    if args.command == 'list':
        for name, item in load_catalog(root)[0].items():
            print(f'{name} [{item["phase"]}]: {item["description"]}')
    elif args.command == 'plan':
        plan(root, plan_file, os.environ.get('STARTUP_INSTRUCTION', ''),
             args.preview or os.environ.get('NEEDLE_PREVIEW_ONLY') == '1', args.install_runtime)
    else:
        if args.phase is None:
            parser.error('execute exige --phase')
        execute(root, plan_file, args.phase)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Do not echo the prompt, environment values or the model's reasoning.
        if isinstance(exc, (ValueError, RuntimeError)):
            message = str(exc)
        else:
            message = f'Falha na automacao ({type(exc).__name__}). Confira o catalogo, dependencias e rede.'
        print('::error::' + message, file=sys.stderr)
        sys.exit(1)
