import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import learned_tools as learned
import needle_startup as needle
import sync_learned_tools as sync


class API:
    def __init__(self, remote=None):
        self.remote = remote or {}
        self.calls = []

    def content(self, path, ref):
        self.calls.append(('content', path, ref))
        return self.remote.get(path)

    def call(self, method, path, data=None, missing=False):
        self.calls.append((method, path, data))
        if path == '/git/ref/heads/' + learned.BRANCH:
            return {'object': {'sha': 'old'}} if self.remote else None
        if path == '':
            return {'default_branch': 'main'}
        if path == '/git/ref/heads/main':
            return {'object': {'sha': 'base'}}
        if path.startswith('/git/commits/'):
            return {'tree': {'sha': 'base-tree'}}
        return {'sha': 'new-' + str(len(self.calls))}


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        code = '#!/bin/bash\nset -euo pipefail\nmkdir -p "$HOME/demo"\ntest -d "$HOME/demo"\n'
        task = learned.task_hash('Prepare demo')
        name = 'learn_' + hashlib.sha256(('before_desktop' + task + code).encode()).hexdigest()[:24]
        self.item = {'name': name, 'description': 'Prepare demo folder', 'script': learned.PREFIX + name + '.sh',
                     'phase': 'before_desktop', 'timeout_seconds': 900, 'task_sha256': task,
                     'script_sha256': hashlib.sha256(code.encode()).hexdigest()}
        path = self.root / self.item['script']
        path.parent.mkdir(parents=True)
        path.write_text(code)
        needle.atomic_json(self.root / learned.CATALOG, {'version': 1, 'routines': [self.item]})

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_commit_has_only_catalog_and_registered_scripts(self):
        api = API()
        sync.publish(api, self.root)
        tree = next(data for method, path, data in api.calls if method == 'POST' and path == '/git/trees')
        self.assertEqual({x['path'] for x in tree['tree']}, {learned.CATALOG, self.item['script']})
        ref = next(data for method, path, data in api.calls if method == 'POST' and path == '/git/refs')
        self.assertEqual(ref['ref'], 'refs/heads/' + learned.BRANCH)
        self.assertFalse(any(path == '/git/refs/heads/main' for _, path, _ in api.calls))

    def test_existing_identical_catalog_creates_no_commit(self):
        remote = {learned.CATALOG: (self.root / learned.CATALOG).read_bytes(),
                  self.item['script']: (self.root / self.item['script']).read_bytes()}
        api = API(remote)
        sync.publish(api, self.root)
        self.assertFalse(any(method in ('POST', 'PATCH') for method, _, _ in api.calls))

    def test_modified_script_is_rejected_before_network_write(self):
        (self.root / self.item['script']).write_text('echo changed')
        api = API()
        with self.assertRaises(ValueError):
            sync.publish(api, self.root)
        self.assertEqual(api.calls, [])

    def test_publication_checks_known_private_values_again(self):
        api = API()
        with self.assertRaises(ValueError):
            sync.publish(api, self.root, secret_values=['Prepare demo folder'])
        self.assertEqual(api.calls, [])

    def test_remote_path_traversal_is_rejected(self):
        bad = dict(self.item, script='../../private.txt')
        api = API({learned.CATALOG: json.dumps({'version': 1, 'routines': [bad]}).encode()})
        with self.assertRaises(ValueError):
            sync.pull(api, self.root)
        self.assertEqual(learned.load(self.root), [self.item])

    def test_pull_validated_remote_catalog(self):
        remote = {learned.CATALOG: (self.root / learned.CATALOG).read_bytes(),
                  self.item['script']: (self.root / self.item['script']).read_bytes()}
        destination = Path(self.temp.name) / 'new'
        destination.mkdir()
        sync.pull(API(remote), destination)
        self.assertEqual(learned.load(destination), [self.item])


if __name__ == '__main__':
    unittest.main()
