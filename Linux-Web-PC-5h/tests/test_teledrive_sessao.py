import asyncio
import io
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import pyzipper

import teledrive_backup as transport
import teledrive_sessao as sessao

PASSWORD = 'uma-senha-bem-longa-123'
SECRET = PASSWORD.encode()


class Doc:
    pass


class Message:
    def __init__(self, id, caption, data):
        self.id, self.message, self.document, self.data = id, caption, Doc(), data


class FakeClient:
    """Canal falso: mensagens da mais nova para a mais antiga."""
    def __init__(self):
        self.messages, self.next_id, self.deleted, self.disconnected = [], 1, [], False

    async def iter_messages(self, channel, search=None, limit=None):
        for message in list(self.messages):
            if search is None or search in (message.message or ''):
                yield message

    async def download_media(self, message, file=None):
        Path(file).write_bytes(message.data)

    async def delete_messages(self, channel, ids):
        self.deleted.extend(ids)
        self.messages = [m for m in self.messages if m.id not in ids]

    async def disconnect(self):
        self.disconnected = True

    def add(self, path, folder, digest=None, caption=None):
        path = Path(path)
        digest = digest or sessao.digest_file(path)
        message = Message(self.next_id, caption or transport.caption_for(path, folder, digest), path.read_bytes())
        self.next_id += 1
        self.messages.insert(0, message)
        return message.id


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        tmp = Path(self.temp.name)
        self.env = {'HOME': str(tmp / 'home'), 'RUNNER_TEMP': str(tmp / 'run'), 'BACKUP_PASSWORD': PASSWORD,
                    'GITHUB_REPOSITORY': 'dono/principal'}
        (tmp / 'run').mkdir()
        self.root = sessao.data_dir(self.env)
        self.client = FakeClient()
        patcher = mock.patch.dict(os.environ, {'BACKUP_PASSWORD': PASSWORD})
        patcher.start()
        self.addCleanup(patcher.stop)

        async def send_document(client, channel, path, folder, digest):
            return client.add(path, folder, digest)
        sender = mock.patch.object(transport, 'send_document', send_document)
        sender.start()
        self.addCleanup(sender.stop)

    async def connect(self):
        return self.client, 'canal'

    def make_app_data(self, extra=None):
        files = {'files/keys/master.key': b'K' * 32, 'files/tdlib/td.binlog': b'binlog' * 500,
                 'files/datastore/prefs.preferences_pb': b'prefs', 'files/notes/n.md': b'nota',
                 'files/tdlib/files/video.mp4': b'V' * 4000, 'cache/thumbnails/a.png': b'png',
                 'files/logs/app.log': b'log', 'files/imports/copia.bin': b'imp'}
        files.update(extra or {})
        for relative, data in files.items():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def bundle_file(self):
        path = Path(self.temp.name) / sessao.bundle_name(self.env)
        sessao.build_bundle(self.root, path, SECRET)
        return path

    def folder(self):
        return sessao.remote_folder(self.env)

    def run_restore(self, target=None):
        return asyncio.run(sessao.do_restore(self.env, connect=self.connect, target=target))

    def run_save(self, **kwargs):
        kwargs.setdefault('closer', lambda: True)
        kwargs.setdefault('freezer', FakeFreezer)
        return asyncio.run(sessao.do_save(self.env, connect=self.connect, **kwargs))


class FakeFreezer:
    entered = exited = 0

    def __enter__(self):
        FakeFreezer.entered += 1
        return self

    def __exit__(self, *args):
        FakeFreezer.exited += 1
        return False


class PathsTests(Base):
    def test_data_dir_follows_xdg_and_home(self):
        self.assertEqual(sessao.data_dir({'HOME': '/h'}), Path('/h/.local/share/TeleDrive'))
        self.assertEqual(sessao.data_dir({'HOME': '/h', 'XDG_DATA_HOME': '/x'}), Path('/x/TeleDrive'))

    def test_each_repository_has_its_own_bundle(self):
        a = sessao.bundle_name({'GITHUB_REPOSITORY': 'dono/a'})
        b = sessao.bundle_name({'GITHUB_REPOSITORY': 'dono/b'})
        self.assertNotEqual(a, b)
        self.assertEqual(a, sessao.bundle_name({'GITHUB_REPOSITORY': 'DONO/A'}))

    def test_remote_folder_is_validated(self):
        self.assertEqual(sessao.remote_folder({'BACKUP_FOLDER': 'Meu Drive/'}), 'Meu Drive/_controle/sessao-teledrive')
        for bad in ('../x', 'a//b', '.', 'x' * 121):
            with self.assertRaises(ValueError):
                sessao.remote_folder({'BACKUP_FOLDER': bad})


class BundleTests(Base):
    def test_roundtrip_keeps_session_and_drops_caches(self):
        self.make_app_data()
        bundle = self.bundle_file()
        out = Path(self.temp.name) / 'out'
        sessao.safe_extract(bundle, out, SECRET)
        got = {p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file()}
        self.assertEqual(got, {'files/keys/master.key', 'files/tdlib/td.binlog',
                               'files/datastore/prefs.preferences_pb', 'files/notes/n.md'})
        self.assertEqual((out / 'files/keys/master.key').read_bytes(), b'K' * 32)

    def test_restored_files_are_private(self):
        self.make_app_data()
        out = Path(self.temp.name) / 'out'
        sessao.safe_extract(self.bundle_file(), out, SECRET)
        self.assertEqual(stat.S_IMODE((out / 'files/keys/master.key').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((out / 'files').stat().st_mode), 0o700)

    def test_bundle_contents_are_encrypted(self):
        # O ZIP AES cifra o conteudo; os nomes dos arquivos continuam legiveis.
        self.make_app_data({'files/tdlib/td.binlog': b'CONTEUDO-SECRETO-DO-BINLOG' * 20})
        raw = self.bundle_file().read_bytes()
        self.assertNotIn(b'K' * 32, raw)
        self.assertNotIn(b'CONTEUDO-SECRETO', raw)
        with self.assertRaises(RuntimeError):
            sessao.safe_extract(self.bundle_file(), Path(self.temp.name) / 'x', b'senha-errada-12345')

    def test_symlinks_are_not_followed_into_the_bundle(self):
        self.make_app_data()
        secret_file = Path(self.temp.name) / 'segredo.txt'
        secret_file.write_text('fora da pasta')
        (self.root / 'files' / 'atalho').symlink_to(secret_file)
        names = [rel for _, rel in sessao.iter_files(self.root)]
        self.assertNotIn('files/atalho', names)

    def test_empty_data_dir_is_refused(self):
        (self.root / 'cache').mkdir(parents=True)
        (self.root / 'cache' / 'x').write_bytes(b'x')
        with self.assertRaises(sessao.SessionError):
            sessao.build_bundle(self.root, Path(self.temp.name) / 'b.zip', SECRET)

    def evil_zip(self, name, link=False):
        path = Path(self.temp.name) / 'evil.zip'
        with pyzipper.AESZipFile(path, 'w', encryption=pyzipper.WZ_AES) as archive:
            archive.setpassword(SECRET)
            archive.writestr(name, '/etc/passwd' if link else b'x')
            if link:
                archive.infolist()[-1].external_attr = (stat.S_IFLNK | 0o777) << 16
        return path

    def test_malicious_entries_are_rejected(self):
        for name, link in (('../fora', False), ('files/../../fora', False), ('/abs', False), ('a\\b', False),
                           ('files/atalho', True)):
            out = Path(self.temp.name) / 'out-evil'
            with self.subTest(name=name, link=link):
                with self.assertRaises(sessao.SessionError):
                    sessao.safe_extract(self.evil_zip(name, link), out, SECRET)
        self.assertFalse((Path(self.temp.name) / 'fora').exists())


class RestoreTests(Base):
    def seed_remote(self, count=1, **add):
        self.make_app_data()
        for _ in range(count):
            self.client.add(self.bundle_file(), self.folder(), **add)
        import shutil
        shutil.rmtree(self.root)  # a VM nova nao tem a pasta

    def test_no_saved_session_returns_3_and_marks_fresh(self):
        self.assertEqual(self.run_restore(), sessao.NO_BUNDLE)
        self.assertTrue(sessao.read_state(self.env)['fresh'])
        self.assertTrue(self.client.disconnected)

    def test_restores_the_session(self):
        self.seed_remote()
        self.assertEqual(self.run_restore(), 0)
        self.assertEqual((self.root / 'files/keys/master.key').read_bytes(), b'K' * 32)
        self.assertFalse(sessao.read_state(self.env)['fresh'])
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_restores_when_an_empty_data_dir_exists(self):
        self.seed_remote()
        self.root.mkdir(parents=True)
        self.assertEqual(self.run_restore(), 0)
        self.assertTrue((self.root / 'files/keys/master.key').exists())
        self.assertFalse((self.root / 'TeleDrive').exists())  # nao foi movida para dentro

    def test_falls_back_to_older_copy_when_newest_is_corrupt(self):
        self.seed_remote()
        newest = Message(99, self.client.messages[0].message.replace(
            self.client.messages[0].message.split('"h":"')[1][:64], '0' * 64), b'lixo')
        self.client.messages.insert(0, newest)
        self.assertEqual(self.run_restore(), 0)
        self.assertTrue((self.root / 'files/keys/master.key').exists())

    def test_wrong_password_fails_and_blocks_saving(self):
        self.seed_remote()
        with mock.patch.object(transport, 'password', return_value=b'outra-senha-bem-longa'):
            with self.assertRaises(sessao.SessionError) as caught:
                self.run_restore()
        self.assertIn('BACKUP_PASSWORD', str(caught.exception))
        self.assertNotIn('outra-senha', str(caught.exception))
        self.assertTrue(sessao.read_state(self.env)['save_blocked'])
        self.assertFalse(self.root.exists())
        self.assertTrue(self.client.disconnected)

    def test_all_copies_corrupt_fails_and_blocks_saving(self):
        self.seed_remote(count=2, digest='0' * 64)
        with self.assertRaises(sessao.SessionError):
            self.run_restore()
        self.assertTrue(sessao.read_state(self.env)['save_blocked'])

    def test_existing_data_is_not_overwritten(self):
        self.make_app_data()
        self.assertEqual(self.run_restore(), 1)
        self.assertTrue(sessao.read_state(self.env)['save_blocked'])
        self.assertEqual((self.root / 'files/keys/master.key').read_bytes(), b'K' * 32)

    def test_other_repositories_sessions_are_ignored(self):
        self.make_app_data()
        other = dict(self.env, GITHUB_REPOSITORY='dono/par')
        path = Path(self.temp.name) / sessao.bundle_name(other)
        sessao.build_bundle(self.root, path, SECRET)
        self.client.add(path, sessao.remote_folder(other))
        import shutil
        shutil.rmtree(self.root)
        self.assertEqual(self.run_restore(), sessao.NO_BUNDLE)


class SaveTests(Base):
    def setUp(self):
        super().setUp()
        FakeFreezer.entered = FakeFreezer.exited = 0

    def test_nothing_to_save_before_the_app_ran(self):
        self.assertEqual(self.run_save(), 0)
        self.assertEqual(self.client.messages, [])

    def test_first_save_uploads_a_td1_bundle_in_the_control_folder(self):
        self.make_app_data()
        self.assertEqual(self.run_save(), 0)
        self.assertEqual(len(self.client.messages), 1)
        caption = self.client.messages[0].message
        self.assertTrue(caption.startswith('td1:'))
        self.assertIn('_controle/sessao-teledrive', caption)
        self.assertIn(sessao.bundle_name(self.env), caption)
        self.assertEqual(FakeFreezer.entered, FakeFreezer.exited)
        self.assertGreaterEqual(FakeFreezer.entered, 1)
        self.assertTrue(self.client.disconnected)

    def test_uploaded_bundle_restores_on_a_fresh_vm(self):
        self.make_app_data()
        self.run_save()
        import shutil
        shutil.rmtree(self.root)
        write = sessao.write_state
        write(self.env, save_blocked=False)
        self.assertEqual(self.run_restore(), 0)
        self.assertEqual((self.root / 'files/keys/master.key').read_bytes(), b'K' * 32)

    def test_keeps_only_the_newest_copies(self):
        self.make_app_data()
        for _ in range(sessao.KEEP + 2):
            self.run_save()
        self.assertEqual(len(self.client.messages), sessao.KEEP)
        self.assertEqual(len(self.client.deleted), 2)

    def test_if_due_respects_the_interval(self):
        self.make_app_data()
        clock = [1_000_000.0]
        now = lambda: clock[0]
        self.run_save(if_due=True, now=now)               # primeira: sempre
        self.run_save(if_due=True, now=now)               # logo depois: nao
        self.assertEqual(len(self.client.messages), 1)
        sessao.write_state(self.env, fresh=True)
        clock[0] += sessao.FRESH_INTERVAL + 1             # sessao nova: a cada 30 min
        self.run_save(if_due=True, now=now)
        self.assertEqual(len(self.client.messages), 2)
        sessao.write_state(self.env, fresh=False)
        clock[0] += sessao.FRESH_INTERVAL + 1             # sessao restaurada: so apos 2 h
        self.run_save(if_due=True, now=now)
        self.assertEqual(len(self.client.messages), 2)
        clock[0] += sessao.STEADY_INTERVAL
        self.run_save(if_due=True, now=now)
        self.assertEqual(len(self.client.messages), 3)

    def test_blocked_state_never_uploads(self):
        self.make_app_data()
        sessao.write_state(self.env, save_blocked=True)
        self.run_save()
        self.assertEqual(self.client.messages, [])

    def test_much_smaller_session_does_not_replace_a_good_copy(self):
        self.make_app_data({'files/tdlib/grande.db': os.urandom(300_000)})
        self.run_save()
        good = self.client.messages[0].id
        for relative in ('files/tdlib/grande.db',):
            (self.root / relative).unlink()
        (self.root / 'files/tdlib/td.binlog').write_bytes(b'x')
        self.run_save()
        self.assertEqual([m.id for m in self.client.messages], [good])

    def test_final_closes_the_app_first_and_warns_if_it_does_not_close(self):
        self.make_app_data()
        calls = []
        self.run_save(final=True, closer=lambda: calls.append('fechou') or True)
        self.assertEqual(calls, ['fechou'])
        self.run_save(final=False, closer=lambda: calls.append('nao deveria') or True)
        self.assertEqual(calls, ['fechou'])

    def test_app_is_always_resumed_even_if_the_bundle_fails(self):
        self.make_app_data()
        with mock.patch.object(sessao, 'build_bundle', side_effect=sessao.SessionError('falhou')):
            with self.assertRaises(sessao.SessionError):
                self.run_save()
        self.assertEqual(FakeFreezer.entered, FakeFreezer.exited)

    def test_failing_to_delete_old_copies_does_not_fail_the_save(self):
        self.make_app_data()
        for _ in range(sessao.KEEP):
            self.run_save()

        async def boom(channel, ids):
            raise PermissionError('sem direito')
        self.client.delete_messages = boom
        self.assertEqual(self.run_save(), 0)
        self.assertEqual(len(self.client.messages), sessao.KEEP + 1)


class ProcessTests(unittest.TestCase):
    def test_frozen_stops_then_always_continues(self):
        calls = []
        kill = lambda pid, sig: calls.append((pid, sig))
        with self.assertRaises(ValueError):
            with sessao.Frozen(kill=kill, pids=lambda: [10, 11]):
                raise ValueError('erro no meio da copia')
        self.assertEqual(calls, [(10, signal.SIGSTOP), (11, signal.SIGSTOP),
                                 (10, signal.SIGCONT), (11, signal.SIGCONT)])

    def test_frozen_ignores_processes_that_vanished(self):
        def kill(pid, sig):
            raise ProcessLookupError()
        with sessao.Frozen(kill=kill, pids=lambda: [1]):
            pass

    def test_close_app_sends_sigterm_and_waits(self):
        calls, alive = [], [[5], [5], []]
        clock = [0.0]
        ok = sessao.close_app(wait=25, kill=lambda pid, sig: calls.append((pid, sig)),
                              alive=lambda: alive.pop(0) if len(alive) > 1 else alive[0],
                              sleep=lambda s: clock.__setitem__(0, clock[0] + s), now=lambda: clock[0])
        self.assertTrue(ok)
        self.assertEqual(calls, [(5, signal.SIGTERM)])

    def test_close_app_gives_up_after_the_deadline(self):
        clock = [0.0]
        ok = sessao.close_app(wait=3, kill=lambda p, s: None, alive=lambda: [7],
                              sleep=lambda s: clock.__setitem__(0, clock[0] + s), now=lambda: clock[0])
        self.assertFalse(ok)

    def test_app_pids_finds_only_the_app(self):
        me = os.getpid()
        self.assertNotIn(me, sessao.app_pids())


class MainTests(Base):
    def test_errors_become_warnings_without_secrets(self):
        env = {k: v for k, v in self.env.items() if k != 'BACKUP_PASSWORD'}
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'BACKUP_PASSWORD': 'curta'}), mock.patch('sys.stderr', err):
            code = sessao.main(['restore'], env)
        self.assertEqual(code, 1)
        self.assertIn('::warning::', err.getvalue())
        self.assertNotIn('curta', err.getvalue())

    def test_due_logic(self):
        self.assertTrue(sessao.due({}, 10 ** 7))
        self.assertFalse(sessao.due({'last_save': 100.0}, 100.0 + 60))
        self.assertTrue(sessao.due({'last_save': 100.0, 'fresh': True}, 100.0 + sessao.FRESH_INTERVAL))
        self.assertFalse(sessao.due({'last_save': 100.0}, 100.0 + sessao.FRESH_INTERVAL))


if __name__ == '__main__':
    unittest.main()
