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

The unforced VCS wheel must follow the latest published stable GitHub release series. This check
uses the published release independently of the candidate version: an unpublished minor candidate
can be built explicitly while Git still derives a development version from the preceding release
tag. Candidate artifact metadata must separately match the explicit candidate version.

## Python 3.15 diagnostics during the Live 0.2 wait

The [canonical compatibility decision](https://github.com/ml4t/ecosystem/blob/56d42098ff91c80c3318d4677c6dc2f178706cf7/decisions/0018-live-020-python315-calendar-wait.md)
limits this wait to Live `>=0.2.0.dev0,<0.3` through 2026-11-26. Candidate metadata must declare
`>=3.12,<3.15`; the shared validator builds the explicitly requested candidate version and checks
its actual wheel identity. Python 3.12-3.14 operating-system, artifact, provider and warning gates
remain required.

The monthly `Python 3.15 dependency and calendar canary` runs on Linux, macOS and Windows.
It resolves every mandatory Live runtime requirement separately, records the actual dependency and
unsupported source-import environment, and exercises causal lifecycle, aggregation and retained
state workflows. Live itself is not installed into that unsupported environment. An independent
public Backtest 0.2.0 / Specs 0.2.0 Hong Kong schedule probe uses newer NumPy with Pandas 3.0.5 and
keeps warnings as errors; it runs even when the declared-dependency/source probe fails. Logs,
versions, schedule output or native failures remain available in the workflow artifact.

A successful source probe is not normal installed-wheel support. An upstream-provider or NumPy
review trigger requires a fresh compatibility review, followed by complete normal three-OS source
and artifact qualification before removing the bound. Do not suppress warnings or substitute a
historically different calendar provider to end the wait.

## Qualify Paper Accounts And External Feeds Separately

Credentialed IB and Alpaca checks run only through the manually dispatched `Paper Qualification`
workflow in the protected `paper` environment. Run it when a provider contract changes. Supply the
exact candidate commit, the successful qualification run that retained `dist-CANDIDATE_SHA`, and
the changed provider. Use `all` only for initial qualification or when shared changes invalidate all
three providers. The workflow downloads the candidate artifact instead of rebuilding it, installs
its wheel outside the checkout with runtime constraints exported from the candidate lock, and rejects
a run whose commit or status does not match. Installed shared-contract, numerical/calendar and
provider-SDK versions must match the recorded lock before a broker or external feed is created.

Each provider must verify its official paper endpoint and paper account before submission. The
workflow then checks positions, all pending orders and an asset-filtered view, cash, account value,
unsupported-capability rejection, risk-policy rejection, a tagged one-share limit order, working
acknowledgement, reconnect reconciliation, replacement, cancellation, cleanup, and reconciliation
from a fresh process. IB working acknowledgement requires a Gateway-assigned permanent order ID;
the SDK's locally created `PendingSubmit` trade alone does not qualify. IB accepts only the standard
paper ports and an account identified by IB as a paper account. Alpaca accepts only the SDK's
official paper endpoint in sandbox mode.

The selected provider runs its short lifecycle or external-feed check and its six-hour soak. Other
providers do not need to be online. A release with unchanged provider contracts does not dispatch
this workflow; its release gate selects retained matching provider evidence automatically.

Each selected broker remains connected to its paper account for at least six continuous hours.
Five-minute snapshots must preserve exact adapter-to-provider positions and pending orders, valid
account metrics, connection state, and the initial account-state checksum. The broker must complete
one controlled reconnect, retain snapshot continuity, keep process RSS growth below 25 MiB, and
disconnect within five seconds. Any unexpected disconnect, reconciliation change, invalid paper
identity, or retained error fails the run.

Every extended report records a provider-contract identity. The identity hashes the provider
adapter, the qualification logic reached by the check, the resolved provider dependency, the
supported Python runtime, and the shared behavior that the provider reaches. IB and Alpaca include
canonical orders, safety policy, reconciliation, persistence, protocols, and state migration. OKX
includes event, queue, persistence, and protocol behavior. Schema 4 also covers the shared engine,
strategy runtime, calendar adapter, shared-contract/numerical/calendar dependency versions, and
provider installer/verifier inputs. A changed identity invalidates only that provider's result.
Documentation, metadata, unrelated workflows, generic build tools, and source code outside
the provider's reachable contract do not invalidate it. Missing inputs or an unsupported identity
schema fail closed.

The workflow stores redacted evidence as a 90-day Actions artifact. Leave `publish-evidence` at
its default `false` to qualify without creating a GitHub release. Publishing a durable prerelease
asset named `provider-evidence-RUN_ID/provider-evidence.zip` requires separate maintainer
authorization and an explicit `publish-evidence: true` request. Retained, validated evidence can
also be archived after qualification once publication is authorized, without repeating the soak.
The release gate takes artifact hashes from the exact candidate wheel and source distribution.
It then selects complete lifecycle,
restart, and soak evidence independently for each matching provider contract. Provider evidence may
come from different current or earlier runs. Evidence has no time-based expiry. Missing, malformed,
mismatched, or unclassifiable evidence fails closed before publication. The Aug 10 run at
`98c414e9d858427c31e1680faccdc8dca498bf6b` is the only legacy result accepted without an embedded
identity; the verifier checks its original identity and compares the current contract inputs across
both revisions.

When the OKX contract changes, the same workflow compares `OKXFundingFeed` events with
provider-native observations, observes consecutive complete candles across restart, rejects stale
input, forces fail-closed overload, and requires shutdown within five seconds. The selected OKX run
then continues for at least six hours with five-minute event, queue, and RSS snapshots and one
retained-state restart. Every complete minute must remain contiguous, final state must match a
native OKX observation, RSS growth must remain below 25 MiB, and errors, rejections, or overflows
fail the run. A completed session rejected by the final checks retains its metrics with
`passed: false`; it cannot satisfy release qualification. The retained feed bundle also proves that
Alpaca, IB, DataBento, and generic CCXT feed
construction requires `experimental=True` and records each adapter's missing guarantees.

The `Monthly provider health` workflow runs the external IB, Alpaca, and OKX integration suites on
the fifteenth day of each month. It detects provider or SDK drift independently of a release. A
failed monthly run is visible for investigation, but it does not revoke matching retained evidence
or block a candidate whose relevant provider contract is unchanged. The health workflow cannot
publish packages or create GitHub releases.

IB Gateway or TWS authentication is an operator prerequisite. Once the paper session is
authenticated, the self-hosted runner can execute the short check or selected soak without further
operator input. IB does not support fully unattended login. A changed IB contract waits during IB
maintenance or until authentication is available; there is no manual release bypass. An unchanged
IB contract reuses matching retained evidence. Do not expose broker credentials to pull-request
code or point IB qualification at a live port.

## Retain Candidate Identity

Retain the full source commit, dependency snapshot, wheel and source-distribution names and SHA-256
digests, supported interpreter results, documentation and claim report, paper workflow identity,
and final scorecard. Publication must consume the qualified artifact without rebuilding it.

## Stop Before Release

Candidate qualification does not create a tag. It does not publish to PyPI or create a GitHub
release. It does not place a live-money order. Those actions require a separate release decision
after all evidence is current and the scorecard has no unresolved stable-release blocker.
