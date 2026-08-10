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
import json
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
from bs4 import BeautifulSoup
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



CARD_DATES = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})")
CARD_FROM = re.compile(r"\bab\s*(\d{2}\.\d{2}\.\d{4})")
CARD_PRICE = re.compile(r"(\d[\d.]*)\s*\u20ac")
TITLES = ("herr", "frau")


def to_date(text):
    d, m, y = text.split(".")
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def clean_name(raw):
    """
    Turn the name shown on a card into (first_name, gender_hint).

    Cards carry things like 'Anna K', 'S. Bund', 'Herr Duong', 'XX'. We want a
    usable first name or nothing at all - a wrong name is worse than none.
    """
    if not raw:
        return "", None
    parts = [x for x in re.split(r"[\s,]+", raw.strip()) if x]
    if not parts:
        return "", None
    gender = None
    if parts[0].lower() in TITLES:
        gender = "f" if parts[0].lower() == "frau" else "m"
        parts = parts[1:]
        if not parts:
            return "", gender
    name = parts[0].strip(".")
    if len(name) < 3 or not re.fullmatch(r"[A-Za-z\u00c4\u00d6\u00dc\u00e4\u00f6\u00fc\u00df-]+", name):
        return "", gender          # initials, "XX" and similar are not names
    if name.isupper():
        return "", gender
    return name, gender


def parse_search_page(html):
    """Read every listing card on a search results page."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for card in soup.select("[id^=\'liste-details-ad-\']"):
        link = card.select_one("h2 a") or card.select_one("a[href*=\'.html\']")
        if not link:
            continue
        href = link.get("href") or ""
        url = href if href.startswith("http") else "https://www.wg-gesucht.de/" + href.lstrip("/")
        m = LISTING_ID.search(url)
        if not m:
            continue
        text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        dates = CARD_DATES.search(text)
        if dates:
            start, end = to_date(dates.group(1)), to_date(dates.group(2))
        else:
            only_from = CARD_FROM.search(text)
            start = to_date(only_from.group(1)) if only_from else None
            end = None
        avatar = card.select_one("img.avatar")
        raw_name = (avatar.get("alt") if avatar else "") or (
            card.select_one("span.ml5").get_text(strip=True) if card.select_one("span.ml5") else "")
        name, gender = clean_name(raw_name)
        price = CARD_PRICE.search(text)
        out.append({
            "id": m.group(1),
            "title": link.get_text(strip=True) or "Angebot",
            "url": url,
            "price": price.group(1) if price else "",
            "start": start,
            "end": end,
            "name": name,
            "gender": gender,
        })
    return out


# --- browser ----------------------------------------------------------------


def visit(page, url, tries=3):
    """
    Open a page, patiently.

    WG-Gesucht sometimes takes a long time to answer, or stalls entirely, from
    a data-centre address. Waiting for "commit" rather than a fully parsed
    document means we carry on as soon as the server actually responds, and we
    give it more than one chance.
    """
    last = None
    for attempt in range(tries):
        try:
            page.goto(url, wait_until="commit", timeout=60000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass          # enough of the page is there to work with
            return True
        except Exception as exc:
            last = exc
            log(f"page did not load (try {attempt + 1}/{tries}): {str(exc)[:120]}")
            page.wait_for_timeout(3000 + attempt * 4000)
    log(f"giving up on {url}: {str(last)[:150]}")
    return False


def dismiss_cookies(page, sel):
    """
    The consent box is injected a second or two after the page loads, so a
    single early click misses it. It sits on top of everything, which would
    block the contact button later, so it is worth being patient here.
    """
    for attempt in range(3):
        for part in sel["cookie_accept"].split(","):
            try:
                button = page.locator(part.strip()).first
                if button.is_visible(timeout=2500):
                    button.click()
                    page.wait_for_timeout(1000)
                    log("cookie banner dismissed")
                    return True
            except Exception:
                continue
        page.wait_for_timeout(1500)
    return False


def logged_in(page, sel):
    """
    Decide whether we are signed in.

    Links to 'mein-wg-gesucht' exist even when logged out, so they prove
    nothing. Two things are reliable:
      * a sign-in trigger in the page means we are logged OUT
      * the word 'abmelden' (log out) only appears when we are logged IN
    """
    try:
        html = page.content().lower()
    except Exception:
        return False

    if "fireloginorregistermodalrequest('sign_in')" in html.replace('"', "'"):
        return False
    if "abmelden" in html or "logout" in html:
        return True

    for part in sel["logged_in_marker"].split(","):
        try:
            if page.locator(part.strip()).first.is_visible(timeout=2000):
                return True
        except Exception:
            continue
    return False


def login_failure_snapshot(page, cfg, why):
    """Send a picture of whatever the browser is looking at, so we can see it."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        shot = DEBUG_DIR / "login_problem.png"
        page.screenshot(path=str(shot), full_page=False)
        send_file(cfg, shot, f"Login problem: {why}", as_photo=True)
        (DEBUG_DIR / "login_problem.html").write_text(page.content())
        send_file(cfg, DEBUG_DIR / "login_problem.html", "Page code at the moment login failed.")
    except Exception as exc:
        log(f"could not take a snapshot: {exc}")


def log_in(page, cfg, sel):
    """
    WG-Gesucht has no ordinary login link - the form lives in a popup that the
    site opens with its own javascript function. So we call that function
    directly instead of hunting for a link to click.
    """
    log("logging in")
    if not visit(page, "https://www.wg-gesucht.de/"):
        log("could not reach wg-gesucht.de at all")
        return False
    dismiss_cookies(page, sel)
    if logged_in(page, sel):
        log("already logged in from the saved session")
        return True

    try:
        page.evaluate("fireLoginOrRegisterModalRequest('sign_in')")
        page.wait_for_timeout(2000)
    except Exception as exc:
        log(f"could not open the login popup: {exc}")
        login_failure_snapshot(page, cfg, "popup would not open")
        return False

    if logged_in(page, sel):
        log("the popup did not appear because we are already logged in")
        return True

    # Step 1: the popup asks for the email address only, then "Weiter".
    try:
        email = page.locator(sel["login_email"]).first
        email.wait_for(state="visible", timeout=10000)
        email.fill(cfg["account"]["email"])
        page.locator(sel["login_email_submit"]).first.click()
        page.wait_for_timeout(3000)
    except Exception as exc:
        if logged_in(page, sel):
            log("no login needed - the cookies already signed us in")
            return True
        log(f"first login step (email) failed: {exc}")
        login_failure_snapshot(page, cfg, f"email step: {str(exc)[:150]}")
        return False

    # Step 2: the password box appears only after the email is accepted.
    try:
        pw = page.locator(sel["login_password"]).first
        pw.wait_for(state="visible", timeout=10000)
        pw.fill(cfg["account"]["password"])
        page.locator(sel["login_submit"]).first.click()
        page.wait_for_timeout(6000)
    except Exception as exc:
        log(f"second login step (password) failed: {exc}")
        login_failure_snapshot(page, cfg, f"password step: {str(exc)[:150]}")
        return False

    if page.locator("#login_two_factor_authentication_form").count():
        try:
            if page.locator("#login_two_factor_authentication_form").first.is_visible(timeout=2000):
                log("WG-Gesucht wants an emailed code because it does not "
                    "recognise this machine. A bot cannot read that code.")
                log("Fix: log in yourself in a normal browser, export your "
                    "cookies, and put them in the WG_COOKIES secret. See the "
                    "README section 'When it asks for a code'.")
                login_failure_snapshot(page, cfg, "two-factor / new device code requested")
                return False
        except Exception:
            pass

    if logged_in(page, sel):
        log("logged in")
        return True
    log("login failed - wrong details, or a captcha appeared")
    login_failure_snapshot(page, cfg, "form submitted but we are still logged out")
    return False


def collect(page, search, sel):
    if not visit(page, search["url"]):
        return []
    dismiss_cookies(page, sel)
    page.wait_for_timeout(2500)
    return parse_search_page(page.content())


def examine(page, listing, sel):
    """Open the listing and read everything we need to decide."""
    if not visit(page, listing["url"]):
        return {"text": "", "start": None, "end": None, "unreachable": True}
    page.wait_for_timeout(1500)
    text = page.inner_text("body")
    start, end = find_dates(text)
    return {"text": text, "start": start, "end": end}


def decide(listing, facts, search, cfg):
    """
    Returns (action, note, extras). action is: send, skip or flag.

    Dates come from the search card, so a listing that is too short is rejected
    without ever being opened.
    """
    start, end = listing.get("start"), listing.get("end")

    earliest = search.get("earliest_start")
    if earliest and start:
        wanted = datetime.strptime(str(earliest), "%Y-%m-%d").date()
        if start < wanted:
            return "skip", f"starts {start:%d.%m.%Y}, you want {wanted:%d.%m.%Y} or later", []

    min_months = search.get("min_months", 0) or 0
    if min_months and end is not None:
        if start is None:
            return "flag", "could not read the dates", []
        length = months_between(start, end)
        if length < min_months - 0.15:
            return "skip", f"only {length:.1f} months, you want {min_months}+", []

    extras, unknown, matched = apply_rules(facts["text"], cfg)
    if unknown:
        return "flag", f"advert asks for something unhandled: {', '.join(unknown)}", extras
    return "send", (", ".join(matched) if matched else ""), extras


def compose(template, facts, extras, cfg, listing=None):
    listing = listing or {}
    name = listing.get("name") or find_name(facts["text"])
    gender = listing.get("gender") or guess_gender(facts["text"], name, cfg)
    body = template.replace("[salutation]", build_salutation(name, gender))
    body = body.replace("[name]", name if name else "zusammen")
    if extras:
        body = body.rstrip() + "\n\n" + " ".join(extras)
    return body



def click_first(page, selector_string, timeout=6000):
    """
    Try each selector in a comma separated list until one works.

    Doing them one at a time (rather than handing the whole list to Playwright)
    means a list can mix ordinary CSS with text matching, so a button can be
    found by its label when its markup changes.
    """
    for part in selector_string.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            target = page.locator(part).first
            target.wait_for(state="visible", timeout=timeout)
            target.click()
            return True, part
        except Exception:
            continue
    return False, None


def dismiss_safety_tips(page, sel):
    """
    Before the message form works, WG-Gesucht shows a "Wichtige Sicherheitstipps"
    box that must be acknowledged. It sits over the form, so the send button
    cannot be clicked until it is gone.
    """
    for part in sel.get("safety_ok", "#sec_advice_submit_button").split(","):
        try:
            button = page.locator(part.strip()).first
            if button.is_visible(timeout=3000):
                button.click()
                page.wait_for_timeout(1200)
                log("  safety tips acknowledged")
                return True
        except Exception:
            continue
    return False


def form_snapshot(page, cfg, why):
    """Capture the contact form when it does not look the way we expect."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        shot = DEBUG_DIR / "form_problem.png"
        page.screenshot(path=str(shot), full_page=False)
        send_file(cfg, shot, f"Contact form problem: {why}", as_photo=True)
        page_file = DEBUG_DIR / "form_problem.html"
        page_file.write_text(page.content())
        send_file(cfg, page_file, "Page code - send this on to get the selectors fixed.")
    except Exception as exc:
        log(f"could not capture the form: {exc}")


def deliver(page, body, sel, really_send, cfg=None):
    clicked, used = click_first(page, sel["contact_button"], timeout=8000)
    if not clicked:
        if cfg:
            form_snapshot(page, cfg, "could not find the 'Nachricht senden' button")
        return False, "no contact button"
    page.wait_for_timeout(3000)

    dismiss_safety_tips(page, sel)
    try:
        box = page.locator(sel["message_box"]).first
        box.wait_for(timeout=8000)
        box.fill(body)
    except Exception as exc:
        if cfg:
            form_snapshot(page, cfg, f"no message box ({str(exc)[:120]})")
        return False, f"no message box ({exc})"
    if not really_send:
        return True, "DRY RUN - not sent"
    clicked, _ = click_first(page, sel["send_button"], timeout=8000)
    if not clicked:
        if cfg:
            form_snapshot(page, cfg, "could not click the Senden button")
        return False, "could not click send"
    page.wait_for_timeout(3000)
    return True, "sent"


# --- config -----------------------------------------------------------------


SAMESITE = {"lax": "Lax", "strict": "Strict", "no_restriction": "None",
            "none": "None", "unspecified": "Lax", "": "Lax"}


def cookies_from_env():
    """
    Accept a browser cookie export so we never have to log in at all.

    WG-Gesucht asks for an emailed code when it sees a new device, which a bot
    cannot answer. Logging in yourself once in a normal browser and handing over
    the resulting cookies avoids the whole problem.

    Understands both a Cookie-Editor style export (a list of cookies) and
    Playwright's own storage_state format.
    """
    raw = os.environ.get("WG_COOKIES", "").strip()
    if not raw:
        # running locally: just drop the export into cookies.json next to this file
        local = HERE / "cookies.json"
        if local.exists():
            raw = local.read_text().strip()
            log("using cookies.json")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"WG_COOKIES is not valid JSON: {exc}")
        return []

    items = data.get("cookies", []) if isinstance(data, dict) else data
    cookies = []
    for c in items:
        name, value = c.get("name"), c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain") or ".wg-gesucht.de"
        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
            "sameSite": SAMESITE.get(str(c.get("sameSite", "lax")).lower(), "Lax"),
        }
        expires = c.get("expires", c.get("expirationDate"))
        if expires and expires > 0:
            cookie["expires"] = int(expires)
        cookies.append(cookie)
    log(f"loaded {len(cookies)} cookie(s) from WG_COOKIES")
    return cookies


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
        cookies = cookies_from_env()
        if cookies:
            try:
                context.add_cookies(cookies)
            except Exception as exc:
                log(f"could not use those cookies: {exc}")
        page = context.new_page()

        if args.inspect:
            DEBUG_DIR.mkdir(exist_ok=True)
            visit(page, cfg["searches"][0]["url"])
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
                if facts.get("unreachable"):
                    log(f"  could not open: {listing['title'][:50]} - leaving it for next time")
                    continue
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

                body = compose(search["message"], facts, extras, cfg, listing)
                ok, result = deliver(page, body, sel, really_send, cfg)
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
