#!/usr/bin/env python3
"""
WG-Gesucht assistant.

For each search URL in config.yaml it opens the listings it has not written to
before, checks them against that search's rules, and sends the matching message.

Per listing it:
  * reads the free-from / free-until dates and can require a minimum duration
  * works out how to address the person (Liebe / Lieber / Hallo)
  * reads the description for instructions the landlord hid in the text
    ("agree to the Ablöse", "send an emoji so I know you read this")
  * appends your prepared answer for the ones you have described in config
  * refuses to send, and pings you instead, when it sees an instruction it does
    not recognise

Usage:
    python wgbot.py --inspect     save screenshot + HTML to Telegram, stop
    python wgbot.py --dry-run     do everything except press send
    python wgbot.py               send for real
"""

import argparse
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.yaml"
DB_FILE = HERE / "contacted.sqlite"
SESSION_FILE = HERE / "session.json"
DEBUG_DIR = HERE / "debug"

LISTING_ID = re.compile(r"\.(\d{6,})\.html")
GERMAN_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --- storage ----------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS contacted ("
        " listing_id TEXT PRIMARY KEY, search TEXT, title TEXT, url TEXT,"
        " status TEXT, note TEXT, sent_at TEXT)"
    )
    conn.commit()
    return conn


def seen_before(conn, listing_id):
    return conn.execute(
        "SELECT 1 FROM contacted WHERE listing_id = ?", (listing_id,)
    ).fetchone() is not None


def remember(conn, listing, search_name, status, note=""):
    conn.execute(
        "INSERT OR REPLACE INTO contacted VALUES (?,?,?,?,?,?,?)",
        (listing["id"], search_name, listing["title"], listing["url"],
         status, note, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def sent_today(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM contacted WHERE status='sent' AND sent_at LIKE ?",
        (f"{date.today().isoformat()}%",),
    ).fetchone()[0]


# --- telegram ---------------------------------------------------------------

def notify(cfg, text):
    tg = cfg.get("telegram") or {}
    if not tg.get("token") or not tg.get("chat_id"):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{tg['token']}/sendMessage",
            json={"chat_id": tg["chat_id"], "text": text,
                  "disable_web_page_preview": False},
            timeout=20,
        )
    except requests.RequestException as exc:
        log(f"could not reach Telegram: {exc}")


def send_file(cfg, path, caption, as_photo=False):
    tg = cfg.get("telegram") or {}
    if not tg.get("token") or not tg.get("chat_id"):
        log("no Telegram set up, so the file stays on the server")
        return
    kind, field = ("sendPhoto", "photo") if as_photo else ("sendDocument", "document")
    try:
        with open(path, "rb") as handle:
            requests.post(
                f"https://api.telegram.org/bot{tg['token']}/{kind}",
                data={"chat_id": tg["chat_id"], "caption": caption[:1000]},
                files={field: handle}, timeout=60,
            )
        log(f"sent {Path(path).name} to Telegram")
    except Exception as exc:
        log(f"could not send {path} to Telegram: {exc}")


# --- reading a listing ------------------------------------------------------

def find_dates(text):
    """Return (free_from, free_until). free_until is None for open-ended."""
    found = []
    for d, m, y in GERMAN_DATE.findall(text):
        try:
            found.append(date(int(y), int(m), int(d)))
        except ValueError:
            continue
    if not found:
        return None, None
    found.sort()
    start = found[0]
    end = found[-1] if len(found) > 1 and found[-1] != start else None
    return start, end


def months_between(start, end):
    """
    Length of a lease in months.

    'frei bis 31.03' means the 31st is included, so 01.10 -> 31.03 is a full
    six months. Counting raw days and dividing by an average month gives 5.98
    and wrongly fails a '6 months minimum' rule, so count calendar months from
    the day after the end date instead.
    """
    after = end + timedelta(days=1)
    months = (after.year - start.year) * 12 + (after.month - start.month)
    months += (after.day - start.day) / 30.44
    return months


def guess_gender(page_text, name, cfg):
    """Male / female / None. Site wording first, then your own name lists."""
    if name:
        if re.search(rf"\bFrau\s+{re.escape(name)}", page_text):
            return "f"
        if re.search(rf"\bHerr\s+{re.escape(name)}", page_text):
            return "m"
        names = cfg.get("names", {}) or {}
        low = name.lower()
        if low in [x.lower() for x in names.get("female", [])]:
            return "f"
        if low in [x.lower() for x in names.get("male", [])]:
            return "m"
    return None


def build_salutation(name, gender):
    if name and gender == "f":
        return f"Liebe {name}"
    if name and gender == "m":
        return f"Lieber {name}"
    if name:
        return f"Hallo {name}"          # unknown gender: no Liebe/r guess
    return "Hallo zusammen"


def find_name(page_text):
    for pattern in (r"Ansprechpartner[:\s]+([A-ZÄÖÜ][\wäöüß-]{1,20})",
                    r"Kontaktperson[:\s]+([A-ZÄÖÜ][\wäöüß-]{1,20})",
                    r"Vermieter[:\s]+([A-ZÄÖÜ][\wäöüß-]{1,20})"):
        if (m := re.search(pattern, page_text)):
            return m.group(1)
    return ""


def apply_rules(description, cfg):
    """
    Look for instructions hidden in the advert.

    Returns (extra_sentences, unknown_hits). A non-empty unknown_hits means the
    advert is asking for something you have not prepared an answer for, so the
    listing is left for you to handle by hand.
    """
    low = description.lower()
    extras, matched_rules = [], []

    for rule in cfg.get("rules", []) or []:
        if any(k.lower() in low for k in rule.get("keywords", [])):
            matched_rules.append(rule.get("name", "rule"))
            if rule.get("add"):
                extras.append(rule["add"])

    unknown = []
    for word in cfg.get("flag_keywords", []) or []:
        if word.lower() in low and not matched_rules:
            unknown.append(word)

    return extras, unknown, matched_rules


# --- browser ----------------------------------------------------------------

def dismiss_cookies(page, sel):
    for part in sel["cookie_accept"].split(","):
        try:
            button = page.locator(part.strip()).first
            if button.is_visible(timeout=2000):
                button.click()
                page.wait_for_timeout(800)
                return
        except Exception:
            continue


def logged_in(page, sel):
    try:
        return page.locator(sel["logged_in_marker"]).first.is_visible(timeout=4000)
    except Exception:
        return False


def log_in(page, cfg, sel):
    log("logging in")
    page.goto("https://www.wg-gesucht.de/", wait_until="domcontentloaded")
    dismiss_cookies(page, sel)
    if logged_in(page, sel):
        log("already logged in from the saved session")
        return True
    try:
        page.locator(sel["login_open"]).first.click()
        page.wait_for_timeout(1500)
        page.fill(sel["login_email"], cfg["account"]["email"])
        page.fill(sel["login_password"], cfg["account"]["password"])
        page.click(sel["login_submit"])
        page.wait_for_timeout(4000)
    except Exception as exc:
        log(f"login form did not behave as expected: {exc}")
        return False
    if logged_in(page, sel):
        log("logged in")
        return True
    log("login failed - wrong details, or a captcha appeared")
    return False


def collect(page, search, sel):
    page.goto(search["url"], wait_until="domcontentloaded")
    dismiss_cookies(page, sel)
    page.wait_for_timeout(2000)
    listings = {}
    for card in page.locator(sel["listing_card"]).all():
        try:
            link = card.locator(sel["listing_link"]).first
            href = link.get_attribute("href") or ""
            title = (link.inner_text() or "").strip().split("\n")[0]
        except Exception:
            continue
        if not href:
            continue
        url = href if href.startswith("http") else "https://www.wg-gesucht.de/" + href.lstrip("/")
        if (m := LISTING_ID.search(url)):
            listings[m.group(1)] = {"id": m.group(1),
                                    "title": title or "Angebot",
                                    "url": url}
    return list(listings.values())


def examine(page, listing, sel):
    """Open the listing and read everything we need to decide."""
    page.goto(listing["url"], wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    text = page.inner_text("body")
    start, end = find_dates(text)
    return {"text": text, "start": start, "end": end}


def decide(listing, facts, search, cfg):
    """
    Returns (action, note, extras).
    action is one of: send, skip, flag
    """
    min_months = search.get("min_months", 0) or 0
    start, end = facts["start"], facts["end"]

    if min_months:
        if end is None:
            pass                      # no end date = open ended = fine
        elif start is None:
            return "flag", "could not read the dates", []
        else:
            length = months_between(start, end)
            if length < min_months - 0.15:      # ~4 days of tolerance
                return "skip", f"only {length:.1f} months, you want {min_months}+", []

    extras, unknown, matched = apply_rules(facts["text"], cfg)
    if unknown:
        return "flag", f"advert asks for something unhandled: {', '.join(unknown)}", extras
    return "send", (", ".join(matched) if matched else ""), extras


def compose(template, facts, extras, cfg):
    name = find_name(facts["text"])
    gender = guess_gender(facts["text"], name, cfg)
    body = template.replace("[salutation]", build_salutation(name, gender))
    body = body.replace("[name]", name if name else "zusammen")
    if extras:
        body = body.rstrip() + "\n\n" + " ".join(extras)
    return body


def deliver(page, body, sel, really_send):
    try:
        page.locator(sel["contact_button"]).first.click()
        page.wait_for_timeout(2500)
    except Exception as exc:
        return False, f"no contact button ({exc})"
    try:
        box = page.locator(sel["message_box"]).first
        box.wait_for(timeout=8000)
        box.fill(body)
    except Exception as exc:
        return False, f"no message box ({exc})"
    if not really_send:
        return True, "DRY RUN - not sent"
    try:
        page.locator(sel["send_button"]).first.click()
        page.wait_for_timeout(3000)
    except Exception as exc:
        return False, f"could not click send ({exc})"
    return True, "sent"


# --- config -----------------------------------------------------------------

def load_config():
    cfg = yaml.safe_load(CONFIG_FILE.read_text())
    cfg.setdefault("account", {})
    cfg.setdefault("telegram", {})
    for env_name, section, key in [
        ("WG_EMAIL", "account", "email"),
        ("WG_PASSWORD", "account", "password"),
        ("TELEGRAM_TOKEN", "telegram", "token"),
        ("TELEGRAM_CHAT_ID", "telegram", "chat_id"),
    ]:
        if (value := os.environ.get(env_name)):
            cfg[section][key] = value
    return cfg


KEEP = {"name", "salutation"}


def unfilled_placeholders(message):
    return [x for x in re.findall(r"\[([^\]]{1,80})\]", message)
            if x.strip() not in KEEP]


def within_active_hours(cfg):
    start, end = cfg["limits"]["active_hours"]
    return start <= datetime.now().hour < end


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    really_send = not args.dry_run

    if not CONFIG_FILE.exists():
        log("config.yaml is missing")
        return 1
    cfg = load_config()
    sel = cfg["selectors"]
    conn = db()

    for search in cfg["searches"]:
        if (left := unfilled_placeholders(search["message"])):
            log(f"the message for '{search['name']}' still has placeholders:")
            for item in left:
                log(f"    [{item}]")
            return 1

    if really_send and not within_active_hours(cfg):
        log("outside your active hours, doing nothing")
        return 0

    budget = min(cfg["limits"]["max_per_run"],
                 cfg["limits"]["max_per_day"] - sent_today(conn))
    if really_send and budget <= 0:
        log(f"daily limit of {cfg['limits']['max_per_day']} already used")
        return 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.show)
        context = browser.new_context(
            storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
            locale="de-DE", viewport={"width": 1366, "height": 900})
        page = context.new_page()

        if args.inspect:
            DEBUG_DIR.mkdir(exist_ok=True)
            page.goto(cfg["searches"][0]["url"], wait_until="domcontentloaded")
            dismiss_cookies(page, sel)
            page.wait_for_timeout(2500)
            page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
            (DEBUG_DIR / "page.html").write_text(page.content())
            found = page.locator(sel["listing_card"]).count()
            log(f"listing_card selector matched {found} element(s)")
            send_file(cfg, DEBUG_DIR / "page.png",
                      f"Inspection: found {found} listing(s).\n"
                      + ("Looks good." if found else "0 means listing_card needs fixing."),
                      as_photo=True)
            send_file(cfg, DEBUG_DIR / "page.html", "Page code, for fixing selectors.")
            browser.close()
            return 0

        if not log_in(page, cfg, sel):
            browser.close()
            return 1
        context.storage_state(path=str(SESSION_FILE))

        sent = 0
        for search in cfg["searches"]:
            if sent >= budget:
                break
            listings = collect(page, search, sel)
            fresh = [x for x in listings if not seen_before(conn, x["id"])]
            log(f"{search['name']}: {len(listings)} listings, {len(fresh)} new")

            for listing in fresh:
                if sent >= budget:
                    break
                facts = examine(page, listing, sel)
                action, note, extras = decide(listing, facts, search, cfg)

                if action == "skip":
                    log(f"  skip: {listing['title'][:50]} - {note}")
                    remember(conn, listing, search["name"], "skipped", note)
                    continue

                if action == "flag":
                    log(f"  needs you: {listing['title'][:50]} - {note}")
                    remember(conn, listing, search["name"], "flagged", note)
                    notify(cfg, "🔎 Write this one yourself\n"
                                f"{listing['title']}\n{note}\n{listing['url']}")
                    continue

                body = compose(search["message"], facts, extras, cfg)
                ok, result = deliver(page, body, sel, really_send)
                log(f"  {listing['title'][:50]} -> {result}"
                    + (f" [{note}]" if note else ""))

                if ok and really_send:
                    remember(conn, listing, search["name"], "sent", note)
                    notify(cfg, f"✉️ Wrote to: {listing['title']}"
                                + (f"\nHandled: {note}" if note else "")
                                + f"\n{listing['url']}")
                    sent += 1
                    pause = random.randint(cfg["limits"]["min_delay_seconds"],
                                           cfg["limits"]["max_delay_seconds"])
                    log(f"  waiting {pause}s")
                    time.sleep(pause)
                elif ok:
                    sent += 1
                else:
                    remember(conn, listing, search["name"], "failed", result)

        context.storage_state(path=str(SESSION_FILE))
        browser.close()

    log(f"finished - {sent} message(s) {'sent' if really_send else 'previewed'}, "
        f"{sent_today(conn)}/{cfg['limits']['max_per_day']} used today")
    return 0


if __name__ == "__main__":
    sys.exit(main())
