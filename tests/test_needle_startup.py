import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('needle_startup', PROJECT / 'scripts/needle_startup.py')
startup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup)


class Agent:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.inputs = []

    def reset(self):
        pass

    def complete(self, text, **kwargs):
        self.inputs.append(text)
        return next(self.responses)


def response(*names, **extra):
    return dict({'success': True, 'confidence': 0.95, 'suppressed_calls': [],
                 'function_calls': [{'name': name, 'arguments': {}} for name in names]}, **extra)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        scripts = self.root / 'automacoes/rotinas'
        scripts.mkdir(parents=True)
        for name in ('prepare', 'open', 'stop'):
            (scripts / f'{name}.sh').write_text('#!/bin/bash\nset -e\ntrue\n')
        self.items = [
            {'name': 'prepare', 'description': 'Prepare folders',
             'script': 'automacoes/rotinas/prepare.sh', 'phase': 'before_desktop'},
            {'name': 'open', 'description': 'Open programs',
             'script': 'automacoes/rotinas/open.sh', 'phase': 'after_desktop'},
            {'name': 'stop', 'description': 'Stop services',
             'script': 'automacoes/rotinas/stop.sh', 'phase': 'before_backup'},
        ]
        self.write_catalog()
        (self.root / 'INICIALIZACAO.txt').write_text('# Disabled by default\n')
        self.plan = self.root / 'plan.json'
        self.env = {'GITHUB_RUN_ID': '123', 'HOME': str(self.root / 'home'),
                    'PATH': os.environ['PATH']}

    def tearDown(self):
        self.temp.cleanup()

    def write_catalog(self):
        (self.root / 'automacoes/rotinas.json').write_text(json.dumps({'version': 1, 'routines': self.items}))

    def make_plan(self, text='Prepare folders', answers=None, preview=False):
        agent = Agent(answers or [response('prepare')])
        record = startup.plan(self.root, self.plan, text, preview=preview,
                              agent_factory=lambda *args: agent, env=self.env)
        return record, agent

    def test_empty_instruction_does_not_load_model(self):
        def forbidden(*args):
            self.fail('An empty configuration must not load Needle')
        result = startup.plan(self.root, self.plan, agent_factory=forbidden, env=self.env)
        self.assertEqual(result['routines'], [])

    def test_input_overrides_repository_file(self):
        (self.root / 'INICIALIZACAO.txt').write_text('Open programs')
        _, agent = self.make_plan('Prepare folders')
        self.assertEqual(agent.inputs, ['Prepare folders'])

    def test_multiline_deduplicates_preserving_order(self):
        result, _ = self.make_plan('Prepare folders\nOpen programs',
                                  [response('prepare'), response('prepare', 'open')])
        self.assertEqual([item['name'] for item in result['routines']], ['prepare', 'open'])

    def test_phases_and_completion(self):
        self.make_plan('Prepare, open and stop', [response('prepare', 'open', 'stop')])
        seen = []
        runner = lambda script, root, env, timeout: seen.append((script.name, env.get('DISPLAY')))
        for phase in startup.PHASES:
            startup.execute(self.root, self.plan, phase, self.env, runner)
            startup.execute(self.root, self.plan, phase, self.env, runner)
        self.assertEqual(seen, [('prepare.sh', None), ('open.sh', ':99'), ('stop.sh', None)])

    def test_preview_never_applies(self):
        self.make_plan(preview=True)
        startup.execute(self.root, self.plan, 'before_desktop', self.env,
                        lambda *args: self.fail('Preview executed a routine'))

    def test_unknown_request_invalidates_old_plan(self):
        self.make_plan()
        with self.assertRaises(ValueError):
            self.make_plan('Do something unsupported', [response()])
        self.assertFalse(self.plan.exists())

    def test_low_confidence_and_suppressed_calls(self):
        for result in [response('prepare', confidence=0.2),
                       response('prepare', suppressed_calls=[{'name': 'open'}]),
                       response('prepare', validation={'ungrounded': ['tool.field']}),
                       response('prepare', confidence=float('nan'))]:
            with self.assertRaises(ValueError):
                self.make_plan(answers=[result])
            self.assertFalse(self.plan.exists())

    def test_negative_requests_never_reach_the_model(self):
        catalog, _ = startup.load_catalog(self.root)
        for text in ('Não instale a lista.', 'Nao abra programas.', 'Nunca prepare pastas.',
                     'Do not install programs.', "Don't prepare folders.", 'Never open programs.'):
            agent = Agent([])
            with self.assertRaises(ValueError):
                startup.choose_routines([text], catalog, agent)
            self.assertEqual(agent.inputs, [])

    def test_unknown_name_or_generated_command_is_rejected(self):
        for result in [response('nonexistent'), response('prepare', function_calls=[
            {'name': 'prepare', 'arguments': {'command': 'touch /tmp/forbidden'}}])]:
            with self.assertRaises(ValueError):
                self.make_plan(answers=[result])

    def test_script_changes_block_execution(self):
        self.make_plan()
        (self.root / 'automacoes/rotinas/prepare.sh').write_text('echo changed')
        with self.assertRaises(ValueError):
            startup.execute(self.root, self.plan, 'before_desktop', self.env,
                            lambda *args: self.fail('Modified script was executed'))

    def test_other_github_run_cannot_reuse_plan(self):
        self.make_plan()
        with self.assertRaises(ValueError):
            startup.execute(self.root, self.plan, 'before_desktop', dict(self.env, GITHUB_RUN_ID='456'))

    def test_path_traversal_and_missing_scripts(self):
        for path in ('../../outside.sh', '/tmp/outside.sh', 'automacoes/rotinas/missing.sh'):
            self.items[0]['script'] = path
            self.write_catalog()
            with self.assertRaises(ValueError):
                startup.load_catalog(self.root)

    def test_secret_only_passes_to_executor(self):
        self.env['STARTUP_SECRETS_JSON'] = '{"APP_API_KEY":"private-value"}'
        result, agent = self.make_plan()
        self.assertNotIn('private-value', json.dumps(result))
        self.assertNotIn('private-value', ''.join(agent.inputs))
        seen = []
        startup.execute(self.root, self.plan, 'before_desktop', self.env,
                        lambda script, root, env, timeout: seen.append(env))
        self.assertEqual(seen[0]['APP_API_KEY'], 'private-value')
        self.assertNotIn('STARTUP_SECRETS_JSON', seen[0])

    def test_valid_trigger_is_preserved(self):
        self.items[0]['triggers'] = [r'\bprepare\b']
        self.write_catalog()
        catalog, _ = startup.load_catalog(self.root)
        self.assertEqual(catalog['prepare']['triggers'], [r'\bprepare\b'])

    def test_invalid_triggers_are_rejected(self):
        for patterns in ('prepare', ['['], [''], ['a'] * 9, [17]):
            self.items[0]['triggers'] = patterns
            self.write_catalog()
            with self.assertRaises(ValueError):
                startup.load_catalog(self.root)

    def test_invalid_secret_mapping(self):
        for value in ['[]', '{"PATH":"bad"}', '{"GITHUB_TOKEN":"bad"}', '{"APP_KEY":7}']:
            with self.assertRaises(ValueError):
                startup.secret_environment(dict(self.env, STARTUP_SECRETS_JSON=value))

    def test_github_masks_nested_secrets(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            child = startup.secret_environment(dict(self.env, GITHUB_ACTIONS='true',
                                                   STARTUP_SECRETS_JSON='{"APP_KEY":"line1\\nline2"}'))
        self.assertIn('::add-mask::line1%0Aline2', output.getvalue())
        self.assertEqual(child['APP_KEY'], 'line1\nline2')

    @unittest.skipUnless(os.name == 'posix', 'Linux execution test')
    def test_real_folder_routine_uses_isolated_home(self):
        script = PROJECT / 'automacoes/rotinas/preparar-pastas.sh'
        (self.root / 'automacoes/rotinas/prepare.sh').write_bytes(script.read_bytes())
        (self.root / 'automacoes/pastas.json').write_text('["Projetos/demo"]')
        self.make_plan()
        startup.execute(self.root, self.plan, 'before_desktop', self.env)
        self.assertTrue((Path(self.env['HOME']) / 'Projetos/demo').is_dir())

    def test_failed_routine_is_not_marked_complete(self):
        self.make_plan()
        def failing(*args):
            raise RuntimeError('failed')
        with self.assertRaises(RuntimeError):
            startup.execute(self.root, self.plan, 'before_desktop', self.env, failing)
        self.assertEqual(json.loads(self.plan.read_text())['completed'], [])


if __name__ == '__main__':
    unittest.main()
