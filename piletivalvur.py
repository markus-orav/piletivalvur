#!/usr/bin/env python3
"""Piletivalvur – jälgib Eesti Draamateatri ja Tallinna Linnateatri mängukava ning uudiseid.

Teavitab (Maci teavitus + ntfy push), kui
  * uudistes või mängukavas on teada piletimüügi algus (+ meeldetuletus eelmisel õhtul ja 15 min enne),
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

UA = "Piletivalvur/1.0 (isiklik piletiteavitus)"
DT_KAVA_URL = "https://www.draamateater.ee/mangukava/"
LT_KAVA_URL = "https://linnateater.ee/mangukava/"
LT_SHOWS_URL = "https://linnateater.ee/wp-content/uploads/cachedShows/shows.json"

DEFAULT_CONFIG = {
    "theaters": ["draamateater", "linnateater"],
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


def fmt_when(t):
    return f"{'ETKNRLP'[t.weekday()]} {t.day}.{t.month} kl {t:%H:%M}"


def fmt_perf(p):
    d = dt.date.fromisoformat(p["date"])
    wd = "ETKNRLP"[d.weekday()]
    venue = f", {p['venue']}" if p.get("venue") else ""
    return f"{p['title']} – {wd} {d.day}.{d.month} kl {p['time']}{venue}"


def month_span(perfs):
    months = sorted({p["date"][:7] for p in perfs})
    names = [f"{MONTH_NAMES[int(m[5:])]} {m[:4]}" for m in (months[0], months[-1])]
    return names[0] if months[0] == months[-1] else f"{names[0]} – {names[1]}"


# --- teavitused ------------------------------------------------------------

# ~/Applications koopia on LaunchServicesis registreeritud – ainult nii lubab macOS sellel teavitusi saata
TERMINAL_NOTIFIER = next((p for p in (str(Path.home() / "Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier"),
                                      "/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier")
                          if Path(p).exists()), None)


def notify(cfg, title, message, url=None, priority=4, more=None):
    """url avaneb teavitusele klõpsates (ostuleht); more = (silt, url) on telefonis lisanupp."""
    log(f"TEAVITUS: {title} | {message}" + (f" | {url}" if url else ""))
    if cfg.get("mac_notifications", True):
        if TERMINAL_NOTIFIER:  # osascripti teavitus ei oska klõpsu peale linki avada
            cmd = [TERMINAL_NOTIFIER, "-title", title, "-message", message, "-sound", "Glass"]
            if url:
                cmd += ["-open", url]
        else:
            cmd = ["osascript", "-e", "on run argv",
                   "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
                   "-e", "end run", title, message]
        try:
            r = subprocess.run(cmd, check=False, timeout=15, capture_output=True, text=True)
            if r.returncode:
                log(f"Maci teavitus ebaõnnestus ({r.returncode}): {(r.stderr or r.stdout).strip()}")
        except (OSError, subprocess.SubprocessError) as e:
            log(f"Maci teavitus ebaõnnestus: {e}")
    topic = cfg.get("ntfy_topic")
    if topic:
        body = {"topic": topic, "title": title, "message": message,
                "priority": priority, "tags": ["performing_arts"]}
        actions = []
        if url:
            body["click"] = url
            label = "Osta pilet" if any(s in url for s in ("piletimaailm", "piletilevi")) else "Müügis etendused"
            actions.append({"action": "view", "label": label, "url": url})
        if more:
            actions.append({"action": "view", "label": more[0], "url": more[1]})
        if actions:
            body["actions"] = actions
        try:
            req = urllib.request.Request(
                cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/"),
                data=json.dumps(body).encode("utf-8"),
                headers={"User-Agent": UA, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
        except OSError as e:
            log(f"ntfy teavitus ebaõnnestus: {e}")


# --- mängukavad --------------------------------------------------------------
# Iga teatri lugeja tagastab {võti: etendus}, võti = 'YYYY-MM-DD HH:MM Pealkiri'.
# Etenduse status: "buy" (müügis, url = ostuleht), "soldout" või "other" (müük pole alanud;
# kui algusaeg on teada, on see väljal "sale").

def parse_draamateater(page, today):
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


def parse_linnateater(shows, now):
    """shows.json on sama fail, millest linnateater.ee mängukava end JavaScriptiga täidab."""
    perfs = {}
    for s in shows:
        d = s.get("data") or {}
        start, sale = d.get("startTime") or {}, d.get("salesTime") or {}
        try:
            date = dt.date(int(start["year"]), int(start["month"]), int(start["date"]))
        except (KeyError, ValueError):
            continue
        sale_at = dt.datetime.fromtimestamp(int(sale["stamp"])) if sale.get("stamp") else None
        # sama loogika, mis mängukava skriptis
        if s.get("status") in ("sold_out", "no_tickets"):
            status = "soldout"
        elif s.get("status") == "future_sale" and sale_at and sale_at > now:
            status = "other"
        else:
            status = "buy"
        title = html.unescape(s.get("title") or "").strip()
        p = {
            "date": date.isoformat(),
            "time": start.get("time", ""),
            "title": title,
            "venue": (s.get("location") or "").replace("Tallinna Linnateater, ", "").split(",")[0],
            "desc": strip_tags(s.get("extra") or ""),
            "status": status,
            "url": d.get("url") if status == "buy" else None,
        }
        if status == "other":
            p["sale"] = sale_at.isoformat()
        perfs[f"{p['date']} {p['time']} {title}"] = p
    return perfs


THEATERS = {
    "draamateater": {
        "name": "Draamateater",
        "kava": DT_KAVA_URL,
        "buy": DT_KAVA_URL + "?tickets=available",  # ainult etendused, millel on „Osta pilet“
        "news": "https://www.draamateater.ee/wp-json/wp/v2/posts?per_page=10&_fields=id,date,title,link,excerpt",
        "sale_hour": 10,
        "load": lambda now: parse_draamateater(fetch(DT_KAVA_URL), now.date()),
    },
    "linnateater": {
        "name": "Linnateater",
        "kava": LT_KAVA_URL,
        "buy": LT_KAVA_URL + "?filter=available",
        "news": "https://linnateater.ee/wp-json/wp/v2/posts?per_page=10&_fields=id,date,title,link,excerpt",
        "sale_hour": 11,
        "load": lambda now: parse_linnateater(json.loads(fetch(LT_SHOWS_URL)), now),
    },
}


def check_kava(cfg, th, ts, now):
    name = th["name"]
    perfs = th["load"](now)
    if not perfs:
        last = ts.get("parse_alert_at")
        if not last or now - dt.datetime.fromisoformat(last) > dt.timedelta(hours=24):
            notify(cfg, "Piletivalvur vajab hoolitsust",
                   f"{name}: mängukavast ei leitud ühtegi etendust – lehe ülesehitus võis muutuda.", th["kava"], 3)
            ts["parse_alert_at"] = now.isoformat()
        return

    # mängukavas teada olev müügi algus → meeldetuletus (sama aja kohta käiv uudis ei tee topelt)
    upcoming = {}
    for p in perfs.values():
        if p.get("sale"):
            upcoming.setdefault(p["sale"], []).append(p)
    reminders = ts.setdefault("reminders", {})
    for sale, ps in upcoming.items():
        reminders.setdefault(sale, {"title": f"{len(ps)} etendust ({month_span(ps)})", "sale": sale, "sent": []})

    old = ts.get("perfs")
    ts["perfs"] = perfs
    if old is None:
        log(f"{name}: esimene kontroll, {len(perfs)} etendust, "
            f"{sum(p['status'] == 'soldout' for p in perfs.values())} välja müüdud")
        return

    today = now.date().isoformat()
    threshold = cfg.get("batch_threshold", 5)
    added = [p for k, p in perfs.items() if k not in old and p["date"] >= today]
    opened = [p for k, p in perfs.items() if k in old and old[k]["status"] == "other" and p["status"] == "buy"]
    freed = [(k, p) for k, p in perfs.items() if k in old and old[k]["status"] == "soldout" and p["status"] == "buy"]

    if len(added) >= threshold:
        buy = sum(p["status"] == "buy" for p in added)
        sales = sorted(p["sale"] for p in added if p.get("sale"))
        msg = f"Mängukavva lisandus {len(added)} etendust ({month_span(added)})"
        if buy:
            msg += f", neist {buy} kohe müügis"
        if sales:
            msg += f". Müük algab {fmt_when(dt.datetime.fromisoformat(sales[0]))}"
        notify(cfg, f"{name}: uued etendused " + ("müügis!" if buy else "kavas"), msg + ".", th["buy"], 5)
    else:
        for p in added:
            if not watched(cfg, p["title"]):
                continue
            if p["status"] == "buy":
                label = "müügis"
            elif p["status"] == "soldout":
                label = "piletid juba otsas"
            elif p.get("sale"):
                label = f"müük algab {fmt_when(dt.datetime.fromisoformat(p['sale']))}"
            else:
                label = "müük pole veel alanud"
            notify(cfg, f"{name}: uus etendus", f"{fmt_perf(p)} ({label})", p["url"] or th["kava"],
                   4 if p["status"] == "buy" else 3)

    if len(opened) >= threshold:
        notify(cfg, f"{name}: piletimüük avanes!",
               f"Müüki tuli {len(opened)} etendust ({month_span(opened)}).", th["buy"], 5)
    else:
        for p in opened:
            if watched(cfg, p["title"]):
                notify(cfg, f"{name}: müük avanes", fmt_perf(p), p["url"], 5)

    notified = ts.setdefault("freed_notified", {})
    for k, p in freed:
        if not watched(cfg, p["title"]):
            continue
        last = notified.get(k)
        if last and now - dt.datetime.fromisoformat(last) < dt.timedelta(minutes=cfg.get("renotify_minutes", 60)):
            continue
        notified[k] = now.isoformat()
        notify(cfg, f"{name}: vabanes pileteid!", fmt_perf(p), p["url"], 5)
    ts["freed_notified"] = {k: v for k, v in notified.items() if k[:10] >= today}


# --- uudised ja meeldetuletused ----------------------------------------------

def parse_sale_time(text, posted, default_hour=10):
    """Leiab lausest, mis räägib müügist, müügi alguse aja. None, kui ei leia."""
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-ZÕÄÖÜ„])", text):
        if not SALE_WORDS.search(sentence):
            continue
        dm = DATE_RE.search(sentence)
        if not dm:
            continue
        month = next(n for s, n in MONTH_STEMS if dm.group(2).lower().startswith(s))
        tm = TIME_RE.search(sentence)
        hour, minute = (int(tm.group(1)), int(tm.group(2) or 0)) if tm else (default_hour, 0)
        year = posted.year + (1 if month < posted.month else 0)
        try:
            return dt.datetime(year, month, int(dm.group(1)), hour, minute)
        except ValueError:
            return None
    return None


def check_news(cfg, th, ts, now):
    posts = json.loads(fetch(th["news"]))
    first_run = "seen_news" not in ts
    seen = set(ts.get("seen_news", []))
    reminders = ts.setdefault("reminders", {})
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
        sale = parse_sale_time(excerpt + " " + title, dt.datetime.fromisoformat(post["date"]), th["sale_hour"])
        if sale and sale > now:
            r = reminders.setdefault(sale.isoformat(), {"sale": sale.isoformat(), "sent": []})
            r.update(title=title, link=post["link"])
        if not first_run:
            msg = excerpt[:220] + ("…" if len(excerpt) > 220 else "")
            notify(cfg, f"{th['name']}: {title}", msg, th["buy"], 5, ("Loe uudist", post["link"]))
    ts["seen_news"] = sorted(seen, key=int)[-50:]


def send_reminders(cfg, th, ts, now):
    reminders = ts.get("reminders", {})
    for key, r in list(reminders.items()):
        sale = dt.datetime.fromisoformat(r["sale"])
        if now >= sale:
            del reminders[key]
            continue
        eve = (sale - dt.timedelta(days=1)).replace(hour=20, minute=0)
        soon = sale - dt.timedelta(minutes=15)
        more = ("Loe uudist", r["link"]) if r.get("link") else None
        if now >= soon and "soon" not in r["sent"]:
            r["sent"] += ["eve", "soon"]
            notify(cfg, f"{th['name']}: piletimüük algab 15 min pärast!",
                   f"{fmt_when(sale)} – {r['title']}", th["buy"], 5, more)
        elif now >= eve and "eve" not in r["sent"]:
            r["sent"].append("eve")
            day = "täna" if sale.date() == now.date() else "homme"
            notify(cfg, f"{th['name']}: piletimüük algab {day}",
                   f"{fmt_when(sale)} – {r['title']}", th["buy"], 4, more)


# --- käivitus --------------------------------------------------------------

def theater_state(state, tid):
    theaters = state.setdefault("theaters", {})
    if tid == "draamateater" and tid not in theaters and "perfs" in state:
        # vanem seis oli ainult Draamateatri oma ja asus juurtasemel
        theaters[tid] = {k: state.pop(k) for k in ("perfs", "seen_news", "reminders", "freed_notified", "parse_alert_at")
                         if k in state}
    return theaters.setdefault(tid, {})


def run(force=False):
    cfg = load_config()
    now = dt.datetime.now().replace(second=0, microsecond=0)
    if not force and in_quiet_hours(cfg, now):
        return
    state = load_json(STATE_PATH, {})
    for tid in cfg.get("theaters") or list(THEATERS):
        th = THEATERS.get(tid)
        if not th:
            log(f"Tundmatu teater seadetes: {tid}")
            continue
        ts = theater_state(state, tid)
        for step, check in (("mängukava", check_kava), ("uudised", check_news), ("meeldetuletused", send_reminders)):
            try:
                check(cfg, th, ts, now)
            except Exception as e:  # võrgukatkestus vms – proovime järgmisel korral uuesti
                log(f"{th['name']} {step}: viga {type(e).__name__}: {e}")
    state["last_check"] = now.isoformat()
    save_json(STATE_PATH, state)


def status():
    state = load_json(STATE_PATH, {})
    print(f"Viimane kontroll: {state.get('last_check', '–')}")
    for tid, ts in state.get("theaters", {}).items():
        print(THEATERS[tid]["name"] if tid in THEATERS else tid)
        by_month = {}
        for p in ts.get("perfs", {}).values():
            b = by_month.setdefault(p["date"][:7], [0, 0])
            b[0 if p["status"] == "buy" else 1] += 1
        for m in sorted(by_month):
            print(f"  {MONTH_NAMES[int(m[5:])]:>10} {m[:4]}: müügis {by_month[m][0]:3}, otsas/tulekul {by_month[m][1]:3}")
        for r in ts.get("reminders", {}).values():
            print(f"  Meeldetuletus: {r['title']} → müük algab {r['sale']}")


def main():
    ap = argparse.ArgumentParser(description="Draamateatri ja Linnateatri piletivalvur")
    ap.add_argument("--test", action="store_true", help="saada proovitekst kõikidesse kanalitesse")
    ap.add_argument("--status", action="store_true", help="näita viimase kontrolli kokkuvõtet")
    ap.add_argument("--force", action="store_true", help="kontrolli ka vaikse aja sees")
    args = ap.parse_args()
    if args.test:
        notify(load_config(), "Piletivalvur töötab", "Klõpsa – avaneb müügis olevate etenduste leht.",
               THEATERS["draamateater"]["buy"])
    elif args.status:
        status()
    else:
        run(force=args.force)


if __name__ == "__main__":
    main()
