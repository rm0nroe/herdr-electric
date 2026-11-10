import io
import json
import os
import sys
import time
import unittest
import unittest.mock

import test_cli
import test_electric
import test_install
from herdr_electrified import __version__, electric

PROCESSES = electric.processes

# Answers `plugin list` / `plugin install` and `server stop` the way Herdr does, recording each call.
PLUGIN_STUB = '''elif 'plugin' in sys.argv or sys.argv[-2:] == ['server', 'stop']:
 import json
 home = Path(os.environ['HOME'])
 with (home / 'herdr-calls.txt').open('a') as log: log.write(' '.join(sys.argv[1:]) + '\\n')
 state = home / 'plugin.json'
 rest = sys.argv[sys.argv.index('plugin') + 1:] if 'plugin' in sys.argv else []
 if rest[:1] == ['list']:
  print(json.dumps({'id': 'cli:plugin', 'result': {'plugins': [json.loads(state.read_text())] if state.exists() else [], 'type': 'plugin_list'}}))
 elif rest[:1] == ['install']:
  state.write_text(json.dumps({'plugin_id': 'herdr-electrified', 'source': {'kind': 'github', 'owner': 'rm0nroe',
                   'repo': rest[1].split('/')[1], 'requested_ref': rest[rest.index('--ref') + 1]}}))
else: sys.exit(2)
'''


class TTY(io.StringIO):
    def isatty(self):
        return True


class Upgrade(unittest.TestCase):
    run_cli = test_cli.CLI.run_cli
    serve = test_install.Install.serve
    archive = test_install.Install.archive
    pin = test_install.Install.pin
    bundle = test_electric.Electric.bundle
    ghostty_host = test_electric.Ghostty.ghostty_host
    launch = test_electric.Ghostty.launch

    def setUp(self):
        test_electric.Electric.setUp(self)
        self.bin.write_text(self.bin.read_text().replace('else: sys.exit(2)\n', PLUGIN_STUB))
        self.bundles = self.root / '.local/share/herdr-electrified/bundles'
        self.notice = self.root / '.local/share/herdr-electrified/update-notice'
        self.ps('')

    def release(self, tag, extra=None):
        """Serve the latest-release answer and the pinned bundle."""
        data = self.archive()
        self.pin('BUNDLE_SHA', data)
        self.serve({electric.LATEST_URL: json.dumps({'tag_name': tag}).encode(), electric.BUNDLE_URL: data} | (extra or {}))

    def ps(self, output):
        patcher = unittest.mock.patch('herdr_electrified.electric.processes', return_value=output)
        patcher.start()
        self.addCleanup(patcher.stop)

    def old_bundle(self, version):
        old = self.bundles / f'herdr-electrified-{version}-macos-arm64'
        (old / 'codex/bin').mkdir(parents=True)
        (old / 'herdr').write_text('old')
        return old

    def calls(self):
        log = self.root / 'herdr-calls.txt'
        return log.read_text().splitlines() if log.exists() else []

    def uv(self, tool_dir):
        """A stub uv on PATH: records its argv and answers `uv tool dir`."""
        stubs = self.root / 'uv-stub'
        stubs.mkdir()
        (stubs / 'uv').write_text(f'#!/bin/sh\necho "$@" >> {self.root}/uv-calls.txt\n'
                                  f'[ "$1 $2" = "tool dir" ] && echo {tool_dir}\nexit 0\n')
        (stubs / 'uv').chmod(0o700)
        return {'PATH': f'{stubs}:{self.env["PATH"]}'}

    # Update notice

    def test_rate_limited_lookup_accepts_only_this_repos_stable_release_redirect(self):
        base = f'https://github.com/{electric.REPO}/releases/'
        for url, valid in [(base + 'tag/v2.3.4', True), (base + 'tag/v2.3.4-rc1', False),
                           ('https://github.com/another/project/releases/tag/v2.3.4', False)]:
            with self.subTest(url=url):
                response = unittest.mock.MagicMock()
                response.__enter__.return_value.geturl.return_value = url
                limit = electric.HTTPError(electric.LATEST_URL, 403, 'rate limited', {}, None)
                with unittest.mock.patch.object(electric, 'urlopen', side_effect=[limit, response]) as fetch:
                    if valid:
                        self.assertEqual(electric.latest_release(), 'v2.3.4')
                    else:
                        with self.assertRaises(ValueError):
                            electric.latest_release()
                    self.assertEqual(fetch.call_args.args[0].full_url, base + 'latest')

    def test_check_records_a_notice_only_when_a_newer_release_exists(self):
        self.release('v99.0.0')
        code, result = self.run_cli('upgrade', '--check')
        self.assertEqual(code, 0, result)
        self.assertEqual((result['current'], result['latest']), (__version__, '99.0.0'))
        self.assertIn('herdr-electrified 99.0.0 is available', self.notice.read_text())
        self.assertIn('herdr-electrified upgrade', self.notice.read_text())
        self.release('v' + __version__)
        code, result = self.run_cli('upgrade', '--check')
        self.assertEqual((code, result['latest']), (0, __version__), result)
        self.assertEqual(self.notice.read_text(), '')

    def test_failed_check_keeps_the_notice_and_waits_a_day_to_retry(self):
        self.notice.parent.mkdir(parents=True)
        self.notice.write_text('herdr-electrified 99.0.0 is available\n')
        os.utime(self.notice, (time.time() - 3 * 86400,) * 2)
        self.serve({})
        code, result = self.run_cli('upgrade', '--check')
        self.assertEqual(code, 1, result)
        self.assertIn('error', result)
        self.assertEqual(self.notice.read_text(), 'herdr-electrified 99.0.0 is available\n')
        self.assertGreater(self.notice.stat().st_mtime, time.time() - 60)

    def test_bare_launcher_shows_the_notice_and_refreshes_it_daily_in_the_background(self):
        self.release('v' + __version__)
        env = self.ghostty_host()
        self.assertEqual(self.run_cli('install', '--yes', env=env)[0], 0)
        stubs = self.root / 'cli-stub'
        stubs.mkdir()
        checked = self.root / 'checked.txt'
        (stubs / 'herdr-electrified').write_text(f'#!/bin/sh\necho "$@" > {checked}\n')
        (stubs / 'herdr-electrified').chmod(0o700)
        path = {'PATH': f'{stubs}:/usr/bin:/bin'}
        self.notice.write_text('herdr-electrified 99.0.0 is available\n')

        def launched(*args, env=None):
            checked.unlink(missing_ok=True)
            run, _ = self.launch(*args, env=path | (env or {}))
            for _ in range(50):  # the check runs in the background
                if checked.exists():
                    break
                time.sleep(0.05)
            return run.stdout, checked.read_text().strip() if checked.exists() else None

        self.assertEqual(launched(), ('herdr-electrified 99.0.0 is available\n', None))  # fresh: no check
        os.utime(self.notice, (time.time() - 2 * 86400,) * 2)
        self.assertEqual(launched(), ('herdr-electrified 99.0.0 is available\n', 'upgrade --check'))
        os.utime(self.notice, (time.time() - 2 * 86400,) * 2)
        self.assertEqual(launched(env={'HERDR_ELECTRIFIED_NO_UPDATE_CHECK': '1'}), ('', None))
        self.assertEqual(launched(env={'HERDR_ELECTRIFIED_WINDOW': '1'})[1], None)  # inside its window: herdr runs
        self.assertEqual(launched('server', 'stop')[1], None)

    # Old bundles

    def test_install_removes_old_bundles_no_process_uses(self):
        self.release('v' + __version__)
        unused, busy = self.old_bundle('0.8.0'), self.old_bundle('0.9.0')
        self.ps(f'{busy}/herdr server\n/usr/bin/login\n')
        code, result = self.run_cli('install', '--yes')
        self.assertEqual(code, 0, result)
        self.assertEqual(sorted(p.name for p in self.bundles.iterdir()), sorted([electric.BUNDLE, busy.name]))
        self.ps('')
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        self.assertEqual([p.name for p in self.bundles.iterdir()], [electric.BUNDLE])

    def test_daily_check_removes_old_bundles_once_unused(self):
        # After a restart nothing runs from the old bundle, so the launcher's next daily check finishes the cleanup.
        self.release('v' + __version__)
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        old = self.old_bundle('0.9.0')
        code, result = self.run_cli('upgrade', '--check')
        self.assertEqual((code, result['removed']), (0, [old.name]), result)
        self.assertFalse(old.exists())

    def test_failed_process_listing_keeps_every_old_bundle(self):
        self.release('v' + __version__)
        old = self.old_bundle('0.9.0')
        self.ps(None)  # ps failed: nothing is known to be unused
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        self.assertTrue(old.exists())

    def test_processes_reports_a_failed_ps_as_unknown(self):
        failed = unittest.mock.Mock(returncode=1, stdout='')
        with unittest.mock.patch('herdr_electrified.electric.subprocess.run', return_value=failed):
            self.assertIsNone(PROCESSES())  # the real function; setUp stubs electric.processes

    def test_undo_and_settings_only_leave_bundles_alone(self):
        self.release('v' + __version__)
        old = self.old_bundle('0.9.0')
        self.assertEqual(self.run_cli('install', '--settings-only', '--yes', env={'PATH': f'{self.root}:/usr/bin:/bin'})[0], 0)
        self.assertTrue(old.exists())
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        self.assertFalse(old.exists())
        self.old_bundle('0.9.0')
        self.assertEqual(self.run_cli('undo')[0], 0)
        self.assertTrue(old.exists())

    # Upgrade

    def test_upgrade_reinstalls_the_cli_at_the_new_tag_then_hands_off(self):
        self.release('v99.0.0')
        env = self.uv(sys.prefix.rsplit('/', 1)[0])
        with unittest.mock.patch('herdr_electrified.cli.os.execv', side_effect=SystemExit(0)) as execv, self.assertRaises(SystemExit):
            self.run_cli('upgrade', '--yes', env=env)
        self.assertEqual((self.root / 'uv-calls.txt').read_text().splitlines(),
                         ['tool dir', 'tool install --python 3.12 git+https://github.com/rm0nroe/herdr-electric@v99.0.0'])
        # The new version finishes the job, so its own install, plugin and cleanup logic runs.
        execv.assert_called_once_with(sys.executable, [sys.executable, '-m', 'herdr_electrified.cli', 'upgrade', '--yes', '--json'])

    def test_upgrade_refuses_a_copy_uv_does_not_manage(self):
        self.release('v99.0.0')
        env = self.uv(str(self.root / 'elsewhere'))
        with unittest.mock.patch('herdr_electrified.cli.os.execv') as execv:
            code, result = self.run_cli('upgrade', '--yes', env=env)
        self.assertEqual(code, 1, result)
        self.assertIn('not installed with uv tool', result['error'])
        self.assertEqual((self.root / 'uv-calls.txt').read_text().splitlines(), ['tool dir'])
        execv.assert_not_called()

    def test_homebrew_upgrade_names_both_commands_without_running_uv(self):
        self.release('v99.0.0')
        keg = self.root / 'Cellar/herdr-electrified/1.10.0'
        prefix = keg / 'libexec'
        prefix.mkdir(parents=True)
        (keg / 'INSTALL_RECEIPT.json').write_text('{}')
        env = self.uv(str(self.root))
        with unittest.mock.patch.object(sys, 'prefix', str(prefix)):
            code, result = self.run_cli('upgrade', '--yes', env=env)
        self.assertEqual(code, 1, result)
        self.assertIn('brew upgrade rm0nroe/tap/herdr-electrified, then herdr-electrified upgrade', result['error'])
        self.assertFalse((self.root / 'uv-calls.txt').exists())

    def test_upgrade_without_uv_names_the_command_to_run(self):
        self.release('v99.0.0')
        code, result = self.run_cli('upgrade', '--yes')
        self.assertEqual(code, 1, result)
        self.assertIn('uv tool install --python 3.12 git+https://github.com/rm0nroe/herdr-electric@v99.0.0', result['error'])

    def test_current_upgrade_reapplies_updates_the_plugin_and_cleans_up(self):
        self.release('v' + __version__)
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        old = self.old_bundle('0.9.0')
        (self.root / 'plugin.json').write_text(json.dumps({'plugin_id': 'herdr-electrified', 'source': {
            'kind': 'github', 'owner': 'rm0nroe', 'repo': 'herdr-electrified', 'requested_ref': 'v1.0.0'}}))
        self.notice.write_text('herdr-electrified 1.0.1 is available\n')
        code, result = self.run_cli('upgrade', '--yes')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['latest'], __version__)
        # Electric's own config is reapplied once, by the Electric install, never as a settings-only target.
        self.assertEqual([install['bundle'] for install in result['installs']], ['0.1.0'])
        self.assertFalse(old.exists())
        self.assertEqual(result['removed'], [old.name])
        self.assertIn(f'plugin install rm0nroe/herdr-electric/plugin --ref v{__version__} --yes', self.calls()[-1])
        self.assertEqual(result['plugin'], f'v{__version__}')
        self.assertEqual(self.notice.read_text(), '')
        # Nothing left to do: a second run changes nothing and reinstalls no plugin.
        calls = len(self.calls())
        code, result = self.run_cli('upgrade', '--yes')
        self.assertEqual((code, result['plugin'], result['removed']), (0, None, []), result)
        self.assertEqual(len(self.calls()), calls + 1)  # only the plugin list

    def test_upgrade_updates_a_plugin_from_the_canonical_repository(self):
        self.release('v' + __version__)
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        (self.root / 'plugin.json').write_text(json.dumps({'source': {
            'kind': 'github', 'owner': 'rm0nroe', 'repo': 'herdr-electric', 'requested_ref': 'v1.9.1'}}))
        self.assertEqual(self.run_cli('upgrade', '--yes')[1]['plugin'], f'v{__version__}')

    def test_plugin_linked_locally_or_absent_is_left_alone(self):
        self.release('v' + __version__)
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        self.assertEqual(self.run_cli('upgrade', '--yes')[1]['plugin'], None)
        (self.root / 'plugin.json').write_text(json.dumps({'plugin_id': 'herdr-electrified', 'source': {'kind': 'local', 'path': '/dev/plugin'}}))
        self.assertEqual(self.run_cli('upgrade', '--yes')[1]['plugin'], None)
        self.assertFalse(any('plugin install' in call for call in self.calls()))

    def test_settings_only_upgrade_reapplies_its_own_config_with_its_pinned_herdr(self):
        self.release('v' + __version__)
        self.assertEqual(self.run_cli('apply', '--herdr-bin', str(self.bin), '--yes')[0], 0)
        elsewhere = self.root / 'elsewhere.toml'
        code, result = self.run_cli('upgrade', '--yes', env={'HERDR_CONFIG_PATH': str(elsewhere)})
        self.assertEqual(code, 0, result)
        self.assertEqual([row['path'] for install in result['installs'] for row in install['targets']], [str(self.target.resolve())])
        self.assertFalse(elsewhere.exists())
        self.assertFalse(self.bundles.exists())  # settings-only never downloads the bundle

    def test_upgrade_reapplies_settings_only_beside_electric(self):
        self.release('v' + __version__)
        self.assertEqual(self.run_cli('install', '--settings-only', '--yes', env={'PATH': f'{self.root}:/usr/bin:/bin'})[0], 0)
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        code, result = self.run_cli('upgrade', '--yes')
        self.assertEqual(code, 0, result)
        self.assertEqual([install.get('bundle') for install in result['installs']], ['0.1.0', None])

    def test_declined_install_stops_the_upgrade(self):
        from herdr_electrified import config as c
        self.release('v' + __version__)
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        # An older release's launcher, recorded as ours: the new install has a change to offer.
        launcher = str((self.root / '.local/bin/herdr-electric').resolve())
        receipt = self.root / 'state/herdr-electrified/receipt.json'
        data = json.loads(receipt.read_text())
        entry = data['targets'][launcher]
        entry['owned']['$file']['installed'] = '#!/bin/sh\nexit 0\n'
        entry['installed_hash'] = c.digest(entry['owned']['$file']['installed'])
        receipt.write_text(json.dumps(data))
        os.chmod(launcher, 0o700)
        (self.root / '.local/bin/herdr-electric').write_text('#!/bin/sh\nexit 0\n')
        plugin = json.dumps({'plugin_id': 'herdr-electrified', 'source': {
            'kind': 'github', 'owner': 'rm0nroe', 'repo': 'herdr-electrified', 'requested_ref': 'v1.0.0'}})
        (self.root / 'plugin.json').write_text(plugin)
        code, result = self.run_cli('upgrade', stdin=TTY('n\n'))
        self.assertEqual(code, 0, result)
        self.assertIn('declined', result['notice'])
        self.assertEqual((self.root / 'plugin.json').read_text(), plugin)
        self.assertEqual(self.calls(), [])

    def test_interrupted_write_during_upgrade_says_to_check(self):
        import contextlib
        from herdr_electrified.cli import main
        self.release('v' + __version__)
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)

        def interrupted(args):
            args.writing = True
            raise KeyboardInterrupt
        err = io.StringIO()
        with unittest.mock.patch('herdr_electrified.cli.execute', interrupted), unittest.mock.patch.dict(os.environ, self.env, clear=True), \
                unittest.mock.patch('sys.stderr', err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['upgrade', '--yes']), 130)
        self.assertIn('Interrupted; run herdr-electrified check', err.getvalue())

    def test_nothing_installed_upgrade_says_how_to_install(self):
        self.release('v' + __version__)
        code, result = self.run_cli('upgrade', '--yes')
        self.assertEqual(code, 0, result)
        self.assertIn('herdr-electrified install', result['notice'])
        self.assertFalse(self.bundles.exists())

    # Live handoff

    def test_upgrade_hands_off_from_inside_a_pane_and_prunes_only_after_success(self):
        self.release('v' + __version__)
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        old = self.old_bundle('0.9.0')
        busy = self.old_bundle('0.10.0')
        self.ps(f'{old}/herdr server\n{busy}/codex/bin/codex\n')

        def transfer(session, launcher, current, previous):
            self.assertTrue(old.exists())
            self.assertEqual(session, self.root / 'config/herdr/sessions/herdr-electrified')
            self.assertEqual(current, (self.bundles / electric.BUNDLE).resolve())
            self.ps(f'{current}/herdr server\n{busy}/codex/bin/codex\n')
            return True

        with unittest.mock.patch.object(electric, 'live_handoff', side_effect=transfer):
            code, result = self.run_cli('upgrade', '--yes', env={'HERDR_ENV': '1'})
        self.assertEqual(code, 0, result)
        self.assertIn('panes are still running', result['handoff'])
        self.assertEqual(result['removed'], [old.name])
        self.assertTrue(busy.exists())

    def test_failed_handoff_never_prompts_to_stop_or_prunes_the_live_bundle(self):
        self.release('v' + __version__)
        self.assertEqual(self.run_cli('install', '--yes')[0], 0)
        old = self.old_bundle('0.9.0')
        self.ps(f'{old}/herdr server\n')
        with unittest.mock.patch.object(electric, 'live_handoff', side_effect=TimeoutError('timed out')):
            code, result = self.run_cli('upgrade', stdin=TTY('yes\nyes\n'))
        self.assertEqual(code, 1, result)
        self.assertIn('live handoff not confirmed: timed out', result['error'])
        self.assertFalse(any(call.endswith('server stop') for call in self.calls()))
        self.assertTrue(old.exists())

    def test_handoff_checks_the_socket_owner_before_requesting_a_transfer(self):
        from unittest.mock import patch
        current = (self.bundles / electric.BUNDLE).resolve()
        previous = self.old_bundle('0.10.0')
        (self.root / 'herdr.sock').touch()
        with patch.object(electric.socket, 'socket'), patch.object(electric, '_peer_binary') as peer, \
                patch.object(electric.subprocess, 'check_output') as status:
            peer.return_value = current / 'herdr'
            self.assertFalse(electric.live_handoff(self.root, self.bin, current, previous))
            peer.return_value = self.bin
            with self.assertRaisesRegex(ValueError, 'not served by an Electric bundle'):
                electric.live_handoff(self.root, self.bin, current, previous)
            status.assert_not_called()
            peer.return_value = (previous / 'herdr').resolve()
            server = {'session': 'herdr-electrified', 'socket': str(self.root / 'herdr.sock'), 'capabilities': {}}
            status.return_value = json.dumps(server)
            with self.assertRaisesRegex(ValueError, 'does not support'):
                electric.live_handoff(self.root, self.bin, current, previous)
            status.return_value = json.dumps(dict(server, socket='/other/session'))
            with self.assertRaisesRegex(ValueError, 'different session'):
                electric.live_handoff(self.root, self.bin, current, previous)

    def test_server_rejection_is_not_retried_or_reported_as_success(self):
        from unittest.mock import patch
        current = (self.bundles / electric.BUNDLE).resolve()
        previous = self.old_bundle('0.10.0').resolve()
        endpoint = self.root / 'herdr.sock'
        endpoint.touch()
        statuses = [dict(socket=str(endpoint), capabilities={'live_handoff': True}),
                    dict(binary=str(current / 'herdr'), version='0.9.5', protocol=22)]
        with patch.object(electric.socket, 'socket') as socket, patch.object(electric, '_peer_binary', return_value=previous / 'herdr'), \
                patch.object(electric.subprocess, 'check_output', side_effect=[
                    json.dumps(dict(status, session='herdr-electrified')) for status in statuses]):
            sock = socket.return_value.__enter__.return_value
            sock.makefile.return_value = io.BytesIO(b'{"id":"electric:upgrade","error":{"message":"import failed"}}\n')
            with self.assertRaisesRegex(ValueError, 'import failed'):
                electric.live_handoff(self.root, self.bin, current, previous)
            sock.sendall.assert_called_once()
            request = json.loads(sock.sendall.call_args.args[0])
            self.assertEqual(request['method'], 'server.live_handoff')
            self.assertEqual(request['params'], dict(import_exe=str(current / 'herdr'), expected_version='0.9.5', expected_protocol=22))

    def test_upgrade_rejects_install_options(self):
        with self.assertRaises(SystemExit), unittest.mock.patch('sys.stderr', io.StringIO()):
            self.run_cli('upgrade', '--settings-only')
        with self.assertRaises(SystemExit), unittest.mock.patch('sys.stderr', io.StringIO()):
            self.run_cli('check', '--check')


if __name__ == '__main__':
    unittest.main()
