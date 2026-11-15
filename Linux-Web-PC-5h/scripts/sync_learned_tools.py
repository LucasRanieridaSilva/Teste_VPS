#!/usr/bin/env python3
"""Read/write only the learned catalog and immutable scripts on a dedicated branch."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import learned_tools as learned
from needle_startup import atomic_json


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('GITHUB_REPOSITORY invalido.')
        self.url = 'https://api.github.com/repos/' + repository
        self.token = token

    def call(self, method, path, data=None, missing=False):
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'linux-web-pc-tools',
                   'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        req = urllib.request.Request(self.url + path, method=method, headers=headers,
                    data=json.dumps(data).encode() if data is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if missing and exc.code == 404:
                return None
            # Do not leak response bodies, input files or credentials to public logs.
            raise RuntimeError(f'GitHub recusou a sincronizacao HTTP {exc.code}. Verifique permissoes e regras da branch.') from None

    def content(self, path, ref):
        value = self.call('GET', '/contents/' + urllib.parse.quote(path, safe='/') +
                          '?ref=' + urllib.parse.quote(ref, safe=''), missing=True)
        if value is None:
            return None
        if value.get('encoding') != 'base64' or value.get('size', 0) > 200000:
            raise ValueError('Arquivo remoto de ferramenta invalido.')
        return base64.b64decode(value['content'], validate=False)


def download_catalog(api, destination):
    raw = api.content(learned.CATALOG, learned.BRANCH)
    if raw is None:
        return []
    doc = json.loads(raw)
    if not isinstance(doc, dict) or not isinstance(doc.get('routines'), list) or len(doc['routines']) > 128:
        raise ValueError('Catalogo remoto invalido.')
    for item in doc['routines']:
        name = item.get('name', '') if isinstance(item, dict) else ''
        if not re.fullmatch('learn_[a-f0-9]{24}', name) or item.get('script') != learned.PREFIX + name + '.sh':
            raise ValueError('Caminho remoto de ferramenta invalido.')
        raw_script = api.content(item['script'], learned.BRANCH)
        if raw_script is None:
            raise ValueError('Script remoto ausente.')
        path = destination / item['script']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw_script)
    atomic_json(destination / learned.CATALOG, doc)
    return learned.load(destination)


def pull(api, root):
    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        items = download_catalog(api, temp)
        if not items:
            print('Nenhuma ferramenta aprendida remota; usando catalogo local.')
            return
        # Validate the complete batch before applying any remote file.
        for item in items:
            target = root / item['script']
            if target.is_symlink() or any(p.is_symlink() for p in target.parents if p != root.parent):
                raise ValueError('Destino de ferramenta nao pode conter links.')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(temp / item['script'], target)
        atomic_json(root / learned.CATALOG, {'version': 1, 'routines': items})
        print(f'{len(items)} ferramenta(s) aprendida(s) carregada(s) do repositorio.')


def export(root, output, secrets=()):
    items = learned.load(root, secrets)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, mode=0o700)
    for item in items:
        target = output / item['script']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / item['script'], target)
    atomic_json(output / learned.CATALOG, {'version': 1, 'routines': items})
    print(f'{len(items)} ferramenta(s) validadas para persistencia.')


def publish(api, source, secret_values=()):
    local = learned.load(source, secret_values)
    if not local:
        print('Nenhuma ferramenta para publicar.')
        return
    for attempt in range(3):
        ref = api.call('GET', '/git/ref/heads/' + learned.BRANCH, missing=True)
        exists = ref is not None
        if ref is None:
            repo = api.call('GET', '')
            ref = api.call('GET', '/git/ref/heads/' + urllib.parse.quote(repo['default_branch'], safe=''))
        parent = ref['object']['sha']
        commit = api.call('GET', '/git/commits/' + parent)
        with tempfile.TemporaryDirectory() as td:
            remote_root = Path(td)
            remote = download_catalog(api, remote_root) if exists else []
            merged = {x['name']: x for x in remote}
            changes = []
            for item in local:
                previous = merged.get(item['name'])
                if previous is not None:
                    if previous != item:
                        raise ValueError('Colisao em ferramenta imutavel; publicacao interrompida.')
                    continue
                merged[item['name']] = item
                changes.append(item)
            if not changes:
                print('Repositorio ja possui todas as ferramentas validadas.')
                return
            if len(merged) > 128:
                raise ValueError('Catalogo remoto atingiu o limite de ferramentas.')
            files = {item['script']: (source / item['script']).read_bytes() for item in changes}
            files[learned.CATALOG] = json.dumps({'version': 1, 'routines': list(merged.values())}, ensure_ascii=False, indent=2).encode()
            tree = []
            for path, content in files.items():
                blob = api.call('POST', '/git/blobs', {'content': base64.b64encode(content).decode(), 'encoding': 'base64'})
                tree.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
            new_tree = api.call('POST', '/git/trees', {'base_tree': commit['tree']['sha'], 'tree': tree})
            new_commit = api.call('POST', '/git/commits', {'message': 'Persist validated learned tools',
                              'tree': new_tree['sha'], 'parents': [parent]})
            try:
                if exists:
                    api.call('PATCH', '/git/refs/heads/' + learned.BRANCH, {'sha': new_commit['sha'], 'force': False})
                else:
                    api.call('POST', '/git/refs', {'ref': 'refs/heads/' + learned.BRANCH, 'sha': new_commit['sha']})
                print(f'{len(changes)} ferramenta(s) publicadas na branch {learned.BRANCH}.')
                return
            except RuntimeError:
                if attempt == 2:
                    raise
                time.sleep(1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['pull', 'export', 'publish'])
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--directory', type=Path)
    args = p.parse_args()
    if args.command == 'export':
        if args.directory is None:
            p.error('export exige --directory')
        values = json.loads(os.environ.get('STARTUP_SECRETS_JSON', '{}') or '{}').values()
        export(args.root.resolve(), args.directory.resolve(), values)
    else:
        api = GitHub(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN', ''))
        if args.command == 'pull':
            pull(api, args.root.resolve())
        else:
            if args.directory is None:
                p.error('publish exige --directory')
            values = json.loads(os.environ.get('STARTUP_SECRETS_JSON', '{}') or '{}').values()
            publish(api, args.directory.resolve(), values)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('::error::Persistencia de ferramentas falhou (' + type(exc).__name__ + '). Confira permissoes e catalogo.', file=sys.stderr)
        sys.exit(1)
