# Weather E-Ink Board

Image-first daily weather briefing for a Waveshare e-paper display (`960x640`), built for Raspberry Pi.

The board is designed as a **morning poster**: a generated weather illustration takes most of the screen, with minimal text overlay and a small high-temperature corner chip.

## Features

- Daily weather pipeline: fetch -> transform -> brief -> image -> compose -> display.
- Three refreshes a day that share one fixed daily theme (see *Update schedule*).
- Deterministic fallback path when APIs fail.
- OpenRouter text brief generation (optional).
- Image generation via fal or OpenRouter (optional).
- Art guardrail: after generation, rejects baked-in text, collage/photo-in-frame
  art, and art that leans on colours the 4-ink panel cannot show (off-palette),
  regenerating within a bounded retry. If every attempt is explicitly rejected,
  the flagged candidate is never published: the run reuses a previously
  accepted hero or falls back to the deterministic pictogram, and the run is
  marked degraded with the rejection reasons. See
  `scripts/openrouter/art_guardrail.py`.
- Full-screen poster layout with minimal text.
- Local preview mode and Raspberry Pi hardware mode.
- Eval framework (`scripts/eval/`): A/B prompt variants and compare image models
  with a vision-LLM judge plus deterministic e-ink colour metrics.
- Local generation-history API and dashboard with an append-only event ledger,
  immutable image artifacts, model inputs/prompts, styles, seeds, guardrail
  results, stage logs, and failure history.

## Repository Layout

```text
config/
  settings.json
  sample_openmeteo.json
  prompt_templates/
scripts/
  weather/
  openrouter/      # brief + image generation, art guardrail
  render/          # compose_board, palette_quantize, palette_metrics
  display/
  eval/            # prompt A/B + image-model comparison harness
  history/         # append-only storage, recorder CLI, HTTP API + dashboard
tests/
tokyo_weather.sh
plan.md
```

## Update schedule

The systemd timer (`scripts/ops/systemd/weather-eink-board.timer`) fires three
times a day, each refresh playing a distinct role for the **forecast day**:

- **21:00** -- builds tomorrow's board from scratch, so the new day is already
  showing before you wake. Evening/night runs (>= 18:00) forecast *tomorrow*.
- **08:00** -- refreshes today's board only on a **major update** (a change of
  weather condition or rain level, or a high/low swing of ~3C+); an unchanged
  forecast reuses the 21:00 board untouched.
- **13:00** -- re-frames the brief for the **afternoon/evening** and re-renders
  the hero in the same locked style, unless the new illustration prompt is
  nearly identical to the morning's (then the existing art is kept).

All three runs for one forecast day share a **fixed theme**: the creative angle
(seeded on the target date, not the time of day) and the art style (locked per
target date) stay constant, so only the wording and any major forecast change
move between refreshes. The "major update" sensitivity and afternoon
re-render threshold are tunable in `config/settings.json`
(`regen_min_interval_seconds`, `afternoon_art_prompt_similarity_threshold`).

## Eval framework

Reusable harness under `scripts/eval/` for trying prompt/model changes against
real data before shipping:

- `run_eval.py` -- A/B prompt variants across cities; scores each board with a
  vision-LLM judge (title informativeness, art coherence, text/collage guardrail
  flags) plus deterministic e-ink colour metrics.
- `compare_models.py` -- feed one fixed prompt to several image models, N runs
  each; reports judge scores, colour balance, off-palette %, cost, latency, and
  links to each board. Supports `--append-to <run_dir>` to add a model later.
- `palette_metrics.py` lives in `scripts/render/` (it shares the device palette
  with `palette_quantize.py`) and is reused by the production art guardrail.

Outputs land under `runtime/eval*/` (gitignored).

## Requirements

- Python 3.10+
- `Pillow`
- (Optional) OpenRouter API key
- (Pi mode) Waveshare Python driver installed on the device

Install dependency:

```bash
python3 -m pip install --user pillow certifi
```

## Configuration

Main config: `config/settings.json`

Key sections:

- `location`: lat/lon/timezone and display label.
- `display.mode`:
  - `local_preview` (default) -> build image only.
  - `pi_display` -> push to Waveshare panel.
- `pipeline.enable_openrouter_brief`: enable LLM text brief.
- `pipeline.enable_openrouter_image`: enable generated hero image.
- `pipeline.image_provider`: `fal` (default) or `openrouter`.
- `pipeline.enable_image_guardrail`: post-generation QA on the hero art. When on,
  rejects baked-in text, collage/photo-in-frame, and off-palette art, then
  regenerates:
  - `image_guardrail_max_retries` (default `1`): extra attempts on a rejection.
  - `image_guardrail_timeout_seconds` (default `15`): vision-check timeout.
  - `image_guardrail_max_off_palette_pct` (default `0.15`): reject art with more
    than this fraction of pixels in colours the panel cannot show.
  Each check fails open, so the guardrail never blocks a board from rendering:
  when the vision check is unavailable (no key, timeout, network error) or the
  palette analyzer raises, the image is published but recorded as *unverified*,
  never as accepted (and never eligible for accepted-art reuse). When every
  attempt is explicitly rejected, no rejected bytes are written; the run keeps a
  previously accepted hero if one is recorded, otherwise `compose_board` draws
  its deterministic pictogram, and the run status is degraded with the rejection
  reasons. Adds ~1 vision call (+ a possible regen) per refresh; set the flag to
  `false` to disable.
- `pipeline.brief_timeout_seconds` (default `35`): per-attempt wall-clock cap
  for the non-online brief model. It is sized above the measured useful
  response latency (23s) with headroom, while still rejecting the known-slow
  online attempt and the 193s outlier. It bounds DNS/connect and each socket
  read as well as the whole attempt.
- `pipeline.brief_online_timeout_seconds` (default `20`): shorter cap for the
  optional `:online` web-search attempt. Event search is enrichment, so it gets
  a bounded slice and can never consume the non-online attempts' window.
- `pipeline.brief_total_budget_seconds` (default `100`): wall-clock budget for
  the entire brief stage, including every model attempt, response-body read,
  and retry delay. The shipped schedule (online 20s + backoff 1.5s + two
  non-online attempts of 35s each + backoff 3s = 94.5s worst case) fits inside
  it, so every attempt keeps its full cap; when it expires the pipeline
  publishes the deterministic brief instead of stalling.
- `pipeline.brief_offline_retry_count` (default `2`): bounded non-online
  attempts after an online failure (online -> offline semantics are unchanged).
- `pipeline.brief_retry_backoff_seconds` / `pipeline.brief_retry_backoff_max_seconds`
  (defaults `1.5` / `6`): capped exponential backoff between transient network
  retries (DNS/connect/TLS/read-timeout) so failures are not retried
  immediately; schema-invalid replies still retry right away.
- Brief-stage logs record each attempt's `attempt_elapsed_seconds`,
  `timeout_seconds`, and `failure_category` (`dns`, `connect`, `tls`,
  `read_timeout`, `http`, `attempt_timeout`, or `budget_exhausted`), plus a
  `brief_budget_exhausted` event when the shared stage budget runs out. A
  per-attempt timeout is retryable while stage budget remains; exhaustion stops
  retries and publishes the deterministic fallback. Retry delays are clamped to
  the remaining budget. The deterministic fallback art subject is a plain
  scene with no numbers or poster/forecast wording, so it never feeds caption
  material into the image model when the brief stage degrades.
- `openrouter.text_model`: model for text brief.
- `openrouter.image_model`: image generation model (default: `google/gemini-3.1-flash-image-preview`).
- `openrouter.image_tool_model`: model used to invoke OpenRouter image server tool (default: `openai/gpt-5.2`).

## OpenRouter API Key

The project checks for the API key in this order:

1. `OPENROUTER_API_KEY` environment variable
2. `openrouter.api_key_file` from `config/settings.json`
3. `~/.openrouter.key`
4. `~/.config/openrouter/api_key`

Recommended:

```bash
printf '%s\n' 'sk-or-v1-...' > ~/.openrouter.key
chmod 600 ~/.openrouter.key
```

## Running

Run the full pipeline:

```bash
./scripts/display/update_display.sh
```

This orchestrates:

1. `scripts/weather/fetch_weather.py`
2. `scripts/weather/transform_weather.py`
3. `scripts/openrouter/generate_brief.py`
4. `scripts/openrouter/generate_image.py`
5. `scripts/render/compose_board.py`
6. `scripts/render/palette_quantize.py`
7. `scripts/display/push_to_epd.py`

### Force a full refresh

By default a re-run reuses caches: the brief is kept when inputs are unchanged
within `regen_min_interval_seconds`, the hero is kept when the brief was cached,
and holidays are read from an on-disk cache. A cached hero is only reused when
`runtime/image_style_state.json` records a matching *accepted* validation verdict
for the file on disk; legacy state without that record, a rejected candidate, an
unverified candidate, and a candidate generated while the guardrail was disabled
are regenerated instead of reused. To bypass all of these and regenerate
everything (weather is always fetched fresh regardless):

```bash
./scripts/display/update_display.sh --force   # or: make force
```

The flag fans out to `generate_brief.py --force`, `generate_image.py --force`,
and `fetch_context.py --force`.

## Runtime Artifacts

Outputs are written under `runtime/`:

- `last_payload.json`
- `last_brief.json`
- `hero.png` (when image generation succeeds)
- `final_display.png`
- `preview.png`
- `last_success.json`

## Generation history dashboard

Every full pipeline run now records its inputs, logs, decisions, and outputs.
Start the local dashboard with:

```bash
make history
```

Then open `http://127.0.0.1:8787`. To seed it with the existing
`runtime/history.json` entries and current surviving runtime artifacts:

```bash
make history-import
```

The importer is idempotent. On Raspberry Pi, install the dashboard as a service:

```bash
make history-install
ssh -L 8787:127.0.0.1:8787 <pi-user>@<raspberry-pi>
```

The service binds to localhost by default. Binding `--host 0.0.0.0` is possible
on a trusted network, but the server intentionally has no authentication, so an
SSH tunnel is the safer default.

### On-disk storage schema

`runtime/generations.jsonl` is the canonical append-only ledger. Each line is
one complete JSON event with this envelope:

```json
{
  "schema_version": 1,
  "event_id": "unique-id",
  "recorded_at": "ISO-8601 timestamp",
  "type": "run_started|stage_started|log_recorded|snapshot_added|artifact_added|...",
  "run_id": "generation-id",
  "data": {}
}
```

Writes take an exclusive file lock, append one line, flush, and `fsync`. Readers
take a shared lock and ignore an incomplete trailing line, making recovery after
power loss straightforward. Existing records are never updated or deleted.

The server materializes and indexes the ledger in memory by run ID, target date,
status, selected style, event type, and artifact/snapshot ID. At three scheduled
runs per day this remains small enough to rebuild cheaply for many years. If the
volume eventually grows, SQLite can be added as a disposable derived index
without changing the ledger format.

Generated images are immutable files under:

```text
runtime/generations/<run-id>/<artifact-kind>-<sha256-prefix>.png
```

The ledger stores each artifact's path, MIME type, SHA-256, byte size, and image
dimensions. It also stores JSON/text snapshots for raw weather inputs, Yahoo and
Open-Meteo aggregation, day context, transformed/generated briefs, complete text
and image prompts, model request parameters and random seed, style state,
guardrail verdicts, and each pipeline stage's combined stdout/stderr.

### HTTP API

- `GET /api/health`
- `GET /api/stats`
- `GET /api/runs?limit=50&offset=0&target_date=YYYY-MM-DD&status=degraded&style=Linocut`
- `GET /api/runs/<run-id>`
- `GET /api/snapshots/<snapshot-id>`
- `GET /api/artifacts/<artifact-id>`

### Upload an externally authored update

`POST /create_update` accepts a JSON update authored on another computer (for
example by Codex). The history service still binds to **127.0.0.1:8787** by
default. Access it through an SSH tunnel:

```bash
ssh -N -L 8787:127.0.0.1:8787 <pi-user>@<raspberry-pi>
```

Save a native board payload as `update.json`:

```json
{
  "brief": {
    "headline": "A dry afternoon",
    "subtitle": "Leave the umbrella at home.",
    "accent": "yellow"
  },
  "today": {
    "daily_summary": {
      "date": "2026-10-01",
      "condition": "Clear",
      "weather_code": 0,
      "temp_min_c": 18,
      "temp_max_c": 29
    }
  },
  "day_context": {
    "date_pretty": "Thursday, October 1",
    "daypart_role": "afternoon"
  },
  "timezone": "Asia/Tokyo"
}
```

Render and archive a preview, then publish when ready:

```bash
python3 scripts/ops/upload_update.py --payload update.json --hero illustration.png --preview
python3 scripts/ops/upload_update.py --payload update.json --hero illustration.png
```

For a complete externally composed board at the configured device resolution
(normally 960×640), use `--board board.png` instead. `--payload` is optional in
this mode and is retained as metadata; the supplied board is not recomposed.
Use `--metadata provenance.json` for an arbitrary JSON object describing the
author, source, style, or notes. `--url` selects a different tunnel address.

The wire format is `application/json`:

| Field | Meaning |
| --- | --- |
| `payload` | Full native board payload shown above, including any additional fields. Required unless `board_image` is supplied. |
| `hero_image` | Optional raw base64 PNG/JPEG artwork. Without it, composition uses a weather pictogram, never the previous run's artwork. |
| `board_image` | Optional raw base64 PNG/JPEG complete board at exact device dimensions. Mutually exclusive with `hero_image`. |
| `publish` | Boolean, default `true`. `false` renders and archives without touching hardware or live output files. |
| `metadata` | Optional arbitrary JSON object, preserved in history. |

All native payload fields can be supplied and are archived. The compositor uses:

| Payload field | Rendering behavior |
| --- | --- |
| `brief.headline` | Required nonblank headline for composition. |
| `brief.subtitle` | Subtitle; falls back to `brief.tomorrow_preview`, then a default sentence. |
| `brief.accent` | `black` (or `none`), `red`, or `yellow`; default black. |
| `today.daily_summary.date` | Required ISO date; default date-chip text. |
| `today.daily_summary.temp_min_c`, `temp_max_c` | Required numeric Celsius extrema, rounded for the temperature chip. |
| `today.daily_summary.weather_code` | Optional integer WMO code for the weather glyph; unknown/omitted uses a cloud. |
| `today.daily_summary.condition` | Optional weather description. |
| `day_context.date_pretty` | Optional custom date-chip text. |
| `brief_source` | Defaults to `external`; native deterministic sources show the existing degraded dot. History identifies submission origin as external. |
| `generated_at_local` | ISO timestamp with timezone; defaults to submission time. |
| `timezone` | IANA timezone; defaults to the device's configured location timezone. |

Other fields, including `brief.bullets`, `illustration_prompt`, `mood`,
`event_ref`, `rain_level`, `rain_window`, `temp_range`, `layout_emphasis`,
`day_context`, `today.hourly`, `tomorrow`, `location`, and `brief_context`, are
preserved for provenance and future use. They do not trigger generation or add
new layout elements. ASCII rendering rules apply. Headline and subtitle fitting
uses actual font bounds and the available panel height, allowing up to two lines
per field. Headlines stay at least 32px and subtitles at least 26px; short copy
uses larger fonts. Copy that cannot fit raises a render error rather than silently
truncating forecast advice. Rewrite it concisely, preserving useful timing and
clothing/umbrella advice. Generated copy targets 52 headline and 72 subtitle
characters, but measured fit is authoritative. You can also upload a complete
board for full control of typography and layout. Server paths, credentials,
physical dimensions, and hardware settings are not request options.

`GET /create_update` returns the JSON request schema. Uploads are limited to
16 MiB of JSON (including base64); images to 16 million pixels. PNG transparency
is flattened onto white. Both upload modes are converted to the device palette,
and the archived preview is derived from that final output. Browser-origin writes
are rejected. The API has no authentication; retain the default loopback binding
and use SSH for remote access.

Successful requests return HTTP **201** with `run_id`, `run_url`, `status`,
`published`, `display_mode`, and `artifacts.final_display` / `artifacts.preview`
URLs. The call is synchronous and can take the duration of a panel refresh;
the helper defaults to a 180-second timeout. `published: true` means the configured
display delivery and live-file publication completed; `local_preview` mode skips
physical hardware. Scheduled updates continue normally and can replace an external
update at the next scheduled run. External uploads do not replace scheduled weather,
brief, or hero caches.

Errors use JSON: **400** invalid input, **403** browser origin, **408** body-read
timeout, **409** another update holds the shared lock, **411** missing/unsupported
body length, **413** oversized body, **415** wrong content type, **500** render or
publication failure (with a history `run_id` when processing started). Failed
hardware delivery leaves live output files unchanged, although a driver failure
may occur after a physical refresh has begun. Requests are not deduplicated:
after an ambiguous client timeout, check history before resubmitting.

## Local vs Pi Mode

### Local preview

Set in `config/settings.json`:

```json
"display": { "mode": "local_preview" }
```

Then run:

```bash
./scripts/display/update_display.sh
```

### Raspberry Pi display

Set:

```json
"display": { "mode": "pi_display" }
```

The display script probes both module candidates:

- `waveshare_epd.epd10in2g`
- `waveshare_epd.epd10in2_G`

## Raspberry Pi Bring-Up (SSH/tmux)

From the project root on the Pi:

```bash
chmod +x scripts/ops/setup_pi.sh scripts/ops/install_waveshare_driver.sh scripts/ops/preflight.py scripts/ops/install_systemd.sh scripts/display/update_display.sh
./scripts/ops/setup_pi.sh
```

After initial bring-up, use one command for day-to-day updates:

```bash
make update
```

`make update` runs `git pull --ff-only origin main`, installs/validates dependencies,
and executes the display pipeline.

`setup_pi.sh` installs Python GPIO deps (`spidev`, `RPi.GPIO`) and runs
`scripts/ops/install_waveshare_driver.sh`, which will:

- reuse the driver checkout at `~/e-Paper` if it is already installed
- otherwise clone `https://github.com/waveshare/e-Paper.git` into `~/e-Paper`
- install BCM2835 if missing

Update `config/settings.json` for device mode:

```json
"display": { "mode": "pi_display" }
```

Run preflight checks:

```bash
.venv/bin/python3 scripts/ops/preflight.py
```

Preflight reports two classes of check. `required` checks gate `--strict`
success (systemd runs the preflight with `--strict`); `optional` diagnostics are
informational and never fail a strict preflight. The human-readable summary
prints `required=`, `optional=` and `overall=`, where `overall=` mirrors
required readiness and therefore agrees with the process exit status. The JSON
summary exposes `ok` (every check passed), `strict_ok` (required readiness) and
`optional_ok` (optional diagnostics).

`openrouter_https_reachability` is an unauthenticated, non-mutating GET to
`https://openrouter.ai`: any HTTP status (including the root page's 403) proves
DNS, TCP and TLS reachability and is reported as reachable. It does not
authenticate or validate API usability.

Manual full run:

```bash
./scripts/display/update_display.sh
```

Install daily automation (08:00 local time):

```bash
./scripts/ops/install_systemd.sh
systemctl list-timers weather-eink-board.timer
```

See logs:

```bash
journalctl -u weather-eink-board.service -n 100 --no-pager
```

## Design Notes (Current Layout)

- Full-bleed generated image as background.
- Bottom minimal text panel:
  - headline (large)
  - subtitle (smaller)
- Small high-temperature chip in top-right (e.g. `30C`).
- Date in top-left.

## Troubleshooting

### 1) Image generation fallback shows proxy tunnel 403

Your environment proxy is blocking OpenRouter. Check allowlists/policy for `openrouter.ai`.

### 2) TLS cert verification failure

If your environment uses custom root CAs, set:

```json
"openrouter": {
  "ca_bundle_file": "/path/to/your/ca-bundle.pem"
}
```

The image script also attempts to use `certifi` automatically.

### 3) OpenRouter image runs but no hero appears

Run directly and inspect output:

```bash
python3 scripts/openrouter/generate_image.py --force-openrouter
```

If successful, it should write `runtime/hero.png`.

### 4) No live weather response available

When live fetch fails, the pipeline can use `config/sample_openmeteo.json` (`allow_sample_weather_on_failure`).

## Quick Sanity Checks

Deterministic-only:

```bash
python3 scripts/weather/fetch_weather.py
python3 scripts/weather/transform_weather.py
python3 scripts/render/compose_board.py
python3 scripts/render/palette_quantize.py
```

Force OpenRouter calls (for debugging):

```bash
python3 scripts/openrouter/generate_brief.py --force-openrouter
python3 scripts/openrouter/generate_image.py --force-openrouter
```
