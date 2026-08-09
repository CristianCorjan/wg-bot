#!/usr/bin/env python3
"""
WG-Gesucht assistant.

Reads one or more filtered search URLs, and for each listing it has not
contacted before, sends the message template belonging to that search.

Which template is used is decided by which search the listing came from, so a
Zwischenmiete search and a long-term search get different texts without any
guessing.

Safety first — it will not send anything until you pass --send:

    python wgbot.py --inspect      # save screenshot + HTML, check selectors
    python wgbot.py                # dry run: shows what it WOULD send
    python wgbot.py --send         # actually sends
"""

import argparse
import json
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, date
from pathlib import Path

import requests
import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.yaml"
DB_FILE = HERE / "contacted.sqlite"
SESSION_FILE = HERE / "session.json"   # keeps cookies so we log in rarely
DEBUG_DIR = HERE / "debug"

LISTING_ID = re.compile(r"\.(\d{6,})\.html")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --- storage ----------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS contacted ("
        " listing_id TEXT PRIMARY KEY,"
        " search TEXT, title TEXT, url TEXT, sent_at TEXT)"
    )
    conn.commit()
    return conn


def already_contacted(conn, listing_id):
    return conn.execute(
        "SELECT 1 FROM contacted WHERE listing_id = ?", (listing_id,)
    ).fetchone() is not None


def record(conn, listing, search_name):
    conn.execute(
        "INSERT OR REPLACE INTO contacted VALUES (?,?,?,?,?)",
        (listing["id"], search_name, listing["title"], listing["url"],
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def sent_today(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM contacted WHERE sent_at LIKE ?",
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
    """Send a file to Telegram, so you never have to copy it off the server."""
    tg = cfg.get("telegram") or {}
    if not tg.get("token") or not tg.get("chat_id"):
        log("no Telegram set up, so the file stays on the server")
        return
    kind = "sendPhoto" if as_photo else "sendDocument"
    field = "photo" if as_photo else "document"
    try:
        with open(path, "rb") as handle:
            requests.post(
                f"https://api.telegram.org/bot{tg['token']}/{kind}",
                data={"chat_id": tg["chat_id"], "caption": caption[:1000]},
                files={field: handle},
                timeout=60,
            )
        log(f"sent {Path(path).name} to Telegram")
    except Exception as exc:
        log(f"could not send {path} to Telegram: {exc}")


# --- browser ----------------------------------------------------------------

def dismiss_cookies(page, sel):
    for part in sel["cookie_accept"].split(","):
        try:
            button = page.locator(part.strip()).first
            if button.is_visible(timeout=2000):
                button.click()
                page.wait_for_timeout(800)
                return
        except PWTimeout:
            continue
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
    log("login failed — wrong details, or a captcha appeared")
    return False


def collect(page, search, sel):
    """Return the listings shown by one search URL."""
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
        match = LISTING_ID.search(url)
        if not match:
            continue
        listings[match.group(1)] = {"id": match.group(1), "title": title or "Angebot", "url": url}

    return list(listings.values())


def contact_name(page):
    """Best effort at the landlord's first name, for the [name] placeholder."""
    text = page.inner_text("body")
    for pattern in (r"Ansprechpartner[:\s]+([A-ZÄÖÜ][\wäöüß-]+)",
                    r"Kontaktperson[:\s]+([A-ZÄÖÜ][\wäöüß-]+)"):
        if (m := re.search(pattern, text)):
            return m.group(1)
    return ""


def send_message(page, listing, template, sel, really_send):
    page.goto(listing["url"], wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    name = contact_name(page)
    body = template.replace("[name]", name if name else "zusammen")

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
        return True, "DRY RUN — not sent"

    try:
        page.locator(sel["send_button"]).first.click()
        page.wait_for_timeout(3000)
    except Exception as exc:
        return False, f"could not click send ({exc})"

    return True, "sent"


# --- main -------------------------------------------------------------------

def unfilled_placeholders(message):
    """Any [bracketed] text other than [name] means the template isn't finished."""
    return [x for x in re.findall(r"\[([^\]]{1,80})\]", message) if x.strip() != "name"]


def within_active_hours(cfg):
    start, end = cfg["limits"]["active_hours"]
    return start <= datetime.now().hour < end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be sent, without sending it")
    ap.add_argument("--inspect", action="store_true",
                    help="save a screenshot and the HTML, then stop")
    ap.add_argument("--show", action="store_true",
                    help="show the browser window instead of hiding it")
    args = ap.parse_args()
    really_send = not args.dry_run

    if not CONFIG_FILE.exists():
        log("config.yaml is missing — copy config.example.yaml and fill it in")
        return 1
    cfg = yaml.safe_load(CONFIG_FILE.read_text())
    sel = cfg["selectors"]
    conn = db()

    for search in cfg["searches"]:
        leftover = unfilled_placeholders(search["message"])
        if leftover:
            log(f"the message for '{search['name']}' still has placeholders in it:")
            for item in leftover:
                log(f"    [{item}]")
            log("finish the text in config.yaml first — nothing was sent")
            return 1

    if really_send and not within_active_hours(cfg):
        log("outside your active hours, doing nothing")
        return 0

    budget_today = cfg["limits"]["max_per_day"] - sent_today(conn)
    budget = min(cfg["limits"]["max_per_run"], budget_today)
    if really_send and budget <= 0:
        log(f"daily limit of {cfg['limits']['max_per_day']} already used")
        return 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.show)
        context = browser.new_context(
            storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
            locale="de-DE",
            viewport={"width": 1366, "height": 900},
        )
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
            verdict = (f"Inspection: found {found} listing(s) on the page.\n"
                       + ("Looks good." if found else
                          "0 means the listing_card selector needs fixing."))
            send_file(cfg, DEBUG_DIR / "page.png", verdict, as_photo=True)
            send_file(cfg, DEBUG_DIR / "page.html", "The page code, for fixing selectors.")
            log(f"saved {DEBUG_DIR/'page.png'} and {DEBUG_DIR/'page.html'}")
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
            fresh = [x for x in listings if not already_contacted(conn, x["id"])]
            log(f"{search['name']}: {len(listings)} listings, {len(fresh)} new")

            for listing in fresh:
                if sent >= budget:
                    break
                ok, note = send_message(page, listing, search["message"], sel, really_send)
                log(f"  {listing['title'][:60]} -> {note}")
                if ok and really_send:
                    record(conn, listing, search["name"])
                    notify(cfg, f"✉️ Wrote to: {listing['title']}\n{listing['url']}")
                    sent += 1
                    pause = random.randint(cfg["limits"]["min_delay_seconds"],
                                           cfg["limits"]["max_delay_seconds"])
                    log(f"  waiting {pause}s")
                    time.sleep(pause)
                elif ok:
                    sent += 1   # count dry runs too, so the preview is realistic

        context.storage_state(path=str(SESSION_FILE))
        browser.close()

    log(f"finished — {sent} message(s) {'sent' if really_send else 'previewed'}, "
        f"{sent_today(conn)}/{cfg['limits']['max_per_day']} used today")
    return 0


if __name__ == "__main__":
    sys.exit(main())
