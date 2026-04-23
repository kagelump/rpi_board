# Raspberry Pi Bring-Up TODO

This checklist captures what is still needed to run this project reliably on a real Raspberry Pi + Waveshare panel.

## 1) Hardware + OS Baseline

- [ ] Confirm target Pi model and Raspberry Pi OS version.
- [ ] Enable SPI:
  - [ ] `sudo raspi-config` -> Interface Options -> SPI -> Enable
  - [ ] Reboot and verify SPI device exists (`/dev/spidev0.0`).
- [ ] Confirm Waveshare 10.2" e-paper wiring and power stability (especially 5V supply quality).

## 2) System Packages + Python Runtime

- [ ] Install OS prerequisites:
  - [ ] `python3`, `python3-pip`, `python3-venv`
  - [ ] font/image libs (`libjpeg-dev`, `zlib1g-dev`, `libfreetype6-dev`) if needed
- [ ] Install Python packages used by this project:
  - [ ] `pillow`
  - [ ] `certifi`
- [ ] (Recommended) Use a project virtualenv and run scripts from that env.

## 3) Waveshare Driver Integration

- [ ] Install Waveshare e-paper Python driver package on Pi.
- [ ] Verify import path on device:
  - [ ] `waveshare_epd.epd10in2g` or
  - [ ] `waveshare_epd.epd10in2_G`
- [ ] Run `python3 scripts/display/push_to_epd.py --mode pi_display` with an existing `runtime/final_display.png` to confirm panel update works.
- [ ] Validate panel refresh behavior visually (no stuck partial refresh artifacts).

## 4) Configuration for Pi

- [ ] Update `config/settings.json` for Pi usage:
  - [ ] `display.mode = "pi_display"`
  - [ ] confirm `location` fields
  - [ ] confirm model settings (`openrouter.text_model`, `openrouter.image_model`, `openrouter.image_tool_model`)
- [ ] Configure OpenRouter API key:
  - [ ] set `~/.openrouter.key` with `chmod 600`
  - [ ] or use `OPENROUTER_API_KEY`
- [ ] If needed, set `openrouter.ca_bundle_file` to enterprise/custom CA bundle path.

## 5) Networking + OpenRouter Health

- [ ] Confirm Pi can reach `https://openrouter.ai`.
- [ ] Verify no proxy tunnel policy blocks OpenRouter (403 CONNECT issues).
- [ ] Validate TLS trust chain on Pi (no cert verification failures).
- [ ] Smoke test:
  - [ ] `python3 scripts/openrouter/generate_image.py --force-openrouter`
  - [ ] confirm `runtime/hero.png` is created.

## 6) Operationalization (Phase 4)

- [ ] Add `systemd` service unit to run `scripts/display/update_display.sh`.
- [ ] Add `systemd` timer unit for daily morning run (~08:00 local).
- [ ] Add an install script under `scripts/ops/` to copy/enable units.
- [ ] Add log rotation policy for `runtime/logs/`.
- [ ] Add retention cleanup policy for runtime artifacts.
- [ ] Add stale-data marker policy and health checks for unattended runs.

## 7) End-to-End Validation on Device

- [ ] Manual full run on Pi:
  - [ ] `./scripts/display/update_display.sh`
  - [ ] verify panel output is legible and image-first.
- [ ] Verify fallback behavior:
  - [ ] disable network -> ensure deterministic fallback still renders.
  - [ ] image generation failure -> ensure board still updates cleanly.
- [ ] Reboot test:
  - [ ] reboot Pi and verify timer/service behavior.
- [ ] Reliability goal:
  - [ ] run unattended for 7 consecutive days without intervention.

## 8) Nice-to-Have Hardening

- [ ] Pin Python dependencies in a requirements file.
- [ ] Add a preflight check script (`scripts/ops/preflight.py`) for:
  - [ ] SPI availability
  - [ ] Waveshare import detection
  - [ ] OpenRouter connectivity
  - [ ] API key presence
- [ ] Add a `--dry-run` mode to orchestration script for diagnostics.
