from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

CONFIG_PATH = Path("config.json")
SEEN_PATH = Path("seen_listings.json")
REPORT_PATH = Path("latest_report.md")
REPORTS_DIR = Path("reports")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_price(text: str) -> Optional[int]:
    match = re.search(r"£\s*([\d,]+)", text or "")
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def parse_bedrooms(text: str) -> Optional[int]:
    patterns = [r"(\d+)\s+bed", r"(\d+)\s+bedroom"]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def infer_property_type(text: str) -> Optional[str]:
    types = [
        "semi-detached",
        "terraced",
        "detached",
        "bungalow",
        "house",
        "flat",
        "apartment",
    ]
    lower = text.lower()
    for item in types:
        if item in lower:
            return item
    return None


def fetch_html(url: str) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=25,
    )
    response.raise_for_status()
    return response.text


def extract_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    listings: list[dict] = []
    seen = set()

    cards = soup.find_all(
        ["article", "div"],
        class_=re.compile(r"property|card|result", re.I),
    )

    for card in cards:
        text = clean_text(card.get_text(" "))
        if not text:
            continue

        anchor = card.find(
            "a",
            href=re.compile(
                r"/properties/\d+|https://www\.rightmove\.co\.uk/properties/\d+"
            ),
        )
        if not anchor:
            continue

        href = anchor.get("href", "")
        if href.startswith("/"):
            url = f"https://www.rightmove.co.uk{href}"
        elif href.startswith("http"):
            url = href
        else:
            continue

        if url in seen:
            continue
        seen.add(url)

        title = clean_text(anchor.get_text(" ")) or "Property listing"
        price_match = re.search(r"£\s*[\d,]+", text)
        price_text = price_match.group(0) if price_match else "Price not found"

        listings.append(
            {
                "title": title,
                "url": url,
                "price_text": price_text,
                "price_value": parse_price(price_text),
                "bedrooms": parse_bedrooms(text),
                "property_type": infer_property_type(text),
                "text": text[:1200],
            }
        )

    return listings


def score_listing(item: dict, criteria: dict) -> tuple[int, list[str]]:
    text = f"{item.get('title', '')} {item.get('text', '')}".lower()
    score = 0
    reasons: list[str] = []

    price_value = item.get("price_value")
    max_price = criteria.get("max_price")
    if price_value is not None and max_price is None:
        score += 3
        reasons.append(f"price found ({item['price_text']})")
    elif price_value is not None and max_price is not None:
        if price_value <= max_price:
            score += 20
            reasons.append("within budget")
        else:
            score -= 50
            reasons.append("over budget")

    min_bedrooms = criteria.get("min_bedrooms")
    bedrooms = item.get("bedrooms")
    if min_bedrooms is not None and bedrooms is not None:
        if bedrooms >= min_bedrooms:
            score += 12
            reasons.append(f"{bedrooms} bedrooms")
        else:
            score -= 20
            reasons.append(f"only {bedrooms} bedrooms")

    prop_type = (item.get("property_type") or "").lower()
    allowed_types = [x.lower() for x in criteria.get("property_types", [])]
    if prop_type:
        if prop_type in allowed_types:
            score += 8
            reasons.append(prop_type)
        else:
            score -= 8
            reasons.append(f"less preferred type: {prop_type}")

    matched_locations = [
        loc for loc in criteria.get("preferred_locations", [])
        if loc.lower() in text
    ]
    if matched_locations:
        score += 8 + len(matched_locations)
        reasons.append("location match")

    nice_to_have = [
        keyword
        for keyword in criteria.get("nice_to_have_keywords", [])
        if keyword.lower() in text
    ]
    if nice_to_have:
        score += 6 * len(nice_to_have)
        reasons.append("signals: " + ", ".join(nice_to_have[:4]))

    must_have = criteria.get("must_include_keywords", [])
    missing_must_have = [
        keyword for keyword in must_have if keyword.lower() not in text
    ]
    if must_have:
        if missing_must_have:
            score -= 20
            reasons.append("missing must-haves")
        else:
            score += 12
            reasons.append("all must-haves present")

    excluded = [
        keyword
        for keyword in criteria.get("exclude_keywords", [])
        if keyword.lower() in text
    ]
    if excluded:
        score -= 100
        reasons.append("excluded: " + ", ".join(excluded))

    return score, reasons


def format_markdown(matches: list[dict]) -> str:
    lines = ["# Rightmove investment shortlist", ""]
    if not matches:
        lines.append("No new qualifying listings found.")
        return "\n".join(lines)

    for i, item in enumerate(matches, start=1):
        lines += [
            f"## {i}. {item['title']}",
            f"- Score: **{item['score']}**",
            f"- Price: {item['price_text']}",
            f"- Bedrooms: {item.get('bedrooms', 'Unknown')}",
            f"- Type: {item.get('property_type') or 'Unknown'}",
            f"- Why it matched: {', '.join(item['reasons'])}",
            f"- Link: {item['url']}",
            "",
        ]
    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        },
        timeout=20,
    ).raise_for_status()


def save_reports(report_text: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    dated_report_path = REPORTS_DIR / f"report_{timestamp}.md"

    REPORT_PATH.write_text(report_text, encoding="utf-8")
    dated_report_path.write_text(report_text, encoding="utf-8")

    return dated_report_path


def main() -> None:
    config = load_json(CONFIG_PATH, {})
    criteria = config.get("criteria", {})
    search_urls = config.get("search_urls", [])
    seen_urls = set(load_json(SEEN_PATH, []))

    all_items: list[dict] = []

    for url in search_urls:
        try:
            html = fetch_html(url)
            items = extract_listings(html)
            all_items.extend(items)
            time.sleep(1)
        except Exception as exc:
            print(f"Failed to fetch {url}: {exc}")

    deduped: dict[str, dict] = {}
    for item in all_items:
        deduped[item["url"]] = item

    scored: list[dict] = []
    for item in deduped.values():
        score, reasons = score_listing(item, criteria)
        item["score"] = score
        item["reasons"] = reasons
        scored.append(item)

    scored.sort(key=lambda x: x["score"], reverse=True)

    threshold = criteria.get("min_score_to_report", 15)
    new_matches = [
        item
        for item in scored
        if item["score"] >= threshold and item["url"] not in seen_urls
    ]

    report_text = format_markdown(new_matches)
    dated_report_path = save_reports(report_text)

    for item in new_matches:
        seen_urls.add(item["url"])
    save_json(SEEN_PATH, sorted(seen_urls))

    telegram_token = (
        Path(".telegram_token").read_text(encoding="utf-8").strip()
        if Path(".telegram_token").exists()
        else ""
    )
    telegram_chat_id = (
        Path(".telegram_chat_id").read_text(encoding="utf-8").strip()
        if Path(".telegram_chat_id").exists()
        else ""
    )

    if new_matches and telegram_token and telegram_chat_id:
        chunks = []
        for item in new_matches[:10]:
            chunks.append(
                "\n".join(
                    [
                        f"{item['title']}",
                        f"Price: {item['price_text']}",
                        f"Score: {item['score']}",
                        f"Why: {', '.join(item['reasons'][:3])}",
                        item["url"],
                    ]
                )
            )

        message = (
            "New Rightmove matches\n\n"
            + "\n\n".join(chunks)
            + f"\n\nSaved report: {dated_report_path}"
        )
        send_telegram_message(telegram_token, telegram_chat_id, message)

    print(report_text)
    print(f"Saved latest report to: {REPORT_PATH}")
    print(f"Saved dated report to: {dated_report_path}")


if __name__ == "__main__":
    main()