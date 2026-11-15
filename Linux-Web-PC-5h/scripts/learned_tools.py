#!/usr/bin/env python3
"""Validate portable learned tools before local reuse or public persistence."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse

CATALOG = 'automacoes/aprendidas.json'
BRANCH = 'ferramentas-aprendidas'
PREFIX = 'automacoes/rotinas/aprendidas/'


def protect_public_text(text, secret_values=()):
    if not isinstance(text, str) or len(text) > 40000 or '\x00' in text:
        raise ValueError('Ferramenta gerada invalida ou grande demais.')
    for secret in secret_values:
        if not secret:
            continue
        variants = {secret, json.dumps(secret)[1:-1], urllib.parse.quote(secret, safe=''),
                    base64.b64encode(secret.encode()).decode()}
        if any(value and value in text for value in variants):
            raise ValueError('Ferramenta contem uma credencial; a publicacao foi bloqueada.')
    patterns = [r'-----BEGIN [A-Z ]*PRIVATE KEY-----', r'\b(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]{16,}',
                r'https?://[^\s/:]+:[^\s/@]+@', r'/home/[^/\s"\x27]+',
                r'\b\d{7,}:[A-Za-z0-9_-]{25,}',
                r'(?i)\b[A-Z0-9_]*(?:PASSWORD|TOKEN|API_KEY|SECRET|SESSION)[A-Z0-9_]*\s*=\s*["\x27]?[A-Za-z0-9/+_-]{16,}',
                r'(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{80,}(?![A-Za-z0-9])']
    if any(re.search(pattern, text) for pattern in patterns):
        raise ValueError('Conteudo privado ou credencial aparente na ferramenta; nao sera publicada.')


def validate_item(item, source, secret_values=()):
    fields = {'name', 'description', 'script', 'phase', 'timeout_seconds', 'task_sha256', 'script_sha256'}
    if not isinstance(item, dict) or set(item) != fields:
        raise ValueError('Metadados de ferramenta aprendida invalidos.')
    name = item['name']
    if not isinstance(name, str) or not re.fullmatch(r'learn_[a-f0-9]{24}', name):
        raise ValueError('Nome da ferramenta aprendida invalido.')
    if item['script'] != PREFIX + name + '.sh':
        raise ValueError('Caminho da ferramenta aprendida invalido.')
    if item['phase'] not in ('before_desktop', 'after_desktop', 'before_backup'):
        raise ValueError('Fase da ferramenta aprendida invalida.')
    if type(item['timeout_seconds']) is not int or not 1 <= item['timeout_seconds'] <= 900:
        raise ValueError('Timeout da ferramenta aprendida invalido.')
    if not isinstance(item['description'], str) or not 1 <= len(item['description']) <= 500:
        raise ValueError('Descricao da ferramenta aprendida invalida.')
    if not isinstance(item['task_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', item['task_sha256']):
        raise ValueError('Identificador de tarefa invalido.')
    path = (source / item['script'])
    if path.is_symlink() or not path.is_file() or source.resolve() not in path.resolve().parents:
        raise ValueError('Arquivo de ferramenta aprendida invalido.')
    code = path.read_text(encoding='utf-8')
    if hashlib.sha256(code.encode()).hexdigest() != item['script_sha256']:
        raise ValueError('Checksum da ferramenta aprendida invalido.')
    expected = 'learn_' + hashlib.sha256((item['phase'] + item['task_sha256'] + code).encode()).hexdigest()[:24]
    if name != expected:
        raise ValueError('Identidade da ferramenta aprendida invalida.')
    protect_public_text(json.dumps(item, ensure_ascii=False) + '\n' + code, secret_values)
    subprocess.run(['bash', '-n', str(path)], check=True, capture_output=True)
    return code


def load(source, secret_values=()):
    path = source / CATALOG
    if not path.exists():
        return []
    if path.is_symlink():
        raise ValueError('Catalogo aprendido nao pode ser um link.')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('routines'), list):
        raise ValueError('Catalogo aprendido invalido.')
    if len(data['routines']) > 128:
        raise ValueError('Catalogo aprendido excedeu 128 ferramentas.')
    seen = set()
    for item in data['routines']:
        if item['name'] in seen:
            raise ValueError('Ferramenta aprendida duplicada.')
        seen.add(item['name'])
        validate_item(item, source, secret_values)
    return data['routines']


def task_hash(task):
    return hashlib.sha256(' '.join(task.casefold().split()).encode()).hexdigest()
