.PHONY: setup run test test-unit test-int smoke db eval

setup:      ## install everything (creates .venv + uv.lock)
	uv sync

run:        ## start the API on http://127.0.0.1:8000/docs
	uv run uvicorn app.main:create_app --factory --reload --app-dir backend  # reads .env

test:       ## all tests
	uv run pytest

test-unit:  ## unit tests only
	uv run pytest --ignore=backend/tests/integration

test-int:   ## integration tests (need truecost.db)
	uv run pytest backend/tests/integration

smoke:      ## black-box test of a RUNNING API:  make smoke URL=https://xyz.ngrok.app
	uv run python scripts/api_smoke_test.py --base-url $(or $(URL),http://localhost:8000)

db:         ## rebuild truecost.db from data/ (~10 min)
	uv run python scripts/build_db.py

eval:       ## extraction accuracy on 10 synthetic notes (GEMINI=1 and/or CLAUDE=1 to include an LLM)
	uv run python scripts/eval_extractor.py $(if $(GEMINI),--gemini) $(if $(CLAUDE),--claude)
