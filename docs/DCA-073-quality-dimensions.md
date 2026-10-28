# DCA-073 — Expose separate quality dimensions

Stage: Evidence and identity resolution · key `quality-dimensions` · source sections 6, 7.

## Prerequisite note

The story names DCA-072 (and the earlier sequence) as the prerequisite. In this snapshot DCA-070–072
are **deferred** (their story files live under `data/user-stories-python-india/failed/`; DCA-070's
field-correction work is present only as **uncommitted** working-tree changes, and DCA-071/072 were
not implemented). Those three are the identity-**merge-review** stories (protect corrections / approve
an ambiguous merge / reverse a merge) — a different axis from quality. DCA-073 is a pure, read-only
quality summary composed from the **committed** foundation (through DCA-069): the DCA-061 field
observations, the DCA-067/068 matcher conclusions, the DCA-069 canonical selection, and the DCA-055
policy decision. It therefore does **not** depend on the merge-review work and is implemented without
relying on the uncommitted DCA-070 changes. (Decision recorded here rather than silently proceeding.)

## What this adds

A pure, deterministic, network-free **quality-summary** layer (`app/quality/quality_summary.py`, the
conclusion sibling of `app.matching` / `app.canonical` / the policy engine). For ONE prospect subject
it grades **five independent quality dimensions** — identity confidence, freshness, completeness,
deliverability and business fit — each with its own stable rating + reason code, and carries the
DCA-055 permission decision through on a **separate eligibility axis**. It reads only what it is
handed and **writes / mutates / deletes nothing**; acting on a summary stays a disabled future
capability.

Vocabularies (small, stable, safe codes — never free text):

- Identity / freshness / completeness / business fit → `QualityLevel` (`high` / `medium` / `low` /
  `unknown`).
- Deliverability → `Deliverability` (`deliverable` / `undeliverable` / `uncertain` / `unknown`) —
  its own vocabulary because `uncertain` is load-bearing.
- Eligibility (carried apart) → `Eligibility` (`allowed` / `blocked` / `review` / `not_applicable` /
  `unknown`), mapped verbatim from the DCA-055 `PolicyDecision`.

## The load-bearing guarantees (acceptance criteria)

- **A high fit score never changes a blocked permission outcome.** Eligibility is a *different axis*
  from quality. The summarizer never computes or derives eligibility from any quality dimension — it
  copies the caller's pre-decided `PolicyDecision` through unchanged. Raising `business_fit` to the top
  grade leaves a `BLOCKED` eligibility exactly `BLOCKED`. Fit describes desirability; it grants nothing.
- **Catch-all deliverability remains uncertain.** A catch-all / accept-all domain accepts any
  local-part, so acceptance proves nothing about the specific mailbox. A catch-all address is graded
  `uncertain` (never `deliverable`), even when "verified"/accepted by the domain. Only an independently
  verified mailbox on a **non**-catch-all domain earns `deliverable`; a hard failure is `undeliverable`.
- **Reproducible, no state change.** A pure function of its inputs at `now`; the same evidence yields
  an identical summary (frozen dataclasses → value-equality), with nothing to mutate — so both the
  result and the relevant denial reproduce exactly.

Each dimension is graded only from its own evidence; no dimension raises or lowers another, and none
reads eligibility. Unsupplied signals are graded `unknown` (never silently good or bad).

Read-only adapters (`freshness_from_observations`, `completeness_from_observations`) build the
freshness/completeness evidence from stored DCA-061 field observations the caller already read within
its tenant scope — they widen no scope and build value objects only.

## Tests

`tests/test_quality_summary.py`:

- **High fit never unblocks a blocked outcome** — fit goes low→high while eligibility stays `blocked`;
  eligibility passthrough maps every `PolicyDecision`; a string decision is accepted and an
  absent/unknown one is fail-safe `unknown` (never silently `allowed`).
- **Catch-all stays uncertain** — catch-all → `uncertain` (not `deliverable`); verified non-catch-all →
  `deliverable`; `undeliverable` dominates; no contact point → `unknown`.
- **Per-dimension grading** — identity outcome/confidence mapping; freshness grades on the stalest
  field; completeness ratio + missing-field reporting; business fit from score and explicit band.
- **Reproducibility** and the `to_dict()` shape (five dimensions + separate eligibility).
- **Read-only adapters** over stored observations on in-memory SQLite.
- **Durable path on disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`, skipped when unset) — records
  field observations over a real transaction, grades from them, and asserts high-fit + blocked stays
  blocked while both rows remain in history. Never SQLite for the durable path.

## Manual check

```
python scripts/manual_check.py DCA-073
```

Records synthetic field observations (a fresh value + a 400-day-stale value) for one subject in
Business A, builds the freshness/completeness evidence from the real rows, and grades the five
dimensions on a **high-fit + blocked + catch-all** fixture. Prints each dimension's rating/reason and
the separate eligibility, and proves: business fit is `high`, eligibility stays `blocked`,
deliverability stays `uncertain`, the summary is reproducible, and nothing is deleted. Local synthetic
only; no AI/paid/network path; the ephemeral DB is cleaned up.

## Boundary

DCA-only: a pure Python conclusion module + its tests + manual-check scenario + this doc. No AI-service
internals, no model calls, no `.NET`/UI changes, no new HTTP route, no migration (the summarizer is
pure and adds no table). Future capabilities (acting on a summary — ranking, surfacing, acquiring,
sending) stay disabled.
