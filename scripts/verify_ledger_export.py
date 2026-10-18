#!/usr/bin/env python3
"""Verify an exported audit ledger independently of the service (REQ-06, AC-06, Q4.3).

Standard library only, so another team can run it without this codebase. It reimplements the
chain rule documented in app/modules/ledger/chain.py (a unit test keeps the two in step):

    entry_hash = sha256(json.dumps({seq, entry_type, event_id, actor_id, at, payload, prev_hash},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    entry 1's prev_hash is 64 zeros; entry n's prev_hash is entry n-1's entry_hash.

Fetch every page from a running service (optionally saving the export), or check saved pages:

    python scripts/verify_ledger_export.py --base-url https://host --api-key acm_... --save l.json
    python scripts/verify_ledger_export.py l.json

To demonstrate tamper detection (AC-06), edit any payload in a saved export and run it again.
Exits 0 when the chain verifies, 1 when it doesn't, 2 on usage or I/O errors. With
--expect-head-seq/--expect-head-hash it also fails if entries were removed from the end of the
chain since a head you recorded earlier.
"""

import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request

GENESIS_HASH = "0" * 64
FIELDS = ("seq", "entry_type", "event_id", "actor_id", "at", "payload", "prev_hash")
PAGE_SIZE = 10_000


def compute_entry_hash(entry: dict) -> str:
    document = {field: entry[field] for field in FIELDS}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify(entries: list[dict]) -> dict:
    expected_seq, prev_hash = 1, GENESIS_HASH
    for checked, entry in enumerate(entries, start=1):
        if (
            entry["seq"] != expected_seq
            or entry["prev_hash"] != prev_hash
            or entry["entry_hash"] != compute_entry_hash(entry)
        ):
            return {
                "valid": False,
                "entries_checked": checked,
                "first_mismatch": entry["seq"],
                "head_seq": expected_seq - 1,
                "head_hash": prev_hash,
            }
        expected_seq, prev_hash = entry["seq"] + 1, entry["entry_hash"]
    return {
        "valid": True,
        "entries_checked": len(entries),
        "first_mismatch": None,
        "head_seq": expected_seq - 1,
        "head_hash": prev_hash,
    }


def fetch_all(base_url: str, api_key: str) -> list[dict]:
    if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
        raise ValueError("--base-url must be an http(s) URL")
    entries: list[dict] = []
    after_seq: int | None = 0
    while after_seq is not None:
        query = urllib.parse.urlencode({"after_seq": after_seq, "limit": PAGE_SIZE})
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/v1/ledger/export?{query}",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        # Scheme restricted to http(s) above, so file:// and similar can't be opened.
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
            page = json.load(response)
        entries.extend(page["entries"])
        after_seq = page["next_after_seq"]
    return entries


def load_files(paths: list[str]) -> list[dict]:
    entries: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        entries.extend(data["entries"] if isinstance(data, dict) else data)
    return sorted(entries, key=lambda e: e["seq"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an exported audit ledger.")
    parser.add_argument("files", nargs="*", help="saved export pages (JSON)")
    parser.add_argument("--base-url", help="fetch from this service instead of files")
    parser.add_argument("--api-key", help="viewer, reviewer or admin key for --base-url")
    parser.add_argument("--save", help="write the fetched entries to this file")
    parser.add_argument("--expect-head-seq", type=int)
    parser.add_argument("--expect-head-hash")
    args = parser.parse_args(argv)

    try:
        if args.base_url:
            if not args.api_key:
                parser.error("--api-key is required with --base-url")
            entries = fetch_all(args.base_url, args.api_key)
            if args.save:
                with open(args.save, "w", encoding="utf-8") as f:
                    json.dump({"entries": entries}, f, indent=2)
        elif args.files:
            entries = load_files(args.files)
        else:
            parser.error("give export files or --base-url")
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = verify(entries)
    if result["valid"] and args.expect_head_seq is not None:
        if result["head_seq"] < args.expect_head_seq:
            result.update(valid=False, truncated=True)
        elif args.expect_head_hash is not None:
            match = next((e for e in entries if e["seq"] == args.expect_head_seq), None)
            if match is None or match["entry_hash"] != args.expect_head_hash:
                result.update(valid=False, head_mismatch=True)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
