"""Seed and inspect the Midnight Tavern scope in PowerContext.

Examples::

    python3 dify/scripts/tavern_demo.py seed --token "$POWERCONTEXT_DEMO_TOKEN"
    python3 dify/scripts/tavern_demo.py inspect --token "$POWERCONTEXT_DEMO_TOKEN"

The script only uses the Python standard library and the public PowerContext
HTTP API. It is the same scope the Dify app ensures at runtime: title
"午夜记账酒馆" and an idempotency key from ``--scope-key``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_SCOPE_KEY = "midnight-tavern-v1"
TITLE = "午夜记账酒馆"
SUMMARY = "酒馆常客的跨会话账本"
SEED_MEMORIES = (
    "客人上次把花生米倒进钢琴，钢琴师至今不肯碰 Do 键。",
    "客人欠老白 20 元酒钱，说下次一定还。",
    "客人自称是城里的调酒师，却把苦艾酒兑错了。",
)


def post(base_url: str, token: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{path} failed with HTTP {error.code}: {detail}") from error


def ensure_scope(base_url: str, token: str, scope_key: str) -> str:
    response = post(
        base_url,
        token,
        "/v1/scopes",
        {"title": TITLE, "summary": SUMMARY, "idempotency_key": scope_key},
    )
    return response["scope_id"]


def list_entries(base_url: str, token: str, scope_id: str) -> list[dict]:
    response = post(base_url, token, "/v1/memory/entries/list", {"scope_id": scope_id})
    return response.get("entries", [])


def remember(base_url: str, token: str, scope_id: str, text: str) -> None:
    post(
        base_url,
        token,
        "/v1/memory/remember",
        {"scope_id": scope_id, "kind": "tavern-event", "text": text, "reason": "seed demo"},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Midnight Tavern PowerContext helper")
    parser.add_argument("command", choices=("seed", "inspect"))
    parser.add_argument("--base-url", default=os.environ.get("POWERCONTEXT_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.environ.get("POWERCONTEXT_TOKEN") or os.environ.get("POWERCONTEXT_DEMO_TOKEN"))
    parser.add_argument("--scope-key", default=os.environ.get("TAVERN_SCOPE_KEY", DEFAULT_SCOPE_KEY))
    parser.add_argument("--json", action="store_true", help="print raw entries as JSON")
    arguments = parser.parse_args()
    if not arguments.token:
        parser.error("set --token or POWERCONTEXT_TOKEN / POWERCONTEXT_DEMO_TOKEN")

    scope_id = ensure_scope(arguments.base_url, arguments.token, arguments.scope_key)
    entries = list_entries(arguments.base_url, arguments.token, scope_id)
    if arguments.command == "seed":
        if entries:
            print(f"scope {scope_id} already has {len(entries)} entries; seed skipped")
        else:
            for text in SEED_MEMORIES:
                remember(arguments.base_url, arguments.token, scope_id, text)
            entries = list_entries(arguments.base_url, arguments.token, scope_id)
            print(f"seeded {len(entries)} entries into scope {scope_id}")
    if arguments.json:
        print(json.dumps({"scope_id": scope_id, "entries": entries}, ensure_ascii=False, indent=2))
        return
    print(f"scope: {scope_id}")
    print(f"entries: {len(entries)}")
    for index, entry in enumerate(entries, start=1):
        print(f"{index}. [{entry.get('kind', '')}] {entry.get('text', '')}")


if __name__ == "__main__":
    sys.exit(main())
