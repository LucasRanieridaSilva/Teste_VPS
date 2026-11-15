#!/usr/bin/env python3
"""TeleDrive manifests, folder tree and optional native encryption.

Protocol inspected at Mahmud0808/TeleDrive commit
7576df8c6ab12f35286a1359b7a88842e92efadb. This module never creates or replaces
the drive's content key; it can only restore an existing key backup.
"""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import struct
import time
import uuid

from Cryptodome.Cipher import AES

FRAME_SIZE = 1024 * 1024


class NativeBackupError(RuntimeError):
    public_message = True


def seal_bytes(key, data):
    nonce = os.urandom(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
    body, tag = cipher.encrypt_and_digest(data)
    return nonce + body + tag


def open_bytes(key, data):
    if len(data) < 28:
        raise ValueError('Bloco criptografado truncado.')
    cipher = AES.new(key, AES.MODE_GCM, nonce=data[:12], mac_len=16)
    return cipher.decrypt_and_verify(data[12:-16], data[-16:])


def restore_content_key(blob, passphrase):
    if len(blob) < 27 or blob[:5] != b'TDKB\x01':
        raise NativeBackupError('Backup da chave TeleDrive invalido.')
    iterations = struct.unpack('>I', blob[5:9])[0]
    hint_length = struct.unpack('>H', blob[25:27])[0]
    if not 10000 <= iterations <= 10000000 or hint_length > 512 or len(blob) <= 27 + hint_length:
        raise NativeBackupError('Cabecalho da chave TeleDrive invalido.')
    kek = hashlib.pbkdf2_hmac('sha256', passphrase.encode('utf-8'), blob[9:25], iterations, dklen=32)
    try:
        key = open_bytes(kek, blob[27 + hint_length:])
    except ValueError:
        raise NativeBackupError('TELEDRIVE_PASSPHRASE incorreta ou backup da chave corrompido.') from None
    if len(key) != 32:
        raise NativeBackupError('Tamanho de chave TeleDrive invalido.')
    return key


async def existing_content_key(client, channel, passphrase):
    async for message in client.iter_messages(channel, search='teledrive.keybackup'):
        if not message.document or message.file.name != 'teledrive.keybackup':
            continue
        if not passphrase:
            raise NativeBackupError('O drive possui chave nativa. Configure TELEDRIVE_PASSPHRASE nos Secrets para enviar arquivos com essa criptografia.')
        if message.document.size > 4096:
            raise NativeBackupError('Backup da chave TeleDrive grande demais.')
        blob = await client.download_media(message, file=bytes)
        return restore_content_key(blob, passphrase)
    if passphrase:
        raise NativeBackupError('Nao foi encontrado teledrive.keybackup. Ative e configure a recuperacao da criptografia no app primeiro.')
    # Refuse a plaintext downgrade when encrypted documents exist but their key
    # backup cannot be found. No key is silently created or replaced.
    async for message in client.iter_messages(channel, limit=500):
        if (message.message or '').startswith('tde1:'):
            raise NativeBackupError('Ha arquivos criptografados sem chave recuperavel. Restaure/configure a chave pelo TeleDrive antes de continuar.')
    return None


def caption(manifest, key=None):
    payload = json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    text = ('tde1:' + base64.b64encode(seal_bytes(key, payload)).decode('ascii')) if key else ('td1:' + payload.decode())
    if len(text.encode('utf-16-le')) // 2 > 1024:
        # fid still preserves the complete folder path in the folder-state tree.
        if manifest.get('p') and manifest.get('fid'):
            return caption(dict(manifest, p=''), key)
        raise NativeBackupError('Nome ou metadados do arquivo excedem o limite de legenda do Telegram; o nome nao sera truncado.')
    return text


def parse_caption(text, key=None):
    if text.startswith('td1:'):
        value = json.loads(text[4:])
    elif text.startswith('tde1:') and key:
        value = json.loads(open_bytes(key, base64.b64decode(text[5:], validate=True)))
    else:
        return None
    return value if isinstance(value, dict) else None


def write_segment(source, dest, length, digest, key=None):
    """Read one plaintext part; encrypted parts each have their own TDE1 stream."""
    remaining = length
    if key:
        salt = os.urandom(8)
        dest.write(b'TDE1\x01' + salt)
    frame = 0
    while True:
        amount = min(FRAME_SIZE, remaining)
        data = source.read(amount) if amount else b''
        if len(data) != amount:
            raise OSError('Arquivo truncado durante a copia.')
        digest.update(data)
        remaining -= amount
        last = remaining == 0
        if key:
            cipher = AES.new(key, AES.MODE_GCM, nonce=salt + struct.pack('>I', frame), mac_len=16)
            cipher.update(struct.pack('>IIB', 1, frame, int(last)))
            encrypted, tag = cipher.encrypt_and_digest(data)
            marker = (len(encrypted) + 16) | (0x80000000 if last else 0)
            dest.write(struct.pack('>I', marker) + encrypted + tag)
        else:
            dest.write(data)
        if last:
            return
        frame += 1


def merge_folder_states(states):
    result = copy.deepcopy(states[0]) if states else {'v': 1, 'f': [], 'd': []}
    folders, deleted = {}, {}
    for state in states:
        if state.get('v', 1) != 1 or not isinstance(state.get('f', []), list) or not isinstance(state.get('d', []), list):
            raise NativeBackupError('Arvore remota de pastas invalida; nenhuma arvore sera substituida.')
        for item in state.get('f', []):
            old = folders.get(item['id'])
            if old is None or item.get('ca', item['mt']) > old.get('ca', old['mt']):
                folders[item['id']] = copy.deepcopy(item)
        for item in state.get('d', []):
            if item['id'] not in deleted or item['at'] > deleted[item['id']]['at']:
                deleted[item['id']] = dict(item)
    result['f'] = [item for ident, item in folders.items() if ident not in deleted or item.get('ca', item['mt']) > deleted[ident]['at']]
    result['d'] = list(deleted.values())
    return result


class FolderTree:
    def __init__(self, state, profile):
        self.state, self.profile = state, profile
        self.by_id = {x['id']: x for x in state.get('f', [])}
        self.by_path = {}
        self.changed = False
        for ident, item in self.by_id.items():
            path = self.path_of(ident)
            if path is not None and item.get('tr') is None:
                self.by_path.setdefault(path, ident)

    def path_of(self, ident, visited=None):
        if ident is None:
            return ''
        visited = set() if visited is None else visited
        if ident in visited or len(visited) >= 64:
            raise NativeBackupError('Arvore de pastas com ciclo ou profundidade excessiva.')
        visited.add(ident)
        item = self.by_id.get(ident)
        if item is None:
            return None
        parent = self.path_of(item.get('p'), visited)
        return None if parent is None else '/'.join(filter(None, (parent, item['n'])))

    def ensure(self, path):
        path = str(path).strip('/')
        if not path:
            return None
        if path in self.by_path:
            return self.by_path[path]
        parts = path.split('/')
        if len(parts) > 64:
            raise NativeBackupError('Caminho excede a profundidade de pastas suportada pelo TeleDrive.')
        if any(part in ('', '.', '..') for part in parts):
            raise NativeBackupError('Caminho logico de pasta invalido.')
        parent = self.ensure('/'.join(parts[:-1]))
        now = int(time.time() * 1000)
        ident = str(uuid.uuid5(uuid.NAMESPACE_URL, 'teledrive-mirror-folder:' + self.profile + ':' + path))
        item = {'id': ident, 'p': parent, 'n': parts[-1], 'ct': now, 'mt': now, 'ca': now}
        self.by_id[ident] = item
        self.by_path[path] = ident
        self.changed = True
        return ident

    def document(self):
        return dict(self.state, f=list(self.by_id.values()))


async def read_folder_tree(client, channel, key, profile):
    states = []
    newest = None
    async for message in client.iter_messages(channel, search='teledrive.folders.json'):
        if not message.document or message.file.name != 'teledrive.folders.json':
            continue
        timestamp = message.date.timestamp()
        newest = timestamp if newest is None else newest
        if newest - timestamp > 600:
            break
        if message.document.size > 64 * 1024 * 1024:
            raise NativeBackupError('Arvore de pastas remota grande demais.')
        data = await client.download_media(message, file=bytes)
        if data.startswith(b'TDFS'):
            if key is None:
                raise NativeBackupError('A arvore de pastas e criptografada; configure TELEDRIVE_PASSPHRASE.')
            data = open_bytes(key, data[4:])
        states.append(json.loads(data))
    return FolderTree(merge_folder_states(states), profile)


async def publish_folder_tree(client, channel, tree, key, force=False):
    if not tree.changed and not force:
        return
    # Merge fresh state again before publication; keep other devices' folders,
    # tombstones, index pointers and unknown fields. Never delete their messages.
    fresh = await read_folder_tree(client, channel, key, tree.profile)
    merged = merge_folder_states([fresh.document(), tree.document()])
    data = json.dumps(merged, ensure_ascii=True, separators=(',', ':')).encode()
    if key:
        data = b'TDFS' + seal_bytes(key, data)
    stream = io.BytesIO(data)
    stream.name = 'teledrive.folders.json'
    result = await client.send_file(channel, stream, force_document=True,
                    caption='#teledrive-folders', parse_mode=None)
    if not result or not result.document or result.document.size != len(data):
        raise NativeBackupError('A publicacao da arvore de pastas nao foi confirmada.')
    updated = FolderTree(merged, tree.profile)
    tree.state, tree.by_id, tree.by_path = updated.state, updated.by_id, updated.by_path
    tree.changed = False
