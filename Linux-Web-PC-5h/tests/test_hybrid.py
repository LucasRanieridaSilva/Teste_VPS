import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hybrid_startup as hybrid
import learned_tools as learned
import needle_startup as needle
import sync_learned_tools as sync


class FakeQwen:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.closed = False
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def ask(self, system, messages, **kwargs):
        self.requests.append((system, messages))
        return next(self.responses)


class FakeNeedle:
    def reset(self):
        pass

    def complete(self, *args, **kwargs):
        return {'success': True, 'confidence': .99, 'function_calls': [{'name': 'known', 'arguments': {}}]}


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        (self.root / 'automacoes/rotinas').mkdir(parents=True)
        (self.root / 'automacoes/rotinas/known.sh').write_text('true\n')
        needle.atomic_json(self.root / 'automacoes/rotinas.json', {'version': 1, 'routines': [
            {'name': 'known', 'description': 'Known routine', 'script': 'automacoes/rotinas/known.sh', 'phase': 'before_desktop'}]})
        (self.root / 'INICIALIZACAO.txt').write_text('Prepare a pasta de teste.')
        self.env = {'HOME': str(Path(self.temp.name) / 'home'), 'PATH': os.environ['PATH'],
                    'RUNNER_TEMP': str(Path(self.temp.name) / 'runtime')}
        Path(self.env['RUNNER_TEMP']).mkdir()
        Path(self.env['HOME']).mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_phase(self, replies, **kwargs):
        qwen = FakeQwen(replies)
        hybrid.run_phase(self.root, 'before_desktop', env=self.env, qwen_factory=lambda _: qwen,
                         needle_factory=lambda *a: FakeNeedle(), **kwargs)
        self.assertTrue(qwen.closed)
        return qwen

    def test_known_tool_is_dispatched_without_qwen_commands(self):
        seen = []
        self.run_phase([{'phase': 'before_desktop'}, {'covered': True}],
                       runner=lambda *args: seen.append(args[0].name),
                       command=lambda *args: self.fail('Qwen executed a known tool'))
        self.assertEqual(seen, ['known.sh'])

    def test_unknown_task_is_executed_verified_and_learned(self):
        result = self.run_phase([{'phase': 'before_desktop'}, {'covered': False},
                  {'action': 'finish', 'description': 'Create a demo folder. Criar pasta demo.',
                   'script': 'mkdir -p "$HOME/demo"', 'verify': 'test -d "$HOME/demo"'}])
        self.assertTrue((Path(self.env['HOME']) / 'demo').is_dir())
        items = learned.load(self.root)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['task_sha256'], learned.task_hash('Prepare a pasta de teste.'))

    def test_failed_verification_is_not_saved_and_model_unloads(self):
        q = FakeQwen([{'phase': 'before_desktop'}, {'covered': False},
                     {'action': 'finish', 'description': 'Demo', 'script': 'true', 'verify': 'test -d "$HOME/nonexistent"'},
                     {'action': 'blocked'}])
        with self.assertRaises(RuntimeError):
            hybrid.run_phase(self.root, 'before_desktop', env=self.env, qwen_factory=lambda _: q)
        self.assertTrue(q.closed)
        self.assertEqual(learned.load(self.root), [])
        self.assertEqual(list((self.root / learned.PREFIX).glob('*.sh')), [])

    def test_preview_does_not_execute_or_learn(self):
        self.run_phase([{'phase': 'before_desktop'}, {'covered': False}], preview=True,
                       command=lambda *a: self.fail('preview executed'))
        self.assertEqual(learned.load(self.root), [])

    def test_secret_is_not_in_model_requests_or_public_tool(self):
        self.env['STARTUP_SECRETS_JSON'] = '{"APP_API_KEY":"very-private-value"}'
        q = self.run_phase([{'phase': 'before_desktop'}, {'covered': False},
               {'action': 'finish', 'description': 'Create demo', 'script': 'mkdir -p "$HOME/demo"',
                'verify': 'test -d "$HOME/demo"'}])
        self.assertNotIn('very-private-value', str(q.requests))
        self.assertIn('APP_API_KEY', str(q.requests))
        for f in (self.root / 'automacoes').rglob('*'):
            if f.is_file():
                self.assertNotIn('very-private-value', f.read_text())

    def test_public_guard_blocks_known_secrets_and_private_keys(self):
        for code, secrets in [('echo private-value', ['private-value']),
                              ('TOKEN=1234567890123456789012345678', []),
                              ('-----BEGIN RSA PRIVATE KEY-----', []),
                              ('cd /home/someone/private', [])]:
            with self.assertRaises(ValueError):
                learned.protect_public_text(code, secrets)

    def test_export_never_copies_unrelated_files(self):
        (self.root / 'private.txt').write_text('private')
        destination = Path(self.temp.name) / 'export'
        sync.export(self.root, destination)
        self.assertEqual([str(x.relative_to(destination)) for x in destination.rglob('*') if x.is_file()], [learned.CATALOG])

    def test_empty_tasks_do_not_load_qwen(self):
        (self.root / 'INICIALIZACAO.txt').write_text('# off')
        hybrid.run_phase(self.root, 'before_desktop', env=self.env,
                         qwen_factory=lambda _: self.fail('loaded unnecessarily'))

    def test_failed_needle_falls_back_to_qwen(self):
        def fail(*args):
            raise RuntimeError('tool failed')
        self.run_phase([{'phase': 'before_desktop'}, {'covered': True},
              {'action': 'finish', 'description': 'Prepare demo', 'script': 'mkdir -p "$HOME/demo"',
               'verify': 'test -d "$HOME/demo"'}], runner=fail)
        self.assertEqual(len(learned.load(self.root)), 1)

    def test_gui_task_is_deferred_until_after_desktop(self):
        self.run_phase([{'phase': 'after_desktop'}])
        self.assertEqual(learned.load(self.root), [])
        q = FakeQwen([{'covered': False}, {'action': 'finish', 'description': 'Prepare demo',
                     'script': 'mkdir -p "$HOME/demo"', 'verify': 'test -d "$HOME/demo"'}])
        hybrid.run_phase(self.root, 'after_desktop', env=self.env, qwen_factory=lambda _: q)
        self.assertTrue(q.closed)
        self.assertEqual(learned.load(self.root)[0]['phase'], 'after_desktop')

    def test_shutdown_uses_its_own_instruction_file(self):
        (self.root / 'ENCERRAMENTO.txt').write_text('Prepare shutdown marker')
        q = FakeQwen([{'covered': False}, {'action': 'finish', 'description': 'Prepare shutdown marker',
                     'script': 'mkdir -p "$HOME/closed"', 'verify': 'test -d "$HOME/closed"'}])
        hybrid.run_phase(self.root, 'before_backup', env=self.env, qwen_factory=lambda _: q)
        self.assertTrue(q.closed)
        self.assertTrue((Path(self.env['HOME']) / 'closed').is_dir())
        self.assertEqual(learned.load(self.root)[0]['phase'], 'before_backup')


if __name__ == '__main__':
    unittest.main()
