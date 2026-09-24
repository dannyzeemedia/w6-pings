"""ops_log.py — the single place every scheduled job reports to.

Before this existed, a failing job had three ways to disappear:

  1. it crashed, GitHub went red, and nothing told anyone;
  2. it caught the error and posted to Slack, where the message scrolled away
     (the beehiiv session-expiry notice fired six times in two days and no one
     actioned it, because a Slack ping creates no obligation);
  3. it never ran at all — laptop asleep, cron paused, routine disabled — and
     silence looks exactly like success.

Everything here exists to close those three. Runs land in 📊 Run Log, errors
land deduplicated in 🚨 Error Log, and ⏰ Scheduled Jobs gets a Last Success
stamp so the watchdog can spot the jobs that have gone quiet.

Usage — wrap the whole job:

    from ops_log import OpsLog

    with OpsLog("klaviyo_health_scan") as ops:
        ops.step("reading clients")
        ...
        ops.warn("Vue skipped: no API key")     # → Partial, not Failed

An uncaught exception inside the block is logged as a Crash and re-raised, so
behaviour of the calling job is unchanged. Nothing in this module is allowed to
break its caller: every Airtable call is wrapped, and failures here degrade to
a printed line.

Also runs as a CLI, for GitHub Actions `if: failure()` steps that need to
report a crash the Python process never got to handle itself:

    python3 ops_log.py crash --job klaviyo_health_scan \
        --message "workflow failed" --log-tail "$(tail -c 4000 run.log)"
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import traceback

import requests

BASE = "appjECFZpDz5d6Gr4"
T_JOBS = "tbl9p17OVjaFsnKF7"     # ⏰ Scheduled Jobs
T_ERRORS = "tblPr4wjgDc1h2xCy"   # 🚨 Error Log
T_RUNS = "tblkI0H9xgk3d8xjx"     # 📊 Run Log
API = "https://api.airtable.com/v0"

TOKEN = os.environ.get("AIRTABLE_TOKEN", "")

# Errors that fix themselves. These still get counted so a genuine outage shows
# up as a spike, but they never escalate and never become a proposed code fix.
TRANSIENT = re.compile(
    r"rate.?limit|\b429\b|\b50[0234]\b|timed?.?out|timeout|connection (reset|aborted|refused)"
    r"|temporarily unavailable|service unavailable|bad gateway|gateway time|throttl"
    r"|econnreset|remote end closed|ssl|handshake|read timed out|max retries",
    re.I,
)

# Errors a human can clear but no patch can. Matched in order; first hit wins.
# This is the class the beehiiv session expiry falls into — persistent, real,
# and completely pointless to file as a code fix.
REMEDIES = [
    (re.compile(r"sessionexpired|session expired|page state: login|not logged in", re.I),
     "Session expired. Run `python3 assisted_login.py` in the job's repo on the laptop to "
     "re-authenticate. It waits out any PerimeterX IP block, opens Chrome on the saved "
     "profile, prefills the email and types the 2FA code from #beehiiv-2fa — you only need "
     "to type the password."),
    (re.compile(r"page state: blocked|perimeterx|cloudflare|\b403\b.*(block|forbidden)", re.I),
     "The laptop's IP is being blocked by bot protection. It clears on its own, usually "
     "within a few hours. Re-run after that; if it persists past a day, the profile likely "
     "needs a fresh assisted_login."),
    (re.compile(r"\b401\b|unauthorized|invalid[_ ]auth|token_revoked|invalid.{0,10}token"
                r"|authentication fail|not_authed", re.I),
     "A credential was rejected. Rotate the key in the API Keys sheet, then push the new "
     "value into the matching GitHub secret (pipe it to `gh secret set` via stdin — "
     "`--body -` sets the literal string '-') and confirm with a real dispatch."),
    # "reached your specified API usage limits" is Anthropic's exact wording when
    # the monthly spend cap is hit, and it was matched by none of the original
    # alternatives — "usage limits" is not "quota", and "reached your" is not
    # "exceeded your". So the one error that stops every Claude-powered job in
    # the fleet got no remedy, was never escalated, and ran for a month.
    (re.compile(r"quota|insufficient.{0,20}(credit|fund|balance)|billing|payment required"
                r"|\b402\b|plan limit|(reach|exceed)(ed)? your"
                r"|usage limit|spend limit|credit balance", re.I),
     "An account quota or billing cap has been reached. For Anthropic this is the "
     "monthly spend cap at https://platform.claude.com/settings/billing — raise it "
     "or wait for the period reset; every Claude-powered job stays broken until "
     "then. Check the 📊 Run Log's LLM Cost column to see which job spent it."),
    # A permission denial is NEVER transient. It also gets a remedy for a second
    # reason beyond being useful: auto_resolve() skips any row that has an Action
    # Required, so attaching one is what stops the 72h silence rule closing it.
    #
    # Without this, a Google Sheets 403 on Tipaw's A/B tracker ran from 27 Aug to
    # 1 Sep 2026, appeared as one truncated line at the bottom of two digests,
    # was auto-resolved on the 30th for "going quiet" — and then kept firing for
    # three more days. It went quiet because nobody launched anything new for
    # that client, not because anything was fixed. Ten launches never reached the
    # tracker and a human retyped them by hand.
    (re.compile(r"(?i:does not have permission|permission_denied|caller does not have"
                r"|insufficient permission|forbidden|access denied|not authorized"
                r"|requires? (?:edit|write) access)"
                r"|403(?!.*(?:block|perimeterx|cloudflare))", 0),
     "A credential was refused ACCESS to a specific resource — this is not a bad "
     "key, it is a sharing problem, and it will never clear on its own. For a "
     "Google Sheet, share it as Editor with "
     "zm-ai-agent@zm-ai-agent.iam.gserviceaccount.com; the error names the "
     "spreadsheet id. Then re-run the job for the affected records, because "
     "nothing retries them automatically."),
    # A Slack bot cannot read a private channel it was never invited to, and
    # Slack reports that as channel_not_found rather than a permission error.
    # No credential is wrong and no patch helps — someone has to type /invite.
    (re.compile(r"not_in_channel|channel_not_found|SCAN CHANNEL UNREADABLE"
                r"|is_archived|missing_scope", re.I),
     "Slack is refusing the bot access to a channel. If the message names a "
     "channel, run `/invite @clara_ai` in it — a bot cannot read a PRIVATE "
     "channel it has not been invited to, and Slack reports that as "
     "'channel_not_found', not as a permission error. If it says missing_scope, "
     "the app needs the scope added and reinstalled at api.slack.com/apps."),
    # Every alternative here must name a credential, not merely sit near one.
    # A bare `|is not set|` used to be in this list and matched any sentence
    # containing those three words, which is how the W6 listener's KeyError:
    # 'score' — an ordinary code bug — was handed the remedy "a required secret
    # is missing". Worse than useless: promote() skips anything with an Action
    # Required, so a wrong remedy stops the real bug reaching the Fix Queue.
    # Case matters in the env-var alternatives — an UPPER_SNAKE name is the
    # signal — so this pattern is case-SENSITIVE, with (?i:...) opted back in
    # per alternative for the prose ones.
    (re.compile(r"KeyError: ['\"]?[A-Z][A-Z0-9_]{3,}['\"]?"
                r"|(?i:environment variable)"
                r"|\b[A-Z][A-Z0-9_]{3,}\b (?:is |was )?not (?:set|found|configured)"
                r"|(?i:secret .{0,20}not set)"
                r"|(?i:missing .{0,15}(?:key|token|secret|credential))", 0),
     "A required secret or environment variable is missing for this job. Check the "
     "workflow's env block against the API Keys sheet and set whatever is absent."),
]


BRIS = dt.timezone(dt.timedelta(hours=10))

# Statuses the pipeline may move back to Recurring when the same fault fires
# again. Everything not listed here is the human's to keep.
#
# "Fixed" is in this list on purpose, and that is the whole point of it. "Fixed"
# is a falsifiable claim about the world, and a recurrence falsifies it — while
# digest() filters Fixed rows out, so a fix that did not hold goes permanently
# invisible. That is not hypothetical: the Anthropic usage-cap error was marked
# Fixed on 2026-07-29 after the cap was raised, then fired 390 more times
# through 2026-08-24 — including the day the cap was actually hit again — and
# never appeared in a single digest. Evidence closes a row (resolve_by_success);
# evidence must be able to reopen one too, or the two rules are not symmetric.
#
# "Ignored" is deliberately NOT here. That status is the real mute: it means "I
# know, stop telling me", and it has to keep meaning that or there is no way to
# silence known noise. For a temporary snooze, use Muted Until.
# "Investigating" and "Promoted to Fix Queue" are also left alone, because the
# digest already surfaces both — leaving them be hides nothing.
REOPENABLE = ("New", "Auto-Resolved", "Fixed", None)


def _sel(v):
    """Read a singleSelect, which comes back as a bare string over REST but as
    {"id","name","color"} through some clients. Handling one shape only is how
    the first live digest crashed."""
    if isinstance(v, dict):
        return v.get("name")
    return v or None


def reopen_fields(prev, now):
    """Status/Notes to merge when an existing error row fires again.

    Returns {} when the row's status belongs to the human. Reopening a Fixed row
    leaves a dated note, so the row reads as a history rather than silently
    flipping back and forth.
    """
    status = _sel(prev.get("Status"))
    if status not in REOPENABLE:
        return {}
    out = {"Status": "Recurring"}
    if status == "Fixed":
        note = (prev.get("Notes") or "").rstrip()
        out["Notes"] = (
            (note + "\n\n" if note else "")
            + f"[{now.astimezone(BRIS):%Y-%m-%d %H:%M} AEST] Reopened "
              f"automatically: this was marked Fixed, but the same fault fired "
              f"again. The fix did not hold. Set it back to Fixed once it "
              f"stops, or to Ignored to mute it for good.")
    return out


def last_success(job_key, token=None, default_hours=48.0, cap_hours=168.0):
    """Hours since this job last SUCCEEDED — the watermark for an incremental job.

    A poller that looks back a fixed "now - N minutes" has a hole in it: if one
    poll dies or is skipped, the messages in that window are never looked at
    again by anything. Anchoring on the last success closes it, because the
    window stretches by itself to cover whatever was missed — ops_log stamps
    Last Success at the end of a successful run and deliberately does NOT stamp
    it on a failure, so a failed poll cannot advance the watermark past work it
    never did.

    default_hours: no stamp yet (first run, or the job was never registered).
    cap_hours: ceiling, so a fortnight's outage doesn't trigger a scan so large
    it times out and fails — which would pin the watermark and make it
    permanent. A capped scan that succeeds is worth more than a complete one
    that never finishes.

    Never raises: a watermark lookup that fails falls back to default_hours,
    because a poller that stops polling is worse than one that re-reads a day.
    """
    at = _Airtable(token or TOKEN)
    try:
        rec = at.find(T_JOBS, f"{{Job Key}}='{_esc(job_key)}'")
        stamp = (rec or {}).get("fields", {}).get("Last Success")
        if not stamp:
            return default_hours
        then = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        hrs = (_now() - then).total_seconds() / 3600
        return max(0.0, min(hrs, cap_hours))
    except Exception as e:
        _say(f"watermark lookup failed for {job_key} ({e}) — using {default_hours}h")
        return default_hours


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _say(msg):
    # stderr, not stdout: `start` prints OPS_RUN_ID=... on stdout for the shell
    # to eval / append to $GITHUB_ENV, so stdout has to stay machine-clean.
    print(f"[ops_log] {msg}", file=sys.stderr, flush=True)


def _esc(s):
    return str(s).replace("\\", "\\\\").replace("'", "\\'")


def _run_url():
    """Deep link to the current GitHub Actions run, when there is one."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    rid = os.environ.get("GITHUB_RUN_ID")
    if repo and rid:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server}/{repo}/actions/runs/{rid}"
    return ""


def _detect_platform():
    # OPS_PLATFORM is the escape hatch for anywhere we cannot sniff. The
    # fallback below is "Laptop cron", which is a guess, not a detection — the
    # Understudy jobs run on a VPS and every one of their 600+ Run Log rows
    # says "Laptop cron", contradicting their own ⏰ Scheduled Jobs row.
    override = os.environ.get("OPS_PLATFORM")
    if override:
        return override
    if os.environ.get("GITHUB_ACTIONS"):
        return "GitHub Actions"
    if os.environ.get("RENDER"):
        return "Render"
    return "Laptop cron"


def _detect_trigger():
    ev = os.environ.get("GITHUB_EVENT_NAME", "")
    if ev == "schedule":
        return "Scheduled"
    if ev in ("workflow_dispatch", "repository_dispatch"):
        # cron-job.org fires real scheduled runs as workflow_dispatch, so this
        # is only a genuine manual run when a human is at the keyboard.
        return "Manual" if os.environ.get("OPS_MANUAL") else "Scheduled"
    if ev:
        return "Event"
    return "Scheduled"


def _clip_words(text, limit):
    """Shorten to `limit` without ending mid-word.

    Prefers the last sentence end inside the budget, falls back to the last
    space, and only ever hard-cuts a single unbroken run of characters. An
    ellipsis is added so a reader can tell the difference between "this is the
    whole message" and "there is more on the record".
    """
    t = str(text or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    dot = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(" — "))
    if dot > limit * 0.5:
        return cut[:dot + 1].strip()
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit * 0.4 else cut).rstrip(" ,;:—-") + "…"


def fingerprint(job_key, exc_type, message):
    """Stable dedup key: the same fault always collapses onto one row.

    Everything variable is stripped out — ids, digits, URLs, quoted strings,
    hex blobs — so 'record recAbc123 failed' and 'record recXyz789 failed'
    are recognised as the same problem rather than two.
    """
    m = str(message or "").lower()
    m = re.sub(r"https?://\S+", "<url>", m)
    m = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", m)
    m = re.sub(r"\b(rec|tbl|app|fld|sel|usr|flow|camp)[a-z0-9]{8,}\b", "<id>", m)
    # Any mixed letter+digit token is an identifier of some kind (campaign ids,
    # run ids, message ids). Must run before digits are stripped, or 'abc123def'
    # and 'abc999def' would survive as two different fingerprints.
    m = re.sub(r"\b(?=[a-z0-9_-]{6,}\b)(?=[a-z_-]*\d)[a-z0-9_-]+\b", "<id>", m)
    # Klaviyo-style ids are short and often all letters ('VXKwcA'), so nothing
    # about the token itself marks it as an id — anchor on the noun instead.
    m = re.sub(r"\b(flow|campaign|message|segment|list|template|profile|metric|"
               r"experiment|variation|client|account)\s+[a-z0-9_-]{4,}", r"\1 <id>", m)
    m = re.sub(r"['\"][^'\"]{0,80}['\"]", "<str>", m)
    m = re.sub(r"\d+", "<n>", m)
    m = re.sub(r"\s+", " ", m).strip()[:300]
    raw = f"{job_key}|{exc_type}|{m}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _exception_from_log(log):
    """Pull the final 'SomeError: detail' line out of a captured log tail.

    Shell-wrapped jobs can only report an exit code, which says nothing. The
    traceback they printed to their log file says everything, so mine it.
    """
    if not log:
        return ""
    lines = [l.rstrip() for l in str(log).strip().splitlines() if l.strip()]
    for line in reversed(lines[-40:]):
        if line.startswith((" ", "\t")) or line.startswith("File "):
            continue
        m = re.match(r"^(?:[\w.]+\.)?([A-Z]\w*(?:Error|Exception|Expired|Timeout"
                     r"|Blocked|Failure))\b\s*:?\s*(.*)$", line)
        if m:
            name, detail = m.group(1), m.group(2).strip()
            return f"{name}: {detail}"[:200] if detail else name
    return ""


def classify(message, tb=""):
    """→ (Transient|Persistent, action_required or '')."""
    blob = f"{message}\n{tb}"
    for pattern, remedy in REMEDIES:
        if pattern.search(blob):
            return "Persistent", remedy
    if TRANSIENT.search(blob):
        return "Transient", ""
    return "Persistent", ""


class _Airtable:
    """Thin REST wrapper. Every method swallows its own failures — a logging
    outage must never take down the job it is logging for."""

    def __init__(self, token):
        self.h = {"Authorization": f"Bearer {token}",
                  "Content-Type": "application/json"}
        self.ok = bool(token)

    def _req(self, method, url, **kw):
        if not self.ok:
            return None
        try:
            r = requests.request(method, url, headers=self.h, timeout=30, **kw)
            if r.status_code >= 300:
                _say(f"airtable {r.status_code}: {r.text[:300]}")
                return None
            return r.json()
        except Exception as e:
            _say(f"airtable call failed: {e}")
            return None

    def get(self, table, rec_id):
        return self._req("GET", f"{API}/{BASE}/{table}/{rec_id}")

    def find(self, table, formula, fields=None):
        params = {"filterByFormula": formula, "maxRecords": 1}
        if fields:
            params["fields[]"] = fields
        data = self._req("GET", f"{API}/{BASE}/{table}", params=params)
        recs = (data or {}).get("records", [])
        return recs[0] if recs else None

    def create(self, table, fields):
        data = self._req("POST", f"{API}/{BASE}/{table}",
                         json={"fields": fields, "typecast": True})
        return (data or {}).get("id")

    def update(self, table, rec_id, fields):
        data = self._req("PATCH", f"{API}/{BASE}/{table}/{rec_id}",
                         json={"fields": fields, "typecast": True})
        return (data or {}).get("id")


# List rates per million tokens, matched by prefix so a dated model id still
# prices. Estimates only - the console is authoritative - but an estimate on
# every Run Log row is what makes "which job spent the money" a thirty-second
# query instead of the day of forensics it took in August 2026, when the org hit
# its $100 cap and the only per-key view showed one shared key.
# Verified against Anthropic list pricing 2026-08-26. Cache reads bill at 0.1x
# input; cache writes at 1.25x input (applied at the call site, not here).
#
# ORDER MATTERS: _llm_rate returns the first prefix that matches, so the more
# specific id must come first — "claude-opus-4" would otherwise swallow
# "claude-opus-4-8" and price it at the retired Opus 4.1 rate. The August 2026
# table did exactly that, billing Opus 4.8 at $15/$75 against a real $5/$25.
LLM_RATES = {                       # (input, output, cache_read) USD per 1M
    "claude-opus-5": (5.0, 25.0, 0.5),
    "claude-opus-4-8": (5.0, 25.0, 0.5),
    "claude-opus-4-7": (5.0, 25.0, 0.5),
    "claude-opus-4-6": (5.0, 25.0, 0.5),
    "claude-opus-4-5": (5.0, 25.0, 0.5),
    "claude-opus-4": (15.0, 75.0, 1.5),     # 4.1 and earlier
    "claude-sonnet-5": (2.0, 10.0, 0.2),
    "claude-sonnet": (3.0, 15.0, 0.3),
    "claude-haiku": (1.0, 5.0, 0.1),
    "claude-fable": (10.0, 50.0, 1.0),
    "claude-mythos": (10.0, 50.0, 1.0),
}

# Unknown ids are priced as the dearest model we actually sell against, so a
# typo'd or brand-new id over-reports rather than hiding spend.
UNKNOWN_RATE = (10.0, 50.0, 1.0)


def _llm_rate(model):
    for prefix, r in LLM_RATES.items():
        if (model or "").startswith(prefix):
            return r
    return UNKNOWN_RATE


_CURRENT = None                     # the OpsLog run in progress, for instrument()


# Absolute, and shared by every working copy on the machine. A CWD-relative
# path meant each of the nine repos spilled into its own checkout, so a drain
# run in ~/zm-ai-agent could never see the others — which is how the 22:00 UTC
# hour of 2026-09-03 (9.7M billed tokens, $7.06) ended up with no run to charge
# it to. OPS_SPILL_FILE overrides it for tests, or anywhere HOME is not ours.
SPILL_FILE = os.environ.get("OPS_SPILL_FILE") or \
    os.path.expanduser("~/.llm_usage.jsonl")

# The old per-checkout path. Still read and cleared on the way past, so money
# already stranded in a working copy banks itself the next time anything runs.
LEGACY_SPILL_FILE = ".llm_usage.jsonl"


def _spill_files():
    """Every spill file that exists, shared one first."""
    out, seen = [], set()
    for p in (SPILL_FILE, LEGACY_SPILL_FILE):
        ap = os.path.abspath(p)
        if ap in seen or not os.path.exists(ap):
            continue
        seen.add(ap)
        out.append(ap)
    return out


def _read_spill():
    """Every spilled record, from every spill file. Never raises."""
    rows = []
    for path in _spill_files():
        try:
            with open(path) as fh:
                rows += [json.loads(line) for line in fh if line.strip()]
        except Exception as e:
            _say(f"spill unreadable at {path} ({e})")
    return rows


def _rewrite_spill(keep):
    """Clear the spill files, writing back whatever could not be banked.

    The delete used to be unconditional while _Airtable swallows its own
    failures, so a transient 429 destroyed the only record of money that had
    really been spent. Anything that failed to bank survives to the next drain.
    """
    for path in _spill_files():
        try:
            os.remove(path)
        except OSError:
            pass
    if not keep:
        return
    try:
        with open(SPILL_FILE, "a") as fh:
            for r in keep:
                fh.write(json.dumps(r) + "\n")
    except Exception as e:
        _say(f"could not keep {len(keep)} unbanked record(s) ({e})")


def record_usage(resp, model=None):
    """Route one response's usage to the active run, or spill it to disk.

    Jobs whose Run Log row is managed by the CLI (start in one process, finish in
    another) have no in-process OpsLog, so the wrapper appends to a spill file
    that `ops_log.py finish` sums into the row and removes. Never raises.
    """
    if _CURRENT is not None:
        _CURRENT.record_usage(resp, model)
        return
    try:
        usage = getattr(resp, "usage", resp)
        row = {"model": model or getattr(resp, "model", None) or "unknown",
               "in": getattr(usage, "input_tokens", 0) or 0,
               "out": getattr(usage, "output_tokens", 0) or 0,
               "cache_r": getattr(usage, "cache_read_input_tokens", 0) or 0,
               "cache_w": getattr(usage, "cache_creation_input_tokens", 0) or 0,
               # Stamped so a drain can bank each call to the day it was
               # actually spent. Without this the hourly drain credits
               # everything to the day it happened to run, which quietly
               # misattributes spend across a date boundary — exactly the bug
               # that put an August threshold email into September.
               "at": _iso(_now())}
        with open(SPILL_FILE, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as e:
        _say(f"usage not spilled ({e})")


def _at_utc(r):
    """When a spilled record was written, in UTC. None if it predates the stamp."""
    at = r.get("at")
    if not at:
        return None
    try:
        return dt.datetime.fromisoformat(str(at).replace("Z", "+00:00")) \
                 .astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _drain_spill(since=None):
    """Claim the finishing run's own spilled usage. Its row's fields, or None.

    Only records stamped at or after `since` are claimed. The spill file is
    shared by every working copy on the machine, so an unwindowed drain let one
    run's row swallow another checkout's spend — and a run finishing just after
    midnight UTC swallow the previous day's. Whatever is not claimed stays on
    disk for drain_to_rollup() to bank against the day it was really spent,
    instead of being charged to this run.
    """
    rows = _read_spill()
    if not rows:
        return None
    mine, keep = [], []
    for r in rows:
        when = _at_utc(r)
        if since is not None and (when is None or when < since):
            keep.append(r)
        else:
            mine.append(r)
    _rewrite_spill(keep)
    return _sum_rows(mine) if mine else None


def _spill_by_day():
    """Spill records grouped by the UTC day they were spent.

    UTC because Anthropic buckets cost by UTC day, and the whole point of this
    file is to reconcile against their number.
    """
    out = {}
    for r in _read_spill():
        when = _at_utc(r)
        # Records written before timestamps existed keep their own "undated"
        # batch — _bank_day gives those a row of their own rather than charging
        # them to whichever day the drain happens to run on.
        out.setdefault(when.strftime("%Y-%m-%d") if when else "undated",
                       []).append(r)
    return out


def _sum_rows(rows):
    calls, tin, tout, cost, models = 0, 0, 0, 0.0, {}
    for r in rows:
        rate_in, rate_out, rate_cache = _llm_rate(r["model"])
        calls += 1
        tin += r["in"] + r["cache_w"] + r["cache_r"]
        tout += r["out"]
        cost += (r["in"] * rate_in + r["cache_w"] * rate_in * 1.25
                 + r["cache_r"] * rate_cache + r["out"] * rate_out) / 1e6
        key = r["model"].rsplit("-2", 1)[0]
        models[key] = models.get(key, 0) + 1
    return {"LLM Calls": calls, "LLM Tokens In": tin, "LLM Tokens Out": tout,
            "LLM Cost (USD)": round(cost, 4),
            "LLM Models": ", ".join(f"{m} x{n}" for m, n in sorted(models.items()))}


def drain_to_rollup(job_key="adhoc_local", job_name="Ad-hoc & manual runs",
                    token=None):
    """Bank whatever is sitting in the spill file to a daily rollup row.

    record_usage() spills to disk when there is no active OpsLog. Nothing ran
    `ops_log.py finish` for a script started by hand — and nothing ran this on a
    schedule either, despite the comments calling it hourly — so the spill just
    grew: on 2026-09-04 the zm-ai-agent working copy
    held **83 undrained records, 4.17M input tokens of Opus 5**, every one of
    them billed and none of them attributed. That was most of the "unattributed"
    spend on 2026-09-02 that we first blamed on Claude Code — wrongly, since
    Claude Code runs on the claude.ai team subscription and never touches the
    API key at all.

    Attributed to its own pseudo-job rather than folded into whatever ran next,
    because "development and manual runs" is a real, useful category to see on
    the spend report — not noise to hide inside another job's number.
    """
    by_day = _spill_by_day()
    if not by_day:
        return None
    at = _Airtable(token or os.environ.get("AIRTABLE_TOKEN", ""))
    if not at.ok:
        _say("cannot bank the spill — no AIRTABLE_TOKEN")
        return None
    banked, keep = [], []
    for day, rows in sorted(by_day.items()):
        fields = _bank_day(at, job_name, day, _sum_rows(rows))
        if fields is None:
            # The write was refused and _Airtable already swallowed the reason.
            # Hold these back rather than deleting the only copy of them.
            keep.extend(rows)
        else:
            banked.append(fields)
    _rewrite_spill(keep)
    if keep:
        _say(f"{len(keep)} record(s) could not be banked — kept for the next drain")
    return banked


def _bank_day(at, job_name, day, fields):
    """Bank one day's worth of spill onto that day's rollup row.

    An "undated" batch predates the `at` stamp, so there is no honest way to say
    when it was spent. It used to be relabelled as the day the drain happened to
    run, which charged that day money it never spent: $5.23 of spill from around
    2026-09-02 landed on a row named 2026-09-04, while 2026-09-02 went on showing
    its full $13.52 gap. Now it gets a row of its own — no date in the name, no
    Started stamp — which the spend report leaves out of every day's split. The
    money stays visible and openly unattributed instead of being charged to a day
    at random.
    """
    now = _now()
    dated = day != "undated"
    name = (f"{job_name} — {day} (daily rollup)" if dated
            else f"{job_name} — undated (spill, day unknown)")
    prev = at.find(T_RUNS, f"{{Run}}='{_esc(name)}'")
    if prev:
        pf = prev.get("fields", {})
        models = {}
        for part in (pf.get("LLM Models") or "").split(","):
            m, _, n = part.strip().rpartition(" x")
            if m and n.isdigit():
                models[m] = models.get(m, 0) + int(n)
        for part in (fields.get("LLM Models") or "").split(","):
            m, _, n = part.strip().rpartition(" x")
            if m and n.isdigit():
                models[m] = models.get(m, 0) + int(n)
        merged = {
            "LLM Calls": int(pf.get("LLM Calls") or 0) + fields["LLM Calls"],
            "LLM Tokens In": int(pf.get("LLM Tokens In") or 0) + fields["LLM Tokens In"],
            "LLM Tokens Out": int(pf.get("LLM Tokens Out") or 0) + fields["LLM Tokens Out"],
            "LLM Cost (USD)": round(float(pf.get("LLM Cost (USD)") or 0)
                                    + fields["LLM Cost (USD)"], 4),
            "LLM Models": ", ".join(f"{m} x{n}" for m, n in sorted(models.items())),
            "Finished": _iso(now),
        }
        if not at.update(T_RUNS, prev["id"], merged):
            _say(f"could NOT bank ${fields['LLM Cost (USD)']:.4f} onto {day}")
            return None
        _say(f"banked ${fields['LLM Cost (USD)']:.4f} of ad-hoc usage onto {day}")
        return merged
    row = dict(fields)
    row.update({
        "Run": name, "Job Name": job_name, "Status": "Success",
        "Finished": _iso(now),
        "Platform": _detect_platform(), "Trigger": "Manual",
        "Summary": ("Claude usage from runs that had no OpsLog around them — "
                    "local scripts, one-off backfills, anything started by hand. "
                    "Real money; it just had no job to charge until now."),
    })
    if dated:
        # Dated to the day the tokens were SPENT, not the day we noticed.
        stamp = f"{day}T12:00:00+00:00"
        row["Started"], row["Finished"] = stamp, stamp
    else:
        # No Started at all: the spend report skips a row it cannot date, which
        # is the honest answer. Inventing one charges an innocent day.
        row["Summary"] = ("Claude usage spilled before calls carried a timestamp, "
                          "so the day it was spent cannot be established. Real "
                          "money, deliberately left out of the per-day split "
                          "rather than charged to the day it was found.")
    if not at.create(T_RUNS, row):
        _say(f"could NOT bank ${fields['LLM Cost (USD)']:.4f} to {day}")
        return None
    _say(f"banked ${fields['LLM Cost (USD)']:.4f} of ad-hoc usage to {day}")
    return row


_DRAINED = False


def _drain_local_spill():
    """Bank stranded local usage, once per process. Never raises.

    The drain was only ever described as hourly: no workflow, no cron and no ⏰
    Scheduled Jobs row ever called it, so on the laptop it happened only when
    somebody typed it — twice, ever. Doing it as each run opens means spend from
    ANY working copy is banked by whatever runs next. Free when there is no
    spill file, which is the normal case on a CI runner.
    """
    global _DRAINED
    if _DRAINED or os.environ.get("OPS_NO_DRAIN"):
        return
    _DRAINED = True
    if not _spill_files():
        return
    try:
        drain_to_rollup()
    except Exception as e:
        _say(f"spill drain skipped ({e})")


def instrument(client):
    """Wrap an Anthropic client so every messages.create() reports its tokens to
    the active OpsLog run automatically.

        client = ops_log.instrument(Anthropic(api_key=...))

    Streaming calls do not pass through create(), so a job that uses
    messages.stream() should call ops.record_usage(final_message) itself on the
    stream's final message. Nothing here can break the caller: with no active
    run, the wrapper is a pass-through.
    """
    real_create = client.messages.create

    def create(*args, **kwargs):
        resp = real_create(*args, **kwargs)
        record_usage(resp, kwargs.get("model"))
        return resp

    client.messages.create = create
    return client


class OpsLog:
    """Context manager that records one run of one job."""

    def __init__(self, job_key, job_name=None, platform=None, token=None,
                 log_runs=True):
        """log_runs=False suits high-frequency jobs. The W6 listener polls every
        five minutes, which would be ~5,800 📊 Run Log rows a month for no gain.
        Quiet jobs still stamp Last Success (so the watchdog sees them alive) and
        still file errors — they just don't write a row per uneventful poll."""
        self.job_key = job_key
        self.job_name = job_name or job_key.replace("_", " ").title()
        self.platform = platform or _detect_platform()
        self.trigger = _detect_trigger()
        self.log_runs = log_runs
        self.at = _Airtable(token or TOKEN)
        self.started = _now()
        self.run_id = None
        self.job_rec = None
        self.errors = 0
        self.warnings = 0
        self.notes = []
        self._finished = False
        self.llm = {"calls": 0, "in": 0, "out": 0, "cache": 0,
                    "cost": 0.0, "models": {}}

    # ── lifecycle ─────────────────────────────────────────────────────────
    def __enter__(self):
        global _CURRENT
        _CURRENT = self
        self.start()
        # Which budget this run is spending. The workflows compute it from the
        # same condition that picks the API key, in the clear, so a run says so
        # itself rather than leaving anyone to infer it from a masked secret.
        # Added after three hand-dispatched Fix Queue runs spent $9.36 of that
        # job's monthly ceiling on 2026-09-08 and nothing recorded that they
        # were not the job running itself.
        budget = os.environ.get("SPEND_BUDGET")
        if budget:
            _say(f"spending the {budget.upper()} budget")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self.crash(exc, "".join(traceback.format_exception(exc_type, exc, tb)))
            self.finish(status="Failed")
        else:
            self.finish()
        return False  # never swallow — the caller's behaviour is unchanged

    def start(self):
        # Bank anything an earlier hand-run left in the spill file before this
        # run's own usage lands on top of it.
        _drain_local_spill()
        self.job_rec = self._resolve_job()
        stamp = self.started.astimezone(dt.timezone(dt.timedelta(hours=10)))
        fields = {
            "Run": f"{self.job_name} — {stamp:%Y-%m-%d %H:%M}",
            "Job Name": self.job_name,
            "Status": "Running",
            "Started": _iso(self.started),
            "Platform": self.platform,
            "Trigger": self.trigger,
            "Run URL": _run_url(),
        }
        if self.job_rec:
            fields["Job"] = [self.job_rec]
        if self.log_runs:
            self.run_id = self.at.create(T_RUNS, fields)
        _say(f"run started: {self.job_name} ({self.trigger.lower()})")
        return self

    def finish(self, status=None, summary=None):
        if self._finished:
            return
        self._finished = True
        ended = _now()
        dur = int((ended - self.started).total_seconds())
        if status is None:
            status = "Failed" if self.errors else ("Partial" if self.warnings else "Success")
        text = summary or " · ".join(self.notes[-12:]) or "No detail recorded."
        global _CURRENT
        if _CURRENT is self:
            _CURRENT = None
        if self.run_id:
            fields = {
                "Status": status,
                "Finished": _iso(ended),
                "Duration (s)": dur,
                "Errors": self.errors,
                "Summary": text[:8000],
            }
            if self.llm["calls"]:
                fields.update({
                    "LLM Calls": self.llm["calls"],
                    "LLM Tokens In": self.llm["in"] + self.llm["cache"],
                    "LLM Tokens Out": self.llm["out"],
                    "LLM Cost (USD)": round(self.llm["cost"], 4),
                    "LLM Models": ", ".join(
                        f"{m} x{n}" for m, n in sorted(self.llm["models"].items())),
                })
            self.at.update(T_RUNS, self.run_id, fields)
        elif self.llm["calls"]:
            # No Run Log row to carry it, but real money was spent. Bank it.
            self._rollup_usage(ended)
        if self.job_rec:
            patch = {"Last Status": status}
            if status in ("Success", "Partial"):
                patch["Last Success"] = _iso(ended)
            self.at.update(T_JOBS, self.job_rec, patch)
        _say(f"run {status.lower()} in {dur}s "
             f"({self.errors} error(s), {self.warnings} warning(s))")

    def _rollup_usage(self, ended):
        """Bank LLM usage from a run that writes no Run Log row of its own.

        log_runs=False exists so high-frequency jobs don't flood the Run Log —
        the W6 listener polls every five minutes, which would be ~5,800 rows a
        month for nothing. But it also sent their token spend nowhere, and those
        are the jobs that spend the most: through August 2026 the entire Run Log
        recorded $0.01 across 2,798 runs while the org hit its $100 cap and
        every Claude-powered job stopped. The instrument existed and was wired
        to a row that never got created.

        One row per job per UTC day keeps the Run Log readable and still answers
        "what did this job spend". UTC, not Brisbane: the spend report reads the
        day straight out of this row's name and reconciles it against Anthropic,
        who bill by UTC day, and the ad-hoc spill drain names its rows by UTC
        too — two conventions meant a name like "2026-09-04" could mean either
        of two days, and 12 of 18 rollup rows carried a name that disagreed with
        their own timestamp. Named for the day the run STARTED, which is the day
        the Started stamp carries, so name and stamp cannot drift apart. Only
        reached when there was actual usage, so uneventful polls remain free.
        """
        day = self.started.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
        name = f"{self.job_name} — {day} (daily rollup)"
        prev = self.at.find(T_RUNS, f"{{Run}}='{_esc(name)}'")
        add_in = self.llm["in"] + self.llm["cache"]
        models = dict(self.llm["models"])
        runs = 1

        if prev:
            pf = prev.get("fields", {})
            calls = int(pf.get("LLM Calls") or 0) + self.llm["calls"]
            tin = int(pf.get("LLM Tokens In") or 0) + add_in
            tout = int(pf.get("LLM Tokens Out") or 0) + self.llm["out"]
            cost = float(pf.get("LLM Cost (USD)") or 0) + self.llm["cost"]
            # "opus x3, sonnet x1" round-trips back into counts so the tally
            # keeps growing across the day instead of being overwritten.
            for part in (pf.get("LLM Models") or "").split(","):
                m, _, n = part.strip().rpartition(" x")
                if m and n.isdigit():
                    models[m] = models.get(m, 0) + int(n)
            hit = re.search(r"(\d+) run", pf.get("Summary") or "")
            runs = (int(hit.group(1)) if hit else 0) + 1
        else:
            calls, tin, tout = self.llm["calls"], add_in, self.llm["out"]
            cost = self.llm["cost"]

        fields = {
            "Run": name,
            "Job Name": self.job_name,
            "Status": "Success",
            "Finished": _iso(ended),
            "Platform": self.platform,
            "Trigger": self.trigger,
            "Summary": (f"Daily rollup: {runs} run(s) that used Claude. Runs "
                        f"themselves are not logged individually for this job "
                        f"(log_runs=False); this row exists to attribute spend."),
            "LLM Calls": calls,
            "LLM Tokens In": tin,
            "LLM Tokens Out": tout,
            "LLM Cost (USD)": round(cost, 4),
            "LLM Models": ", ".join(f"{m} x{n}" for m, n in sorted(models.items())),
        }
        if prev:
            self.at.update(T_RUNS, prev["id"], fields)
        else:
            fields["Started"] = _iso(self.started)
            if self.job_rec:
                fields["Job"] = [self.job_rec]
            self.at.create(T_RUNS, fields)
        _say(f"usage banked on the {day} rollup (${cost:.4f} to date)")

    # ── recording ─────────────────────────────────────────────────────────
    def record_usage(self, resp, model=None):
        """Count one API response's tokens against this run.

        Accepts a Message (create), a final message from a stream, or a bare
        usage object. Never raises: a malformed response loses one data point,
        not the job.
        """
        try:
            usage = getattr(resp, "usage", resp)
            model = model or getattr(resp, "model", None) or "unknown"
            tin = getattr(usage, "input_tokens", 0) or 0
            tout = getattr(usage, "output_tokens", 0) or 0
            cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
            rate_in, rate_out, rate_cache = _llm_rate(model)
            self.llm["calls"] += 1
            self.llm["in"] += tin + cache_w
            self.llm["out"] += tout
            self.llm["cache"] += cache_r
            # cache writes bill at 1.25x input; close enough at list rates
            self.llm["cost"] += (tin * rate_in + cache_w * rate_in * 1.25
                                 + cache_r * rate_cache + tout * rate_out) / 1e6
            key = (model or "unknown").rsplit("-2", 1)[0]
            self.llm["models"][key] = self.llm["models"].get(key, 0) + 1
        except Exception as e:
            _say(f"usage not recorded ({e})")

    def step(self, msg):
        """A progress note. Ends up in the Run Log summary, searchable later."""
        self.notes.append(str(msg))
        _say(msg)

    def warn(self, msg):
        """Degraded but survivable. Marks the run Partial, logs a Warning."""
        self.warnings += 1
        self.notes.append(f"⚠ {msg}")
        self._log_error(str(msg), "", "Warning")

    def error(self, exc, context=""):
        """A caught failure mid-run. The run continues; the error is filed."""
        self.errors += 1
        msg = f"{context}: {exc}" if context else str(exc)
        self.notes.append(f"✗ {msg}")
        tb = traceback.format_exc() if sys.exc_info()[0] else ""
        self._log_error(msg, tb, "Error")

    def crash(self, exc, tb=""):
        """The job died. Logged as Crash, which the digest always surfaces."""
        self.errors += 1
        self._log_error(str(exc), tb or traceback.format_exc(), "Crash")

    def _resolve_job(self):
        """Find this job's row in ⏰ Scheduled Jobs, by stable key then by name."""
        rec = self.at.find(T_JOBS, f"{{Job Key}}='{_esc(self.job_key)}'")
        if not rec:
            rec = self.at.find(T_JOBS, f"LOWER({{Job}})=LOWER('{_esc(self.job_name)}')")
        if not rec:
            _say(f"no ⏰ Scheduled Jobs row matched '{self.job_key}' — "
                 f"logging unlinked (set its Job Key to fix)")
            return None
        return rec["id"]

    def _log_error(self, message, tb, severity):
        """Upsert into 🚨 Error Log, deduplicated on fingerprint."""
        exc_type = ""
        if tb:
            last = [l for l in tb.strip().splitlines() if l and not l.startswith(" ")]
            if last:
                exc_type = last[-1].split(":")[0].strip()[:80]
        fp = fingerprint(self.job_key, exc_type, message)
        transient, action = classify(message, tb)
        now = _iso(_now())

        # Cut at a WORD BOUNDARY, and at a sentence if there is one inside the
        # budget. A hard [:90] ended a live fault headline on "…its feed is",
        # which reads as a broken page rather than a clipped string. Danny:
        # "ends abruptly at 'its feed is' ... not helpful."
        flat = re.sub(r"\s+", " ", str(message)).strip()
        title = f"{self.job_name} — {_clip_words(flat, 150)}"

        existing = self.at.find(T_ERRORS, f"{{Fingerprint}}='{fp}'")
        if existing:
            prev = existing.get("fields", {})
            count = int(prev.get("Occurrences") or 0) + 1
            patch = {
                "Occurrences": count,
                "Last Seen": now,
                "Error Message": str(message)[:8000],
                "Run URL": _run_url(),
            }
            # Keep the Title in step with the message. It used to be written
            # once at creation and never again, so a row could say one thing in
            # its Title and another in its Error Message — the Monthly KPI row
            # read "finished with status 'failure'" for six days while its
            # message said 'cancelled'. The Title is what the digest quotes and
            # what you scan the grid by, so a stale one is a lie in the most
            # read field.
            #
            # Only refresh a Title still in machine shape ("<job> — <message>").
            # A human who has renamed a row keeps their name.
            if str(prev.get("Title") or "").startswith(f"{self.job_name} — "):
                patch["Title"] = title
            # A resolved error that fires again is not resolved — and neither is
            # a "Fixed" one. See REOPENABLE for why that distinction matters.
            patch.update(reopen_fields(prev, _now()))
            if tb:
                patch["Traceback"] = tb[-8000:]
            if action and not prev.get("Action Required"):
                patch["Action Required"] = action
            self.at.update(T_ERRORS, existing["id"], patch)
            _say(f"error recurred ×{count}: {str(message)[:90]}")
            return existing["id"]

        fields = {
            "Title": title,
            "Severity": severity,
            "Status": "New",
            "Occurrences": 1,
            "First Seen": now,
            "Last Seen": now,
            "Job Name": self.job_name,
            "Platform": self.platform,
            "Transient?": transient,
            "Error Message": str(message)[:8000],
            "Traceback": (tb or "")[-8000:],
            "Fingerprint": fp,
            "Run URL": _run_url(),
        }
        if action:
            fields["Action Required"] = action
        if self.job_rec:
            fields["Job"] = [self.job_rec]
        rid = self.at.create(T_ERRORS, fields)
        _say(f"error logged [{severity}/{transient}]: {str(message)[:90]}")
        return rid


# ── CLI, for workflows that bracket a run from outside Python ────────────────
#
# A GitHub workflow opens a run right after checkout and closes it in an
# `if: always()` step, so the outcome is recorded whether the job succeeded,
# crashed, or was cancelled — including crashes that happen in a shell step
# where no Python ever ran:
#
#   - run: python3 ops_log.py start --job my_job >> "$GITHUB_ENV"
#   - if: always()
#     run: python3 ops_log.py finish --job my_job --status ${{ job.status }}
#
STATUS_MAP = {"success": "Success", "failure": "Failed", "cancelled": "Timed Out"}


def _cli():
    p = argparse.ArgumentParser(description="Report a job run to the Run / Error Log.")
    p.add_argument("mode", choices=["start", "finish", "crash", "drain"])
    # Not required for `drain`, which banks to its own pseudo-job by design.
    p.add_argument("--job", default="", help="Job Key from ⏰ Scheduled Jobs")
    p.add_argument("--name", default="", help="Human job name (falls back to the key)")
    p.add_argument("--status", default="failure", help="success | failure | cancelled")
    p.add_argument("--run-id", default="", help="Run Log record id from `start`")
    p.add_argument("--message", default="")
    p.add_argument("--log-tail", default="", help="Last few KB of the run log")
    args = p.parse_args()

    ops = OpsLog(args.job, job_name=args.name or None)

    if args.mode != "drain" and not args.job:
        p.error("--job is required for start, finish and crash")

    if args.mode == "drain":
        # Nothing to do is the normal case and must stay silent, or an hourly
        # cron entry becomes an hourly line of noise in the log.
        drain_to_rollup()
        return 0

    if args.mode == "start":
        ops.start()
        # These workflows open their Run Log through the CLI rather than the
        # context manager, so the budget marker has to be printed here too.
        budget = os.environ.get("SPEND_BUDGET")
        if budget:
            _say(f"spending the {budget.upper()} budget")
        # Printed in KEY=value form so the caller can pipe it into $GITHUB_ENV.
        print(f"OPS_RUN_ID={ops.run_id or ''}")
        return 0

    ops.job_rec = ops._resolve_job()

    if args.mode == "crash":
        ops._log_error(args.message or "Job crashed with no message captured.",
                       args.log_tail, "Crash")
        return 0

    # finish — close the row opened by `start`, or fall back to the newest
    # Running row for this job if the env var was lost between steps.
    status = STATUS_MAP.get(args.status.strip().lower(), "Failed")
    rec = None
    if args.run_id:
        rec = ops.at.get(T_RUNS, args.run_id)
    if not rec:
        rec = ops.at.find(
            T_RUNS, f"AND({{Job Name}}='{_esc(ops.job_name)}',{{Status}}='Running')")
    if rec:
        ops.run_id = rec["id"]
        started = (rec.get("fields") or {}).get("Started")
        if started:
            try:
                ops.started = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
            except ValueError:
                pass
        # Claim only what was spilled after this run opened. The file is shared
        # machine-wide, so anything older belongs to some other run and often to
        # another day; a minute of slack absorbs clock skew between this machine
        # and the Started stamp Airtable handed back.
        spill = _drain_spill(since=ops.started - dt.timedelta(minutes=1))
        if spill:
            ops.at.update(T_RUNS, ops.run_id, spill)
        # Anything left over predates this run: bank it against its own day
        # rather than leave it to die with an ephemeral runner.
        drain_to_rollup()

    if status != "Success":
        msg = args.message or f"Workflow finished with status '{args.status}'."
        # "recalc.py exited 1" is a useless title. If the captured log ends in a
        # traceback, lead with the actual exception instead — that is what makes
        # the row scannable and what the fingerprint should key on.
        cause = _exception_from_log(args.log_tail)
        if cause:
            msg = f"{cause} — {msg}"
        ops._log_error(msg, args.log_tail, "Crash")
        ops.errors = 1

    ops.finish(status=status,
               summary=args.message or f"Workflow reported '{args.status}'.")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
