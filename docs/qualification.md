# Qualify A Beta Candidate

This procedure validates one exact candidate commit and its built artifacts. It stops before any
release action.

## Fix The Candidate

Use a clean checkout at the intended commit. Confirm the lock file and direct dependency revisions
are unchanged. Record the full commit identifier before running any check. A dirty worktree or a
supporting-repository change invalidates the result.

## Run The Credential-Free Gate

From the repository root, run:

```bash
uv sync --python 3.12 --all-extras --dev --locked
uv run python scripts/qualification/run_beta_gate.py
```

The gate must leave the worktree unchanged. It checks source quality, Python 3.12-3.14 behavior,
dependency policy, deterministic integration, bounded stress and controlled faults, sustained
performance, public claims, strict documentation, reproducible artifacts, installed wheel and
source-distribution profiles, Python 3.15 rejection, external typing, secret scanning, and metadata.

The performance stage runs three 360,000-event dispatcher repetitions representing one hour at 100
events per second across 32 symbols. Each run must preserve event and canonical-intent checksums,
keep post-warmup RSS growth below 25 MiB, keep no-op dispatch p99 below 10 ms, and shut down within
5 seconds. Separate idle, full-queue slow-strategy, burst-overload, high-order-rate, and reconnect
workloads check their exact counts and observable outcomes. The report records every repetition,
host load, Python and dependency identity, latency distribution, throughput, RSS, queue occupancy,
shutdown, recovery, and checksum evidence. These limits measure credential-free framework behavior
on Linux; they do not state venue latency or capacity.

## Qualify Paper Accounts And External Feeds Separately

Credentialed IB and Alpaca checks run only through the manually dispatched `Paper Qualification`
workflow in the protected `paper` environment. Supply the exact candidate commit and the successful
qualification run that retained `dist-CANDIDATE_SHA`. The workflow downloads that artifact instead
of rebuilding it, installs its wheel outside the checkout, and rejects a run whose commit or status
does not match.

Each provider must verify its official paper endpoint and paper account before submission. The
workflow then checks positions, all pending orders and an asset-filtered view, cash, account value,
unsupported-capability rejection, risk-policy rejection, a tagged one-share limit order, working
acknowledgement, reconnect reconciliation, replacement, cancellation, cleanup, and reconciliation
from a fresh process. IB accepts only the standard paper ports and an account identified by IB as a
paper account. Alpaca accepts only the SDK's official paper endpoint in sandbox mode.

The retained bundle contains counts and pass/fail state, not account or order identifiers. The
release gate downloads and validates the bundle, requires both phases for both brokers, and returns
the qualified wheel's SHA-256 digest. Before publication, the tag workflow compares that digest with
the only wheel in its qualified distribution artifact. A successful workflow conclusion or artifact
name is not sufficient by itself. The evidence must also remain within the release workflow's
freshness interval.

The same workflow qualifies `OKXFundingFeed` against the public OKX service. It compares adapter
events with provider-native observations, observes consecutive complete candles across restart,
rejects stale input, forces fail-closed overload, and requires shutdown within five seconds. The
retained feed bundle also proves that Alpaca, IB, DataBento, and generic CCXT feed construction
requires `experimental=True` and records each adapter's missing guarantees. The release gate
requires the paper and feed bundles to identify the same commit and wheel.

Do not expose broker credentials to pull-request code. Do not substitute a run from another commit,
reuse an artifact after a source or workflow change, or point IB qualification at a live port.

## Retain Candidate Identity

Retain the full source commit, dependency snapshot, wheel and source-distribution names and SHA-256
digests, supported interpreter results, documentation and claim report, paper workflow identity,
and final scorecard. Publication must consume the qualified artifact without rebuilding it.

## Stop Before Release

Candidate qualification does not create a tag. It does not publish to PyPI or create a GitHub
release. It does not place a live-money order. Those actions require a separate release decision
after all evidence is current and the scorecard has no unresolved beta blocker.
