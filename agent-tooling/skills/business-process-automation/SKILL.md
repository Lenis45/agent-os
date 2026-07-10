---
name: business-process-automation
description: Design business and system analysis deliverables for AI-assisted automation: discovery, process map, data boundaries, prototype, dashboard, HITL approval, audit, backup, and rollout. Use for employer-facing or product automation planning.
metadata:
  short-description: Business/system analysis workflow for automation
---

# Business Process Automation

Use this skill when converting a messy business workflow into a working, auditable automation
system. The output should be useful both to a decision maker and to an implementation agent.

## Workflow

1. Define the business outcome.
   - What decision, action, or publication should become faster or safer?
   - Who approves the final action?
   - What should never be automated without a human?
2. Map the current process.
   - Inputs, actors, tools, handoffs, queues, exceptions, and failure points.
   - Identify repeated manual work and hidden quality checks.
3. Define the target operating model.
   - Trigger -> analysis -> draft/action -> review -> publish/execute -> audit.
   - Keep human-in-the-loop for irreversible or brand-sensitive actions.
4. Model data boundaries.
   - Source of truth, private data, public data, generated artifacts, retention, backups.
   - Mark anything that must stay out of git and logs.
5. Choose automation primitives.
   - Skills for repeatable agent behavior.
   - MCP for tools/data/actions.
   - Dashboards for queue visibility.
   - Tests/doctor scripts for operational confidence.
6. Prototype the smallest valuable slice.
   - One real input, one real queue, one review screen, one safe output.
7. Add control surfaces.
   - Status, failures, retry, approval, rollback, support bundle, and runbook.
8. Evaluate.
   - Time saved, error reduction, approval quality, traceability, and recovery.

## Deliverable shape

Produce:

- Problem statement
- Current-state process map
- Target-state workflow
- Data and permission model
- Automation candidates ranked by value/risk
- MVP scope
- Acceptance criteria
- Risks and rollback plan
- Operator runbook

## Quality bar

- Avoid "AI magic" language. Tie every agent step to a business action or control.
- Prefer measurable outcomes: minutes saved, fewer manual handoffs, faster publication,
  safer approvals, better recovery.
- Make assumptions explicit and mark what requires user confirmation.
