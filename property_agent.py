from __future__ import annotations
    requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        },
        timeout=20,
    ).raise_for_status()


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
        item for item in scored if item["score"] >= threshold and item["url"] not in seen_urls
    ]

    REPORT_PATH.write_text(format_markdown(new_matches), encoding="utf-8")

    for item in new_matches:
        seen_urls.add(item["url"])
    save_json(SEEN_PATH, sorted(seen_urls))

    telegram_token = Path(".telegram_token").read_text(encoding="utf-8").strip() if Path(".telegram_token").exists() else ""
    telegram_chat_id = Path(".telegram_chat_id").read_text(encoding="utf-8").strip() if Path(".telegram_chat_id").exists() else ""

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
                        item['url'],
                    ]
                )
            )
        message = "New Rightmove matches\n\n" + "\n\n".join(chunks)
        send_telegram_message(telegram_token, telegram_chat_id, message)

    print(format_markdown(new_matches))


if __name__ == "__main__":
    main()