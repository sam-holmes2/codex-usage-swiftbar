#!/usr/bin/env python3
"""Render fictional example output without credentials, cache writes or network."""
import importlib.util
from pathlib import Path
import time

spec = importlib.util.spec_from_file_location('widget', Path(__file__).with_name('codex_usage.1m.py'))
widget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(widget)

accounts = []
for label, short, email, used, reset in [
    ('Personal', 'P', 'personal@example.test', 77, 5 * 86400 + 18 * 3600),
    ('Second account', 'S', 'second@example.test', 29, 86400 + 17 * 3600),
]:
    accounts.append({'profile': {'label': label, 'short': short, 'email': email, 'app': 'Demo'},
                     'result': {'rateLimitsByLimitId': {'codex': {'primary': {
                         'usedPercent': used, 'windowDurationMins': 10080,
                         'resetsAt': time.time() + reset + 1}}}}})

if __name__ == '__main__':
    widget.render(accounts)
