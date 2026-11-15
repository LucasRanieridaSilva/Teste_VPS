import asyncio
import base64
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import original_backup as backup
import teledrive_native as native
import restaurar_incremental as restore
import incremental_backup as incremental


class Document:
    def __init__(self, content, name):
        self.content, self.name, self.size = content, name, len(content)


class Telegram:
    def __init__(self):
        self.messages = {}
        self.next_id = 0
        self.uploaded_bytes = 0
        self.clones = 0
        self.hook = None

    async def send_file(self, channel, file, caption, **kwargs):
        if isinstance(file, Document):
            data, name = file.content, file.name
            self.clones += 1
        elif hasattr(file, 'read'):
            data, name = file.read(), file.name
            self.uploaded_bytes += len(data)
        else:
            path = Path(file)
            data, name = path.read_bytes(), path.name
            self.uploaded_bytes += len(data)
        self.next_id += 1
        message = SimpleNamespace(id=self.next_id, document=Document(data, name),
                    file=SimpleNamespace(name=name), message=caption,
                    date=datetime.datetime.now(datetime.timezone.utc))
        self.messages[message.id] = message
        if self.hook:
            self.hook(message)
        return message

    async def edit_message(self, channel, mid, text, **kwargs):
        self.messages[mid].message = text

    async def get_messages(self, channel, ids):
        return [self.messages.get(mid) for mid in ids]

    async def download_media(self, message, file):
        if file is bytes:
            return message.document.content
        Path(file).write_bytes(message.document.content)
        return file

    async def iter_messages(self, channel, search=None, min_id=0, limit=None):
        count = 0
        for m in sorted(self.messages.values(), key=lambda m: m.id, reverse=True):
            if m.id <= min_id or (search and search not in m.file.name and search not in m.message):
                continue
            yield m
            count += 1
            if limit and count >= limit:
                break


def open_stream(key, blob):
    # Independent reader matching the published Kotlin TDE1 frame protocol.
    assert blob[:5] == b'TDE1\x01'
    salt = blob[5:13]
    position, index, result = 13, 0, b''
    while True:
        marker = struct.unpack('>I', blob[position:position + 4])[0]
        position += 4
        last = bool(marker & 0x80000000)
        length = marker & 0x7fffffff
        sealed = blob[position:position + length]
        position += length
        cipher = native.AES.new(key, native.AES.MODE_GCM, nonce=salt + struct.pack('>I', index), mac_len=16)
        cipher.update(struct.pack('>IIB', 1, index, int(last)))
        result += cipher.decrypt_and_verify(sealed[:-16], sealed[-16:])
        if last:
            assert position == len(blob)
            return result
        index += 1


def key_backup(key, phrase):
    salt = bytes(range(16))
    kek = hashlib.pbkdf2_hmac('sha256', phrase.encode(), salt, 310000, dklen=32)
    return b'TDKB\x01' + struct.pack('>I', 310000) + salt + b'\x00\x00' + native.seal_bytes(kek, key)


class CryptoTests(unittest.TestCase):
    def test_existing_key_backup_and_wrong_passphrase(self):
        key = bytes(range(32))
        blob = key_backup(key, 'frase de recuperacao')
        self.assertEqual(native.restore_content_key(blob, 'frase de recuperacao'), key)
        with self.assertRaises(native.NativeBackupError):
            native.restore_content_key(blob, 'wrong')

    def test_native_stream_frames_empty_and_multiple_frames(self):
        key = bytes(range(32))
        for data in (b'', b'hello', b'z' * (native.FRAME_SIZE + 13)):
            out = io.BytesIO()
            h = hashlib.sha256()
            native.write_segment(io.BytesIO(data), out, len(data), h, key)
            self.assertEqual(open_stream(key, out.getvalue()), data)
            self.assertEqual(h.hexdigest(), hashlib.sha256(data).hexdigest())
            damaged = bytearray(out.getvalue())
            damaged[-1] ^= 1
            with self.assertRaises(ValueError):
                open_stream(key, damaged)

    def test_manifest_unicode_and_encryption(self):
        key = bytes(range(32))
        m = {'n': 'anotações.txt', 'p': 'Projeto/Documentos', 'id': 'test'}
        text = native.caption(m, key)
        self.assertTrue(text.startswith('tde1:'))
        self.assertNotIn('anotações', text)
        self.assertEqual(native.parse_caption(text, key), m)

    def test_folder_merge_keeps_unknown_fields_and_deletions(self):
        state = {'v': 1, 'ix': 99, 'future': {'a': 1}, 'f': [
                    {'id': 'keep', 'n': 'Keep', 'ct': 1, 'mt': 2, 'custom': True},
                    {'id': 'gone', 'n': 'Gone', 'ct': 1, 'mt': 2}],
                 'd': [{'id': 'gone', 'at': 3}]}
        result = native.merge_folder_states([state])
        self.assertEqual([x['id'] for x in result['f']], ['keep'])
        self.assertEqual(result['ix'], 99)
        self.assertEqual(result['future'], {'a': 1})
        self.assertTrue(result['f'][0]['custom'])


class OriginalNamesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'VPS'
        self.root.mkdir()
        self.client = Telegram()
        self.secret = b'backup-secret-for-tests'

    def tearDown(self):
        self.temp.cleanup()

    async def run_cycle(self, key=None, part_bytes=512 * 1024 * 1024):
        tree = await native.read_folder_tree(self.client, 'channel', key, 'profile')
        store = backup.NamedStore(self.client, 'channel', 'Linux Web PC', 'profile', self.secret, key, tree, part_bytes)
        result = await backup.cycle([self.root], [], store)
        return store, result

    def visible(self, key=None):
        result = []
        for msg in self.client.messages.values():
            m = native.parse_caption(msg.message, key)
            if m and not m.get('hd') and not m.get('ar') and m.get('p', '').startswith('Linux Web PC/Arquivos/'):
                result.append((m, msg))
        return result

    async def test_names_bytes_nested_folders_and_same_basename(self):
        for name, data in [('Projeto A/config.json', b'first'), ('Projeto B/config.json', b'second'), ('Projeto A/notas com acentos ç.txt', b'note')]:
            p = self.root / name
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(data)
        _, index = await self.run_cycle()
        visible = self.visible()
        self.assertEqual(len(visible), 3)
        for m, message in visible:
            original = Path('/' + (m['p'] + '/' + m['n']).removeprefix('Linux Web PC/Arquivos/'))
            self.assertEqual(message.document.content, original.read_bytes())
            self.assertEqual(m['n'], original.name)
        self.assertEqual(len(index['files']), 3)

    async def test_empty_directories_are_real_folder_records(self):
        (self.root / 'Vazia/subpasta').mkdir(parents=True)
        await self.run_cycle()
        tree = await native.read_folder_tree(self.client, 'channel', None, 'profile')
        expected = backup.tree_path('Linux Web PC/Arquivos', str(self.root / 'Vazia/subpasta'))
        self.assertIn(expected, tree.by_path)
        self.assertEqual(self.visible(), [])

    async def test_unchanged_files_are_not_sent_again_in_next_session(self):
        (self.root / 'arquivo.txt').write_bytes(b'data')
        await self.run_cycle()
        ids = {msg.id for _, msg in self.visible()}
        _, index = await self.run_cycle()
        self.assertEqual({msg.id for _, msg in self.visible()}, ids)
        self.assertEqual(index['counts']['reused_files'], 1)

    async def test_same_content_in_another_folder_uses_server_clone(self):
        (self.root / 'one').write_bytes(b'data')
        await self.run_cycle()
        (self.root / 'Nova').mkdir()
        (self.root / 'Nova/two').write_bytes(b'data')
        _, index = await self.run_cycle()
        self.assertEqual(index['counts']['cloned_files'], 1)
        self.assertEqual(self.client.clones, 1)
        self.assertEqual({m['n'] for m, _ in self.visible()}, {'one', 'two'})

    async def test_changed_file_replaces_visible_version_and_keeps_history(self):
        path = self.root / 'arquivo.txt'
        path.write_bytes(b'old')
        await self.run_cycle()
        path.write_bytes(b'new')
        _, index = await self.run_cycle()
        visible = self.visible()
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0][1].document.content, b'new')
        self.assertEqual(len(index['versions']), 2)
        archived = [m for msg in self.client.messages.values() if (m := native.parse_caption(msg.message)) and m.get('ar')]
        self.assertEqual(len(archived), 1)

    async def test_large_file_has_one_logical_name_and_correct_parts(self):
        data = b'123456789abcdef'
        (self.root / 'video.bin').write_bytes(data)
        await self.run_cycle(part_bytes=4)
        visible = sorted(self.visible(), key=lambda pair: pair[0]['pi'])
        self.assertEqual(len({m['id'] for m, _ in visible}), 1)
        self.assertEqual({m['n'] for m, _ in visible}, {'video.bin'})
        self.assertEqual([m['po'] for m, _ in visible], [0, 4, 8, 12])
        self.assertEqual(b''.join(msg.document.content for _, msg in visible), data)

    async def test_native_encryption_preserves_original_names_for_the_app(self):
        key = bytes(range(32))
        (self.root / 'segredo.txt').write_bytes(b'private bytes')
        await self.run_cycle(key=key, part_bytes=4)
        visible = sorted(self.visible(key), key=lambda pair: pair[0]['pi'])
        self.assertEqual({m['n'] for m, _ in visible}, {'segredo.txt'})
        self.assertTrue(all(m['e'] for m, _ in visible))
        self.assertEqual(b''.join(open_stream(key, msg.document.content) for _, msg in visible), b'private bytes')
        self.assertTrue(all(msg.message.startswith('tde1:') for _, msg in visible))

    async def test_encrypted_drive_never_downgrades_without_passphrase(self):
        key = bytes(range(32))
        stream = io.BytesIO(key_backup(key, 'frase'))
        stream.name = 'teledrive.keybackup'
        await self.client.send_file('channel', stream, caption='#teledrive-keybackup')
        with self.assertRaises(native.NativeBackupError):
            await native.existing_content_key(self.client, 'channel', '')
        self.assertEqual(await native.existing_content_key(self.client, 'channel', 'frase'), key)

    async def test_complete_uncheckpointed_file_is_recovered_without_upload(self):
        (self.root / 'one').write_bytes(b'data')
        await self.run_cycle()
        for ident, msg in list(self.client.messages.items()):
            if msg.file.name.startswith('arvore-'):
                del self.client.messages[ident]
        ids = {m.id for _, m in self.visible()}
        _, index = await self.run_cycle()
        self.assertEqual({m.id for _, m in self.visible()}, ids)
        self.assertEqual(index['counts']['reused_files'], 1)

    async def test_mutating_multipart_file_is_not_promoted_or_indexed(self):
        path = self.root / 'changing'
        path.write_bytes(b'123456789')
        def mutate(message):
            m = native.parse_caption(message.message)
            if m and m.get('n') == 'changing' and m.get('pi') == 0:
                path.write_bytes(b'XXXXXXXXX')
        self.client.hook = mutate
        _, index = await self.run_cycle(part_bytes=4)
        self.assertNotIn(str(path), index['files'])
        self.assertEqual(self.visible(), [])
        self.assertFalse(index['scan_complete'])

    async def test_missing_remote_file_is_uploaded_again(self):
        path = self.root / 'one'
        path.write_bytes(b'data')
        await self.run_cycle()
        for _, message in self.visible():
            del self.client.messages[message.id]
        _, index = await self.run_cycle()
        self.assertEqual(len(self.visible()), 1)
        self.assertEqual(index['counts']['uploaded_files'], 1)

    async def test_empty_plain_file_is_preserved_as_metadata_and_restored(self):
        empty = self.root / 'vazio.txt'
        empty.touch()
        (self.root / 'notas.txt').write_text('notes')
        _, index = await self.run_cycle()
        self.assertIn(str(empty), index['empty_files'])
        self.assertEqual({m['n'] for m, _ in self.visible()}, {'notas.txt'})
        downloaded = Path(self.temp.name) / 'Arquivos'
        for m, message in self.visible():
            path = downloaded / (m['p'] + '/' + m['n']).removeprefix('Linux Web PC/Arquivos/')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(message.document.content)
        index_path = Path(self.temp.name) / 'index.zip'
        incremental.encode_index(index, index_path, self.secret)
        output = Path(self.temp.name) / 'restore'
        self.assertEqual(restore.restore(index_path, downloaded, output, self.secret), 2)
        self.assertEqual((output / str(empty).lstrip('/')).read_bytes(), b'')

    async def test_empty_encrypted_file_is_a_normal_native_document(self):
        key = bytes(range(32))
        (self.root / 'vazio.txt').touch()
        _, index = await self.run_cycle(key=key)
        visible = self.visible(key)
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0][0]['n'], 'vazio.txt')
        self.assertEqual(visible[0][0]['s'], 0)
        self.assertEqual(open_stream(key, visible[0][1].document.content), b'')
        self.assertTrue(index['scan_complete'])

    async def test_existing_foreign_folders_and_flags_are_not_lost(self):
        state = {'v': 1, 'ix': 91, 'other': 'preserved', 'f': [
            {'id': 'foreign', 'n': 'Outra pasta', 'ct': 1, 'mt': 1, 'fv': True}], 'd': []}
        stream = io.BytesIO(json.dumps(state).encode())
        stream.name = 'teledrive.folders.json'
        await self.client.send_file('channel', stream, caption='#teledrive-folders')
        (self.root / 'Vazia').mkdir()
        await self.run_cycle()
        tree = await native.read_folder_tree(self.client, 'channel', None, 'profile')
        self.assertTrue(tree.by_id['foreign']['fv'])
        self.assertEqual(tree.state['ix'], 91)
        self.assertEqual(tree.state['other'], 'preserved')

    async def test_native_key_and_folder_state_are_read_end_to_end(self):
        key = bytes(range(32))
        phrase = 'minha frase de recuperacao'
        stream = io.BytesIO(key_backup(key, phrase))
        stream.name = 'teledrive.keybackup'
        await self.client.send_file('channel', stream, caption='#teledrive-keybackup')
        (self.root / 'notas.txt').write_text('notes')
        result = await backup.run_with_client(self.client, 'channel', 'Linux Web PC', 'profile', self.secret,
                     [self.root], [], {'TELEDRIVE_PASSPHRASE': phrase})
        self.assertTrue(result['scan_complete'])
        self.assertEqual(self.visible(key)[0][0]['n'], 'notas.txt')

    async def test_different_backup_folders_do_not_share_file_identity(self):
        (self.root / 'one').write_text('same data')
        await self.run_cycle()
        tree = await native.read_folder_tree(self.client, 'channel', None, 'profile')
        other = backup.NamedStore(self.client, 'channel', 'Outra copia', 'profile', self.secret, None, tree)
        await backup.cycle([self.root], [], other)
        files = [m for msg in self.client.messages.values() if (m := native.parse_caption(msg.message))
                 and m.get('n') == 'one' and not m.get('hd') and not m.get('ar')]
        self.assertEqual(len(files), 2)
        self.assertEqual(len({m['id'] for m in files}), 2)


if __name__ == '__main__':
    unittest.main()
