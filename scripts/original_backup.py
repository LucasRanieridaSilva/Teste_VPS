#!/usr/bin/env python3
"""Incremental TeleDrive tree with original names, multipart files and empty folders."""
import asyncio
import copy
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import tempfile
import time
import uuid

import pyzipper
from telethon.errors import MessageNotModifiedError

import teledrive_backup as transport
import teledrive_native as native


def version_id(profile, path, digest, key_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ':'.join(('teledrive-mirror', profile, path, digest, key_id or 'plain'))))


def tree_path(base, path):
    value = PurePosixPath(path)
    if not value.is_absolute() or '..' in value.parts:
        raise native.NativeBackupError('Caminho de origem invalido.')
    return base.rstrip('/') + ('/' + value.as_posix().lstrip('/') if str(value) != '/' else '')


def fresh_index(profile, key_id):
    return {'format': 3, 'profile': profile, 'key_id': key_id, 'files': {}, 'versions': {},
            'directories': [], 'symlinks': {}, 'empty_files': {}, 'errors': [], 'scan_complete': False}


class NamedStore:
    def __init__(self, client, channel, folder, profile, secret, key, tree, part_bytes=transport.PART_BYTES):
        self.client, self.channel, self.folder = client, channel, folder
        self.profile, self.secret, self.key, self.tree = profile, secret, key, tree
        self.key_id = hashlib.sha256(key).hexdigest() if key else None
        self.base = folder + '/Arquivos'
        channel_id = str(getattr(channel, 'id', channel))
        self.identity_scope = profile + ':' + channel_id + ':' + self.base
        self.tree.profile = profile + ':' + channel_id
        self.control = folder + '/_controle/' + profile
        self.index_name = 'arvore-' + profile + '.zip'
        self.part_bytes = part_bytes
        self.index = fresh_index(profile, self.key_id)
        self.latest_index_id = 0
        self.pending = 0
        self.available = {}

    async def load(self):
        from incremental_backup import digest_file
        async for message in self.client.iter_messages(self.channel, search=self.index_name):
            try:
                meta = native.parse_caption(message.message or '', self.key)
            except (ValueError, json.JSONDecodeError):
                continue
            if not message.document or not meta or meta.get('n') != self.index_name or meta.get('p') != self.control:
                continue
            with tempfile.TemporaryDirectory(prefix='named-backup-index-') as td:
                path = Path(td) / self.index_name
                await self.client.download_media(message, file=str(path))
                if digest_file(path) != meta.get('h'):
                    raise native.NativeBackupError('Indice da arvore com checksum incorreto.')
                with pyzipper.AESZipFile(path) as archive:
                    archive.setpassword(self.secret)
                    if archive.getinfo('index.json').file_size > 256 * 1024 * 1024:
                        raise native.NativeBackupError('Indice da arvore grande demais.')
                    data = json.loads(archive.read('index.json'))
            if data.get('format') != 3 or data.get('profile') != self.profile or not isinstance(data.get('files'), dict) or not isinstance(data.get('versions'), dict):
                raise native.NativeBackupError('Indice da arvore invalido ou de outro perfil.')
            if data.get('key_id') is not None and data['key_id'] != self.key_id:
                raise native.NativeBackupError('A chave nativa mudou. Use outra BACKUP_FOLDER para preservar os dados antigos.')
            self.index = data
            self.index['key_id'] = self.key_id
            self.latest_index_id = message.id
            break
        await self.recover_recent()
        ids = list({r['message_id'] for entry in self.index['files'].values() for r in entry['refs']})
        for offset in range(0, len(ids), 100):
            batch = ids[offset:offset + 100]
            messages = await self.client.get_messages(self.channel, ids=batch)
            for mid, message in zip(batch, messages):
                self.available[mid] = message.document.size if message and message.document else None
        return self.index

    async def recover_recent(self):
        # Recover completed files sent after the last checkpoint. Hidden staged
        # parts are never trusted: their source hash may not have been verified.
        groups = {}
        async for message in self.client.iter_messages(self.channel, min_id=self.latest_index_id, limit=2000):
            if not message.document:
                continue
            try:
                m = native.parse_caption(message.message or '', self.key)
                if not m or m.get('hd') or m.get('ar') or m.get('tr') is not None:
                    continue
                parent = m.get('p') or self.tree.path_of(m.get('fid'))
                if not parent or not parent.startswith(self.base + '/') or not isinstance(m.get('n'), str) or '/' in m['n']:
                    continue
                dest = parent + '/' + m['n']
                source = '/' + dest[len(self.base) + 1:]
                h = m.get('h', '')
                if len(h) != 64 or any(c not in '0123456789abcdef' for c in h):
                    continue
                if m.get('id') != version_id(self.identity_scope, source, h, self.key_id):
                    continue
                count = max(1, m.get('pc', 0))
                part = m.get('pi', 0)
                if type(count) is not int or type(part) is not int or not 0 <= part < count <= 100000:
                    continue
                bucket = groups.setdefault(m['id'], {})
                previous = bucket.get(part)
                if previous is None or previous['message_id'] < message.id:
                    bucket[part] = {'message_id': message.id, 'bytes': message.document.size, 'manifest': m}
            except (ValueError, KeyError, TypeError):
                continue
        for ident, parts in groups.items():
            first = parts.get(0)
            if not first:
                continue
            m = first['manifest']
            count = max(1, m.get('pc', 0))
            if set(parts) != set(range(count)):
                continue
            if any(p['manifest'].get('h') != m['h'] or p['manifest'].get('s') != m['s'] for p in parts.values()):
                continue
            source = '/' + ((m.get('p') or self.tree.path_of(m.get('fid'))) + '/' + m['n'])[len(self.base) + 1:]
            entry = {'path': source, 'sha256': m['h'], 'size': m['s'], 'mtime_ns': m.get('bn', m['mt'] * 1000000),
                     'mode': m.get('bm', 0o600), 'key_id': self.key_id, 'version_id': ident,
                     'refs': [parts[i] for i in range(count)]}
            old = self.index['files'].get(source)
            self.index['versions'][ident] = entry
            if old is None or first['message_id'] > old['refs'][0]['message_id']:
                self.index['files'][source] = entry

    async def checkpoint(self, force=False):
        if not force and self.pending < 25:
            return
        from incremental_backup import encode_index, digest_file
        with tempfile.TemporaryDirectory(prefix='named-backup-index-') as td:
            path = Path(td) / self.index_name
            encode_index(self.index, path, self.secret)
            m = {'v': 1, 'id': str(uuid.uuid4()), 'n': self.index_name, 'p': self.control,
                 'm': 'application/zip', 's': path.stat().st_size, 'h': digest_file(path),
                 'e': False, 'hd': True, 'ct': int(time.time() * 1000), 'mt': int(time.time() * 1000)}
            result = await self.client.send_file(self.channel, str(path), force_document=True,
                            caption=native.caption(m), parse_mode=None)
            if not result or not result.document or result.document.size != path.stat().st_size:
                raise native.NativeBackupError('Indice da arvore nao foi confirmado pelo Telegram.')
            self.latest_index_id = result.id
        self.pending = 0

    async def refs_available(self, entry):
        refs = entry.get('refs', [])
        if not refs:
            return False
        missing = [r['message_id'] for r in refs if r['message_id'] not in self.available]
        if missing:
            messages = await self.client.get_messages(self.channel, ids=missing)
            for mid, message in zip(missing, messages):
                self.available[mid] = message.document.size if message and message.document else None
        return all(self.available.get(r['message_id']) == r['bytes'] for r in refs)

    async def set_flags(self, entry, hidden=False, archived=False):
        for ref in entry['refs']:
            m = dict(ref['manifest'], hd=hidden, ar=archived)
            if ref['manifest'].get('hd', False) != hidden or ref['manifest'].get('ar', False) != archived:
                m['mt'] = max(m['mt'], int(time.time() * 1000))
            try:
                await self.client.edit_message(self.channel, ref['message_id'],
                                native.caption(m, self.key), parse_mode=None)
            except MessageNotModifiedError:
                pass
            ref['manifest'] = m

    def manifest(self, item, ident, count, part, offset, size):
        dest = PurePosixPath(tree_path(self.base, item['path']))
        parent = dest.parent.as_posix()
        fid = self.tree.ensure(parent)
        result = {'v': 2 if count > 1 else 1, 'id': ident, 'n': dest.name, 'p': parent, 'fid': fid,
                  'm': mimetypes.guess_type(dest.name)[0] or 'application/octet-stream',
                  's': item['size'], 'h': item['sha256'], 'e': self.key is not None,
                  'hd': count > 1, 'ct': int(time.time() * 1000), 'mt': item['mtime_ns'] // 1000000,
                  'bm': item['mode'], 'bn': item['mtime_ns']}
        if count > 1:
            result.update(pc=count, pi=part, po=offset, ps=size)
        return result

    async def upload(self, item, content_source=None):
        from incremental_backup import regular_reader, fingerprint
        ident = version_id(self.identity_scope, item['path'], item['sha256'], self.key_id)
        count = max(1, math.ceil(item['size'] / self.part_bytes))
        entry = dict(item, key_id=self.key_id, version_id=ident, refs=[])
        old = self.index['files'].get(item['path'])
        staged = []
        # A server-side clone avoids uploading identical bytes for another path.
        cloned = bool(content_source and await self.refs_available(content_source))
        if cloned:
            for part, original in enumerate(content_source['refs']):
                count = len(content_source['refs'])
                source_m = original['manifest']
                size = source_m.get('ps', item['size'])
                offset = source_m.get('po', 0)
                m = self.manifest(item, ident, count, part, offset, size)
                m['hd'] = True
                message = (await self.client.get_messages(self.channel, ids=[original['message_id']]))[0]
                result = await self.client.send_file(self.channel, message.document,
                            caption=native.caption(m, self.key), force_document=True, parse_mode=None)
                if not result or not result.document or result.document.size != original['bytes']:
                    raise native.NativeBackupError('Copia interna do documento nao confirmada.')
                staged.append({'message_id': result.id, 'bytes': result.document.size, 'manifest': m})
        else:
            with regular_reader(Path(item['path'])) as source:
                before = os.fstat(source.fileno())
                h = hashlib.sha256()
                for part in range(count):
                    offset = part * self.part_bytes
                    size = min(self.part_bytes, max(0, item['size'] - offset))
                    m = self.manifest(item, ident, count, part, offset, size)
                    # Read/verify a single-part file before sending it visibly.
                    with tempfile.TemporaryDirectory(prefix='named-file-part-') as td:
                        # Part transport names can be short; the manifest keeps
                        # the exact original name, even at the filesystem limit.
                        name = (uuid.uuid4().hex + '.bin') if self.key or count > 1 else m['n']
                        path = Path(td) / name
                        with path.open('wb') as dest:
                            native.write_segment(source, dest, size, h, self.key)
                        if count == 1 and (h.hexdigest() != item['sha256'] or fingerprint(before) != fingerprint(os.fstat(source.fileno()))):
                            raise OSError('Arquivo alterado durante a copia.')
                        result = await self.client.send_file(self.channel, str(path), force_document=True,
                                    caption=native.caption(m, self.key), parse_mode=None)
                        if not result or not result.document or result.document.size != path.stat().st_size:
                            raise native.NativeBackupError('Envio do arquivo nao confirmado pelo Telegram.')
                        staged.append({'message_id': result.id, 'bytes': result.document.size, 'manifest': m})
                if h.hexdigest() != item['sha256'] or fingerprint(before) != fingerprint(os.fstat(source.fileno())):
                    raise OSError('Arquivo alterado durante a copia.')
        entry['refs'] = staged
        for ref in staged:
            self.available[ref['message_id']] = ref['bytes']
        # No multipart file becomes visible before all parts and source hash are confirmed.
        if any(ref['manifest'].get('hd') for ref in staged):
            await self.set_flags(entry, hidden=False)
        if old and old.get('version_id') != ident:
            await self.set_flags(old, archived=True)
        self.index['files'][item['path']] = entry
        self.index['versions'][ident] = entry
        self.index.setdefault('empty_files', {}).pop(item['path'], None)
        self.index.setdefault('symlinks', {}).pop(item['path'], None)
        self.pending += 1
        await self.checkpoint()
        return entry, cloned


async def cycle(roots, excludes, store):
    from incremental_backup import walk_files, digest_file, fingerprint
    import stat
    index = await store.load()
    errors = []
    counts = {'scanned': 0, 'uploaded_files': 0, 'reused_files': 0, 'special_files': 0, 'cloned_files': 0}
    paths = []
    # Folder records first, so empty folders and all file fid references are durable.
    for path in walk_files(roots, excludes, errors):
        try:
            st = path.lstat()
            if stat.S_ISDIR(st.st_mode):
                store.tree.ensure(tree_path(store.base, path.as_posix()))
                paths.append(('directory', path, st))
            elif stat.S_ISLNK(st.st_mode):
                index['symlinks'][str(path)] = os.readlink(path)
            elif stat.S_ISREG(st.st_mode):
                store.tree.ensure(str(PurePosixPath(tree_path(store.base, path.as_posix())).parent))
                paths.append(('file', path, st))
            else:
                counts['special_files'] += 1
        except (OSError, UnicodeError, native.NativeBackupError) as exc:
            errors.append({'path': str(path), 'reason': type(exc).__name__})
    await native.publish_folder_tree(store.client, store.channel, store.tree, store.key)
    index['directories'] = [str(p) for kind, p, _ in paths if kind == 'directory']
    by_hash = {(r['sha256'], r.get('key_id')): r for r in index['versions'].values()}
    for kind, path, _ in paths:
        if kind != 'file':
            continue
        name = str(path)
        try:
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise OSError('Tipo de arquivo mudou durante a varredura.')
            counts['scanned'] += 1
            h = await asyncio.to_thread(digest_file, path)
            if fingerprint(st) != fingerprint(path.lstat()):
                raise OSError('Arquivo alterado durante a leitura.')
            old = index['files'].get(name)
            if old and old['sha256'] == h and old.get('key_id') == store.key_id and await store.refs_available(old):
                counts['reused_files'] += 1
                continue
            item = {'path': name, 'sha256': h, 'size': st.st_size, 'mode': stat.S_IMODE(st.st_mode), 'mtime_ns': st.st_mtime_ns}
            if st.st_size == 0 and store.key is None:
                # Telegram rejects empty document uploads. Preserve exact zero
                # bytes in the encrypted inventory, never upload fake contents.
                index.setdefault('empty_files', {})[name] = item
                if old:
                    await store.set_flags(old, archived=True)
                    index['files'].pop(name, None)
                errors.append({'path': name, 'reason': 'Arquivo vazio preservado no indice; nao e um documento visivel sem criptografia nativa.'})
                continue
            candidate = by_hash.get((h, store.key_id))
            entry, cloned = await store.upload(item, candidate)
            by_hash[(h, store.key_id)] = entry
            counts['cloned_files' if cloned else 'uploaded_files'] += 1
        except (OSError, UnicodeError, native.NativeBackupError) as exc:
            errors.append({'path': name, 'reason': type(exc).__name__})
    index.update(updated_at=int(time.time()), errors=errors, counts=counts, scan_complete=not errors,
                 roots=[str(p) for p in roots], excluded=[str(p) for p in excludes], tree_root=store.base)
    await store.checkpoint(force=True)
    # Keep the folder-state marker recent even after a long first upload.
    await native.publish_folder_tree(store.client, store.channel, store.tree, store.key, force=True)
    print('Arvore original: ' + json.dumps(dict(counts, errors=len(errors))), flush=True)
    return index


async def run_with_client(client, channel, folder, profile, secret, roots, excludes, env):
    key = await native.existing_content_key(client, channel, env.get('TELEDRIVE_PASSPHRASE', ''))
    tree = await native.read_folder_tree(client, channel, key, profile)
    store = NamedStore(client, channel, folder, profile, secret, key, tree)
    return await cycle(roots, excludes, store)
