#!/usr/bin/env python3
"""Content-addressed encrypted incremental backups, with a remote durable index."""
import argparse
import asyncio
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import uuid

import pyzipper
import teledrive_backup as transport

ROOT = Path(__file__).resolve().parents[1]


def digest_file(path):
    h = hashlib.sha256()
    with regular_reader(path) as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def regular_reader(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError('Not a regular file')
    return os.fdopen(fd, 'rb')


def profile_for(env):
    """Perfil do indice: comum aos dois repositorios do revezamento; senao, um por repositorio."""
    import handoff
    return handoff.shared_profile(env) or hashlib.sha256(env.get('GITHUB_REPOSITORY', 'local').encode()).hexdigest()[:16]


def fingerprint(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def config(root, env):
    data = json.loads((root / 'backup/escopo.json').read_text(encoding='utf-8'))
    values = json.loads(env['BACKUP_PATHS_JSON']) if env.get('BACKUP_PATHS_JSON', '').strip() else data['roots']
    if not isinstance(values, list) or not values or any(not isinstance(x, str) or not Path(x).is_absolute() for x in values):
        raise ValueError('As origens precisam ser uma lista de caminhos absolutos.')
    roots = []
    for value in values:
        p = Path(value)
        if p.is_symlink() or not p.exists():
            raise ValueError('Origem ausente ou com link simbolico.')
        p = p.resolve()
        if any(p == other or other in p.parents for other in roots):
            continue
        roots = [other for other in roots if p not in other.parents] + [p]
    excludes = [Path(x).absolute() for x in data.get('exclude', [])]
    # Virtual mounts and live staging can never be backed up recursively.
    excludes += list(map(Path, ('/proc', '/sys', '/dev', '/run', '/tmp', '/mnt', '/media')))
    if env.get('RUNNER_TEMP'):
        excludes.append(Path(env['RUNNER_TEMP']).resolve())
    if data.get('exclude_home_caches', True):
        home = Path(env.get('HOME', str(Path.home())))
        excludes += [home / '.cache', home / '.npm/_cacache']
    size = data.get('batch_mebibytes', 128)
    count = data.get('batch_files', 2000)
    if type(size) is not int or not 8 <= size <= 512 or type(count) is not int or not 1 <= count <= 10000:
        raise ValueError('Tamanho de lote invalido.')
    return roots, excludes, size * 1024 * 1024, count


def ignored(path, excluded):
    return any(path == p or p in path.parents for p in excluded)


def walk_files(roots, excluded, errors):
    for root in roots:
        if ignored(root, excluded):
            continue
        yield root
        if not root.is_dir():
            continue
        def failed(exc):
            errors.append({'path': str(exc.filename), 'reason': type(exc).__name__})
        for parent, dirs, files in os.walk(root, followlinks=False, onerror=failed):
            # lstat in the consumer records symlink directories without following them.
            dirs[:] = [x for x in dirs if not ignored(Path(parent) / x, excluded)]
            for name in dirs + files:
                path = Path(parent) / name
                if not ignored(path, excluded):
                    yield path


def new_index(profile):
    return {'format': 2, 'profile': profile, 'entries': {}, 'objects': {}, 'packs': {}, 'scan_complete': False}


def validate_index(data, profile):
    if not isinstance(data, dict) or data.get('format') != 2 or data.get('profile') != profile:
        raise ValueError('Indice incremental invalido ou de outro perfil.')
    for key in ('entries', 'objects', 'packs'):
        if not isinstance(data.get(key), dict):
            raise ValueError('Indice incremental incompleto.')
    for key, item in data['objects'].items():
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key) or item.get('pack') not in data['packs'] or item.get('entry') != key:
            raise ValueError('Referencia de objeto invalida.')
    for path, item in data['entries'].items():
        if not isinstance(path, str) or not path.startswith('/') or not isinstance(item, dict):
            raise ValueError('Entrada de arquivo invalida.')
        if item.get('kind') == 'file' and item.get('sha256') not in data['objects']:
            raise ValueError('Arquivo sem conteudo confirmado.')
    return data


def encode_index(index, path, secret):
    with pyzipper.AESZipFile(path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as z:
        z.setpassword(secret)
        z.setencryption(pyzipper.WZ_AES, nbits=256)
        z.writestr('index.json', json.dumps(index, ensure_ascii=True))


def decode_index(path, secret, profile):
    with pyzipper.AESZipFile(path) as z:
        z.setpassword(secret)
        entry = z.getinfo('index.json')
        if entry.file_size > 256 * 1024 * 1024:
            raise ValueError('Indice incremental grande demais.')
        return validate_index(json.loads(z.read('index.json')), profile)


def make_pack(items, path, secret):
    accepted = []
    errors = []
    with pyzipper.AESZipFile(path, 'w', compression=pyzipper.ZIP_DEFLATED,
                            compresslevel=1, encryption=pyzipper.WZ_AES, allowZip64=True) as z:
        z.setpassword(secret)
        z.setencryption(pyzipper.WZ_AES, nbits=256)
        seen = set()
        for item in items:
            source = Path(item['path'])
            try:
                if item['sha256'] not in seen:
                    with regular_reader(source) as f:
                        before = os.fstat(f.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise OSError('Not a regular file')
                        actual = hashlib.sha256()
                        # Zip64 is required even when a file grows during the copy.
                        info = z.zipinfo_cls(item['sha256'])
                        with z.open(info, 'w', force_zip64=True) as entry:
                            for block in iter(lambda: f.read(1024 * 1024), b''):
                                actual.update(block)
                                entry.write(block)
                        after = os.fstat(f.fileno())
                    if actual.hexdigest() != item['sha256'] or fingerprint(before) != fingerprint(after):
                        errors.append({'path': str(source), 'reason': 'Alterado durante a copia'})
                        continue
                    seen.add(item['sha256'])
                accepted.append(item)
            except OSError as exc:
                if exc.errno == 28:
                    raise RuntimeError('Sem espaco para preparar um lote do backup.') from None
                errors.append({'path': str(source), 'reason': type(exc).__name__})
    return accepted, errors


class TelegramStore:
    def __init__(self, client, channel, folder, profile, secret):
        self.client, self.channel, self.folder = client, channel, folder
        self.profile, self.secret = profile, secret
        self.index_name = 'indice-' + profile + '.zip'

    async def load(self):
        async for msg in self.client.iter_messages(self.channel, search=self.index_name):
            caption = msg.message or ''
            if not caption.startswith('td1:') or not msg.document:
                continue
            try:
                metadata = json.loads(caption[4:])
            except json.JSONDecodeError:
                continue
            if metadata.get('n') != self.index_name or metadata.get('p') != self.folder:
                continue
            with tempfile.TemporaryDirectory(prefix='incremental-index-') as td:
                path = Path(td) / self.index_name
                await self.client.download_media(msg, file=str(path))
                if digest_file(path) != metadata.get('h'):
                    raise ValueError('Checksum do indice remoto incorreto.')
                # Wrong passwords fail closed; do not assume an empty backup.
                index = decode_index(path, self.secret, self.profile)
            ids = {p['message_id'] for pack in index['packs'].values() for p in pack['parts']}
            available = {}
            id_list = list(ids)
            for offset in range(0, len(ids), 100):
                batch = id_list[offset:offset + 100]
                for message in await self.client.get_messages(self.channel, ids=batch):
                    if message and message.document:
                        available[message.id] = message.document.size
            lost = {name for name, pack in index['packs'].items()
                    if any(available.get(p['message_id']) != p['bytes'] for p in pack['parts'])}
            if lost:
                invalid = {h for h, obj in index['objects'].items() if obj['pack'] in lost}
                index['entries'] = {p: e for p, e in index['entries'].items() if e.get('sha256') not in invalid}
                index['objects'] = {h: obj for h, obj in index['objects'].items() if h not in invalid}
                index['packs'] = {h: pack for h, pack in index['packs'].items() if h not in lost}
            return index
        return new_index(self.profile)

    async def save_index(self, index):
        with tempfile.TemporaryDirectory(prefix='incremental-index-') as td:
            path = Path(td) / self.index_name
            encode_index(index, path, self.secret)
            await transport.send_document(self.client, self.channel, path, self.folder, digest_file(path))

    async def upload_pack(self, path, pack_id):
        parts = []
        overall = hashlib.sha256()
        with path.open('rb') as source:
            number = 0
            while chunk := source.read(transport.PART_BYTES):
                number += 1
                overall.update(chunk)
                part = path.parent / f'{pack_id}.zip.part{number:05d}'
                part.write_bytes(chunk)
                h = hashlib.sha256(chunk).hexdigest()
                try:
                    mid = await transport.send_document(self.client, self.channel, part, self.folder + '/pacotes', h)
                finally:
                    part.unlink(missing_ok=True)
                parts.append({'name': part.name, 'bytes': len(chunk), 'sha256': h, 'message_id': mid})
        return {'parts': parts, 'sha256': overall.hexdigest()}


async def cycle(roots, excludes, store, secret, batch_bytes=128 * 1024 * 1024, batch_files=2000):
    index = await store.load()
    working = copy.deepcopy(index)
    working['scan_complete'] = False
    errors, seen, pending = [], set(), []
    counts = {'scanned': 0, 'uploaded_files': 0, 'reused_files': 0, 'special_files': 0}
    pending_bytes = 0

    async def flush():
        nonlocal pending, pending_bytes
        if not pending:
            return
        pack_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix='incremental-pack-') as td:
            path = Path(td) / (pack_id + '.zip')
            try:
                accepted, failed = await asyncio.to_thread(make_pack, pending, path, secret)
            except (OSError, RuntimeError) as exc:
                # A file too large for staging must not prevent subsequent
                # batches of small files from being saved.
                errors.extend({'path': item['path'], 'reason': 'Falha no staging: ' + type(exc).__name__} for item in pending)
                pending, pending_bytes = [], 0
                print('::warning::Um lote nao coube no staging ou nao pode ser preparado; continuando os proximos.', flush=True)
                return
            errors.extend(failed)
            if accepted:
                pack = await store.upload_pack(path, pack_id)
                working['packs'][pack_id] = pack
                for item in accepted:
                    h = item['sha256']
                    working['objects'][h] = {'pack': pack_id, 'entry': h}
                    working['entries'][item['path']] = {k: v for k, v in item.items() if k != 'path'}
                    counts['uploaded_files'] += 1
                # Remote commit after each confirmed batch, before scanning further.
                working['updated_at'] = int(time.time())
                await store.save_index(working)
        pending, pending_bytes = [], 0

    for path in walk_files(roots, excludes, errors):
        name = str(path)
        seen.add(name)
        try:
            st = path.lstat()
            common = {'mode': stat.S_IMODE(st.st_mode), 'mtime_ns': st.st_mtime_ns}
            if stat.S_ISLNK(st.st_mode):
                working['entries'][name] = dict(common, kind='symlink', target=os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                working['entries'][name] = dict(common, kind='directory')
            elif stat.S_ISREG(st.st_mode):
                counts['scanned'] += 1
                h = await asyncio.to_thread(digest_file, path)
                if fingerprint(st) != fingerprint(path.lstat()):
                    errors.append({'path': name, 'reason': 'Alterado durante a leitura'})
                    continue
                item = dict(common, kind='file', path=name, sha256=h, size=st.st_size)
                if h in working['objects']:
                    working['entries'][name] = {k: v for k, v in item.items() if k != 'path'}
                    counts['reused_files'] += 1
                else:
                    pending.append(item)
                    pending_bytes += st.st_size
                    if pending_bytes >= batch_bytes or len(pending) >= batch_files:
                        await flush()
            else:
                counts['special_files'] += 1
        except OSError as exc:
            errors.append({'path': name, 'reason': type(exc).__name__})
    await flush()
    if not errors:
        working['entries'] = {p: e for p, e in working['entries'].items() if p in seen}
    # Never delete remote packs. Old indexes preserve previous versions/deletions.
    working.update(updated_at=int(time.time()), scan_complete=not errors, errors=errors,
                   counts=counts, roots=[str(p) for p in roots], excluded=[str(p) for p in excludes])
    await store.save_index(working)
    print('Incremental: ' + json.dumps(dict(counts, errors=len(errors))), flush=True)
    return working


async def run(root=ROOT):
    env = os.environ
    roots, excludes, batch_bytes, batch_files = config(root, env)
    folder = env.get('BACKUP_FOLDER', 'Linux Web PC').strip().strip('/')
    if not folder or len(folder) > 120 or any(p in ('', '.', '..') for p in folder.split('/')):
        raise ValueError('BACKUP_FOLDER invalida.')
    profile = profile_for(env)
    layout = json.loads((root / 'backup/escopo.json').read_text(encoding='utf-8')).get('layout', 'encrypted_packs')
    if layout not in ('original_names', 'encrypted_packs'):
        raise ValueError('layout deve ser original_names ou encrypted_packs.')
    secret = transport.password()
    # Indice compartilhado: a sessao nova espera a antiga terminar para ninguem sobrescrever o indice do outro.
    import handoff
    handoff.wait_for_older_peer(env)
    lock_dir = Path(env.get('RUNNER_TEMP', tempfile.gettempdir()))
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / 'incremental.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        client, channel = await transport.connect()
        try:
            if layout == 'original_names':
                from original_backup import run_with_client
                result = await run_with_client(client, channel, folder, profile, secret, roots, excludes, env)
            else:
                store = TelegramStore(client, channel, folder + '/_incremental/' + profile, profile, secret)
                result = await cycle(roots, excludes, store, secret, batch_bytes, batch_files)
            summary = env.get('GITHUB_STEP_SUMMARY')
            if summary:
                with open(summary, 'a', encoding='utf-8') as output:
                    output.write(f'\nIncremental: {result["counts"]["uploaded_files"]} enviados; {result["counts"]["reused_files"]} reutilizados; {len(result["errors"])} erros de leitura.\n')
            return result
        finally:
            await client.disconnect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.root.resolve()))
        sys.exit(2 if result['errors'] else 0)
    except Exception as exc:
        detail = str(exc) if getattr(exc, 'public_message', False) else type(exc).__name__
        print('::error::Backup incremental falhou: ' + detail + '. O indice anterior foi preservado.', file=sys.stderr)
        sys.exit(1)
