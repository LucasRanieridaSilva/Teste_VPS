#!/usr/bin/env python3
"""Restore a downloaded incremental index + packs into a separate empty folder."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import pyzipper


def sha_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def restore_named(index, source, output, prefix):
    """Copy verified files already downloaded/decrypted through TeleDrive."""
    def selected(name):
        return not prefix or name == prefix or name.startswith(prefix.rstrip('/') + '/')
    def safe_target(base, name):
        parts = name.lstrip('/').split('/')
        if not name.startswith('/') or any(x in ('..', '.') or (os.name == 'nt' and (':' in x or '\\' in x)) for x in parts):
            raise ValueError('Caminho de restauracao invalido.')
        target = base.joinpath(*parts)
        if target != base and base.resolve() not in target.resolve().parents:
            raise ValueError('Caminho fora do destino.')
        return target
    for name in index.get('directories', []):
        if selected(name):
            safe_target(output, name).mkdir(parents=True, exist_ok=True)
    count = 0
    for name, item in index.get('files', {}).items():
        if not selected(name):
            continue
        original = safe_target(source.resolve(), name)
        if not original.is_file() or original.stat().st_size != item['size'] or sha_file(original) != item['sha256']:
            raise ValueError('Arquivo original ausente ou diferente do indice.')
        target = safe_target(output, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        try:
            with original.open('rb') as src, target.open('xb') as dest:
                for data in iter(lambda: src.read(1024 * 1024), b''):
                    h.update(data)
                    dest.write(data)
            if h.hexdigest() != item['sha256']:
                raise ValueError('Arquivo alterado durante a restauracao.')
            target.chmod(item['mode'] & 0o777)
            os.utime(target, ns=(item['mtime_ns'], item['mtime_ns']))
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        count += 1
    for name, item in index.get('empty_files', {}).items():
        if selected(name) and name not in index.get('files', {}):
            target = safe_target(output, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.open('xb').close()
            target.chmod(item['mode'] & 0o777)
            os.utime(target, ns=(item['mtime_ns'], item['mtime_ns']))
            count += 1
    links = [{'path': p, 'target': t} for p, t in index.get('symlinks', {}).items() if selected(p)]
    report = output / 'links-nao-restaurados.json'
    number = 0
    while report.exists():
        number += 1
        report = output / f'links-nao-restaurados-{number}.json'
    with report.open('x', encoding='utf-8') as f:
        json.dump(links, f, ensure_ascii=True, indent=2)
    return count


def restore(index_path, packs_directory, output, password, prefix=None):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use uma pasta de destino vazia; arquivos existentes nao serao substituidos.')
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    with pyzipper.AESZipFile(index_path) as z:
        z.setpassword(password)
        entry = z.getinfo('index.json')
        if entry.file_size > 256 * 1024 * 1024:
            raise ValueError('Indice grande demais.')
        index = json.loads(z.read('index.json'))
    if index.get('format') == 3:
        return restore_named(index, packs_directory, output, prefix)
    if index.get('format') != 2:
        raise ValueError('Formato de indice invalido.')
    grouped, links = {}, []
    dirs = []
    for name, meta in index['entries'].items():
        if prefix and not (name == prefix or name.startswith(prefix.rstrip('/') + '/')):
            continue
        parts = name.lstrip('/').split('/')
        if any(x in ('..', '.') or (os.name == 'nt' and (':' in x or '\\' in x)) for x in parts):
            raise ValueError('Caminho de restauracao invalido.')
        target = output.joinpath(*parts)
        if target != output and output not in target.resolve().parents:
            raise ValueError('Entrada fora da pasta de destino.')
        if meta['kind'] == 'directory':
            target.mkdir(parents=True, exist_ok=True)
            dirs.append((target, meta))
        elif meta['kind'] == 'symlink':
            links.append({'path': name, 'target': meta['target']})
        elif meta['kind'] == 'file':
            obj = index['objects'][meta['sha256']]
            grouped.setdefault(obj['pack'], []).append((target, meta, obj))
    for pack_id, files in grouped.items():
        if not re.fullmatch('[a-f0-9]{32}', pack_id):
            raise ValueError('Identificador de pacote invalido.')
        pack = index['packs'][pack_id]
        with tempfile.TemporaryDirectory(prefix='restore-incremental-') as td:
            archive = Path(td) / 'pack.zip'
            overall = hashlib.sha256()
            with archive.open('wb') as dest:
                for number, item in enumerate(pack['parts'], 1):
                    expected = f'{pack_id}.zip.part{number:05d}'
                    if item['name'] != expected:
                        raise ValueError('Nome de parte invalido.')
                    matches = list(packs_directory.rglob(expected))
                    if len(matches) != 1:
                        raise ValueError('Parte ausente ou duplicada: ' + expected)
                    source = matches[0]
                    if source.stat().st_size != item['bytes'] or sha_file(source) != item['sha256']:
                        raise ValueError('Parte corrompida.')
                    with source.open('rb') as f:
                        for data in iter(lambda: f.read(1024 * 1024), b''):
                            overall.update(data)
                            dest.write(data)
            if overall.hexdigest() != pack['sha256']:
                raise ValueError('Checksum do pacote incorreto.')
            with pyzipper.AESZipFile(archive) as z:
                z.setpassword(password)
                for target, meta, obj in files:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    h, size = hashlib.sha256(), 0
                    try:
                        with z.open(obj['entry']) as source, target.open('xb') as dest:
                            for data in iter(lambda: source.read(1024 * 1024), b''):
                                h.update(data)
                                size += len(data)
                                dest.write(data)
                        if h.hexdigest() != meta['sha256'] or size != meta['size']:
                            raise ValueError('Conteudo restaurado incorreto.')
                        target.chmod(meta['mode'] & 0o777)
                        os.utime(target, ns=(meta['mtime_ns'], meta['mtime_ns']))
                    except BaseException:
                        target.unlink(missing_ok=True)
                        raise
    # Symlinks are reported, never followed/created during restore.
    report = output / 'links-nao-restaurados.json'
    number = 0
    while report.exists():
        number += 1
        report = output / f'links-nao-restaurados-{number}.json'
    with report.open('x', encoding='utf-8') as f:
        json.dump(links, f, ensure_ascii=False, indent=2)
    for target, meta in reversed(dirs):
        if target != output:
            target.chmod(meta['mode'] & 0o777)
            os.utime(target, ns=(meta['mtime_ns'], meta['mtime_ns']))
    return sum(len(files) for files in grouped.values())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--indice', type=Path, required=True)
    parser.add_argument('--pacotes', type=Path, required=True)
    parser.add_argument('--destino', type=Path, required=True)
    parser.add_argument('--prefixo')
    args = parser.parse_args()
    secret = os.environ.get('BACKUP_PASSWORD') or getpass.getpass('Senha do backup: ')
    count = restore(args.indice, args.pacotes, args.destino, secret.encode(), args.prefixo)
    print(f'{count} arquivos restaurados em {args.destino}. Links simbolicos foram listados separadamente.')
