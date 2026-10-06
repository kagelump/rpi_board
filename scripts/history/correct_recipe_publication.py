#!/usr/bin/env python3
"""Append-only corrections for false published-recipe ledger rows (issue #21).

The shared published-recipe ledger is append-only: a mistaken
``recipe_published`` row is never edited or deleted. Instead, a
``recipe_publication_corrected`` row keyed by the exact ``artwork_sha256`` is
appended. Readers (``published_recipes``, ``recent_published_recipes``,
novelty/cooldown selection, and ``GET /api/art-recipes``) then ignore the
retired publication, while the original row stays in the raw ledger for audit.

Issue #21 rollout for the two umbrella publications booked after suppressed dry
2026-10-06 deliveries:

    python3 scripts/history/correct_recipe_publication.py \
        --known-suppressed-dry-oct6

The command is idempotent: running it again appends nothing. Generic one-off
corrections are also supported:

    python3 scripts/history/correct_recipe_publication.py \
        --artwork-sha256 <exact-sha256> --reason "why this row is false"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from scripts.common import load_settings
from scripts.openrouter import art_recipes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append idempotent corrections for false published-recipe rows"
    )
    parser.add_argument(
        "--known-suppressed-dry-oct6",
        action="store_true",
        help=(
            "correct the two umbrella publications booked for the dry "
            "2026-10-06 08:00/13:00 deliveries whose recipe was suppressed"
        ),
    )
    parser.add_argument(
        "--artwork-sha256",
        action="append",
        default=[],
        metavar="HASH",
        help="exact artwork sha256 of a fictitious recipe_published row (repeatable)",
    )
    parser.add_argument(
        "--reason",
        help="human-readable reason; required with --artwork-sha256",
    )
    parser.add_argument(
        "--issue",
        default="#21",
        help="tracking issue reference recorded on the correction (default: #21)",
    )
    parser.add_argument(
        "--corrected-at",
        help="optional ISO-8601 timestamp, for deterministic rollout/tests",
    )
    parser.add_argument(
        "--ledger-file",
        help="override the configured art-recipe ledger path (tests/offline review)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.known_suppressed_dry_oct6 and not args.artwork_sha256:
        parser.error("specify --known-suppressed-dry-oct6 and/or --artwork-sha256")
    if args.artwork_sha256 and not args.reason:
        parser.error("--reason is required with --artwork-sha256")

    settings = load_settings()
    if args.ledger_file:
        settings = dict(settings)
        settings["runtime"] = dict(settings.get("runtime") or {})
        settings["runtime"]["art_recipe_ledger_file"] = args.ledger_file

    results: list[dict] = []
    if args.known_suppressed_dry_oct6:
        results.extend(
            art_recipes.correct_legacy_suppressed_publications(
                settings, corrected_at=args.corrected_at
            )
        )
    for artwork_sha256 in args.artwork_sha256:
        results.append(
            art_recipes.correct_publication(
                settings,
                artwork_sha256=artwork_sha256,
                reason=args.reason,
                issue=args.issue,
                corrected_at=args.corrected_at,
            )
        )

    print(json.dumps(results, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    if any(str(result.get("status", "")).startswith("skipped") for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
