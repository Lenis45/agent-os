PYTHON ?= $(if $(wildcard .venv/bin/python),$(CURDIR)/.venv/bin/python,/opt/anaconda3/bin/python3)

.PHONY: bootstrap-runtime doctor security-check dependency-audit test audit llm-report model-eval backup restore-test storage-maintenance dev-stacks-start dev-stacks-stop dev-stacks-status release-check

bootstrap-runtime:
	/opt/anaconda3/bin/python3 -m venv --clear .venv
	.venv/bin/python -m pip install --upgrade "pip>=26.1.2"
	.venv/bin/python -m pip install -r requirements.lock.txt
	.venv/bin/python -m pip check

doctor:
	$(PYTHON) scripts/system_doctor.py

security-check:
	$(PYTHON) scripts/security_check.py

dependency-audit:
	@test -x .venv/bin/python || (echo "Missing .venv; run make bootstrap-runtime"; exit 1)
	.venv/bin/python scripts/verify_praison_approval.py
	.venv/bin/python -m pip_audit --timeout 60 --path .venv/lib/python3.12/site-packages --progress-spinner off --ignore-vuln PYSEC-2026-2946
	.venv/bin/python -m bandit -r agents dashboard scripts mcp/server.py -x agents/tests,mcp/.venv -q -lll -ii

test:
	cd agents && PYTHONPATH=. $(PYTHON) -m pytest tests

audit:
	$(PYTHON) agents/audit_agents.py
	$(PYTHON) agents/audit_agent_outputs.py

llm-report:
	PYTHONPATH=agents $(PYTHON) scripts/llm_usage_report.py --days 30

model-eval:
	PYTHONPATH=agents $(PYTHON) scripts/evaluate_groq_routing.py

backup:
	bash backups/backup.sh

restore-test:
	bash backups/restore_test.sh

storage-maintenance:
	bash scripts/storage_maintenance.sh

dev-stacks-start:
	bash scripts/dev_stacks.sh start

dev-stacks-stop:
	bash scripts/dev_stacks.sh stop

dev-stacks-status:
	bash scripts/dev_stacks.sh status

release-check: doctor security-check dependency-audit test audit
	docker compose config --quiet
	git diff --check
