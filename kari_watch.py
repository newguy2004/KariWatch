#!/usr/bin/env python3
"""
K-ARI slot watcher.

Checks the 3 Highland, UT locations (Highland, VIP, Pink Lounge) for openings that fit
your group, and sends a Telegram message the moment a NEW slot appears (a cancellation
or a freshly released time). It never books anything: you tap the link and book yourself.

You can also message your bot to check on demand:
  /check 3                 everything open now for 3 people
  /check 2                 same for 2 people
  /check 4 exosome         a different facial (signature, collagen, peptide, exosome, acne)
  /check 3 14              look 14 days ahead instead of the default
  /help                    shows these commands

Uses only the Python standard library. Python 3.9+.

Usage:
  python3 kari_watch.py                 # run forever: auto-checks + listens for commands
  python3 kari_watch.py --once          # one check, then exit (for cron / Task Scheduler)
  python3 kari_watch.py --cloud         # one short run for GitHub Actions (answers commands + checks)
  python3 kari_watch.py --find-chat-id  # after you message your bot, prints your chat id
  python3 kari_watch.py --test-alert    # sends a test Telegram message

Run only ONE copy at a time per bot, or Telegram will complain about conflicts.
"""
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ───────────────────────── SETTINGS ─────────────────────────
SERVICE = "signature"        # signature | collagen | peptide | exosome | acne
GUEST_COUNTS = [3, 2]        # automatic alerts look for room for 3 first, then 2
DAYS_AHEAD = 7               # "this week" = today through 7 days out
CHECK_EVERY_MIN = 5          # polite polling interval for automatic alerts

# Fill these in, or set them as environment variables of the same name.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# ────────────────────────────────────────────────────────────

BASE = "https://www.k-ari.com"
MT = ZoneInfo("America/Denver")
STATE_FILE = Path(__file__).with_name("kari_seen.json")

# Public IDs taken from the site's own config.js
LOCATIONS = {
    "Highland": ("highland", "LC6EP1N4VM7D1"),
    "VIP": ("vip", "L9RXRWGA82AH7"),
    "Pink Lounge": ("lounge", "LTYH6840X1MX8"),
}
SERVICES = {  # name: (square service variation id, minutes)
    "signature": ("JCO2YG6VPCFBXXIPUQO36HT4", 35),
    "collagen": ("RLWPYAWXSSRNYQEYYRZ4BJUG", 35),
    "peptide": ("NG7VJMBVW6BX5BN7C4CQKCT4", 35),
    "exosome": ("6AY2FGWEL7EBVJUWEZCGBQ7V", 45),
    "acne": ("HG3FCPRG4MPI7CPXQ32V4ZW2", 45),
}
HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (personal slot watcher)"}

HELP_TEXT = (
    "K-ARI watcher commands:\n"
    "/check 3  - everything open now for 3 people\n"
    "/check 2  - same for 2 people (any number 1-10)\n"
    "/check 4 exosome  - pick a facial: signature, collagen, peptide, exosome, acne\n"
    "/check 3 14  - look 14 days ahead (default " + str(DAYS_AHEAD) + ")\n"
    "New openings are also sent to you automatically."
)


def http_json(url, payload=None, timeout=25):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def tg(method, params, timeout=15):
    return http_json(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", params, timeout=timeout)


def fetch_slots(square_loc_id, guests, service=None, days=None):
    """Return the set of start_at strings (UTC ISO) open for `guests` people, or None on error."""
    svc_id, minutes = SERVICES[service or SERVICE]
    seg = {"serviceVariationId": svc_id, "baseDurationMinutes": minutes,
           "addonMinutes": 0, "durationMinutes": minutes}
    today = datetime.now(MT).date()
    body = {"locationId": square_loc_id,
            "startDate": today.isoformat(),
            "endDate": (today + timedelta(days=days or DAYS_AHEAD)).isoformat(),
            "segments": [seg] * guests}
    try:
        res = http_json(f"{BASE}/api/availability", body, timeout=15)
    except Exception as e:
        print(f"  ! request failed ({square_loc_id}, {guests} guests): {e}")
        return None
    if not isinstance(res, list):
        print(f"  ! unexpected response ({square_loc_id}): {str(res)[:120]}")
        return None
    now = datetime.now(MT)
    out = set()
    for a in res:
        start = a.get("start_at")
        if not start:
            continue
        dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(MT)
        if dt > now:
            out.add(start)
    print(f"  {square_loc_id} for {guests}: {len(out)} open", flush=True)
    return out


def slot_link(loc_key, start_utc, guests):
    dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00")).astimezone(MT)
    q = urllib.parse.urlencode({"loc": loc_key, "date": dt.strftime("%Y-%m-%d"),
                                "time": dt.strftime("%H:%M"), "guests": guests})
    return f"{BASE}/booking.html?{q}"


def pretty(start_utc):
    dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00")).astimezone(MT)
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%a %b')} {dt.day}, {hour}:{dt.strftime('%M %p')}"


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[no Telegram configured, printing instead]\n" + text + "\n")
        return
    try:
        tg("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True})
    except Exception as e:
        print(f"  ! Telegram send failed: {e}")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None  # None = first run


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def check_once():
    """Automatic check: alerts only on slots that were not open last time."""
    prev = load_state()
    first_run = prev is None
    prev = prev or {}
    state = {}
    ok_any = False
    alerts = []  # (loc_name, loc_key, guests, start)

    for name, (key, sq_id) in LOCATIONS.items():
        found_higher = set()
        for g in sorted(GUEST_COUNTS, reverse=True):
            slots = fetch_slots(sq_id, g)
            sk = f"{key}|{g}"
            if slots is None:            # request failed: keep what we knew, don't re-alert later
                state[sk] = prev.get(sk, [])
                continue
            ok_any = True
            only_this = slots - found_higher   # a slot for 3 already covers 2
            state[sk] = sorted(only_this)
            for s in sorted(only_this - set(prev.get(sk, []))):
                alerts.append((name, key, g, s))
            found_higher |= slots
        time.sleep(1.5)

    fails = 0 if ok_any else int(prev.get("_fails", 0)) + 1
    state["_fails"] = fails
    if fails == 6:
        send_telegram("K-ARI watcher: the last 6 checks all failed. The site may be down or blocking "
                      "this connection. I'll keep trying and go quiet until it works again.")
    save_state(state)
    total = sum(len(v) for k, v in state.items() if not k.startswith("_"))
    print(f"{datetime.now(MT):%a %H:%M} checked: {total} open slots, {len(alerts)} new")

    if alerts:
        lines = [f"K-ARI openings ({SERVICE}){' [first check: everything currently open]' if first_run else ''}"]
        for name, key, g, s in alerts[:8]:
            lines.append(f"\n{name}: fits {g}  {pretty(s)}\n{slot_link(key, s, g)}")
        if len(alerts) > 8:
            lines.append(f"\n+{len(alerts) - 8} more at {BASE}/booking.html")
        send_telegram("\n".join(lines))


# ───────────────────── on-demand commands ─────────────────────
def parse_command(text):
    """Return 'help', 'usage', None (not a command), or (guests, service, days)."""
    parts = (text or "").strip().lower().split()
    if not parts:
        return None
    head = parts[0].split("@")[0].lstrip("/")
    if head in ("help", "start"):
        return "help"
    if head != "check":
        return None
    guests = days = None
    service = None
    for p in parts[1:]:
        if p in SERVICES:
            service = p
        elif p.isdigit():
            if guests is None:
                guests = int(p)
            elif days is None:
                days = int(p)
    if guests is None or not 1 <= guests <= 10:
        return "usage"
    days = min(max(days or DAYS_AHEAD, 1), 60)
    return guests, service or SERVICE, days


def check_now(guests, service, days):
    """Everything open right now for `guests` people (not just new slots)."""
    lines, total, shown = [], 0, 0
    for name, (key, sq_id) in LOCATIONS.items():
        slots = fetch_slots(sq_id, guests, service, days)
        if slots is None:
            lines.append(f"\n{name}: couldn't check (site error), try again in a minute")
        elif not slots:
            lines.append(f"\n{name}: nothing open")
        else:
            lines.append(f"\n{name}: {len(slots)} open")
            for s in sorted(slots):
                total += 1
                if shown < 10:
                    shown += 1
                    lines.append(f"  {pretty(s)}\n  {slot_link(key, s, guests)}")
        time.sleep(1)
    if total > shown:
        lines.append(f"\n+{total - shown} more at {BASE}/booking.html")
    head = f"Open now: {service}, {guests} guest{'s' if guests != 1 else ''}, next {days} days"
    return head + "\n" + "\n".join(lines)


def handle_message(text):
    cmd = parse_command(text)
    if cmd is None:
        return
    if cmd == "help":
        send_telegram(HELP_TEXT)
    elif cmd == "usage":
        send_telegram("Tell me how many people, like /check 3\n\n" + HELP_TEXT)
    else:
        guests, service, days = cmd
        send_telegram(f"Checking {guests} guest{'s' if guests != 1 else ''}...")
        send_telegram(check_now(guests, service, days))


def poll_commands(offset, wait):
    """Wait up to `wait` seconds for Telegram messages; answer any from YOUR chat only."""
    res = tg("getUpdates", {"offset": offset, "timeout": wait, "allowed_updates": ["message"]},
             timeout=wait + 15)
    for u in res.get("result", []):
        offset = max(offset, u["update_id"] + 1)
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
            continue  # ignore anyone else who finds the bot
        try:
            handle_message(m.get("text", ""))
        except Exception as e:
            print(f"  ! command failed: {e}")
            send_telegram("Something went wrong running that. Try again in a minute.")
    return offset


def answer_pending_commands():
    """For cloud runs: answer any /check messages that arrived since the last run."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        ups = tg("getUpdates", {"timeout": 0, "allowed_updates": ["message"]}).get("result", [])
    except Exception as e:
        print(f"  ! couldn't read Telegram messages: {e}")
        return
    if not ups:
        return
    try:  # confirm them first so a slow reply can never cause the same command to run twice
        tg("getUpdates", {"offset": ups[-1]["update_id"] + 1, "timeout": 0})
    except Exception as e:
        print(f"  ! couldn't confirm Telegram messages: {e}")
    cutoff = time.time() - 3600  # ignore anything older than an hour
    for u in ups:
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
            continue
        if m.get("date", 0) < cutoff:
            continue
        try:
            handle_message(m.get("text", ""))
        except Exception as e:
            print(f"  ! command failed: {e}")


class Deadline(BaseException):
    """Raised by the safety timer; BaseException so no 'except Exception' can swallow it."""


def cloud_run():
    """One short run for GitHub Actions: answer pending commands, then check for new slots."""
    import signal

    def _stop(signum, frame):
        raise Deadline("cloud run exceeded 200 seconds")

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _stop)
        signal.alarm(200)
    print("cloud run: reading Telegram commands", flush=True)
    answer_pending_commands()
    print("cloud run: checking K-ARI", flush=True)
    check_once()
    print("cloud run: done", flush=True)


def find_chat_id():
    if not TELEGRAM_BOT_TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN first, then send your bot any message in Telegram.")
    res = http_json(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates")
    ids = {u["message"]["chat"]["id"] for u in res.get("result", []) if "message" in u}
    print("Chat id(s):", ", ".join(map(str, ids)) or "none yet. Message your bot first, then rerun.")


def run_forever():
    listening = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    print(f"Watching K-ARI every ~{CHECK_EVERY_MIN} min"
          f"{' and listening for /check commands' if listening else ''}. Ctrl+C to stop.")
    offset = 0
    if listening:
        try:  # skip any old messages that piled up while the script was off
            r = tg("getUpdates", {"offset": -1, "timeout": 0}).get("result", [])
            if r:
                offset = r[-1]["update_id"] + 1
        except Exception as e:
            print(f"  ! couldn't reach Telegram: {e}")
        send_telegram("K-ARI watcher is running. Send /check 3 any time, or /help.")
    next_check = 0.0
    while True:
        now = time.time()
        if now >= next_check:
            try:
                check_once()
            except Exception as e:
                print(f"  ! check failed: {e}")
            next_check = time.time() + CHECK_EVERY_MIN * 60 + random.randint(-30, 30)
            continue
        wait = int(max(1, min(25, next_check - now)))
        if listening:
            try:
                offset = poll_commands(offset, wait)
            except Exception as e:
                print(f"  ! command poll failed: {e}")
                time.sleep(5)
        else:
            time.sleep(wait)


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)  # so logs survive if the run is killed
    except Exception:
        pass
    args = set(sys.argv[1:])
    if "--find-chat-id" in args:
        return find_chat_id()
    if "--test-alert" in args:
        return send_telegram("K-ARI watcher test: alerts are working.")
    if "--cloud" in args:
        return cloud_run()
    if "--once" in args:
        return check_once()
    run_forever()


if __name__ == "__main__":
    main()
