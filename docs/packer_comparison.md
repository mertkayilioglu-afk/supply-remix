# Greedy vs MILP packer: what the comparison shows

The raw tables are generated into [packer_comparison_data.md](packer_comparison_data.md) by

```bash
python scripts/compare_packers.py --time-limit 10 --retry-timeouts 60
```

These numbers were produced with PuLP 4.2 (CBC) on Python 3.13. `requirements.txt` pins PuLP 3.3.2 so
Python 3.11 is supported; solves that stop at the time limit depend on solver version and machine speed, so a
re-run can differ in the "feasible" and "time-out" rows. Greedy results are deterministic.

The greedy packer stays the default. The MILP packer (`RemixConfig(packer="milp")`, PuLP + CBC) is an
experiment kept behind a flag so the two can be compared on the same pools and the same hard rules.

## Summary on the sample data (61 at-risk orders, 10 s per MILP solve)

| Metric | Greedy (default) | MILP |
|---|---:|---:|
| Buildable orders (>= 1 container) | **23** | 21 |
| Containers built (all orders) | **297** | 225 |
| Mean container date spread, days (19 orders built by both) | 16.8 | **8.6** |
| Max container date spread, days (same 19 orders) | 89 | **61** |
| Mean days a unit waits for its container (same 19 orders) | 9.8 | **5.3** |
| Total runtime | **6 s** | 192 s |
| MILP group solves | - | 10 optimal, 11 feasible (not proven optimal), 7 time-outs |

## How to read it

**Independent cases are few.** Several orders of one customer share the same pool, so they get the same
containers. The 23 orders greedy builds are only **8 distinct container plans**; the averages above
weight a plan by how many orders share it. Read the rows below as 9 cases, not 61.

**Container count cannot differ for a built order.** The rule fixes it at `ceil(volume / 30 CBM)`, and
both packers use that. The 297 vs 225 gap comes only from which orders get built at all.

| Case (customer) | Orders | Greedy | MILP | Who wins |
|---|---|---|---|---|
| CUST-037 | 4 | 12 containers, spread 25.9 / 89 days | 12 containers, 9.9 / 50 (feasible, not proven) | MILP |
| CUST-021 | 2 | 7, spread 28.0 / 61 | 7, 8.7 / 61 | MILP |
| CUST-022 | 4 | 16, spread 14.0 / 45 | 16, 10.9 / 45 | MILP |
| CUST-002 | 3 | 17, spread 13.2 / 55 | 17, 3.7 / 43 | MILP |
| CUST-030 | 2 | 4, spread 11.0 / 40 | 4, 11.0 / 44 | Greedy on max spread (MILP minimises the total, not the worst container) |
| CUST-025, CUST-017 | 2 + 2 | identical plans | identical plans | Tie |
| CUST-008 (Tumble Dryers, 23 containers) | 4 | **23, spread 3.6 / 37, wait 3.0** | no solution in 10 s; at 60 s: 23 containers, 27.3 / 74, wait 13.9 | **Greedy, clearly** |
| CUST-039 | 2 | `mixed`: one group cannot balance | 10 containers, proven optimal in ~3 s | **MILP** (builds a group greedy's repair could not) |
| CUST-027 (Refrigerators) | 3 | `mixed` | no solution even at 60 s | Neither; MILP cannot even prove it impossible |

## Where greedy wins

* **Large pools.** On the 23-container group the MILP finds nothing in 10 s, and with 60 s it finds a
  valid plan that is far worse than greedy's (mean spread 27.3 vs 3.6 days; units wait 13.9 vs 3.0 days).
  The model has one integer and one binary variable per segment per container, and it grows quickly.
* **Speed and predictability.** 6 s for all 61 orders, against 192 s; greedy always returns in
  milliseconds per order, and never hits a time limit.
* **Worst container.** For CUST-030 the MILP's worst container spreads 44 days against greedy's 40.
* **Explainability.** "Earliest container fills first, the last one borrows from above" is something a
  planner can check by hand. A MILP answer is only "the solver found this".

## Where MILP wins

* **Date spread on small and medium pools**, where it usually solves to (or near) optimality: mean spread
  roughly halves and units wait about half as long for their container.
* **One extra buildable case** (CUST-039): greedy's local repair gives up on a group, while the MILP
  proves a plan exists under the same hard rules.

## Caveats (stated, not hidden)

* **"Fully-OK containers never donate" had to be translated.** Greedy enforces it as a step in its repair.
  The MILP enforces it as a constraint: at least as many fully-OK, in-bounds containers as the plain
  earliest-first fill produces (the ones greedy locks). This is equivalent in spirit, not line-for-line, so
  part of the CUST-039 gain may come from the translation rather than from better search.
* **"feasible" is not "optimal".** 11 of the 21 MILP solves that produced a plan were stopped by the time
  limit before optimality was proven; their spreads are upper bounds on what the MILP could reach.
* **Spread is the MILP's own objective.** Greedy is not trying to minimise it, so the comparison favours
  the MILP on that metric by construction. Greedy optimises for "earliest goods leave first".
* **Synthetic data, one seed.** Pool sizes and date patterns are generated; real order books will differ.

## What I would do with this

Keep greedy as the default. A practical hybrid would run the MILP only on small pools (for example up to
around 10 containers) with a short time limit, and keep greedy's plan whenever the MILP times out or does
not beat it. That is not built; it would be a change to how proposals are chosen, which is a decision
for the owner.
