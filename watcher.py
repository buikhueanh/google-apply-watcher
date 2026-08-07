"""
Google Careers "Apply button" watcher.

Why this works without a browser:
    Google Careers job pages are server-side rendered. The Apply control is a plain
    anchor in the initial HTML:

        <a href="https://www.google.com/about/careers/applications/apply
                 ?jobId=<opaque-token>&loc=US&title=Software+Engineer">Apply</a>

    A posting with no apply window simply omits that anchor. So detection is a
    substring/regex match on the raw HTML - no Selenium, no Playwright, no headless
    Chrome. That decision is the whole design: it makes a run cost ~200ms and ~80KB,
    which is what lets us poll every 60s from a free tier without being abusive.

    Verified 2026-08-07 by diffing two live pages:
      - 78703249065943750 (Early Career, Campus) -> no apply anchor
      - 114905638462464710 (Vertex AI, Warsaw)   -> apply anchor present

    The jobId token is opaque and server-issued, so it CANNOT be forged or guessed
    from the numeric job id in the URL. Waiting for it to appear is the only path.

Usage:
    python watcher.py --once          # single check, exit 0 if open (for cron/CI)
    python watcher.py --loop 60       # poll forever every 60s (laptop mode)
    python watcher.py --selftest      # verify the detector against a known-open job
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

# --- configuration ------------------------------------------------------------

TARGETS = {
    "swe-early-career-campus-us": (
        "https://www.google.com/about/careers/applications/jobs/results/"
        "78703249065943750-software-engineer-early-career-campus"
    ),
    # Add more here. The poster mentioned a Google Canada link; drop it in as
    # "swe-early-career-campus-ca": "<url>" and everything else just works.
    "CANARY-TEST-REMOVE-ME": (
        "https://www.google.com/about/careers/applications/jobs/results/"
        "114905638462464710-senior-software-engineer-vertex-ai-workbench"
    ),
}

# A page that is known to have an open apply button. Used by --selftest so we can
# tell "the button isn't up yet" apart from "my detector silently broke because
# Google changed their markup". Without this, a false negative is invisible.
CANARY = (
    "https://www.google.com/about/careers/applications/jobs/results/"
    "114905638462464710-senior-software-engineer-vertex-ai-workbench"
)

# --- how detection actually works, after being wrong once ---------------------
#
# First attempt matched the absolute URL
#   https://www.google.com/about/careers/applications/apply?jobId=...
# and found nothing, because the page declares
#   <base href="https://www.google.com/about/careers/applications/">
# and writes the link RELATIVE:  href="apply?jobId=..."
#
# The bigger trap: a CLOSED job page already contains ~20 apply?jobId= tokens.
# They belong to the other postings inlined in the sidebar list, sitting inside a
# JSON blob with &-escaped ampersands. Matching "any apply?jobId=" would fire
# on day one, every time, forever. A watcher with a 100% false positive rate is
# strictly worse than no watcher, because you stop trusting it.
#
# The signal that actually distinguishes the two states is the button element
# itself: the real Apply control carries id="apply-action-button", and nothing
# else on the page does. Everything below hangs off that.
BASE = "https://www.google.com/about/careers/applications/"
BUTTON_ID = "apply-action-button"
ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)

STATE_PATH = Path(os.environ.get("WATCHER_STATE", "state.json"))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 20


@dataclass
class Result:
    key: str
    url: str
    open_now: bool
    apply_url: str | None
    error: str | None = None


# --- fetching -----------------------------------------------------------------

def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def check(key: str, url: str) -> Result:
    """One probe. Network/parse failures are captured, never raised: a watcher that
    dies on a transient 503 is a watcher that isn't watching."""
    try:
        html = fetch(url)
    except Exception as exc:
        return Result(key, url, False, None, error=f"{type(exc).__name__}: {exc}")

    return detect(key, url, html)


def detect(key: str, url: str, html: str) -> Result:
    """Pure function over HTML - separated from fetch() so it can be unit tested
    offline against saved fixtures. Anything that touches the network can't be
    tested reliably; anything that can't be tested reliably will rot.

    Scans <a> tags rather than doing one big regex over the document, so the
    button id and the href are read off the SAME element. Checking that both
    strings merely exist somewhere in a 1.1MB page would reintroduce the
    false-positive problem from a different direction."""
    for tag in ANCHOR_RE.finditer(html):
        raw = tag.group(0)
        if BUTTON_ID not in raw:
            continue
        href = HREF_RE.search(raw)
        if not href:
            # Button present but no link on it. Treat as not-yet-open rather than
            # crashing: an ambiguous page should never look like a confirmed hit.
            continue
        # &amp; -> & so the URL is clickable straight out of the notification,
        # then resolve the relative href against the page's <base>.
        return Result(key, url, True, urllib.parse.urljoin(
            BASE, href.group(1).replace("&amp;", "&")))
    return Result(key, url, False, None)


# --- notifiers ----------------------------------------------------------------
# Each notifier is a plain function (subject, body) -> None. Adding a channel means
# adding a function and one line in notify_all. No base classes, no registry: with
# three implementations that would be ceremony, not abstraction.

def notify_telegram(subject: str, body: str) -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": f"{subject}\n\n{body}",
        "disable_web_page_preview": "false",
    }).encode()
    urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/sendMessage", payload, timeout=TIMEOUT
    ).read()


def notify_macos(subject: str, body: str) -> None:
    """Desktop banner + repeated alert sound. Zero accounts, zero credentials, so it
    is the one channel that cannot be misconfigured. Only fires in laptop mode:
    NOTIFY_MACOS must be set explicitly, since on a CI runner it would be a no-op
    that silently swallows time."""
    if not os.environ.get("NOTIFY_MACOS") or sys.platform != "darwin":
        return
    import subprocess

    def as_literal(s: str) -> str:
        """AppleScript string literal. NOT shlex.quote - that produces POSIX shell
        quoting, and osascript is a different language with different rules. On a
        string with no shell metacharacters shlex returns it bare, which AppleScript
        then reads as loose identifiers and rejects with a syntax error."""
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        s = s.replace("\r", " ").replace("\n", " - ")  # literals cannot span lines
        return f'"{s}"'

    script = (
        f"display notification {as_literal(body)} "
        f'with title {as_literal(subject)} sound name "Glass"'
    )
    proc = subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        # Surface it instead of letting osascript scribble on stderr unattributed.
        raise RuntimeError(f"osascript: {proc.stderr.strip()}")
    # A single banner is easy to miss. Three spaced beeps is not.
    for _ in range(3):
        subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"],
                       check=False, timeout=10)


def notify_discord(subject: str, body: str) -> None:
    """Webhook POST. No bot, no OAuth, no account: the URL itself is the credential,
    which is why this is the lowest-friction channel available."""
    url = os.environ.get("DISCORD_WEBHOOK")
    if not url:
        return
    # <@USER_ID> pings you specifically, which is what escapes Discord's default
    # notification batching and actually lights up your phone. Without it the
    # message can sit silently in a channel you aren't looking at.
    mention = os.environ.get("DISCORD_USER_ID")
    prefix = f"<@{mention}> " if mention else ""
    payload = json.dumps({
        "content": f"{prefix}**{subject}**\n{body}",
        "allowed_mentions": {"parse": ["users"]},
    }).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def notify_email(subject: str, body: str) -> None:
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    to = os.environ.get("NOTIFY_EMAIL", user)
    if not (user and pwd):
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT) as s:
        s.login(user, pwd)
        s.send_message(msg)


def notify_twilio(subject: str, body: str) -> None:
    """SMS. Costs money and needs a verified number, so it stays opt-in via env."""
    sid = os.environ.get("TWILIO_SID")
    tok = os.environ.get("TWILIO_TOKEN")
    frm, to = os.environ.get("TWILIO_FROM"), os.environ.get("TWILIO_TO")
    if not all((sid, tok, frm, to)):
        return
    data = urllib.parse.urlencode({
        "From": frm, "To": to, "Body": f"{subject} {body}"[:1500],
    }).encode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data
    )
    import base64
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def notify_all(subject: str, body: str) -> None:
    for fn in (notify_macos, notify_discord, notify_telegram,
               notify_email, notify_twilio):
        try:
            fn(subject, body)
        except Exception as exc:
            # One dead channel must not suppress the others. This is the single
            # moment the whole system exists for; degrade, don't abort.
            print(f"[warn] {fn.__name__} failed: {exc}", file=sys.stderr)


# --- state --------------------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


# --- orchestration ------------------------------------------------------------

def run_once(verbose: bool = True) -> bool:
    """Returns True if any target is open. Fires notifications only on the
    closed -> open transition, so a 1-minute poll doesn't become 1440 texts/day."""
    state = load_state()
    any_open = False
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    for key, url in TARGETS.items():
        r = check(key, url)
        was_open = state.get(key, {}).get("open", False)

        if r.error:
            if verbose:
                print(f"[{stamp}] {key}: ERROR {r.error}")
            continue

        if verbose:
            print(f"[{stamp}] {key}: {'OPEN' if r.open_now else 'closed'}")

        if r.open_now:
            any_open = True
            if not was_open:
                notify_all(
                    "APPLY BUTTON IS LIVE - Google",
                    f"{key}\nApply: {r.apply_url}\nPosting: {url}\nDetected {stamp}",
                )
                print(f"[{stamp}] NOTIFIED -> {r.apply_url}")

        state[key] = {"open": r.open_now, "apply_url": r.apply_url, "checked": stamp}

    save_state(state)
    return any_open


def selftest() -> int:
    """Guards against silent breakage: the detector must find an anchor on a page
    that definitely has one. If this fails, the regex is stale, not the job."""
    r = check("canary", CANARY)
    if r.error:
        print(f"selftest inconclusive (network): {r.error}")
        return 2
    if not r.open_now:
        print("SELFTEST FAILED - detector found no apply anchor on a known-open job.")
        print("Google likely changed their markup. Update APPLY_RE.")
        return 1
    print(f"selftest ok - detector works. canary apply url: {r.apply_url[:80]}...")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop", type=int, metavar="SECONDS")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--testnotify", action="store_true")
    args = p.parse_args()

    if args.testnotify:
        # Proves the delivery path works BEFORE the real event. Otherwise the first
        # time you exercise the notifier is the one time it has to work.
        configured = [
            name for name, var in (
                ("macos", "NOTIFY_MACOS"), ("discord", "DISCORD_WEBHOOK"),
                ("telegram", "TELEGRAM_TOKEN"), ("email", "SMTP_USER"),
                ("twilio", "TWILIO_SID"),
            ) if os.environ.get(var)
        ]
        print(f"configured channels: {configured or 'NONE - nothing will be sent'}")
        notify_all("TEST - watcher is alive",
                   "If you can read this, the real alert will reach you too.")
        return 0

    if args.selftest:
        return selftest()

    if args.loop:
        print(f"polling {len(TARGETS)} target(s) every ~{args.loop}s. ctrl-c to stop.")
        while True:
            run_once()
            # Jitter so we aren't a perfectly periodic signal hitting their edge.
            time.sleep(args.loop + random.uniform(0, args.loop * 0.2))

    run_once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
