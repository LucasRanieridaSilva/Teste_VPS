import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import incremental_backup as inc
import restaurar_incremental as restore
import session_loop


class Store:
    def __init__(self, folder):
        self.folder = folder
        self.index = inc.new_index('test')
        self.uploads = 0
        self.fail = False

    async def load(self):
        return copy.deepcopy(self.index)

    async def save_index(self, index):
        self.index = copy.deepcopy(index)

    async def upload_pack(self, path, pack_id):
        if self.fail:
            raise RuntimeError('network failed')
        self.uploads += 1
        data = path.read_bytes()
        name = pack_id + '.zip.part00001'
        (self.folder / name).write_bytes(data)
        h = hashlib.sha256(data).hexdigest()
        return {'parts': [{'name': name, 'bytes': len(data), 'sha256': h, 'message_id': self.uploads}], 'sha256': h}


class IncrementalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        self.root.mkdir()
        self.packs = self.base / 'remote'
        self.packs.mkdir()
        self.store = Store(self.packs)
        self.secret = b'password-for-tests-123'

    def tearDown(self):
        self.temp.cleanup()

    async def cycle(self):
        return await inc.cycle([self.root], [], self.store, self.secret)

    async def test_new_changed_and_identical_files_across_sessions(self):
        a = self.root / 'a.txt'
        a.write_text('first')
        first = await self.cycle()
        self.assertEqual(first['counts']['uploaded_files'], 1)
        old_uploads = self.store.uploads
        # A new scanner instance uses the durable remote index, not local timestamps.
        second = await inc.cycle([self.root], [], self.store, self.secret)
        self.assertEqual(self.store.uploads, old_uploads)
        self.assertEqual(second['counts']['reused_files'], 1)
        (self.root / 'copy.txt').write_text('first')
        await self.cycle()
        self.assertEqual(self.store.uploads, old_uploads)
        previous_time = a.stat().st_mtime_ns
        a.write_text('other')  # same length and manually restored mtime must still be detected
        os.utime(a, ns=(previous_time, previous_time))
        third = await self.cycle()
        self.assertEqual(third['counts']['uploaded_files'], 1)
        self.assertEqual(len(third['objects']), 2)

    async def test_upload_failure_never_marks_content_as_saved(self):
        (self.root / 'a').write_text('new')
        self.store.fail = True
        with self.assertRaises(RuntimeError):
            await self.cycle()
        self.assertEqual(self.store.index['objects'], {})
        self.store.fail = False
        await self.cycle()
        self.assertEqual(len(self.store.index['objects']), 1)

    async def test_roundtrip_restore_and_encrypted_metadata(self):
        (self.root / 'one').write_bytes(b'same\x00data')
        (self.root / 'two').write_bytes(b'same\x00data')
        index = await self.cycle()
        path = self.base / 'index.zip'
        inc.encode_index(index, path, self.secret)
        self.assertNotIn(str(self.root).encode(), path.read_bytes())
        decoded = inc.decode_index(path, self.secret, 'test')
        self.assertEqual(decoded, index)
        with self.assertRaises(Exception):
            inc.decode_index(path, b'wrong password', 'test')
        output = self.base / 'restored'
        count = restore.restore(path, self.packs, output, self.secret)
        self.assertEqual(count, 2)
        self.assertEqual((output / str(self.root / 'one').lstrip('/')).read_bytes(), b'same\x00data')

    async def test_deletion_preserves_historical_objects(self):
        path = self.root / 'a'
        path.write_text('old')
        await self.cycle()
        path.unlink()
        index = await self.cycle()
        self.assertNotIn(str(path), index['entries'])
        self.assertEqual(len(index['objects']), 1)

    async def test_exclusions_and_symlinks_do_not_follow_outside_scope(self):
        excluded = self.root / 'cache'
        excluded.mkdir()
        (excluded / 'never').write_text('skip')
        outside = self.base / 'outside'
        outside.write_text('not traversed')
        (self.root / 'link').symlink_to(outside)
        index = await inc.cycle([self.root], [excluded], self.store, self.secret)
        self.assertEqual(index['objects'], {})
        self.assertEqual(index['entries'][str(self.root / 'link')]['kind'], 'symlink')

    async def test_immutable_remote_index_is_checkpointed_per_batch(self):
        (self.root / 'one').write_text('one')
        (self.root / 'two').write_text('two')
        await inc.cycle([self.root], [], self.store, self.secret, batch_files=1)
        self.assertEqual(self.store.uploads, 2)
        self.assertEqual(len(self.store.index['objects']), 2)

    async def test_changed_during_copy_is_not_accepted(self):
        path = self.root / 'changing'
        path.write_text('actual content')
        accepted, errors = inc.make_pack([{'path': str(path), 'sha256': '0' * 64}], self.base / 'pack.zip', self.secret)
        self.assertEqual(accepted, [])
        self.assertEqual(len(errors), 1)

    async def test_pack_content_cannot_be_read_without_password(self):
        (self.root / 'one').write_text('private content')
        index = await self.cycle()
        pack = next(self.packs.glob('*.part00001'))
        with inc.pyzipper.AESZipFile(pack) as z:
            with self.assertRaises(RuntimeError):
                z.read(next(iter(index['objects'])))

    async def test_corrupt_download_is_rejected_before_restore(self):
        (self.root / 'one').write_text('data')
        index = await self.cycle()
        path = self.base / 'index.zip'
        inc.encode_index(index, path, self.secret)
        next(self.packs.glob('*.part00001')).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            restore.restore(path, self.packs, self.base / 'out', self.secret)

    async def test_staging_failure_does_not_block_subsequent_files(self):
        (self.root / 'one').write_text('one')
        (self.root / 'two').write_text('two')
        original = inc.make_pack
        calls = [0]
        def flaky(*args):
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError('disk full')
            return original(*args)
        with patch.object(inc, 'make_pack', flaky):
            index = await inc.cycle([self.root], [], self.store, self.secret, batch_files=1)
        self.assertEqual(len(index['errors']), 1)
        self.assertEqual(index['counts']['uploaded_files'], 1)
        self.assertFalse(index['scan_complete'])


class LoopTests(unittest.TestCase):
    def test_half_hour_slots_and_no_overlapping_scans(self):
        current = [0]
        starts = []
        def now():
            return current[0]
        def sleep(seconds):
            current[0] += seconds
        def scan():
            starts.append(current[0])
            current[0] += 5
            return 0
        session_loop.loop(0, duration=4000, interval=1800, now=now, sleep=sleep, scan=scan)
        self.assertEqual(starts, [0, 1800, 3600])

    def test_slow_scan_skips_missed_slot_without_overlap(self):
        clock = [0]
        starts = []
        def scan():
            starts.append(clock[0])
            clock[0] += 2000
            return 0
        session_loop.loop(0, duration=4000, interval=1800, now=lambda: clock[0],
                          sleep=lambda s: clock.__setitem__(0, clock[0] + s), scan=scan)
        self.assertEqual(starts, [0, 3600])


if __name__ == '__main__':
    unittest.main()
