import contextlib
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import enviar_link
import handoff
import incremental_backup

TOKEN = 'github_pat_SEGREDO_NAO_PODE_VAZAR'
ENV = {'PEER_REPO': 'dono/par', 'PEER_REPO_TOKEN': TOKEN, 'GITHUB_REPOSITORY': 'dono/principal'}
WF = '/repos/dono/par/actions/workflows/linux-web-pc.yml'


class Response:
    def __init__(self, status, payload):
        self.status = status
        self._raw = json.dumps(payload).encode() if payload is not None else b''

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeGitHub:
    """urlopen falso: handler(method, path, body) -> (status, payload) ou levanta URLError."""
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, request, timeout=None):
        path = request.full_url.replace(handoff.API, '')
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), path, body, request.get_header('Authorization')))
        status, payload = self.handler(request.get_method(), path, body)
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, 'erro', {}, io.BytesIO(json.dumps(payload).encode()))
        return Response(status, payload)


def healthy(runs=None, dispatch_status=204):
    runs = [] if runs is None else runs

    def handler(method, path, body):
        if method == 'GET' and path == '/repos/dono/par':
            return 200, {'default_branch': 'main'}
        if method == 'GET' and path == WF:
            return 200, {'state': 'active'}
        if method == 'GET' and path.startswith(WF + '/runs'):
            return 200, {'workflow_runs': runs() if callable(runs) else runs}
        if method == 'POST' and path == WF + '/dispatches':
            return dispatch_status, {} if dispatch_status == 204 else {'message': 'Unexpected inputs'}
        raise AssertionError((method, path))
    return handler


class Clock:
    def __init__(self, start=0.0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def run_quiet(function, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = function(*args, **kwargs)
    return result, out.getvalue()


class SettingsTests(unittest.TestCase):
    def test_off_without_peer(self):
        self.assertIsNone(handoff.settings({'GITHUB_REPOSITORY': 'dono/principal'}))

    def test_valid_configuration_defaults_workflow(self):
        cfg = handoff.settings(ENV)
        self.assertEqual((cfg['peer'], cfg['workflow']), ('dono/par', 'linux-web-pc.yml'))

    def test_rejects_unsafe_or_incomplete_configuration(self):
        for change in ({'PEER_REPO': 'dono/principal'}, {'PEER_REPO': 'DONO/Principal'},
                       {'PEER_REPO': '../x/y z'}, {'PEER_REPO_TOKEN': ' '},
                       {'PEER_WORKFLOW': '../evil.yml'}, {'PEER_WORKFLOW': 'a b.yml'},
                       {'HANDOFF_STARTUP_INSTRUCTION': 'x' * 70000}):
            with self.subTest(change=change):
                with self.assertRaises(handoff.HandoffError):
                    handoff.settings(dict(ENV, **change))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.cfg = handoff.settings(ENV)

    def test_check_returns_peer_default_branch(self):
        self.assertEqual(handoff.check(self.cfg, FakeGitHub(healthy())), 'main')

    def test_check_rejects_inactive_workflow(self):
        def handler(method, path, body):
            return (200, {'default_branch': 'main'}) if path == '/repos/dono/par' else (200, {'state': 'disabled_manually'})
        with self.assertRaisesRegex(handoff.HandoffError, 'nao esta ativo'):
            handoff.check(self.cfg, FakeGitHub(handler))

    def test_error_messages_never_contain_token(self):
        for status in (401, 403, 404, 422, 500):
            gh = FakeGitHub(lambda m, p, b, s=status: (s, {'message': 'ruim'}))
            with self.assertRaises(handoff.HandoffError) as caught:
                handoff.check(self.cfg, gh)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_network_failure_becomes_handoff_error(self):
        def boom(request, timeout=None):
            raise urllib.error.URLError('sem rede')
        with self.assertRaisesRegex(handoff.HandoffError, 'falha de rede'):
            handoff.check(self.cfg, boom)

    def test_peer_busy_detects_active_and_queued_runs(self):
        for status in ('in_progress', 'queued', 'waiting', 'pending'):
            gh = FakeGitHub(healthy(runs=[{'status': 'completed'}, {'status': status}]))
            self.assertTrue(handoff.peer_busy(self.cfg, gh), status)
        self.assertFalse(handoff.peer_busy(self.cfg, FakeGitHub(healthy(runs=[{'status': 'completed'}]))))

    def test_peer_busy_fails_closed_on_run_without_status(self):
        with self.assertRaises(handoff.HandoffError):
            handoff.peer_busy(self.cfg, FakeGitHub(healthy(runs=[{'id': 1}])))

    def test_dispatch_sends_branch_and_only_non_empty_inputs(self):
        gh = FakeGitHub(healthy())
        handoff.dispatch(self.cfg, 'main', gh)
        post = [c for c in gh.calls if c[0] == 'POST'][0]
        self.assertEqual(post[2], {'ref': 'main'})
        self.assertEqual(post[3], 'Bearer ' + TOKEN)
        gh = FakeGitHub(healthy())
        handoff.dispatch(dict(self.cfg, instruction='Abra o Obsidian.'), 'main', gh)
        post = [c for c in gh.calls if c[0] == 'POST'][0]
        self.assertEqual(post[2], {'ref': 'main', 'inputs': {'startup_instruction': 'Abra o Obsidian.'}})

    def test_dispatch_rejects_non_204(self):
        with self.assertRaisesRegex(handoff.HandoffError, '422'):
            handoff.dispatch(self.cfg, 'main', FakeGitHub(healthy(dispatch_status=422)))


class RunHandoffTests(unittest.TestCase):
    def setUp(self):
        self.cfg = handoff.settings(ENV)
        self.clock = Clock()

    def run_handoff(self, gh, deadline=1800):
        return run_quiet(handoff.run_handoff, self.cfg, deadline, now=self.clock.now,
                         sleep=self.clock.sleep, urlopen=gh, retry=60)

    def test_dispatches_immediately_when_peer_is_idle(self):
        gh = FakeGitHub(healthy())
        outcome, out = self.run_handoff(gh)
        self.assertEqual(outcome, 'dispatched')
        self.assertEqual(sum(1 for c in gh.calls if c[0] == 'POST'), 1)
        self.assertNotIn(TOKEN, out)

    def test_waits_for_busy_peer_then_dispatches_once(self):
        gh = FakeGitHub(healthy(runs=lambda: [{'status': 'in_progress' if self.clock.t < 300 else 'completed'}]))
        outcome, out = self.run_handoff(gh)
        self.assertEqual(outcome, 'dispatched')
        self.assertEqual(sum(1 for c in gh.calls if c[0] == 'POST'), 1)
        self.assertGreaterEqual(self.clock.t, 300)
        self.assertEqual(out.count('::warning::'), 1)  # avisa uma vez, nao a cada tentativa

    def test_busy_until_deadline_never_dispatches(self):
        gh = FakeGitHub(healthy(runs=[{'status': 'in_progress'}]))
        outcome, out = self.run_handoff(gh)
        self.assertEqual(outcome, 'busy')
        self.assertFalse([c for c in gh.calls if c[0] == 'POST'])
        self.assertNotIn('::error::', out)

    def test_persistent_failure_reports_error_and_retries(self):
        gh = FakeGitHub(lambda m, p, b: (401, {'message': 'Bad credentials'}))
        outcome, out = self.run_handoff(gh)
        self.assertEqual(outcome, 'failed')
        self.assertGreater(len(gh.calls), 5)
        self.assertIn('::error::', out)
        self.assertNotIn(TOKEN, out)

    def test_recovers_after_transient_failure(self):
        state = {'n': 0}

        def handler(method, path, body):
            if path == '/repos/dono/par':
                state['n'] += 1
                if state['n'] < 3:
                    return 500, {}
            return healthy()(method, path, body)
        outcome, _ = self.run_handoff(FakeGitHub(handler))
        self.assertEqual(outcome, 'dispatched')


class TimingTests(unittest.TestCase):
    def test_dispatch_happens_30_minutes_before_the_end(self):
        clock = Clock(start=1000.0)
        started = 1000.0
        dispatched_at = []

        def handler(method, path, body):
            if method == 'POST':
                dispatched_at.append(clock.t - started)
            return healthy()(method, path, body)
        cfg = handoff.settings(ENV)
        run_quiet(handoff._worker, cfg, started, 18000, 1800, clock.now, clock.sleep, FakeGitHub(handler))
        self.assertEqual(len(dispatched_at), 1)
        self.assertGreaterEqual(dispatched_at[0], 16200)   # 4h30
        self.assertLess(dispatched_at[0], 16200 + 60)

    def test_start_background_is_skipped_when_off_or_preview(self):
        self.assertIsNone(handoff.start_background(0, {'GITHUB_REPOSITORY': 'dono/principal'}))
        env = dict(ENV, NEEDLE_PREVIEW_ONLY='1')
        thread, out = run_quiet(handoff.start_background, 0, env)
        self.assertIsNone(thread)
        self.assertIn('somente planejamento', out)

    def test_start_background_with_invalid_settings_does_not_crash_session(self):
        thread, out = run_quiet(handoff.start_background, 0, dict(ENV, PEER_REPO_TOKEN=''))
        self.assertIsNone(thread)
        self.assertIn('::error::', out)

    def test_start_background_announces_time_without_secrets(self):
        # now sempre apos o gatilho e deadline ja passado: a thread termina na hora.
        thread, out = run_quiet(handoff.start_background, -40000, ENV, 18000, 1800, lambda: 0.0, lambda s: None)
        thread.join(timeout=5)
        self.assertIn('dono/par', out)
        self.assertNotIn(TOKEN, out)


class PreflightTests(unittest.TestCase):
    def test_off_returns_zero(self):
        code, out = run_quiet(handoff.preflight, {'GITHUB_REPOSITORY': 'dono/principal'})
        self.assertEqual(code, 0)
        self.assertIn('desligado', out)

    def test_ok_when_peer_reachable(self):
        code, out = run_quiet(handoff.preflight, ENV, FakeGitHub(healthy()))
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, out)

    def test_retries_network_errors_only(self):
        calls = {'n': 0}
        good = FakeGitHub(healthy())

        def flaky(request, timeout=None):
            calls['n'] += 1
            if calls['n'] <= 2:
                raise urllib.error.URLError('rede')
            return good(request, timeout)
        code, _ = run_quiet(handoff.preflight, ENV, flaky, lambda s: None)
        self.assertEqual(code, 0)
        with self.assertRaises(handoff.HandoffError):  # 401 nao e repetido
            run_quiet(handoff.preflight, ENV, FakeGitHub(lambda m, p, b: (401, {})), lambda s: None)


def iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


NOW = 1_800_000_000.0


class ProfileTests(unittest.TestCase):
    def test_both_sides_compute_the_same_profile(self):
        a = handoff.shared_profile({'GITHUB_REPOSITORY': 'dono/a', 'PEER_REPO': 'dono/b'})
        b = handoff.shared_profile({'GITHUB_REPOSITORY': 'dono/b', 'PEER_REPO': 'dono/a'})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)
        self.assertEqual(a, handoff.shared_profile({'GITHUB_REPOSITORY': 'DONO/A', 'PEER_REPO': 'Dono/B'}))

    def test_no_shared_profile_when_off_or_invalid(self):
        for env in ({'GITHUB_REPOSITORY': 'dono/a'}, {'PEER_REPO': 'dono/b'},
                    {'GITHUB_REPOSITORY': 'dono/a', 'PEER_REPO': 'dono/a'},
                    {'GITHUB_REPOSITORY': 'dono/a', 'PEER_REPO': 'sem barra'}):
            self.assertIsNone(handoff.shared_profile(env), env)

    def test_backup_profile_is_per_repo_when_off_and_shared_when_on(self):
        off = incremental_backup.profile_for({'GITHUB_REPOSITORY': 'dono/a'})
        on = incremental_backup.profile_for({'GITHUB_REPOSITORY': 'dono/a', 'PEER_REPO': 'dono/b'})
        other = incremental_backup.profile_for({'GITHUB_REPOSITORY': 'dono/b', 'PEER_REPO': 'dono/a'})
        self.assertNotEqual(off, on)
        self.assertEqual(on, other)
        self.assertEqual(off, incremental_backup.profile_for({'GITHUB_REPOSITORY': 'dono/a'}))


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = handoff.settings(ENV)

    def test_stale_active_run_is_ignored_but_fresh_one_counts(self):
        zombie = {'status': 'queued', 'created_at': iso(NOW - 8 * 3600)}
        fresh = {'status': 'in_progress', 'created_at': iso(NOW - 3600)}
        self.assertFalse(handoff.peer_busy(self.cfg, FakeGitHub(healthy(runs=[zombie])), now=lambda: NOW))
        self.assertTrue(handoff.peer_busy(self.cfg, FakeGitHub(healthy(runs=[zombie, fresh])), now=lambda: NOW))

    def test_peer_older_active_only_counts_older_sessions(self):
        mine = NOW
        older = {'status': 'in_progress', 'created_at': iso(NOW - 4.5 * 3600)}
        younger = {'status': 'in_progress', 'created_at': iso(NOW + 600)}
        done = {'status': 'completed', 'created_at': iso(NOW - 4.5 * 3600)}
        check = lambda runs: handoff.peer_older_active(self.cfg, mine, FakeGitHub(healthy(runs=runs)), lambda: NOW)
        self.assertTrue(check([older]))
        self.assertFalse(check([younger]))
        self.assertFalse(check([done]))
        self.assertFalse(check([]))


class WaitForOlderPeerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(start=NOW)
        self.env = dict(ENV, SESSION_STARTED=str(NOW))

    def wait(self, gh, **kwargs):
        return run_quiet(handoff.wait_for_older_peer, self.env, now=self.clock.now, sleep=self.clock.sleep,
                         urlopen=gh, **kwargs)

    def older(self):
        return [{'status': 'in_progress' if self.clock.t < NOW + 600 else 'completed',
                 'created_at': iso(NOW - 4.5 * 3600)}]

    def test_waits_until_the_older_session_finishes(self):
        result, out = self.wait(FakeGitHub(healthy(runs=self.older)))
        self.assertEqual(result, 'idle')
        self.assertGreaterEqual(self.clock.t, NOW + 600)
        self.assertIn('Aguardando', out)

    def test_does_not_wait_for_a_younger_session(self):
        gh = FakeGitHub(healthy(runs=[{'status': 'in_progress', 'created_at': iso(NOW + 300)}]))
        result, _ = self.wait(gh)
        self.assertEqual(result, 'idle')
        self.assertEqual(self.clock.t, NOW)

    def test_gives_up_after_the_limit_instead_of_blocking_the_backup(self):
        gh = FakeGitHub(healthy(runs=[{'status': 'in_progress', 'created_at': iso(NOW - 4.5 * 3600)}]))
        result, out = self.wait(gh, max_wait=900)
        self.assertEqual(result, 'timeout')
        self.assertLessEqual(self.clock.t - NOW, 900 + 60)
        self.assertIn('::warning::', out)

    def test_api_failures_do_not_block_the_backup(self):
        result, out = self.wait(FakeGitHub(lambda m, p, b: (500, {})))
        self.assertEqual(result, 'unknown')
        self.assertNotIn(TOKEN, out)

    def test_off_without_peer_or_without_session_start(self):
        gh = FakeGitHub(healthy())
        self.assertEqual(run_quiet(handoff.wait_for_older_peer, {'GITHUB_REPOSITORY': 'dono/a'})[0], 'off')
        self.assertEqual(run_quiet(handoff.wait_for_older_peer, ENV, urlopen=gh)[0], 'off')  # sem SESSION_STARTED
        self.assertEqual(gh.calls, [])


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = handoff.settings(ENV)
        self.clock = Clock()

    def run_with_notify(self, gh):
        sent = []
        outcome, _ = run_quiet(handoff.run_handoff, self.cfg, 600, now=self.clock.now, sleep=self.clock.sleep,
                               urlopen=gh, retry=60, notify=lambda subject, text: sent.append((subject, text)))
        return outcome, sent

    def test_failure_sends_one_email_without_the_token(self):
        outcome, sent = self.run_with_notify(FakeGitHub(lambda m, p, b: (401, {'message': 'Bad credentials'})))
        self.assertEqual(outcome, 'failed')
        self.assertEqual(len(sent), 1)
        self.assertIn('falhou', sent[0][0])
        self.assertNotIn(TOKEN, sent[0][1])
        self.assertIn('PEER_REPO_TOKEN', sent[0][1])

    def test_success_and_busy_peer_send_no_email(self):
        self.assertEqual(self.run_with_notify(FakeGitHub(healthy())), ('dispatched', []))
        outcome, sent = self.run_with_notify(FakeGitHub(healthy(runs=[{'status': 'in_progress'}])))
        self.assertEqual((outcome, sent), ('busy', []))

    def test_notifier_sends_plain_notice_with_run_link(self):
        env = dict(ENV, SMTP_HOST='smtp.exemplo.com', SMTP_USER='eu@exemplo.com', SMTP_PASSWORD='x',
                   EMAIL_TO='eu@exemplo.com', GITHUB_RUN_ID='123')
        with mock.patch.object(enviar_link, 'send') as send:
            handoff.make_notifier(env)('Assunto', 'Texto')
        message = send.call_args[0][1]
        self.assertEqual(message['Subject'], 'Assunto')
        self.assertEqual(message['To'], 'eu@exemplo.com')
        self.assertIn('actions/runs/123', message.get_content())
        self.assertNotIn('SMTP', message.get_content())

    def test_notifier_is_silent_without_smtp_and_never_raises(self):
        with mock.patch.object(enviar_link, 'send') as send:
            handoff.make_notifier(ENV)('Assunto', 'Texto')
        send.assert_not_called()
        env = dict(ENV, SMTP_HOST='smtp.exemplo.com', SMTP_USER='eu@exemplo.com', SMTP_PASSWORD='x', EMAIL_TO='eu@exemplo.com')
        with mock.patch.object(enviar_link, 'send', side_effect=OSError('rede')):
            _, out = run_quiet(handoff.make_notifier(env), 'Assunto', 'Texto')
        self.assertIn('::warning::', out)


if __name__ == '__main__':
    unittest.main()
