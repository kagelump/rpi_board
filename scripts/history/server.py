#!/usr/bin/env python3
"""Local HTTP API and dashboard for historic generation records."""
from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.append(str(Path(__file__).resolve().parents[2]))

from scripts.common import load_settings
from scripts.history.store import GenerationStore
from scripts.history.create_update import MAX_BODY_BYTES, SCHEMA, UpdateBusy, UpdateFailed, UpdateService
from scripts.openrouter import art_recipes


DASHBOARD_DIR = Path(__file__).with_name("web")
DASHBOARD_HTML = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")
STATIC_FILES = {
    "/static/review.css": ("review.css", "text/css; charset=utf-8"),
    "/static/review.js": ("review.js", "text/javascript; charset=utf-8"),
}


class HistoryRequestHandler(BaseHTTPRequestHandler):
    store: GenerationStore
    updates: UpdateService

    def log_message(self, format, *args):
        print(f"[history-server] {self.address_string()} {format % args}")

    def _json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self):
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _post_rating(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin.rstrip("/") != f"http://{self.headers.get('Host', '')}":
            self._json({"error": "cross-origin rating writes are not supported"}, HTTPStatus.FORBIDDEN)
            return
        if self.headers.get_content_type() != "application/json":
            self._json({"error": "Content-Type must be application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            self._json({"error": "one Content-Length header is required; chunked uploads are unsupported"}, HTTPStatus.LENGTH_REQUIRED)
            return
        try:
            length = int(self.headers["Content-Length"])
            if length <= 0:
                raise ValueError("request body must not be empty")
            if length > 32768:
                self._json({"error": "rating request exceeds 32768 bytes"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            self.connection.settimeout(15)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete request body")

            def invalid_constant(value):
                raise ValueError(f"invalid JSON constant: {value}")

            request = json.loads(raw, parse_constant=invalid_constant)
            if not isinstance(request, dict):
                raise ValueError("request body must be a JSON object")
            target_type = request.get("target_type")
            target_id = request.get("target_id")
            if not isinstance(target_id, str) or not target_id.strip():
                raise ValueError("target_id must be a non-empty string")
            result = self.store.add_rating(
                target_type,
                target_id,
                request.get("rating"),
                request.get("comment", ""),
            )
        except KeyError as error:
            self._json({"error": str(error).strip("'")}, HTTPStatus.NOT_FOUND)
        except TimeoutError:
            self._json({"error": "request body timed out"}, HTTPStatus.REQUEST_TIMEOUT)
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            self._json({"error": f"internal error: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            self._json(result, HTTPStatus.CREATED)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/ratings":
            self._post_rating()
            return
        if path != "/create_update":
            self._not_found()
            return
        if self.headers.get("Origin") is not None:
            self._json({"error": "browser-origin writes are not supported; use an API client"}, HTTPStatus.FORBIDDEN)
            return
        if self.headers.get_content_type() != "application/json":
            self._json({"error": "Content-Type must be application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            self._json({"error": "one Content-Length header is required; chunked uploads are unsupported"}, HTTPStatus.LENGTH_REQUIRED)
            return
        try:
            length = int(self.headers["Content-Length"])
            if length <= 0:
                raise ValueError("request body must not be empty")
            if length > MAX_BODY_BYTES:
                self._json({"error": f"request exceeds {MAX_BODY_BYTES} bytes"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            self.connection.settimeout(15)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete request body")
            def invalid_constant(value):
                raise ValueError(f"invalid JSON constant: {value}")
            request = json.loads(raw, parse_constant=invalid_constant)
            result = self.updates.create(request)
        except UpdateBusy as error:
            self._json({"error": str(error)}, HTTPStatus.CONFLICT)
        except UpdateFailed as error:
            self._json({"error": str(error), "run_id": error.run_id,
                        "run_url": f"/api/runs/{error.run_id}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except TimeoutError:
            self._json({"error": "request body timed out"}, HTTPStatus.REQUEST_TIMEOUT)
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            self._json({"error": f"internal error: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            self._json(result, HTTPStatus.CREATED)

    @staticmethod
    def _boolean_query(query, name):
        value = query.get(name, [None])[0]
        if value is None:
            return None
        if value not in ("true", "false"):
            raise ValueError(f"{name} must be true or false")
        return value == "true"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/":
                data = (DASHBOARD_DIR / "index.html").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
                return
            if path in STATIC_FILES:
                filename, mime_type = STATIC_FILES[path]
                data = (DASHBOARD_DIR / filename).read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/health":
                self._json({"ok": True, "schema_version": 1, "create_update": True})
                return
            if path == "/create_update":
                self._json(SCHEMA)
                return
            if path == "/api/stats":
                self._json(self.store.stats())
                return
            if path == "/api/ratings":
                query = parse_qs(parsed.query)
                self._json(self.store.list_ratings(
                    run_id=query.get("run_id", [None])[0],
                    limit=int(query.get("limit", [200])[0]),
                    offset=int(query.get("offset", [0])[0]),
                ))
                return
            if path in ("/api/art-recipes", "/api/art_recipes"):
                # Issue #18: compact, read-only view of the shared published-
                # artwork ledger and selection policy for external/Codex
                # authoring. Contains no credentials or request bodies.
                self._json(art_recipes.history_summary(getattr(self, "settings", None)))
                return
            if path == "/api/runs":
                query = parse_qs(parsed.query)
                self._json(self.store.list_runs(
                    limit=int(query.get("limit", [50])[0]),
                    offset=int(query.get("offset", [0])[0]),
                    target_date=query.get("target_date", [None])[0],
                    status=query.get("status", [None])[0],
                    style=query.get("style", [None])[0],
                    rating=query.get("rating", [None])[0],
                    has_final=self._boolean_query(query, "has_final"),
                ))
                return
            parts = path.split("/")
            if len(parts) == 4 and parts[1:3] == ["api", "runs"]:
                result = self.store.get_run(parts[3])
                self._json(result) if result else self._not_found()
                return
            if len(parts) == 4 and parts[1:3] == ["api", "snapshots"]:
                result = self.store.get_snapshot(parts[3])
                self._json(result) if result else self._not_found()
                return
            if len(parts) == 4 and parts[1:3] == ["api", "artifacts"]:
                result = self.store.get_artifact(parts[3])
                if not result:
                    self._not_found(); return
                metadata, artifact_path = result
                if not artifact_path.exists():
                    self._not_found(); return
                data = artifact_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", metadata["mime_type"])
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(data)
                return
            self._not_found()
        except (ValueError, TypeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:  # noqa: BLE001
            self._json({"error": f"internal error: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def make_server(
    host: str,
    port: int,
    store: GenerationStore | None = None,
    updates=None,
    settings: dict | None = None,
):
    store = store or GenerationStore()
    handler = type("ConfiguredHistoryHandler", (HistoryRequestHandler,), {
        "store": store,
        "updates": updates or UpdateService(store, settings),
        "settings": settings or load_settings(),
    })
    return ThreadingHTTPServer((host, port), handler)


def main():
    parser = argparse.ArgumentParser(description="Serve the weather-board history dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--records")
    parser.add_argument("--artifacts-dir")
    parser.add_argument("--ratings")
    args = parser.parse_args()
    store = GenerationStore(args.records, args.artifacts_dir, args.ratings)
    server = make_server(args.host, args.port, store)
    print(f"History dashboard: http://{args.host}:{server.server_port}")
    print(f"Record ledger: {store.record_file}")
    print(f"Ratings ledger: {store.ratings_file}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
