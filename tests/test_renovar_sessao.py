import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import handoff
import renovar_sessao
from test_handoff import ENV, NOW, TOKEN, FakeGitHub, iso, run_quiet

OWN = 'dono/principal'
WF = f'/repos/{OWN}/actions/workflows/linux-web-pc.yml'
BASE_ENV = {'RENOVAR': 'true', 'RENOVAR_TOKEN': TOKEN, 'GITHUB_REPOSITORY': OWN}


def renovar_env(**extra):
    env = dict(BASE_ENV, **extra)
    return env


def run(id=1, status='completed', conclusion='success', age=4 * 3600, duration=5 * 3600,
        ended_ago=None, created=None):
    inicio = NOW - age if created is None else created
    fim = NOW - (ended_ago if ended_ago is not None else age - duration)
    return {'id': id, 'status': status, 'conclusion': conclusion if status == 'completed' else None,
            'created_at': iso(inicio), 'updated_at': iso(fim)}


def github(runs, dispatch_status=204):
    """GitHub falso do proprio repositorio. Registra os POSTs em .posts."""
    posts = []

    def handler(method, path, body):
        if method == 'GET' and path == f'/repos/{OWN}':
            return 200, {'default_branch': 'main'}
        if method == 'GET' and path == WF:
            return 200, {'state': 'active'}
        if method == 'GET' and path.startswith(WF + '/runs'):
            return 200, {'workflow_runs': runs() if callable(runs) else runs}
        if method == 'POST' and path == WF + '/dispatches':
            posts.append(body)
            return dispatch_status, {} if dispatch_status == 204 else {'message': 'ruim'}
        raise AssertionError((method, path))
    fake = FakeGitHub(handler)
    fake.posts = posts
    return fake


class SettingsTests(unittest.TestCase):
    def test_off_without_renovar_true(self):
        self.assertIsNone(renovar_sessao.settings({'RENOVAR': 'false', 'GITHUB_REPOSITORY': OWN}))
        self.assertIsNone(renovar_sessao.settings({'GITHUB_REPOSITORY': OWN}))

    def test_valid_configuration_defaults(self):
        cfg = renovar_sessao.settings(renovar_env())
        self.assertEqual((cfg['peer'], cfg['workflow'], cfg['max_day'], cfg['max_month']),
                         (OWN, 'linux-web-pc.yml', 20.0, 0.0))

    def test_rejects_incomplete_or_conflicting_configuration(self):
        for change in ({'RENOVAR_TOKEN': ' '}, {'RENOVAR_WORKFLOW': '../evil.yml'},
                       {'RENOVAR_MAX_HORAS': 'muito'}, {'RENOVAR_MAX_HORAS': '2000'},
                       {'PEER_REPO': 'dono/par'},
                       {'HANDOFF_STARTUP_INSTRUCTION': 'x' * 70000}):
            with self.subTest(change=change):
                with self.assertRaises((renovar_sessao.RenovarError, handoff.HandoffError)):
                    renovar_sessao.settings(renovar_env(**change))

    def test_comma_and_zero_limits(self):
        cfg = renovar_sessao.settings(renovar_env(RENOVAR_MAX_HORAS='7,5', RENOVAR_MAX_HORAS_MES='0'))
        self.assertEqual((cfg['max_day'], cfg['max_month']), (7.5, 0.0))


class DecideTests(unittest.TestCase):
    def decide(self, runs, own_id=None, now=NOW, max_day=20, max_month=0):
        return renovar_sessao.decide(runs, own_id, now, max_day, max_month)

    def test_other_active_run_blocks(self):
        self.assertEqual(self.decide([run(1, 'in_progress')])[0], 'active')
        self.assertEqual(self.decide([run(1, 'queued')])[0], 'active')

    def test_own_run_does_not_block_renovar(self):
        self.assertEqual(self.decide([run(1, 'in_progress')], own_id=1)[0], 'start')

    def test_stale_queued_run_does_not_block(self):
        self.assertEqual(self.decide([run(1, 'queued', age=9 * 3600)])[0], 'start')

    def test_daily_cap_pauses(self):
        # Duas sessoes completas nas ultimas 24h = 10h; limite de 10h pausa.
        runs = [run(1, age=6 * 3600), run(2, age=14 * 3600)]
        self.assertEqual(self.decide(runs, max_day=10)[0], 'cap_day')
        # A propria execucao ativa entra na conta do uso (1 h) + 7 h da anterior.
        runs = [run(1, 'in_progress', age=1 * 3600, duration=0), run(2, age=8 * 3600, duration=7 * 3600)]
        self.assertEqual(self.decide(runs, own_id=1, max_day=8)[0], 'cap_day')

    def test_daily_cap_ignores_runs_older_than_window(self):
        runs = [run(1, age=30 * 3600), run(2, age=31 * 3600)]
        self.assertEqual(self.decide(runs, max_day=1)[0], 'start')

    def test_monthly_cap_pauses(self):
        runs = [run(1, age=25 * 3600), run(2, age=25 * 86400), run(3, age=26 * 86400)]
        self.assertEqual(self.decide(runs, max_day=0, max_month=10)[0], 'cap_month')

    def test_breaker_pauses_after_three_recent_failures(self):
        runs = [run(1, conclusion='failure', ended_ago=600),
                run(2, conclusion='failure', age=6 * 3600),
                run(3, conclusion='timed_out', age=12 * 3600)]
        self.assertEqual(self.decide(runs)[0], 'paused')

    def test_breaker_ignores_old_failures(self):
        runs = [run(1, conclusion='failure', ended_ago=4 * 3600),
                run(2, conclusion='failure', age=6 * 3600),
                run(3, conclusion='failure', age=12 * 3600)]
        self.assertEqual(self.decide(runs)[0], 'start')

    def test_breaker_requires_three_failures(self):
        runs = [run(1, conclusion='failure', ended_ago=600),
                run(2, conclusion='success', age=6 * 3600),
                run(3, conclusion='failure', age=12 * 3600)]
        self.assertEqual(self.decide(runs)[0], 'start')


class MainTests(unittest.TestCase):
    def renovar(self, env, fake, argv=('x', 'renovar')):
        return renovar_sessao.main(list(argv), env=env, urlopen=fake, now=lambda: NOW)

    def test_disabled_does_nothing(self):
        fake = github([])
        result, out = run_quiet(self.renovar, {'RENOVAR': 'false', 'GITHUB_REPOSITORY': OWN}, fake)
        self.assertEqual((result, fake.posts), (0, []))
        self.assertIn('desligada', out)

    def test_preview_mode_is_ignored(self):
        fake = github([])
        result, out = run_quiet(self.renovar, renovar_env(NEEDLE_PREVIEW_ONLY='1'), fake)
        self.assertEqual((result, fake.posts), (0, []))
        self.assertIn('planejamento', out)

    def test_renovar_dispatches_with_same_instruction(self):
        fake = github([run(1, 'in_progress', age=5 * 3600, duration=0)])
        env = renovar_env(GITHUB_RUN_ID='1', HANDOFF_STARTUP_INSTRUCTION='Abra o editor')
        result, out = run_quiet(self.renovar, env, fake)
        self.assertEqual(result, 0)
        self.assertEqual(fake.posts, [{'ref': 'main', 'inputs': {'startup_instruction': 'Abra o editor'}}])
        self.assertIn('nova sessao solicitada', out)

    def test_renovar_without_instruction_uses_files(self):
        fake = github([run(1, 'in_progress', age=5 * 3600, duration=0)])
        result, _ = run_quiet(self.renovar, renovar_env(GITHUB_RUN_ID='1'), fake)
        self.assertEqual(result, 0)
        self.assertEqual(fake.posts, [{'ref': 'main'}])

    def test_renovar_skips_when_another_run_is_active(self):
        fake = github([run(1, 'in_progress', age=1 * 3600), run(2, 'queued', age=60)])
        env = renovar_env(GITHUB_RUN_ID='1')
        result, out = run_quiet(self.renovar, env, fake)
        self.assertEqual((result, fake.posts), (0, []))
        self.assertIn('nada a fazer', out)

    def test_vigiar_dispatches_when_idle(self):
        fake = github([run(1, age=3600)])
        result, out = run_quiet(self.renovar, renovar_env(), fake, argv=('x', 'vigiar'))
        self.assertEqual(result, 0)
        self.assertEqual(fake.posts, [{'ref': 'main'}])
        self.assertIn('nova sessao solicitada', out)

    def test_vigiar_skips_when_active(self):
        fake = github([run(1, 'in_progress', age=1 * 3600)])
        result, out = run_quiet(self.renovar, renovar_env(), fake, argv=('x', 'vigiar'))
        self.assertEqual((result, fake.posts), (0, []))
        self.assertIn('nada a fazer', out)

    def test_daily_cap_blocks_dispatch(self):
        runs = [run(1, age=6 * 3600), run(2, age=14 * 3600)]
        fake = github(runs)
        env = renovar_env(RENOVAR_MAX_HORAS='10')
        result, out = run_quiet(self.renovar, env, fake, argv=('x', 'vigiar'))
        self.assertEqual((result, fake.posts), (0, []))
        self.assertIn('limite diario', out)

    def test_breaker_blocks_dispatch(self):
        runs = [run(1, conclusion='failure', ended_ago=600),
                run(2, conclusion='failure', age=6 * 3600),
                run(3, conclusion='startup_failure', age=12 * 3600)]
        fake = github(runs)
        result, out = run_quiet(self.renovar, renovar_env(), fake, argv=('x', 'vigiar'))
        self.assertEqual((result, fake.posts), (0, []))
        self.assertIn('pausa', out)

    def test_dispatch_failure_is_reported(self):
        fake = github([], dispatch_status=422)
        result, out = run_quiet(self.renovar, renovar_env(), fake, argv=('x', 'vigiar'))
        self.assertEqual(result, 1)
        self.assertIn('::error::', out)

    def test_missing_token_fails_early(self):
        fake = github([])
        result, out = run_quiet(self.renovar, {'RENOVAR': 'true', 'GITHUB_REPOSITORY': OWN}, fake)
        self.assertEqual(result, 1)
        self.assertIn('RENOVAR_TOKEN', out)

    def test_peer_repo_conflict_is_refused(self):
        fake = github([])
        env = renovar_env(PEER_REPO='dono/par')
        result, out = run_quiet(self.renovar, env, fake)
        self.assertEqual((result, fake.posts), (1, []))
        self.assertIn('revezamento OU a renovacao', out)

    def test_invalid_usage(self):
        self.assertEqual(renovar_sessao.main(['x']), 2)


if __name__ == '__main__':
    unittest.main()
