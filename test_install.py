"""Exercise installation only inside temporary directories."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('installer', Path(__file__).with_name('install.py'))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def profile(label='Example', short='E', email='user@example.test'):
    return dict(source='codex', path='~/.codex/auth.json', label=label,
                short=short, app='Codex', email=email)


class InstallationTests(unittest.TestCase):
    def test_custom_single_account_and_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'accounts.json'
            path.write_text(json.dumps({'accounts': [profile('My account', 'M')]}))
            profiles = installer.validate(path)
            self.assertEqual(len(profiles), 1)
            with patch.object(installer.widget, 'inspect') as inspect:
                with patch.object(installer.widget, 'CONFIG', Path(folder) / 'state/accounts.json'):
                    plugin, config = installer.install(Path(folder) / 'plugins', path)
            inspect.assert_not_called()
            self.assertEqual(plugin.read_bytes(), installer.SOURCE.read_bytes())
            self.assertEqual(json.loads(config.read_text()), json.loads(path.read_text()))
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(plugin.stat().st_mode & 0o777, 0o755)
            with patch.object(installer.widget.time, 'time', return_value=1000):
                import contextlib
                import io
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    installer.widget.render([{'profile': profiles[0]}])
            self.assertIn('M = My account', output.getvalue())
            self.assertNotIn('W = Work', output.getvalue())

    def test_multiple_accounts_and_duplicate_email_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'accounts.json'
            entries = [profile(str(i), str(i), f'user{i}@example.test') for i in range(3)]
            path.write_text(json.dumps({'accounts': entries}))
            self.assertEqual(len(installer.validate(path)), 3)
            entries[2]['email'] = entries[0]['email'].upper()
            path.write_text(json.dumps({'accounts': entries}))
            with self.assertRaises(installer.widget.UsageError):
                installer.validate(path)

    def test_reinstall_preserves_configuration_and_unrelated_plugins(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'state/accounts.json'
            config.parent.mkdir()
            data = json.dumps({'accounts': [profile()]}).encode()
            config.write_bytes(data)
            watched = root / 'plugins'
            watched.mkdir()
            plugin = watched / installer.SOURCE.name
            plugin.write_text('previous plugin')
            other = watched / 'unrelated.1m.py'
            other.write_text('unrelated')
            with patch.object(installer.widget, 'CONFIG', config), \
                 patch.object(installer.widget, 'inspect') as inspect:
                installer.install(watched)
                installer.install(watched)
            inspect.assert_not_called()
            self.assertEqual(json.loads(config.read_text()), json.loads(data))
            self.assertEqual(other.read_text(), 'unrelated')
            backups = list((config.parent / 'backups').iterdir())
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'previous plugin')
            self.assertTrue(backups[0].name.endswith('.disabled'))
            self.assertEqual(set(p.name for p in watched.iterdir()), {plugin.name, other.name})

    def test_invalid_configuration_never_replaces_plugin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            invalid = root / 'invalid.json'
            invalid.write_text('{"accounts": []}')
            watched = root / 'plugins'
            watched.mkdir()
            plugin = watched / installer.SOURCE.name
            plugin.write_text('previous plugin')
            config = root / 'state/accounts.json'
            with patch.object(installer.widget, 'CONFIG', config):
                with self.assertRaises(installer.widget.UsageError):
                    installer.install(watched, invalid)
            self.assertEqual(plugin.read_text(), 'previous plugin')
            self.assertFalse(config.exists())

    def test_first_install_discovers_only_default_codex_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(installer.widget, 'CONFIG', root / 'state/accounts.json'), \
                 patch.object(installer.Path, 'home', return_value=root), \
                 patch.object(installer.widget, 'inspect', return_value={
                     'email': 'user@example.test'}) as inspect:
                _, config = installer.install(root / 'plugins')
            self.assertEqual(inspect.call_count, 1)
            self.assertEqual(inspect.call_args.args[0]['source'], 'codex')
            self.assertEqual(inspect.call_args.args[0]['path'], str(root / '.codex/auth.json'))
            self.assertEqual(json.loads(config.read_text())['accounts'][0]['email'],
                             'user@example.test')


if __name__ == '__main__':
    unittest.main()
