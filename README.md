# Codex Usage for SwiftBar

Keep your remaining Codex quota and reset times in the macOS menu bar. Display one account or several together, with independent colours and clear labels for older readings.

```text
P23% 5d18h │ S71% 1d17h
```

This is an independent SwiftBar plugin, unaffiliated with OpenAI. It displays subscription quota, rather than token consumption or API spending.

## Requirements

- macOS, [SwiftBar](https://github.com/swiftbar/SwiftBar), and Python 3.9 or later available as `python3` to SwiftBar.
- The official Codex or ChatGPT desktop app installed in `/Applications` or `~/Applications`, with its bundled Codex executable.
- A ChatGPT subscription signed into Codex locally. OpenCode sign-ins are optional, configured explicitly.

The plugin uses experimental external-token login in the [official Codex app-server protocol](https://learn.chatgpt.com/docs/app-server). Host-app credential formats and bundled executable layouts can change. It stops a usage check if it cannot verify the executable or account attribution.

## Install

Choose a plugin folder in SwiftBar, then:

```sh
git clone https://github.com/sam-holmes2/codex-usage-swiftbar.git
cd codex-usage-swiftbar
python3 install.py
```

The installer discovers SwiftBar's configured folder, falling back to `~/Documents/swiftbar`. Use `python3 install.py --plugin-dir ~/path/to/plugins` to choose another. Make sure SwiftBar watches that folder and can run `python3`; configure launch at login in SwiftBar itself.

On first install, the default setup pins the current email from `~/.codex/auth.json` as a single `C` account. Reinstallation preserves the private mapping. Existing copies of `codex_usage.1m.py` are backed up outside the watched folder before replacement. If migrating from an older meter, move its other scheduled plugin files outside SwiftBar's watched folder to avoid duplicate menu items.

## Multiple accounts and custom labels

For a custom first install, copy `codex-accounts.example.json` to a local `codex-accounts.json`, replace the fictional emails and paths, then run:

```sh
python3 install.py --accounts ./codex-accounts.json
```

To change an existing setup, edit `~/Library/Application Support/codex-usage-swiftbar/codex-accounts.json`, then refresh SwiftBar. Each entry needs `source` (`codex` or `opencode`), `path`, `email`, `label`, `short`, and `app`. Configure one or more distinct emails. Paths accept `~`. Names and short labels are yours to choose; the dropdown legend follows them.

Only explicitly configured sources are inspected. For another subscription, use an existing sign-in you are authorised to access. The plugin does not create a second login, switch accounts, or renew host-app credentials. The usage website link opens whichever account your browser currently has selected.

## Reading the meter

- Percentage means **remaining** quota, calculated as `100 - usedPercent` for the primary Codex window, falling back to the secondary window. The dropdown shows both windows and any additional model buckets.
- The time after the percentage is the reset countdown. The dropdown includes the exact local reset time.
- Green means plenty remains, then yellow, orange, and red as quota decreases. Grey means unavailable.
- `~3m` means the reading is historical and was identified three minutes ago. Older readings can be retained for up to seven days. A passed reset says `reset` while preserving the old percentage until a new verified reading arrives.
- `?` means unavailable. A missing reading never becomes an invented 100%.

Refreshes every minute and when opening the menu. Each live lookup retries once after a temporary failure. Two configured sources signed into the same subscription share one lookup. Slots follow the pinned email, so an app signing into another account cannot silently relabel its quota.

## Privacy and credentials

The public repository contains code and fictional examples. Real emails, account mappings, readings and credentials stay local. Emails appear in your local dropdown, so check them before sharing a screenshot or plugin output.

The plugin reads the configured host-app access token into memory and passes it through stdin to an isolated official app-server process. macOS must verify the executable's OpenAI signing identity before it receives tokens. The helper uses a minimal environment without inherited API keys, proxy overrides or loader settings. It makes quota requests without inference calls or reading chat history. Host apps own token renewal; if an access token expires, open the source app to renew it.

Private configuration and cache files are written with owner-only permissions. The cache contains hashed email keys, timestamps and allowlisted display fields, including quota windows and available credit balances. It omits access tokens, account IDs and unknown response fields. Email hashes are identifiers, not a substitute for keeping the cache private.

## Verify and troubleshoot

```sh
python3 -m unittest discover -p 'test_*.py'
python3 demo.py
```

Tests and the demo use fictional data, without contacting OpenAI or reading real sign-ins. To check your own setup locally, run the installed plugin with network access. Avoid sharing its raw output, which includes account emails.

If the meter stays unavailable, check the configured email and path, open the source app to renew its sign-in, and confirm that the official desktop app is installed. A signature failure stops the check; there is no override that sends tokens to an arbitrary executable. Historical readings retain their age until live verification succeeds.

MIT licensed. Contributions welcome; use synthetic fixtures when reporting a bug.
