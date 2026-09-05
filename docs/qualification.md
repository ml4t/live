# Qualify A Stable Candidate

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
uv run python scripts/qualification/run_stable_gate.py
```

The gate must leave the worktree unchanged. It checks source quality, Python 3.12-3.14 behavior,
dependency policy, deterministic integration, bounded stress and controlled faults, sustained
performance, public claims, strict documentation, reproducible artifacts, installed wheel and
source-distribution profiles, external typing, secret scanning, and metadata. The ecosystem
qualification workflow separately requires Python 3.15 prerelease installation and the
non-hardware-dependent suite on Linux, macOS, and Windows.

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

The order-lifecycle checks run for every candidate. Extended Alpaca, IB, and OKX soaks run
independently. Set `extended-provider` to the provider whose contract changed, to `all` for initial
qualification, or to `none` when all three current extended results remain applicable.

Each selected broker remains connected to its paper account for at least six continuous hours.
Five-minute snapshots must preserve exact adapter-to-provider positions and pending orders, valid
account metrics, connection state, and the initial account-state checksum. The broker must complete
one controlled reconnect, retain snapshot continuity, keep process RSS growth below 25 MiB, and
disconnect within five seconds. Any unexpected disconnect, reconciliation change, invalid paper
identity, or retained error fails the run.

Every extended report records a provider-contract identity. The identity hashes the provider
adapter, qualification logic reached by that soak, the resolved provider dependency, and the Linux
CPython 3.12 runtime contract. OKX also hashes the shared event and queue contracts reached by its
feed. A changed identity invalidates only that provider's result. Documentation, CI metadata, and
source code outside the extended provider path do not invalidate it.

The workflow stores redacted evidence as a 90-day Actions artifact and as a durable prerelease
asset named `provider-evidence-RUN_ID/provider-evidence.zip`. The release gate validates the exact
candidate's short exercises, then selects a matching extended result for each provider from current
or earlier runs. Evidence has no time-based expiry. Missing, malformed, or mismatched evidence fails
closed. The Aug 10 run at `98c414e9d858427c31e1680faccdc8dca498bf6b` is the only legacy result
accepted without an embedded identity; the verifier recomputes its narrower reviewed identity.

The same workflow qualifies `OKXFundingFeed` against the public OKX service for every candidate. It compares adapter
events with provider-native observations, observes consecutive complete candles across restart,
rejects stale input, forces fail-closed overload, and requires shutdown within five seconds. The
selected OKX extended run then continues for at least six hours with five-minute event, queue, and
RSS snapshots and one retained-state restart. Every complete minute must remain
contiguous, final state must match a native OKX observation, RSS growth must remain below 25 MiB,
and errors, rejections, or overflows fail the run. The retained feed bundle also proves that Alpaca,
IB, DataBento, and generic CCXT feed construction requires `experimental=True` and records each
adapter's missing guarantees. The release gate requires the paper and feed bundles to identify the
same commit and wheel.

IB Gateway or TWS authentication is an operator prerequisite. Once the paper session is
authenticated, the self-hosted runner can execute the short check or selected soak without further
operator input. IB does not support fully unattended login, so a changed IB contract may still wait
for authentication. Do not expose broker credentials to pull-request code or point IB qualification
at a live port.

## Retain Candidate Identity

Retain the full source commit, dependency snapshot, wheel and source-distribution names and SHA-256
digests, supported interpreter results, documentation and claim report, paper workflow identity,
and final scorecard. Publication must consume the qualified artifact without rebuilding it.

## Stop Before Release

Candidate qualification does not create a tag. It does not publish to PyPI or create a GitHub
release. It does not place a live-money order. Those actions require a separate release decision
after all evidence is current and the scorecard has no unresolved stable-release blocker.
