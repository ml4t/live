# Public Claim Evidence

This register defines the public beta claims and the checks that support them. A passing source unit
test alone is not enough when the claim concerns an installed artifact, sustained behavior, or an
external paper account.

| Area | Public beta claim | Evidence boundary |
| --- | --- | --- |
| Portability | Lifecycle version 1 callbacks and canonical intents are portable across the qualified backtest and live engines | `tests/contracts/test_causal_strategy_parity.py`; minimum, locked, and maximum dependency profiles |
| Safety | Canonical orders fail atomically; live safety may reject or reduce an intent without redefining it | `tests/unit/test_order_contract.py`, `tests/unit/test_safe_broker.py`, `tests/unit/test_secure_persistence.py` |
| Feeds | OKX and bar aggregation emit validated UTC events and halt on overload or continuity loss | installed-wheel external OKX qualification; feed contract, queue, continuity, recovery, and stress tests |
| Brokers | IB and Alpaca implement the documented snapshots, state transitions, canonical order input, capabilities, and reconciliation contract | broker contract and adapter suites; `scripts/qualification/qualify_paper.py`; fresh installed-wheel paper evidence |
| Performance | The credential-free Linux reference workload sustains 100 events per second for one virtual hour with less than 25 MiB post-warmup RSS growth, no-op dispatch p99 below 10 ms, shutdown below 5 seconds, bounded queues, and exact event and intent checksums | `scripts/qualification/qualify_performance.py`; `tests/stress`; candidate performance report |
| Platform | The wheel and source distribution install on Linux with Python 3.12, 3.13, and 3.14; metadata rejects Python 3.15 | `scripts/qualification/qualify_artifacts.py` installed profiles |
| Maturity | IB and Alpaca brokers plus the OKX feed are beta-supported; Alpaca, IB, DataBento, and generic CCXT feeds require experimental opt-in | public claim scan, experimental-feed tests, retained feed evidence, and retained IB plus Alpaca paper bundles tied to the candidate wheel hash |

The complete credential-free command is documented in [Candidate Qualification](qualification.md).
Paper evidence is separate because untrusted pull requests never receive account credentials.
