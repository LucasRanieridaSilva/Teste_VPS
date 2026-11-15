import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import enviar_link
import handoff
import vigia
from test_handoff import ENV, NOW, TOKEN, FakeGitHub, iso, run_quiet

OWN, PEER = 'dono/principal', 'dono/par'
SMTP = dict(ENV, SMTP_HOST='smtp.exemplo.com', SMTP_USER='eu@exemplo.com', SMTP_PASSWORD='x', EMAIL_TO='eu@exemplo.com')


def run(status='completed', conclusion='success', age=3600, ended_ago=None):
    created = NOW - age
    return {'status': status, 'conclusion': conclusion if status == 'completed' else None,
            'created_at': iso(created), 'updated_at': iso(NOW - (ended_ago if ended_ago is not None else age - 600))}


def two_repos(own_runs, peer_runs, dispatch_status=204):
    """GitHub falso com os dois repositorios. Registra os POSTs em .posts."""
    posts = []

    def handler(method, path, body):
        for repo, runs in ((OWN, own_runs), (PEER, peer_runs)):
            base = f'/repos/{repo}'
            wf = f'{base}/actions/workflows/linux-web-pc.yml'
            if method == 'GET' and path == base:
                return 200, {'default_branch': 'main'}
            if method == 'GET' and path == wf:
                return 200, {'state': 'active'}
            if method == 'GET' and path.startswith(wf + '/runs'):
                return 200, {'workflow_runs': runs}
            if method == 'POST' and path == wf + '/dispatches':
                posts.append(repo)
                return dispatch_status, {} if dispatch_status == 204 else {'message': 'ruim'}
        raise AssertionError((method, path))
    gh = FakeGitHub(handler)
    gh.posts = posts
    return gh


class DecideTests(unittest.TestCase):
    def decide(self, own, peer):
        return vigia.decide({OWN: own, PEER: peer}, NOW)

    def test_active_session_in_either_repo_means_nothing_to_do(self):
        self.assertEqual(self.decide([run('in_progress')], [])[0], 'active')
        self.assertEqual(self.decide([], [run('queued')])[0], 'active')

    def test_no_active_session_starts_one(self):
        self.assertEqual(self.decide([run()], [run()]), ('start', None))
        self.assertEqual(self.decide([], []), ('start', None))

    def test_zombie_queued_run_does_not_block_the_restart(self):
        self.assertEqual(self.decide([run('queued', age=9 * 3600)], [])[0], 'start')

    def test_breaker_pauses_after_three_recent_failures(self):
        fails = [run(conclusion='failure', age=3600 * (i + 1), ended_ago=1200 * (i + 1)) for i in range(3)]
        self.assertEqual(self.decide(fails[:2], fails[2:]), ('paused', True))   # falhou ha 20 min: avisa
        old = [run(conclusion='failure', age=3600 * (i + 1), ended_ago=3000 + 600 * i) for i in range(3)]
        self.assertEqual(self.decide(old, []), ('paused', False))               # ha 50 min: nao repete o aviso

    def test_breaker_expires_after_three_hours(self):
        stale = [run(conclusion='failure', age=4 * 3600 + 600 * i, ended_ago=4 * 3600 + 600 * i) for i in range(3)]
        self.assertEqual(self.decide(stale, []), ('start', None))

    def test_breaker_needs_three_failures_in_a_row(self):
        mixed = [run(conclusion='failure', age=1000), run(conclusion='success', age=2000),
                 run(conclusion='failure', age=3000)]
        self.assertEqual(self.decide(mixed, [])[0], 'start')
        self.assertEqual(self.decide(mixed[:1] + mixed[2:], [])[0], 'start')  # so duas
        cancelled = [run(conclusion='cancelled', age=1000 * (i + 1)) for i in range(3)]
        self.assertEqual(self.decide(cancelled, [])[0], 'start')  # cancelar a mao nao e falha


class MainTests(unittest.TestCase):
    def main(self, gh, env=None):
        sent = []
        with mock.patch.object(enviar_link, 'send', side_effect=lambda cfg, msg: sent.append(msg)):
            code, out = run_quiet(vigia.main, env or SMTP, gh, lambda: NOW)
        return code, out, sent

    def test_does_nothing_while_a_session_is_active(self):
        gh = two_repos([], [run('in_progress')])
        code, out, sent = self.main(gh)
        self.assertEqual((code, gh.posts, sent), (0, [], []))

    def test_restarts_the_cycle_in_its_own_repository_and_emails(self):
        gh = two_repos([run()], [run()])
        code, out, sent = self.main(gh)
        self.assertEqual((code, gh.posts), (0, [OWN]))
        self.assertEqual(len(sent), 1)
        self.assertIn('reabriu', sent[0]['Subject'])
        self.assertNotIn(TOKEN, out + sent[0].get_content())

    def test_does_not_dispatch_while_paused(self):
        fails = [run(conclusion='failure', age=3600 * (i + 1), ended_ago=600 * (i + 1)) for i in range(3)]
        gh = two_repos(fails, [])
        code, out, sent = self.main(gh)
        self.assertEqual((code, gh.posts), (0, []))
        self.assertEqual(len(sent), 1)
        self.assertIn('pausa', sent[0]['Subject'])

    def test_api_failure_exits_nonzero_and_emails_without_token(self):
        code, out, sent = self.main(FakeGitHub(lambda m, p, b: (401, {'message': 'Bad credentials'})))
        self.assertEqual(code, 1)
        self.assertEqual(len(sent), 1)
        self.assertNotIn(TOKEN, out + sent[0].get_content())

    def test_failed_dispatch_exits_nonzero(self):
        gh = two_repos([run()], [run()], dispatch_status=422)
        code, _, sent = self.main(gh)
        self.assertEqual(code, 1)
        self.assertIn('falhou', sent[0]['Subject'])

    def test_inactive_without_peer(self):
        code, out, sent = self.main(FakeGitHub(lambda m, p, b: self.fail('nao deveria chamar a API')),
                                    {'GITHUB_REPOSITORY': OWN})
        self.assertEqual((code, sent), (0, []))
        self.assertIn('inativo', out)


if __name__ == '__main__':
    unittest.main()
