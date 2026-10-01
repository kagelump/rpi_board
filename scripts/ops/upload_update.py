#!/usr/bin/env python3
"""Upload a native payload or complete board through the loopback API/SSH tunnel."""
import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--payload", type=Path, help="Native board payload JSON")
    images = parser.add_mutually_exclusive_group()
    images.add_argument("--hero", type=Path, help="PNG/JPEG artwork for composition")
    images.add_argument("--board", type=Path, help="Complete board PNG/JPEG at device dimensions")
    parser.add_argument("--metadata", type=Path, help="Optional provenance JSON object")
    parser.add_argument("--preview", action="store_true", help="Archive/render only; do not publish")
    parser.add_argument("--timeout", type=float, default=180, help="HTTP timeout in seconds")
    args = parser.parse_args()
    if not args.payload and not args.board:
        parser.error("--payload or --board is required")
    body = {"publish": not args.preview}
    if args.payload:
        body["payload"] = json.loads(args.payload.read_text())
    if args.metadata:
        body["metadata"] = json.loads(args.metadata.read_text())
    for key, path in (("hero_image", args.hero), ("board_image", args.board)):
        if path:
            body[key] = base64.b64encode(path.read_bytes()).decode("ascii")
    request = urllib.request.Request(args.url.rstrip("/") + "/create_update",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            print(json.dumps(json.load(response), indent=2))
    except urllib.error.HTTPError as error:
        print(f"HTTP {error.code}: {error.read().decode()}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"Upload failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
