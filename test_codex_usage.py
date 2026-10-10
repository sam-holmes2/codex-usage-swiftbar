"""Offline checks for identity isolation and truthful quota rendering."""
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('widget', Path(__file__).with_name('codex_usage.1m.py'))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


def token(email, expires=None):
    payload = {'https://api.openai.com/profile': {'email': email},
               'exp': time.time() + 3600 if expires is None else expires}
    return 'header.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode() + '.sig'


def account(label, value=None, error=None):
    p = dict(label=label, short=label[0], email=label+'@example.test', app='Test',
             source='test', path='/test/'+label)
    if error:
        return dict(profile=p, error=error)
    return dict(profile=p, result={'rateLimitsByLimitId': {'codex': {
        'primary': {'usedPercent': value, 'windowDurationMins': 10080,
                    'resetsAt': 1000000 + 2 * 86400 + 21 * 3600}}}})


class WidgetTests(unittest.TestCase):
    def test_untrusted_binary_never_starts(self):
        with patch.object(w.subprocess, 'run', return_value=Mock(returncode=1)) as check, \
             patch.object(w.subprocess, 'Popen') as start:
            with self.assertRaisesRegex(w.UsageError, 'signature'):
                w.AppServer('/untrusted/codex', '/private/tmp/isolated')
            start.assert_not_called()
            self.assertIn(w.CODEX_REQUIREMENT, check.call_args.args[0])
            self.assertEqual(check.call_args.args[0][0], '/usr/bin/codesign')
            self.assertTrue(w.CODEX_REQUIREMENT.startswith('='))

    def test_signature_timeout_fails_closed(self):
        with patch.object(w.subprocess, 'run', side_effect=w.subprocess.TimeoutExpired('check', 10)), \
             patch.object(w.subprocess, 'Popen') as start:
            with self.assertRaises(w.UsageError):
                w.AppServer('/untrusted/codex', '/private/tmp/isolated')
            start.assert_not_called()

    def test_no_arbitrary_executable_fallback(self):
        with patch.object(w, 'CODEX_CANDIDATES', ()), \
             patch.dict(w.os.environ, {'CODEX_XBAR_BIN': '/usr/bin/true',
                                       'PATH': '/attacker-controlled'}):
            self.assertIsNone(w.codex_binary())

    def test_helper_does_not_inherit_credentials_or_routing(self):
        with patch.dict(w.os.environ, {'OPENAI_API_KEY':'secret', 'CODEX_API_KEY':'secret',
                                      'HTTPS_PROXY':'https://untrusted.test',
                                      'DYLD_INSERT_LIBRARIES':'/untrusted.dylib',
                                      'RUST_LOG':'trace', 'CODEX_HOME':'/untrusted'}):
            env = w.helper_environment('/private/tmp/isolated')
        self.assertEqual(set(env), {'HOME','CODEX_HOME','TMPDIR','PATH','LANG'})
        self.assertEqual(env['CODEX_HOME'], '/private/tmp/isolated')
        self.assertEqual(env['PATH'], '/usr/bin:/bin:/usr/sbin:/sbin')
        self.assertNotIn('secret', json.dumps(env))

    def test_usage_response_identity_required_before_acceptance(self):
        auth = {'chatgptAccountId': 'expected', 'accessToken': 'fake'}
        for returned in ('wrong', None, 'expected'):
            server = Mock()
            result = dict(account('Personal', 23)['result'], accountId=returned,
                          rateLimitResetCredits={'id':'do-not-store'})
            server.request.side_effect = [{}, {}, result]
            with patch.object(w, 'codex_binary', return_value='/signed/codex'), \
                 patch.object(w, 'AppServer', return_value=server):
                got = w.fetch(auth)
            server.close.assert_called_once()
            if returned == 'expected':
                self.assertEqual(w.remaining(w.main_window(got['result'])), 77)
                self.assertNotIn('accountId', got['result'])
                self.assertNotIn('rateLimitResetCredits', got['result'])
            else:
                self.assertNotIn('result', got)
                self.assertTrue(got['identity'])

    def test_minimal_cache_keeps_entire_display_identical(self):
        rich = account('Personal', 23)
        result = rich['result']
        bucket = result['rateLimitsByLimitId']['codex']
        bucket.update(planType='pro', credits={'unlimited':False, 'balance':'12.50',
                                              'unneeded':'secret'},
                      secondary={'usedPercent':42,'windowDurationMins':300,'resetsAt':1200000},
                      hidden={'private':'secret'})
        result['rateLimitsByLimitId']['extra'] = {
            'limitName':'Extra model', 'primary':{'usedPercent':19,
            'windowDurationMins':1440, 'resetsAt':1500000}, 'unknown':'secret'}
        result.update(accountId='private-account', rateLimitResetCredits={'id':'secret'},
                      futureResponseField='secret')
        old, new = io.StringIO(), io.StringIO()
        with patch.object(w.time, 'time', return_value=1000000):
            with contextlib.redirect_stdout(old): w.render([rich])
            reduced = w.display_result(result)
            with contextlib.redirect_stdout(new): w.render([dict(rich, result=reduced)])
        self.assertEqual(old.getvalue(), new.getvalue())
        self.assertNotIn('secret', json.dumps(reduced))
        self.assertNotIn('private-account', json.dumps(reduced))

    def test_cache_migration_prunes_old_accounts_without_freshening_failure(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(w, 'CACHE', Path(folder)/'cache.json'), \
             patch.object(w.time, 'time', return_value=1000000):
            a = account('Personal', 20)
            b = account('Work', 60)
            previous = dict(b['result'], accountId='private', extra='secret')
            w.CACHE.write_text(json.dumps({'version':1,'accounts':{
                w.legacy_key(b['profile']): {'savedAt':999900,'result':previous},
                'obsolete-account':{'savedAt':999950,'result':previous}}}))
            w.save_cache([a, account('Work', error='offline')])
            records = w.load_cache()
            self.assertEqual(set(records), {w.profile_key(a['profile']),w.profile_key(b['profile'])})
            self.assertEqual(records[w.profile_key(b['profile'])]['savedAt'],999900)
            self.assertNotIn('private', w.CACHE.read_text())
            self.assertNotIn('secret', w.CACHE.read_text())
            self.assertEqual(w.CACHE.stat().st_mode & 0o777, 0o600)
            self.assertEqual(w.apply_cache([account('Work',error='offline')])[0]['stale_seconds'],100)

    def test_unconfigured_account_is_not_queried(self):
        profiles = [account('Personal')['profile'],account('Work')['profile']]
        with patch.object(w, 'load_profiles', return_value=profiles), \
             patch.object(w, 'sources', return_value=[{
                 'email':'unconfigured@example.test','subscription':'other',
                 'auth':{'accessToken':'never-send'}}]), \
             patch.object(w, 'fetch') as fetch, patch.object(w,'save_cache'), \
             patch.object(w,'apply_cache',side_effect=lambda x:x), \
             contextlib.redirect_stdout(io.StringIO()):
            w.main()
        fetch.assert_not_called()

    def test_two_distinct_accounts_keep_separate_verified_readings(self):
        profiles = [account('Personal')['profile'],account('Work')['profile']]
        def source(profile):
            return {'email':profile['email'], 'subscription':profile['label'],
                    'auth':{'chatgptAccountId':profile['label'],'accessToken':'fake'}}
        def server_factory(*args):
            server = Mock()
            identity = {}
            def request(method, params=None):
                if method == 'account/login/start': identity.update(params)
                if method == 'account/rateLimits/read':
                    label = identity['chatgptAccountId']
                    return dict(account(label,20 if label=='Personal' else 60)['result'],
                                accountId=label)
                return {}
            server.request.side_effect = request
            return server
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(w,'CACHE',Path(folder)/'cache.json'), \
             patch.object(w,'load_profiles',return_value=profiles), \
             patch.object(w,'inspect',side_effect=source), \
             patch.object(w,'codex_binary',return_value='/signed/codex'), \
             patch.object(w,'AppServer',side_effect=server_factory), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            w.main()
            self.assertEqual(len(w.load_cache()),2)
        title = output.getvalue().splitlines()[0]
        self.assertIn('P80%',title)
        self.assertIn('W40%',title)
        self.assertNotIn('~',title)

    def test_all_colour_bands_use_swiftbar_supported_indexed_ansi(self):
        for remaining, index in [(100,28),(70,28),(69,120),(50,120),
                                 (49,229),(40,229),(39,221),(30,221),
                                 (29,208),(20,208),(19,203),(10,203),
                                 (9,124),(0,124)]:
            reading = account('Personal', 100 - remaining)
            title = w.account_title(reading)
            self.assertTrue(title.startswith(f'\033[38;5;{index}m'))
            self.assertNotIn('38;2;', title)
            reading['stale_seconds'] = 180
            historical = w.account_title(reading)
            self.assertTrue(historical.startswith(f'\033[38;5;{index}m'))
            self.assertIn('~3m', historical)

    def test_bundle_cli_is_found_without_terminal_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / 'codex'
            modern = root / 'codex-cli/CodexCLI.app/Contents/MacOS/codex'
            modern.parent.mkdir(parents=True)
            modern.write_text('#!/bin/sh\nexit 0\n')
            modern.chmod(0o755)
            with patch.object(w, 'CODEX_CANDIDATES', (legacy, modern)), \
                 patch.dict(w.os.environ, {'PATH': '/usr/bin:/bin'}, clear=True), \
                 patch.dict(w.os.environ, {'CODEX_XBAR_BIN': '/untrusted/codex'}):
                self.assertEqual(w.codex_binary(), str(modern.resolve()))
                legacy.write_text('not executable')
                self.assertEqual(w.codex_binary(), str(modern.resolve()))

    def test_unknown_never_becomes_full_quota(self):
        for window in [None, {}, {'usedPercent': None}, {'usedPercent': 'bad'},
                       {'usedPercent': float('nan')}, {'usedPercent': True}]:
            self.assertIsNone(w.remaining(window))
        self.assertEqual(w.remaining({'usedPercent': 0}), 100)
        self.assertEqual(w.remaining({'usedPercent': 100}), 0)

    def test_multibucket_is_authoritative(self):
        result = {'rateLimits': {'primary': {'usedPercent': 2}},
                  'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 77}}}}
        self.assertEqual(w.remaining(w.main_window(result)), 23)

    def test_secondary_only_and_empty(self):
        result = {'rateLimits': {'secondary': {'usedPercent': 40}}}
        self.assertEqual(w.remaining(w.main_window(result)), 60)
        self.assertIsNone(w.remaining(w.main_window({})))

    def test_both_accounts_always_in_one_title(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(w.time, 'time', return_value=1000000):
            w.render([account('Personal', 77), account('Work', 29)])
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], '\033[38;5;208mP23% 2d21h\033[0m │ \033[38;5;28mW71% 2d21h\033[0m | ansi=true symbolize=false')
        self.assertIn('7-day: 23% remaining', output.getvalue())
        self.assertIn('7-day: 71% remaining', output.getvalue())

    def test_failure_does_not_hide_other_account(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(w.time, 'time', return_value=1000000):
            w.render([account('Personal', 0), account('Work', error='Offline')])
        self.assertEqual(output.getvalue().splitlines()[0], '\033[38;5;28mP100% 2d21h\033[0m │ \033[38;5;246mW? ?\033[0m | ansi=true symbolize=false')
        self.assertIn('Offline', output.getvalue())

    def test_recent_cache_is_shown_as_stale_not_live(self):
        with tempfile.TemporaryDirectory() as folder:
            w.CACHE = Path(folder) / 'cache.json'
            live = account('Personal', 50)
            with patch.object(w.time, 'time', return_value=1000000):
                w.save_cache([live])
                stale = w.apply_cache([account('Personal', error='Offline')])[0]
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    w.render([stale])
            self.assertEqual(stale['stale_seconds'], 0)
            self.assertIn('\033[38;5;120mP50% 2d21h ~1m\033[0m', output.getvalue())
            self.assertIn('Last identified 1m ago; not current', output.getvalue())

    def test_expired_cache_remains_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            w.CACHE = Path(folder) / 'cache.json'
            live = account('Personal', 50)
            with patch.object(w.time, 'time', return_value=1000000):
                w.save_cache([live])
            with patch.object(w.time, 'time', return_value=1000000 + w.MAX_STALE_SECONDS + 1):
                stale = w.apply_cache([account('Personal', error='Offline')])[0]
            self.assertNotIn('result', stale)

    def test_old_cached_reading_is_explicitly_historical(self):
        with tempfile.TemporaryDirectory() as folder:
            w.CACHE = Path(folder) / 'cache.json'
            with patch.object(w.time, 'time', return_value=1000000):
                w.save_cache([account('Work', 100)])
            # Work may be spending under another user, browser, or machine.
            with patch.object(w.time, 'time', return_value=1000000 + 17 * 3600):
                stale = w.apply_cache([account('Work', error='No app signed in')])[0]
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    w.render([stale])
            self.assertIn('result', stale)
            self.assertIn('W0%', output.getvalue())

    def test_unheld_account_shows_last_reading_with_age(self):
        with tempfile.TemporaryDirectory() as folder:
            w.CACHE = Path(folder) / 'cache.json'
            with patch.object(w.time, 'time', return_value=1000000):
                w.save_cache([account('Work', 73)])
                missing = account('Work', error='No monitored app signed in')
                stale = w.apply_cache([missing])[0]
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    w.render([stale])
            self.assertIn('W27%',
                          output.getvalue().splitlines()[0])

    def test_historical_reading_does_not_invent_post_reset_percentage(self):
        with tempfile.TemporaryDirectory() as folder:
            w.CACHE = Path(folder) / 'cache.json'
            live = account('Work', 100)
            w.main_window(live['result'])['resetsAt'] = 1000010
            with patch.object(w.time, 'time', return_value=1000000):
                w.save_cache([live])
            # Keep the identified percentage and say that its window reset.
            with patch.object(w.time, 'time', return_value=1000000 + 20 * 60):
                stale = w.apply_cache([account('Work', error='No app signed in')])[0]
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    w.render([stale])
            self.assertIn('\033[38;5;124mW0% reset ~20m\033[0m',
                          output.getvalue())
            self.assertIn('That quota window has since reset', output.getvalue())
            self.assertEqual(w.main_window(stale['result'])['resetsAt'], 1000010)

    def test_two_apps_on_one_account_read_it_once(self):
        both = 'shared@example.test'
        profiles = [dict(source='codex', path='/a', email=both, label='Personal',
                         short='P', app='Codex'),
                    dict(source='opencode', path='/b', email='work@example.test',
                         label='Work', short='W', app='OpenCode')]
        seen = []
        def one_source(profile):
            return {'auth': {'accessToken': 'x'}, 'email': both,
                    'subscription': 'sub-1', 'app': profile['app']}
        def counted(auth):
            seen.append(auth)
            return {'result': {'rateLimitsByLimitId': {'codex': {
                'primary': {'usedPercent': 10, 'windowDurationMins': 10080,
                            'resetsAt': 2000000}}}}}
        with tempfile.TemporaryDirectory() as folder:
            w.CACHE = Path(folder) / 'cache.json'
            output = io.StringIO()
            with contextlib.redirect_stdout(output), \
                 patch.object(w, 'load_profiles', return_value=profiles), \
                 patch.object(w, 'inspect', side_effect=one_source), \
                 patch.object(w, 'fetch', side_effect=counted), \
                 patch.object(w.time, 'time', return_value=1000000):
                w.main()
        self.assertEqual(len(seen), 1)
        self.assertIn('P90%', output.getvalue())
        self.assertIn('No app signed in to this account', output.getvalue())
        self.assertNotIn('shared@example.test', output.getvalue())

    def test_compact_reset_countdown(self):
        with patch.object(w.time, 'time', return_value=1000000):
            self.assertEqual(w.compact_reset(1000000 + 5 * 86400 + 18 * 3600), '5d18h')
            self.assertEqual(w.compact_reset(1000000 + 17 * 3600 + 42 * 60), '17h42m')
            self.assertEqual(w.compact_reset(1000000 + 38 * 60), '38m')
            self.assertEqual(w.compact_reset(1000000), 'reset')
            self.assertEqual(w.compact_reset(None), '?')

    def test_reading_saved_before_rekeying_is_adopted(self):
        with tempfile.TemporaryDirectory() as folder:
            w.CACHE = Path(folder) / 'cache.json'
            old = account('Work', 100)
            with patch.object(w.time, 'time', return_value=1000000):
                w.CACHE.write_text(json.dumps({'version': 1, 'accounts': {
                    w.legacy_key(old['profile']): {'savedAt': 1000000,
                                                   'result': old['result']}}}))
                stale = w.apply_cache([account('Work', error='No app signed in')])[0]
            self.assertEqual(w.remaining(w.main_window(stale['result'])), 0)
            self.assertEqual(stale['stale_seconds'], 0)

    def test_identity_and_expiry_checked_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'auth.json'
            p = dict(path=str(path), source='opencode', email='work@example.test')
            initial = json.dumps({'openai': {'access': token(p['email']), 'accountId': 'work'}})
            path.write_text(initial)
            self.assertEqual(w.credential(p)['chatgptAccountId'], 'work')
            self.assertEqual(path.read_text(), initial)
            with self.assertRaisesRegex(w.UsageError, 'different account'):
                w.credential(dict(p, email='personal@example.test'))
            path.write_text(json.dumps({'openai': {'access': token(p['email'], 1), 'accountId': 'work'}}))
            with self.assertRaisesRegex(w.UsageError, 'expired'):
                w.credential(p)

    def test_raw_errors_are_not_exposed(self):
        with patch.object(w, 'credential', side_effect=RuntimeError('SECRET')):
            result = w.read_account({'label': 'Work'})
        self.assertNotIn('SECRET', result['error'])

    def test_rpc_handles_batched_notifications(self):
        with tempfile.TemporaryDirectory() as home:
            binary = Path(home)/'fake-codex'
            binary.write_text('#!/usr/bin/env python3\nimport sys,json\nfor line in sys.stdin:\n m=json.loads(line)\n if "id" in m:\n  print(json.dumps({"method":"account/updated"})+"\\n"+json.dumps({"id":m["id"],"result":{"ok":True}}),flush=True)\n')
            binary.chmod(0o700)
            with patch.object(w, 'verify_binary'):
                server = w.AppServer(str(binary), home)
            try:
                self.assertEqual(server.request('read'), {'ok': True})
                self.assertEqual(server.request('read'), {'ok': True})
            finally:
                server.close()


if __name__ == '__main__':
    unittest.main()
