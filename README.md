# Codex Usage for SwiftBar

Remaining Codex quota and reset times in your macOS menu bar, for one or more accounts.

```text
P23% 5d18h │ W25% 4d18h ~1d
```

`W25% 4d18h ~1d` means 25% remained at the last successful check, the saved quota window resets in 4 days 18 hours, and **the reading is about a day old**. It may differ from current usage.

- `~age` appears when a live check fails and a saved reading is available. It measures reading age, not reset time.
- `?` means unavailable. `reset` means the saved window has passed; the old percentage stays until verified again.
- Colours run from green to red as quota decreases. The dropdown shows both quota windows, extra model limits and exact reset times.

Refreshes every minute and on opening. Historical readings expire after seven days. This displays subscription quota, not token totals or API spending.

## Install

You need macOS, [SwiftBar](https://github.com/swiftbar/SwiftBar), Python 3.9+ available to SwiftBar as `python3`, and the official Codex or ChatGPT desktop app in `/Applications` or `~/Applications`. Sign into Codex locally and choose a plugin folder in SwiftBar.

```sh
git clone https://github.com/sam-holmes2/codex-usage-swiftbar.git
cd codex-usage-swiftbar
python3 install.py
```

The installer uses SwiftBar's configured folder, falling back to `~/Documents/swiftbar`. Override with `--plugin-dir ~/path/to/plugins`. It creates a single `C` account from `~/.codex/auth.json`; reinstalling preserves your mapping and backs up the previous plugin outside the watched folder. Remove any older meter files from that folder to avoid duplicates.

## Accounts

For a custom first install, copy `codex-accounts.example.json` to `codex-accounts.json`, replace the example emails and paths, then:

```sh
python3 install.py --accounts ./codex-accounts.json
```

Edit an existing setup at `~/Library/Application Support/codex-usage-swiftbar/codex-accounts.json`. Each account needs `source` (`codex` or `opencode`), `path`, `email`, `label`, `short` and `app`. Paths accept `~`; emails must be distinct. Only configured sources are read, and readings follow the pinned account rather than the app name.

Emails are hidden in the menu by default. Add `"showEmail": true` to an account to display its email. The usage website link uses your browser's signed-in account.

## Security and troubleshooting

Access tokens go through stdin to a temporary app-server process whose OpenAI signature is checked by macOS. The plugin reads quota without model calls, chat-history access or token renewal. It rejects unverifiable account attribution, symlinked inputs, files owned by or writable by other users, and oversized responses. Its own state directory and files use owner-only permissions. The cache holds display fields and hashed email keys, never tokens; keep it private.

If `~age` grows or `?` persists, open the source app to renew its sign-in, check your configured email/path, and refresh. Host apps own renewal. This independent plugin uses [experimental Codex app-server authentication](https://learn.chatgpt.com/docs/app-server); host credential formats can change. It cannot protect credentials from other software running as your macOS user.

```sh
python3 -m unittest discover -p 'test_*.py'
python3 demo.py
```

Tests and demo use fictional data, without real sign-ins or network calls. Review screenshots and bug reports before sharing, especially if you enabled email display.

MIT licensed. Unaffiliated with OpenAI.
