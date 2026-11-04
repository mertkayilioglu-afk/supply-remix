# CLAUDE.md -- project instructions for Claude Code

Read README.md and docs/AI_WORKFLOW.md first, then run `python -m pytest -q` (expect 146 passing) before changing anything.

## What this is
A self-directed personal portfolio project on synthetic data (never claim otherwise; no employer data). It rebuilds, in
Python/FastAPI, a supply-planning tool originally built in Excel/VBA/Power Query. The real goal is to show a hiring team HOW
the tool would be built today with AI in the workflow. The proposal output is not the point; the workflow, guardrails and
engineering quality are.

## Domain rules
1. Split lines by availability date: availability of mix = latest date; None if any chunk is UNPLANNED (never sentinel dates);
   delay < 21 days counts as OK.
2. Score and rank at-risk (NOK) orders with a configurable weighted coefficient. OK orders never compete, on purpose.
3. For ONE chosen order, build containers from all units (OK, late, UNPLANNED) of the same customer's NOK orders in the same or a
   linked category group, ordered by date. Every container is 94.5%-100% full (30 CBM). The earliest container fills first; the
   last one borrows from containers above without dropping them under the floor; a fully-OK container never donates. If volume
   cannot satisfy 94.5% x container count there is no proposal, with reason
   no_candidates | insufficient_volume | cannot_balance | mixed (groups failed for different reasons).
   A fully-OK container may only swap units if it stays fully OK and within 94.5%-100%. A repair move or swap may
   not push a container's ready date more than RemixConfig.max_balancing_delay_days (21) past its post-fill date. Partial proposals are kept, and
   the document/email say which category is not split and why.
4. Presentation: pivot (HTML/Excel), customer Excel document with a reconciliation check, draft .eml (never sent; no SMTP anywhere).

## Non-negotiable design rules
- Staged checkpoints: /analyze -> /remix -> document/email. A human reviews each output. Stages fail visibly with StageError
  (stage / expected / received, HTTP 422). No stage triggers the next.
- The AI layer (app/ai/assistant.py, POST /assistant) only routes to ONE stage and explains the engine's output. It never chains
  stages, never computes numbers, and its text is rejected if it states a number the engine did not produce. It must work without
  an API key.
- Every tunable value lives in app/config/. Type hints everywhere. Tests for every behaviour change.
- Never invent efficiency percentages. The impact figures in docs/AI_WORKFLOW.md section 5 are placeholders for the owner to fill.
- Ask before any change to a business rule above.

## Known state
- On the sample data, 23 of 61 at-risk orders can be containerised; 33 fail on volume, 5 are mixed. This is by design.
- Working-day calendar: built, off by default (app/config/calendar_config.py); shifts availability dates only.
- MILP packer: built behind RemixConfig.packer="milp" (default greedy); comparison in docs/packer_comparison.md.
- Dashboard: GET /dashboard (live) and docs/dashboard/index.html (static, rebuild with
  scripts/build_static_dashboard.py; a test fails if it is stale). Each stage is a deliberate click.
- n8n example: docs/n8n/, UNTESTED design example (structure checked by tests/test_n8n_example.py only).
- The repair step borrows from LATER containers only after borrowing from earlier ones fails. Mention this in the README if touched.

## Tasks (in order; stop after each and show the result; commit after each)
1. Review for bugs and dead code (leftover quantified/unquantifiable/deferred structures, stale imports, unused schemas). Fix, add tests.
2. Holiday/working-day calendar in config: shift availability dates on non-working days to the next working day. Off by default, tested.
3. MILP packer (PuLP or OR-Tools) behind a flag, plus a script comparing it with the greedy packer on the sample data
   (container count, feasibility rate, date spread). Report honestly, including where greedy wins.
4. Optional docs/n8n/ example: schedule -> /analyze -> notify -> manual approval -> /remix -> approval -> create DRAFT only.
   Label it an untested design example unless the owner runs it.
5. GitHub Actions CI (pytest) and a README badge.
