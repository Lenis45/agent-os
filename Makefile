PYTHON ?= /opt/anaconda3/bin/python3

.PHONY: doctor security-check test audit backup restore-test release-check

doctor:
	$(PYTHON) scripts/system_doctor.py

security-check:
	$(PYTHON) scripts/security_check.py

test:
	cd agents && PYTHONPATH=. $(PYTHON) -m pytest tests

audit:
	$(PYTHON) agents/audit_agents.py
	$(PYTHON) agents/audit_agent_outputs.py

backup:
	bash backups/backup.sh

restore-test:
	bash backups/restore_test.sh

release-check: doctor security-check test audit
	docker compose config --quiet
	git diff --check
