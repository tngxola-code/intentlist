"""Synthetic fixture corpus for offline demos and tests.

Every person, address and page here is invented. Pages live under the reserved `.test` TLD and the
emails under `*.test` domains, so nothing can ever be mistaken for, or delivered to, a real person.
The corpus deliberately includes the failure modes the pipeline must catch: handles instead of names,
missing emails, vague requests, seller posts, stale posts, duplicates, catch-all and bouncing addresses,
role addresses, and cross-thread repeats of the same person.
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from html import escape
from pathlib import Path

FIRST = ["Adriana", "Bram", "Chiara", "Declan", "Elif", "Farouk", "Greta", "Hiroshi", "Ines", "Jonas", "Kalinda",
         "Lorenzo", "Maren", "Nikhil", "Odile", "Pieter", "Quentin", "Rosalind", "Soren", "Talia", "Ulrich", "Vesna",
         "Wendell", "Xenia", "Yusuf", "Zora", "Amadou", "Brigid", "Cosimo", "Dagny"]
LAST = ["Achterberg", "Bellweather", "Castellane", "Dunmore", "Eskildsen", "Fairbrook", "Gallardo", "Holloway",
        "Ingersoll", "Jablonski", "Kerrigan", "Lindqvist", "Marchetti", "Nakamura", "Oyelaran", "Prendergast",
        "Quarles", "Rasmussen", "Sokolova", "Thorvald", "Underhill", "Valcourt", "Whitlock", "Yardley", "Zeller"]
MAIL_DOMAINS = ["inbox-demo.test", "postbox-demo.test", "mailhub-demo.test"]

WATCH_ITEMS = [
    ("Rolex", "Daytona", "116500LN with white dial, full set preferred"),
    ("Patek Philippe", "Nautilus", "5711/1A in blue, any year, papers essential"),
    ("Audemars Piguet", "Royal Oak", "15202ST Jumbo, 2015 or later"),
    ("Omega", "Speedmaster", "pre-moon 105.012 in honest condition"),
    ("Rolex", "Submariner", "5513 with meters-first dial, original"),
    ("Heuer", "Autavia", "2446 first execution, 1960s"),
    ("F.P. Journe", "Chronomètre Bleu", "39mm in tantalum"),
    ("Vacheron Constantin", "Overseas", "4500V blue dial"),
    ("Cartier", "Crash", "London Crash, any year"),
    ("Grand Seiko", "Snowflake", "SBGA211, box and papers"),
]
CAR_ITEMS = [
    ("Porsche", "911", "1973 Carrera RS 2.7 Touring, matching numbers"),
    ("Ferrari", "Testarossa", "1987 monospecchio, under 30k miles"),
    ("Mercedes-Benz", "300SL", "1955 Gullwing, restored or original"),
    ("Jaguar", "E-Type", "Series 1 3.8 roadster, 1962"),
    ("Aston Martin", "DB5", "1964, left-hand drive preferred"),
    ("Toyota", "2000GT", "any year, genuine example"),
    ("Lancia", "Delta Integrale", "Evo II, 1993, low mileage"),
    ("BMW", "E30 M3", "1988 in Lachssilber"),
    ("Land Rover", "Defender", "1997 Defender 90 NAS, soft top"),
    ("Ferrari", "Dino", "1972 246 GTS"),
]
INTENTS = ["WTB:", "Looking for", "ISO", "Wanted:", "Want to buy", "Searching for"]


def _post(handle: str, posted: str, body: str, signature: str | None, email: str | None) -> str:
    sig = f"<p>{escape(signature)}</p>" if signature else ""
    mail = f'<p>Contact: <a href="mailto:{email}">{email}</a></p>' if email else ""
    return (f'<article class="post"><div class="meta"><span class="author">{escape(handle)}</span> '
            f'<span class="date">Posted: {posted}</span></div><div class="body"><p>{escape(body)}</p>{sig}{mail}</div></article>')


def _page(title: str, posts: list[str]) -> str:
    return (f"<!doctype html><html><head><title>{escape(title)}</title></head><body>"
            f"<nav>Demo Forum · Home · Marketplace · Wanted</nav><h1>{escape(title)}</h1>{''.join(posts)}"
            f"<footer>Demo Forum · contact: info@demo-forum.test</footer></body></html>")


def generate(out_dir: Path, per_category: int = 130, seed: int = 7, today: date | None = None) -> dict:
    rng = random.Random(seed)
    today = today or date.today()
    pages_dir = Path(out_dir) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict] = {}
    people_used: list[tuple[str, str]] = []
    stats = {"good": 0, "decoys": 0}

    def person() -> tuple[str, str]:
        first, last = rng.choice(FIRST), rng.choice(LAST)
        n = rng.randint(1, 999)
        domain = rng.choice(MAIL_DOMAINS)
        return f"{first} {last}", f"{first.lower()}.{last.lower()}{n}@{domain}"

    for slug, items in (("watches", WATCH_ITEMS), ("cars", CAR_ITEMS)):
        for i in range(per_category):
            maker, model, detail = items[i % len(items)]
            posted = today - timedelta(days=rng.randint(1, 400))
            name, email = person()
            kind = rng.choices(["good", "handle_only", "no_email", "vague", "seller", "stale", "catchall", "bounce",
                                "role", "repeat"], weights=[64, 5, 5, 4, 4, 3, 5, 4, 2, 4])[0]
            intent = rng.choice(INTENTS)
            body = f"{intent} {maker} {model} {detail}. Serious buyer, can travel or arrange shipping."
            signature: str | None = f"Regards, {name}"
            posted_s = posted.isoformat()
            if kind == "handle_only":
                signature = None
            elif kind == "no_email":
                email = None
            elif kind == "vague":
                body = f"{intent} something nice from {maker}, open to ideas, budget flexible."
            elif kind == "seller":
                body = f"FS: {maker} {model} {detail}. Price drop, offers considered."
            elif kind == "stale":
                posted_s = (today - timedelta(days=365 * 4)).isoformat()
            elif kind == "catchall":
                email = email.split("@")[0] + "@catchall-demo.test"
            elif kind == "bounce":
                email = email.split("@")[0] + "@bounce-demo.test"
            elif kind == "role":
                email = "sales@" + rng.choice(MAIL_DOMAINS)
            elif kind == "repeat" and people_used:
                name, email = rng.choice(people_used)
                signature = f"Regards, {name}"
            if kind == "good":
                people_used.append((name, email))
                stats["good"] += 1
            else:
                stats["decoys"] += 1
            handle = f"{name.split()[0].lower()}_{rng.randint(10, 99)}"
            posts = [_post(handle, posted_s, body, signature, email)]
            # Replies by other users add realistic noise: no intent, no contact details.
            for _ in range(rng.randint(0, 2)):
                posts.append(_post(f"collector{rng.randint(100, 999)}", posted_s,
                                   rng.choice(["Good luck with the hunt!", "Try the dealer in town.", "Bump."]), None, None))
            url = f"https://demo-forum.test/{slug}/wanted/thread-{i:04d}"
            fname = f"{slug}-{i:04d}.html"
            title = f"{intent} {maker}, ideas welcome" if kind == "vague" else f"{intent} {maker} {model}"
            (pages_dir / fname).write_text(_page(title, posts), encoding="utf-8")
            index[url] = {"file": fname, "title": title, "snippet": body[:160], "kind": kind}
    (pages_dir / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return stats
