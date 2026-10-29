#!/usr/bin/env python3
"""Piletivalvur – jälgib Eesti Draamateatri mängukava ja uudiseid.

Teavitab (Maci teavitus + ntfy push), kui
  * uudistes teatatakse piletimüügi algusest (+ meeldetuletus eelmisel õhtul ja 15 min enne),
  * mängukavva lisandub etendusi / avaneb uue perioodi müük,
  * välja müüdud etendusele vabaneb pileteid.

Ainult standardteek, töötab macOS-i /usr/bin/python3 (3.9) peal ja GitHub Actionsis.
Pilves annavad seaded keskkonnamuutujad PILETIVALVUR_CONFIG (seadete fail) ja
PILETIVALVUR_NTFY_TOPIC (ntfy teema, et see ei satuks avalikku reposse).
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("PILETIVALVUR_CONFIG") or BASE / "config.json")
STATE_PATH = BASE / "state.json"
LOG_PATH = BASE / "piletivalvur.log"

KAVA_URL = "https://www.draamateater.ee/mangukava/"
NEWS_URL = "https://www.draamateater.ee/wp-json/wp/v2/posts?per_page=10&_fields=id,date,title,link,excerpt"
UA = "Piletivalvur/1.0 (isiklik piletiteavitus)"

DEFAULT_CONFIG = {
    "mac_notifications": True,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "watch_titles": [],          # tühi = kõik lavastused; muidu alamstringid, nt ["Hamlet", "Mets"]
    "quiet_hours": ["00:00", "07:00"],
    "batch_threshold": 5,        # nii mitu uut etendust korraga = üks koondteavitus
    "renotify_minutes": 60,      # sama etenduse vabanemisest ei teavita tihedamini
}

MONTH_STEMS = [
    ("jaanuar", 1), ("veebruar", 2), ("märts", 3), ("aprill", 4), ("mai", 5), ("juun", 6),
    ("juul", 7), ("august", 8), ("septemb", 9), ("oktoob", 10), ("novemb", 11), ("detsemb", 12),
]
MONTH_NAMES = ["", "jaanuar", "veebruar", "märts", "aprill", "mai", "juuni", "juuli",
               "august", "september", "oktoober", "november", "detsember"]
SALE_WORDS = re.compile(r"piletim[üu]{2}k|m[üu]{2}ki|m[üu]{2}gile|lisaetendus", re.I)
DATE_RE = re.compile(r"(\d{1,2})\.\s*(" + "|".join(s for s, _ in MONTH_STEMS) + r")\w*", re.I)
TIME_RE = re.compile(r"\bkl(?:\.|ell)?\s*(\d{1,2})(?:[.:](\d{2}))?")


# --- abifunktsioonid -------------------------------------------------------

def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n"
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 500_000:
            LOG_PATH.write_text(LOG_PATH.read_text(encoding="utf-8")[-250_000:], encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    sys.stdout.write(line)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_config():
    cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_PATH, {})}
    if os.environ.get("PILETIVALVUR_NTFY_TOPIC"):
        cfg["ntfy_topic"] = os.environ["PILETIVALVUR_NTFY_TOPIC"]
    return cfg


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ").strip()


def in_quiet_hours(cfg, now):
    start, end = cfg.get("quiet_hours") or [None, None]
    if not start or not end:
        return False
    cur = now.strftime("%H:%M")
    return start <= cur < end if start < end else (cur >= start or cur < end)


def watched(cfg, title):
    titles = cfg.get("watch_titles") or []
    return not titles or any(t.lower() in title.lower() for t in titles)


def fmt_perf(p):
    d = dt.date.fromisoformat(p["date"])
    wd = "ETKNRLP"[d.weekday()]
    venue = f", {p['venue']}" if p.get("venue") else ""
    return f"{p['title']} – {wd} {d.day}.{d.month} kl {p['time']}{venue}"


# --- teavitused ------------------------------------------------------------

def notify(cfg, title, message, url=None, priority=4):
    log(f"TEAVITUS: {title} | {message}" + (f" | {url}" if url else ""))
    if cfg.get("mac_notifications", True):
        try:
            subprocess.run(
                ["osascript", "-e", "on run argv",
                 "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
                 "-e", "end run", title, message],
                check=False, timeout=15, capture_output=True)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"Maci teavitus ebaõnnestus: {e}")
    topic = cfg.get("ntfy_topic")
    if topic:
        body = {"topic": topic, "title": title, "message": message,
                "priority": priority, "tags": ["performing_arts"]}
        if url:
            body["click"] = url
        try:
            req = urllib.request.Request(
                cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/"),
                data=json.dumps(body).encode("utf-8"),
                headers={"User-Agent": UA, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
        except OSError as e:
            log(f"ntfy teavitus ebaõnnestus: {e}")


# --- mängukava -------------------------------------------------------------

def parse_kava(page, today):
    """Tagastab {võti: etendus}. Võti = 'YYYY-MM-DD HH:MM Pealkiri'."""
    perfs = {}
    year, prev_month, day, month = today.year, None, None, None
    for row in re.findall(r"<tr>(.*?)</tr>", page, re.S):
        m = re.search(r'date--short"[^>]*>\s*<span>[^<]*</span>\s*(\d{1,2})\.(\d{1,2})', row)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            if prev_month is None and month < today.month:
                year += 1
            elif prev_month is not None and month < prev_month:
                year += 1
            prev_month = month
        cell = re.search(r'cell--title">(.*?)</td>', row, re.S)
        if not cell or day is None:
            continue
        title = strip_tags(cell.group(1).split("<p", 1)[0])
        time_m = re.search(r'cell--time">\s*([^<]*)<', row)
        venue_m = re.search(r'cell--location">\s*([^<]*)<', row)
        desc_m = re.search(r'performances-table__description">(.*?)</p>', row, re.S)
        buy_m = re.search(r'class="timetable-button"\s+href="([^"]+)"', row)
        if buy_m:
            status, url = "buy", html.unescape(buy_m.group(1))
        elif "Piletid on otsas" in row:
            status, url = "soldout", None
        else:
            status, url = "other", None
        p = {
            "date": dt.date(year, month, day).isoformat(),
            "time": time_m.group(1).strip() if time_m else "",
            "title": title,
            "venue": venue_m.group(1).strip() if venue_m else "",
            "desc": strip_tags(desc_m.group(1)) if desc_m else "",
            "status": status,
            "url": url,
        }
        perfs[f"{p['date']} {p['time']} {title}"] = p
    return perfs


def month_span(perfs):
    months = sorted({p["date"][:7] for p in perfs})
    names = [f"{MONTH_NAMES[int(m[5:])]} {m[:4]}" for m in (months[0], months[-1])]
    return names[0] if months[0] == months[-1] else f"{names[0]} – {names[1]}"


def check_kava(cfg, state, now):
    page = fetch(KAVA_URL)
    perfs = parse_kava(page, now.date())
    if not perfs:
        last = state.get("parse_alert_at")
        if not last or now - dt.datetime.fromisoformat(last) > dt.timedelta(hours=24):
            notify(cfg, "Piletivalvur vajab hoolitsust",
                   "Mängukavast ei leitud ühtegi etendust – lehe ülesehitus võis muutuda.", KAVA_URL, 3)
            state["parse_alert_at"] = now.isoformat()
        return
    old = state.get("perfs")
    state["perfs"] = perfs
    if old is None:
        log(f"Esimene kontroll: {len(perfs)} etendust, {sum(p['status'] == 'soldout' for p in perfs.values())} välja müüdud")
        return

    added = [p for k, p in perfs.items() if k not in old and p["date"] >= now.date().isoformat()]
    opened = [p for k, p in perfs.items() if k in old and old[k]["status"] == "other" and p["status"] == "buy"]
    freed = [(k, p) for k, p in perfs.items() if k in old and old[k]["status"] == "soldout" and p["status"] == "buy"]

    new_on_sale = added + opened
    if len(new_on_sale) >= cfg.get("batch_threshold", 5):
        buy = sum(p["status"] == "buy" for p in new_on_sale)
        notify(cfg, "Draamateater: uued etendused müügis!",
               f"Mängukavva lisandus {len(new_on_sale)} etendust ({month_span(new_on_sale)}), "
               f"neist {buy} kohe müügis.", KAVA_URL, 5)
    else:
        for p in new_on_sale:
            if not watched(cfg, p["title"]):
                continue
            label = "müügis" if p["status"] == "buy" else ("piletid juba otsas" if p["status"] == "soldout" else "müük pole veel alanud")
            notify(cfg, "Draamateater: uus etendus", f"{fmt_perf(p)} ({label})", p["url"] or KAVA_URL,
                   4 if p["status"] == "buy" else 3)

    notified = state.setdefault("freed_notified", {})
    for k, p in freed:
        if not watched(cfg, p["title"]):
            continue
        last = notified.get(k)
        if last and now - dt.datetime.fromisoformat(last) < dt.timedelta(minutes=cfg.get("renotify_minutes", 60)):
            continue
        notified[k] = now.isoformat()
        notify(cfg, "Vabanes pileteid!", fmt_perf(p), p["url"], 5)
    today = now.date().isoformat()
    state["freed_notified"] = {k: v for k, v in notified.items() if k[:10] >= today}


# --- uudised ---------------------------------------------------------------

def parse_sale_time(text, posted):
    """Leiab lausest, mis räägib müügist, müügi alguse aja. None, kui ei leia."""
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-ZÕÄÖÜ„])", text):
        if not SALE_WORDS.search(sentence):
            continue
        dm = DATE_RE.search(sentence)
        if not dm:
            continue
        month = next(n for s, n in MONTH_STEMS if dm.group(2).lower().startswith(s))
        tm = TIME_RE.search(sentence)
        hour, minute = (int(tm.group(1)), int(tm.group(2) or 0)) if tm else (10, 0)
        year = posted.year + (1 if month < posted.month else 0)
        try:
            return dt.datetime(year, month, int(dm.group(1)), hour, minute)
        except ValueError:
            return None
    return None


def check_news(cfg, state, now):
    posts = json.loads(fetch(NEWS_URL))
    first_run = "seen_news" not in state
    seen = set(state.get("seen_news", []))
    reminders = state.setdefault("reminders", {})
    for post in posts:
        pid = str(post["id"])
        if pid in seen:
            continue
        seen.add(pid)
        title = strip_tags(post["title"]["rendered"])
        excerpt = strip_tags(post["excerpt"]["rendered"])
        if not SALE_WORDS.search(title + " " + excerpt):
            continue
        # sissejuhatus enne pealkirja: pealkirjas on vahel hoopis etenduse kuupäev
        sale = parse_sale_time(excerpt + " " + title, dt.datetime.fromisoformat(post["date"]))
        if sale and sale > now:
            reminders[pid] = {"title": title, "sale": sale.isoformat(), "link": post["link"], "sent": []}
        if not first_run:
            msg = excerpt[:220] + ("…" if len(excerpt) > 220 else "")
            notify(cfg, f"Draamateater: {title}", msg, post["link"], 5)
    state["seen_news"] = sorted(seen, key=int)[-50:]

    for pid, r in list(reminders.items()):
        sale = dt.datetime.fromisoformat(r["sale"])
        if now >= sale:
            del reminders[pid]
            continue
        eve = (sale - dt.timedelta(days=1)).replace(hour=20, minute=0)
        soon = sale - dt.timedelta(minutes=15)
        when = f"{'ETKNRLP'[sale.weekday()]} {sale.day}.{sale.month} kl {sale:%H:%M}"
        if now >= soon and "soon" not in r["sent"]:
            r["sent"] += ["eve", "soon"]
            notify(cfg, "Piletimüük algab 15 min pärast!", f"{r['title']} ({when})", KAVA_URL, 5)
        elif now >= eve and "eve" not in r["sent"]:
            r["sent"].append("eve")
            notify(cfg, "Homme algab piletimüük", f"{r['title']} ({when})", r["link"], 4)


# --- käivitus --------------------------------------------------------------

def run(force=False):
    cfg = load_config()
    now = dt.datetime.now().replace(second=0, microsecond=0)
    if not force and in_quiet_hours(cfg, now):
        return
    state = load_json(STATE_PATH, {})
    for name, check in (("mängukava", check_kava), ("uudised", check_news)):
        try:
            check(cfg, state, now)
        except Exception as e:  # võrgukatkestus vms – proovime järgmisel korral uuesti
            log(f"{name}: viga {type(e).__name__}: {e}")
    state["last_check"] = now.isoformat()
    save_json(STATE_PATH, state)


def status():
    state = load_json(STATE_PATH, {})
    perfs = state.get("perfs", {})
    print(f"Viimane kontroll: {state.get('last_check', '–')}")
    by_month = {}
    for p in perfs.values():
        b = by_month.setdefault(p["date"][:7], [0, 0])
        b[0 if p["status"] == "buy" else 1] += 1
    for m in sorted(by_month):
        print(f"  {MONTH_NAMES[int(m[5:])]:>10} {m[:4]}: müügis {by_month[m][0]:3}, otsas/tulekul {by_month[m][1]:3}")
    for r in state.get("reminders", {}).values():
        print(f"Meeldetuletus: {r['title']} → müük algab {r['sale']}")


def main():
    ap = argparse.ArgumentParser(description="Draamateatri piletivalvur")
    ap.add_argument("--test", action="store_true", help="saada proovitekst kõikidesse kanalitesse")
    ap.add_argument("--status", action="store_true", help="näita viimase kontrolli kokkuvõtet")
    ap.add_argument("--force", action="store_true", help="kontrolli ka vaikse aja sees")
    args = ap.parse_args()
    if args.test:
        cfg = load_config()
        notify(cfg, "Piletivalvur töötab", "Nii näeb välja Draamateatri piletiteavitus.", KAVA_URL)
    elif args.status:
        status()
    else:
        run(force=args.force)


if __name__ == "__main__":
    main()
