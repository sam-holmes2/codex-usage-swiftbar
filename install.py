#!/usr/bin/env python3
"""Install into SwiftBar without changing host-app credentials."""
import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'codex_usage.1m.py'
spec = importlib.util.spec_from_file_location('widget', SOURCE)
widget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(widget)


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate(path):
    previous = widget.CONFIG
    try:
        widget.CONFIG = path
        return widget.load_profiles()
    finally:
        widget.CONFIG = previous


def plugin_directory():
    override = os.environ.get('SWIFTBAR_PLUGIN_DIR')
    if override:
        return Path(override).expanduser()
    try:
        result = subprocess.run(
            ['/usr/bin/defaults', 'read', 'com.ameba.SwiftBar', 'PluginDirectory'],
            capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).expanduser()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path.home() / 'Documents/swiftbar'


def install(plugin_dir, accounts=None):
    config = widget.CONFIG
    if accounts:
        if config.exists():
            raise widget.UsageError('Configuration already exists; edit it locally to change accounts')
        validate(accounts)
        data = accounts.read_bytes()
    elif config.exists():
        validate(config)
        data = None
    else:
        # Only the default personal Codex profile is read on first setup.
        # Other sources are explicitly opted into through an accounts file.
        profile = dict(source='codex', path=str(Path.home() / '.codex/auth.json'),
                       label='Codex', short='C', app='Codex')
        profile['email'] = widget.inspect(profile)['email']
        data = (json.dumps({'accounts': [profile]}, indent=2) + '\n').encode()
    if data is not None:
        atomic_write(config, data, 0o600)
    else:
        config.chmod(0o600)

    plugin = plugin_dir / SOURCE.name
    if plugin.exists() and plugin.read_bytes() != SOURCE.read_bytes():
        # Backups must be outside the folder SwiftBar recursively executes.
        backup_dir = config.parent / 'backups'
        watched = plugin_dir.resolve()
        if backup_dir.resolve() == watched or watched in backup_dir.resolve().parents:
            raise widget.UsageError('Plugin folder must be separate from the private configuration folder')
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        shutil.copy2(plugin, backup_dir / (plugin.name + '.' + stamp + '.disabled'))
    atomic_write(plugin, SOURCE.read_bytes(), 0o755)
    return plugin, config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-dir', type=Path, help='SwiftBar plugin folder')
    parser.add_argument('--accounts', type=Path, help='Explicit account configuration to use on first install')
    args = parser.parse_args()
    try:
        plugin, config = install((args.plugin_dir or plugin_directory()).expanduser(),
                                 args.accounts.expanduser() if args.accounts else None)
    except (widget.UsageError, OSError):
        raise SystemExit('Installation stopped. Check the local account configuration, '
                         'folder permissions, and source-app sign-in.') from None
    print('Installed:', plugin)
    print('Private configuration:', config)


if __name__ == '__main__':
    main()
