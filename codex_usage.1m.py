#!/usr/bin/env python3
# <xbar.title>Codex Usage</xbar.title>
# <xbar.version>v1.0.1</xbar.version>
# <xbar.author>Contributors</xbar.author>
# <xbar.desc>Remaining Codex quota for your configured accounts</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <xbar.refreshOnOpen>true</xbar.refreshOnOpen>
# <swiftbar.refreshOnOpen>true</swiftbar.refreshOnOpen>
"""Read existing host-owned sign-ins through isolated official app servers.

No model calls, browser cookies, credential writes, or account switching.
The host apps remain responsible for renewing their own sign-ins.
Config: ~/Library/Application Support/codex-usage-swiftbar/codex-accounts.json
Protocol: https://learn.chatgpt.com/docs/app-server
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import stat
import subprocess
import tempfile
import time
import unicodedata

VERSION = "1.0.1"
# Keep each live attempt short enough that xbar stays responsive, then retry a
# separate app-server process once for temporary service/network failures.
TIMEOUT_SECONDS = 12
LIVE_ATTEMPTS = 2
MAX_JSON_BYTES = 1024 * 1024
# Retain the last identified reading for context, but always label its age and
# keep that separate from the quota window's reset countdown.
MAX_STALE_SECONDS = 7 * 24 * 60 * 60
USAGE_URL = "https://chatgpt.com/codex/settings/usage"
CONFIG = Path.home() / "Library/Application Support/codex-usage-swiftbar/codex-accounts.json"
CACHE = Path.home() / "Library/Application Support/codex-usage-swiftbar/codex-usage-cache.json"
# App updates can move the bundled CLI. Menu-bar hosts do not inherit the
# terminal PATH, so resolve current and legacy bundle layouts on every refresh.
CODEX_CANDIDATES = tuple(
    root / app / "Contents/Resources" / relative
    for root in (Path("/Applications"), Path.home() / "Applications")
    for app in ("ChatGPT.app", "Codex.app")
    for relative in ("codex-cli/CodexCLI.app/Contents/MacOS/codex",
                     "codex-cli/bin/codex", "codex")
)
CODEX_REQUIREMENT = ('=anchor apple generic and identifier "codex" '
                     'and certificate leaf[subject.OU] = "2DC432GLL2"')


class UsageError(RuntimeError):
    pass


class IdentityError(UsageError):
    """The source app holds a different account than this slot expects."""


def read_json(path):
    """Read a bounded, regular, user-owned file without following a symlink."""
    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise OSError("Unsafe local file")
        data = stream.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise OSError("Local file too large")
    return json.loads(data)


def private_directory(path):
    """Protect plugin-owned state without chmodding a symlink target."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise OSError("Unsafe state directory")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def codex_binary():
    # No PATH or environment override may choose a recipient for account tokens.
    return next((str(p.resolve()) for p in CODEX_CANDIDATES
                 if p.is_file() and os.access(p, os.X_OK)), None)


def helper_environment(home):
    """Deliberately exclude inherited credentials, routing and loader settings."""
    return {"HOME": str(Path.home()), "CODEX_HOME": home, "TMPDIR": home,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}


def verify_binary(binary, env):
    """Require a valid Apple-trusted signature from OpenAI before starting it.

    Verify each launch, without pinning a version/hash that breaks app updates.
    Never expose raw signature diagnostics or fall back to an unverified binary.
    """
    try:
        checked = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", "-R",
             CODEX_REQUIREMENT, binary], env=env, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if checked.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise UsageError("Codex signature could not be verified; usage check stopped")


def claims(token):
    try:
        return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
    except (ValueError, IndexError, TypeError, AttributeError):
        raise UsageError("Sign-in unreadable; open the source app") from None


def inspect(profile):
    """Read a source's sign-in without deciding which slot it belongs to."""
    try:
        data = read_json(profile["path"])
        source = profile["source"]
        values = data["tokens"] if source == "codex" else data["openai"]
        token = values["access_token"] if source == "codex" else values["access"]
        account = values["account_id"] if source == "codex" else values["accountId"]
    except (OSError, ValueError, KeyError, TypeError):
        raise UsageError("Sign-in unavailable; open the source app") from None
    payload = claims(token)
    identity = payload.get("https://api.openai.com/profile", {}).get("email")
    if not identity and values.get("id_token"):
        identity = claims(values["id_token"]).get("email")
    if not identity:
        raise UsageError("Sign-in identity unreadable; open the source app")
    if not account:
        raise UsageError("Account identifier unavailable")
    if payload.get("exp", 0) <= time.time() + 30:
        raise UsageError("Sign-in expired; open the source app to renew it")
    auth_claim = payload.get("https://api.openai.com/auth") or {}
    return {"auth": {"type": "chatgptAuthTokens", "accessToken": token,
                     "chatgptAccountId": account},
            "email": identity,
            # The subscription billed, which is what a reading belongs to.
            "subscription": auth_claim.get("chatgpt_account_id") or account}


def credential(profile):
    found = inspect(profile)
    if found["email"].casefold() != profile["email"].casefold():
        raise IdentityError(
            f"{profile.get('app', 'Source app')} is signed in to a different account")
    return found["auth"]


class AppServer:
    def __init__(self, binary, home):
        env = helper_environment(home)
        verify_binary(binary, env)
        self.proc = subprocess.Popen(
            [binary, "app-server", "--stdio"], cwd=home, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.deadline = time.monotonic() + TIMEOUT_SECONDS
        self.request_id = 0

    def send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode())

    def request(self, method, params=None):
        self.request_id += 1
        req_id = self.request_id
        self.send({"id": req_id, "method": method, "params": params})
        while True:
            if time.monotonic() >= self.deadline:
                raise UsageError("Usage check timed out; retry")
            while b"\n" not in self.buffer:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0 or not self.selector.select(remaining):
                    raise UsageError("Usage check timed out; retry")
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise UsageError("Codex usage service stopped; retry")
                self.buffer += chunk
                if len(self.buffer) > MAX_JSON_BYTES:
                    raise UsageError("Usage response too large; check stopped")
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                raise UsageError("Usage response unreadable; check stopped")
            if "method" in message:
                if "id" in message:
                    # Host apps own renewal. Never rotate their refresh tokens here.
                    self.send({"id": message["id"], "error": {
                        "code": -32000, "message": "Renew sign-in in the source app"}})
                continue
            if message.get("id") != req_id:
                continue
            if "error" in message:
                # Server errors may contain private URLs or auth details.
                raise UsageError("Usage unavailable; refresh sign-in in the source app")
            return message.get("result") or {}

    def close(self):
        self.selector.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()


def fetch(auth):
    """One live quota read, retried once for transient failures."""
    binary = codex_binary()
    if not binary:
        return {"error": "Codex executable not found"}
    error = None
    for _ in range(LIVE_ATTEMPTS):
        try:
            with tempfile.TemporaryDirectory(prefix="codex-usage-") as home:
                server = AppServer(binary, home)
                try:
                    server.request("initialize", {
                        "clientInfo": {"name": "swiftbar-codex-usage", "version": VERSION},
                        "capabilities": {"experimentalApi": True}})
                    server.send({"method": "initialized"})
                    server.request("account/login/start", auth)
                    result = server.request("account/rateLimits/read")
                    expected = auth.get("chatgptAccountId")
                    if not expected or result.get("accountId") != expected:
                        raise IdentityError("Usage account could not be verified; reading rejected")
                    return {"result": display_result(result)}
                finally:
                    server.close()
        except IdentityError as caught:
            return {"error": str(caught), "identity": True}
        except UsageError as caught:
            error = caught
        except Exception:
            error = UsageError("Usage check failed; retry")
    return {"error": str(error or "Usage check failed; retry")}


def read_account(profile):
    """Single-slot read, kept for direct invocation and tests."""
    try:
        auth = credential(profile)
    except UsageError as error:
        return {"profile": profile, "error": str(error),
                "identity": isinstance(error, IdentityError)}
    except Exception:
        return {"profile": profile, "error": "Usage check failed; retry"}
    return dict(fetch(auth), profile=profile)


def profile_key(profile):
    # Keyed by account, so a reading survives the app that produced it.
    return hashlib.sha256(profile["email"].casefold().encode()).hexdigest()


def legacy_key(profile):
    """Pre-3.0 key, so a reading taken before re-keying is not thrown away."""
    try:
        stable = "\0".join(profile[key] for key in ("source", "path", "email"))
    except KeyError:
        return None
    return hashlib.sha256(stable.encode()).hexdigest()


def load_cache():
    try:
        data = read_json(CACHE)
        return data["accounts"] if data.get("version") == 1 and isinstance(data["accounts"], dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def display_result(result):
    """Allowlist only fields rendered by the meter, including all quota buckets."""
    if not isinstance(result, dict):
        return {}

    def window(raw):
        if not isinstance(raw, dict):
            return None
        return {key: value for key in ("usedPercent", "windowDurationMins", "resetsAt")
                if isinstance(value := raw.get(key), (int, float))
                and not isinstance(value, bool) and math.isfinite(value)}

    def bucket(raw):
        out = {key: value for key in ("limitId", "limitName", "planType")
               if isinstance(value := raw.get(key), str)}
        for key in ("primary", "secondary"):
            if isinstance(raw.get(key), dict):
                out[key] = window(raw[key])
        credits = raw.get("credits")
        if isinstance(credits, dict):
            out["credits"] = {}
            if isinstance(credits.get("unlimited"), bool):
                out["credits"]["unlimited"] = credits["unlimited"]
            balance = credits.get("balance")
            if isinstance(balance, str) or (isinstance(balance, (int, float))
                    and not isinstance(balance, bool) and math.isfinite(balance)):
                out["credits"]["balance"] = balance
        return out

    out = {}
    if isinstance(result.get("rateLimits"), dict):
        out["rateLimits"] = bucket(result["rateLimits"])
    if isinstance(result.get("rateLimitsByLimitId"), dict):
        out["rateLimitsByLimitId"] = {
            key: bucket(value) for key, value in result["rateLimitsByLimitId"].items()
            if isinstance(key, str) and isinstance(value, dict)}
    return out


def save_cache(accounts):
    previous = load_cache()
    records = {}
    now = time.time()
    for account in accounts:
        profile = account["profile"]
        key = profile_key(profile)
        if "result" in account and "stale_seconds" not in account:
            records[key] = {"savedAt": now, "result": display_result(account["result"])}
        else:
            cached = previous.get(key, previous.get(legacy_key(profile) or "", {}))
            if not isinstance(cached, dict):
                continue
            saved = cached.get("savedAt")
            if (isinstance(saved, (int, float)) and not isinstance(saved, bool)
                    and 0 <= now - saved <= MAX_STALE_SECONDS
                    and isinstance(cached.get("result"), dict)):
                records[key] = {"savedAt": saved, "result": display_result(cached["result"])}
    temporary = None
    try:
        private_directory(CACHE.parent)
        fd, temporary = tempfile.mkstemp(prefix="." + CACHE.name + ".", dir=CACHE.parent)
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, "accounts": records}, stream, separators=(",", ":"))
        os.replace(temporary, CACHE)
    except OSError:
        # A cache failure must never prevent a live quota reading from showing.
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def apply_cache(accounts):
    records = load_cache()
    now = time.time()
    for account in accounts:
        if "result" in account:
            continue
        profile = account["profile"]
        cached = records.get(profile_key(profile))
        if cached is None:
            cached = records.get(legacy_key(profile) or "", {})
        if not isinstance(cached, dict):
            continue
        saved_at = cached.get("savedAt")
        result = cached.get("result")
        age = now - saved_at if isinstance(saved_at, (int, float)) else None
        if isinstance(result, dict) and isinstance(age, (int, float)) and 0 <= age <= MAX_STALE_SECONDS:
            account["result"] = display_result(result)
            account["stale_seconds"] = int(age)
    return accounts


def stale_label(seconds):
    minutes = max(1, int(seconds) // 60)
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def clean(value):
    # Dynamic text cannot add menu parameters, ANSI escapes or bidi overrides.
    text = "".join(" " if unicodedata.category(c).startswith("C") else c
                   for c in str(value))
    return " ".join(text.replace("|", "∣").split())


def remaining(window):
    if not isinstance(window, dict):
        return None
    value = window.get("usedPercent")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return max(0, min(100, round(100 - value)))


def buckets(result):
    mapped = result.get("rateLimitsByLimitId")
    if isinstance(mapped, dict) and mapped:
        return {k: v for k, v in mapped.items() if isinstance(v, dict)}
    fallback = result.get("rateLimits")
    return {fallback.get("limitId") or "codex": fallback} if isinstance(fallback, dict) else {}


def main_bucket(result):
    mapped = buckets(result)
    return mapped.get("codex", next(iter(mapped.values()), {}))


def main_window(result):
    bucket = main_bucket(result)
    return bucket.get("primary") or bucket.get("secondary")


def window_label(window):
    duration = window.get("windowDurationMins")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return "Quota"
    if duration % 1440 == 0:
        return f"{int(duration // 1440)}-day"
    if duration % 60 == 0:
        return f"{int(duration // 60)}-hour"
    return f"{duration:g}-minute"


def reset_text(timestamp):
    if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        return "Reset unknown"
    try:
        exact = datetime.fromtimestamp(timestamp).astimezone().strftime("%a %-d %b, %-I:%M %p")
    except (ValueError, OverflowError, OSError):
        return "Reset unknown"
    seconds = int(timestamp - time.time())
    if seconds <= 0:
        return "Reset due; refresh to check"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    relative = f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"
    return f"Resets in {relative} ({exact})"


def compact_reset(timestamp):
    """Short reset countdown for the always-visible menu-bar title."""
    if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        return "?"
    seconds = int(timestamp - time.time())
    if seconds <= 0:
        return "reset"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def color_for(value):
    if value is None:
        return "#8E8E93"
    for minimum, color in (
        (70, "#228B22"),  # Dark green
        (50, "#90EE90"),  # Light green
        (40, "#FFF59D"),  # Light yellow
        (30, "#FFD54F"),  # Golden yellow
        (20, "#FF9800"),  # Orange / amber
        (10, "#F44336"),  # Red
        (0, "#B71C1C"),   # Dark red
    ):
        if value >= minimum:
            return color
    return "#B71C1C"


def account_title(account):
    window = main_window(account.get("result", {}))
    value = remaining(window)
    label = clean(account["profile"]["short"])
    stale = account.get("stale_seconds")
    quota = "?" if value is None else f"{value}%"
    countdown = compact_reset(window.get("resetsAt")) if isinstance(window, dict) else "?"
    text = f"{label}{quota} {countdown}"
    if stale is not None:
        text += f" ~{stale_label(stale)}"
    # SwiftBar supports indexed ANSI colours, not RGB (38;2) or dim (2).
    # Match the dropdown's seven colour bands with the nearest palette colours.
    index = {"#228B22": 28, "#90EE90": 120, "#FFF59D": 229,
             "#FFD54F": 221, "#FF9800": 208, "#F44336": 203,
             "#B71C1C": 124, "#8E8E93": 246}[color_for(value)]
    return f"\033[38;5;{index}m{text}\033[0m"


def emit_window(window, prefix=""):
    if not isinstance(window, dict):
        return
    value = remaining(window)
    display = "Unavailable" if value is None else f"{value}% remaining"
    print(f"{prefix}{window_label(window)}: {display} | color={color_for(value)}")
    print(f"{prefix}{reset_text(window.get('resetsAt'))} | color=#8E8E93 size=11")


def render(accounts):
    # SwiftBar's symbol parser conflicts with ANSI attributed colours.
    print(" │ ".join(account_title(account) for account in accounts)
          + " | ansi=true symbolize=false")
    print("---")
    legend = " · ".join(f"{clean(a['profile']['short'])} = {clean(a['profile']['label'])}" for a in accounts)
    print(f"{legend} · time = until reset · ~age = historical | size=11 color=#8E8E93")
    for account in accounts:
        profile = account["profile"]
        result = account.get("result", {})
        print("---")
        print(f"{clean(profile['label'])} | size=14")
        if profile.get("showEmail", False):
            print(f"{clean(profile['email'])} | size=11 color=#8E8E93")
        if account.get("error"):
            print(f"{clean(account['error'])} | color=#FF9F0A")
        if account.get("stale_seconds") is not None:
            print(f"Last identified {stale_label(account['stale_seconds'])} ago; not current | color=#8E8E93")
            reset = (main_window(account.get("result", {})) or {}).get("resetsAt")
            if isinstance(reset, (int, float)) and reset <= time.time():
                print("That quota window has since reset | color=#8E8E93 size=11")
        if account.get("result"):
            bucket = main_bucket(result)
            if not bucket.get("primary") and not bucket.get("secondary"):
                print("Quota unavailable | color=#8E8E93")
            emit_window(bucket.get("primary"))
            emit_window(bucket.get("secondary"))
            for key, extra in buckets(result).items():
                if extra is bucket:
                    continue
                print(clean(extra.get("limitName") or key))
                emit_window(extra.get("primary"), "--")
                emit_window(extra.get("secondary"), "--")
            credits = bucket.get("credits")
            if isinstance(credits, dict):
                if credits.get("unlimited"):
                    print("Credits: unlimited | size=11")
                elif credits.get("balance") is not None:
                    print(f"Credits: {clean(credits['balance'])} | size=11")
            if bucket.get("planType"):
                print(f"Plan: {clean(bucket['planType']).title()} | color=#8E8E93 size=11")
        print(f"Source: {clean(profile['app'])} | color=#8E8E93 size=11")
    print("---")
    print(f"Checked {datetime.now().strftime('%H:%M:%S')} · refreshes every minute and on opening | size=11 color=#8E8E93")
    print("Refresh now | refresh=true")
    print(f"Open usage website (browser account) | href={USAGE_URL}")


def load_profiles():
    try:
        profiles = read_json(CONFIG)["accounts"]
        if not isinstance(profiles, list) or not profiles:
            raise ValueError()
        for p in profiles:
            if p["source"] not in ("codex", "opencode"):
                raise ValueError()
            for key in ("path", "email", "label", "short", "app"):
                if not isinstance(p[key], str) or not p[key].strip():
                    raise ValueError()
            if not isinstance(p.get("showEmail", False), bool):
                raise ValueError()
        if len({p["email"].casefold() for p in profiles}) != len(profiles):
            raise ValueError()
        return profiles
    except (OSError, ValueError, KeyError, TypeError):
        raise UsageError("Account configuration unavailable") from None


def sources(profiles):
    """What each app currently holds, independent of which slot wants it."""
    found = []
    for profile in profiles:
        try:
            found.append(dict(inspect(profile), app=profile["app"]))
        except UsageError as error:
            found.append({"app": profile["app"], "email": None, "error": str(error)})
        except Exception:
            found.append({"app": profile["app"], "email": None,
                          "error": "Sign-in unreadable; open the source app"})
    return found


def unheld(profile, found):
    """Explain a missing account without exposing other source identities."""
    if any(s.get("email") for s in found):
        return "No app signed in to this account; check the source app's sign-in"
    return next((s["error"] for s in found if s.get("error")), "Sign-in unavailable")


def main():
    try:
        profiles = load_profiles()
    except UsageError as error:
        print("Codex ?\n---")
        print(str(error))
        print("Refresh now | refresh=true")
        return
    found = sources(profiles)
    # One read per distinct subscription: two apps on one account is one quota.
    wanted = {}
    allowed_emails = {p["email"].casefold() for p in profiles}
    for source in found:
        if source.get("auth") and source["email"].casefold() in allowed_emails:
            wanted.setdefault(source["subscription"], source)
    with ThreadPoolExecutor(max_workers=max(1, len(wanted))) as pool:
        readings = dict(zip(wanted, pool.map(lambda s: fetch(s["auth"]), wanted.values())))
    accounts = []
    for profile in profiles:
        source = next((s for s in found if (s.get("email") or "").casefold()
                       == profile["email"].casefold()), None)
        if source is None:
            accounts.append({"profile": profile, "error": unheld(profile, found)})
        else:
            accounts.append(dict(readings.get(source["subscription"], {}), profile=profile))
    save_cache(accounts)
    render(apply_cache(accounts))


if __name__ == "__main__":
    main()
