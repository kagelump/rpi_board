.PHONY: update setup preflight run dev timer

update:
	git pull --ff-only origin main
	$(MAKE) setup
	$(MAKE) run

setup:
	chmod +x scripts/ops/*.sh scripts/display/update_display.sh
	./scripts/ops/setup_pi.sh

preflight:
	.venv/bin/python3 scripts/ops/preflight.py --json

run:
	./scripts/display/update_display.sh

dev:
	DISPLAY_MODE_OVERRIDE=local_preview ./scripts/display/update_display.sh

timer:
	./scripts/ops/install_systemd.sh
