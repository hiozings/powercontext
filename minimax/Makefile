PYTHON ?= .venv/bin/python
PROJECT ?= demo_project
POWERCONTEXT_URL ?= http://127.0.0.1:8010

.PHONY: install ingest context spec-update spec-reset test replay promote status db-stats verify run-live clean

install:
	uv venv --python 3.12 .venv
	uv pip install --python $(PYTHON) -r requirements.txt

ingest:
	$(PYTHON) context_module/cli.py ingest

context:
	$(PYTHON) context_module/cli.py context --format json --label latest

spec-update:
	cp $(PROJECT)/specs/books_api.v2.md $(PROJECT)/specs/books_api.md
	$(PYTHON) context_module/cli.py ingest
	$(PYTHON) context_module/cli.py context --label after-spec-update

spec-reset:
	cp $(PROJECT)/specs/books_api.v1.md $(PROJECT)/specs/books_api.md
	$(PYTHON) context_module/cli.py ingest

test:
	cd $(PROJECT) && ../$(PYTHON) -m pytest -q

replay:
	$(PYTHON) context_module/cli.py run --mode replay

promote:
	$(PYTHON) context_module/cli.py promote --run $(RUN)

status:
	$(PYTHON) context_module/cli.py status

db-stats:
	$(PYTHON) context_module/cli.py db-stats

verify:
	$(PYTHON) scripts/verify.py

run-live:
	$(PYTHON) context_module/cli.py run --mode live

clean:
	rm -rf data evidence/*.json evidence/*.jsonl
