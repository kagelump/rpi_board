# Weather E-Ink Board

Image-first daily weather briefing for a Waveshare e-paper display (`960x640`), built for Raspberry Pi.

The board is designed as a **morning poster**: a generated weather illustration takes most of the screen, with minimal text overlay and a small high-temperature corner chip.

## Features

- Daily weather pipeline: fetch -> transform -> brief -> image -> compose -> display.
- Deterministic fallback path when APIs fail.
- OpenRouter text brief generation (optional).
- OpenRouter image generation via server tools (optional).
- Full-screen poster layout with minimal text.
- Local preview mode and Raspberry Pi hardware mode.

## Repository Layout

```text
config/
  settings.json
  sample_openmeteo.json
  prompt_templates/
scripts/
  weather/
  openrouter/
  render/
  display/
tokyo_weather.sh
plan.md
```

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

## Runtime Artifacts

Outputs are written under `runtime/`:

- `last_payload.json`
- `last_brief.json`
- `hero.png` (when image generation succeeds)
- `final_display.png`
- `preview.png`
- `last_success.json`

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

`setup_pi.sh` installs Python GPIO deps (`spidev`, `RPi.GPIO`) and runs
`scripts/ops/install_waveshare_driver.sh`, which will:

- reuse `/home/trainboard/e-Paper` if present (copied with `sudo rsync`)
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
