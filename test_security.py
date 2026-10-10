"""Security regressions using temporary files and a synthetic helper."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('widget', Path(__file__).with_name('codex_usage.1m.py'))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class SecurityTests(unittest.TestCase):
    def test_symlink_input_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'target.json'
            target.write_text('{"private": "fixture"}')
            link = Path(folder) / 'auth.json'
            link.symlink_to(target)
            with self.assertRaises(OSError):
                w.read_json(link)
            self.assertEqual(target.read_text(), '{"private": "fixture"}')

    def test_writable_by_others_and_oversized_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.json'
            path.write_text('{}')
            for mode in (0o620, 0o602):
                path.chmod(mode)
                with self.assertRaises(OSError):
                    w.read_json(path)
            path.chmod(0o600)
            path.write_bytes(b' ' * (w.MAX_JSON_BYTES + 1))
            with self.assertRaises(OSError):
                w.read_json(path)

    def test_nonregular_and_other_owner_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            fifo = Path(folder) / 'fifo'
            os.mkfifo(fifo)
            with self.assertRaises(OSError):
                w.read_json(fifo)
            path = Path(folder) / 'input.json'
            path.write_text('{}')
            info = path.stat()
            wrong_owner = Mock(st_mode=info.st_mode, st_uid=os.getuid() + 1)
            with patch.object(w.os, 'fstat', return_value=wrong_owner):
                with self.assertRaises(OSError):
                    w.read_json(path)

    def test_state_directory_is_private_and_symlink_target_is_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'state'
            target.mkdir(mode=0o755)
            link = Path(folder) / 'link'
            link.symlink_to(target, target_is_directory=True)
            original_mode = target.stat().st_mode
            with self.assertRaises(OSError):
                w.private_directory(link)
            self.assertEqual(target.stat().st_mode, original_mode)
            w.private_directory(target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_cache_replaces_symlink_without_writing_its_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'target.json'
            target.write_text('{"private": "fixture"}')
            state = root / 'state'
            state.mkdir()
            cache = state / 'cache.json'
            cache.symlink_to(target)
            with patch.object(w, 'CACHE', cache):
                self.assertEqual(w.load_cache(), {})
                w.save_cache([])
            self.assertEqual(target.read_text(), '{"private": "fixture"}')
            self.assertFalse(cache.is_symlink())
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)

    def test_dynamic_text_cannot_inject_menu_parameters_or_controls(self):
        malicious = 'Name\n| bash=/bin/sh\x1b[31m\u202ehidden'
        cleaned = w.clean(malicious)
        for control in ('|', '\n', '\x1b', '\u202e'):
            self.assertNotIn(control, cleaned)

    def test_emails_hidden_by_default_and_opt_in_is_explicit(self):
        profile = dict(label='Example', short='E', email='user@example.test', app='Demo')
        for show in (False, True):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                w.render([{'profile': dict(profile, showEmail=show)}])
            self.assertEqual('user@example.test' in output.getvalue(), show)
        missing = w.unheld(profile, [{'email': 'other@example.test'}])
        self.assertNotIn('other@example.test', missing)

    def test_buffered_notifications_cannot_bypass_deadline(self):
        server = w.AppServer.__new__(w.AppServer)
        server.send = Mock()
        server.request_id = 0
        server.deadline = 1
        server.buffer = b'{"method":"notification"}\n' * 100
        with patch.object(w.time, 'monotonic', return_value=2):
            with self.assertRaisesRegex(w.UsageError, 'timed out'):
                server.request('read')

    def test_helper_output_is_bounded(self):
        with tempfile.TemporaryDirectory() as home:
            binary = Path(home) / 'fake-codex'
            binary.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.readline()\n'
                              f'sys.stdout.write("x" * {w.MAX_JSON_BYTES + 1})\n'
                              'sys.stdout.flush()\nsys.stdin.read()\n')
            binary.chmod(0o700)
            with patch.object(w, 'verify_binary'):
                server = w.AppServer(str(binary), home)
            try:
                with self.assertRaisesRegex(w.UsageError, 'too large'):
                    server.request('read')
            finally:
                server.close()


if __name__ == '__main__':
    unittest.main()
