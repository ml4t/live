# Integration Tests

## Running Integration Tests

These tests require IB TWS or IB Gateway running in paper trading mode.

### Setup

1. Start TWS or IB Gateway
2. Use paper trading account (port 7497)
3. Enable API in settings:
   - File → Global Configuration → API → Settings
   - Check "Enable ActiveX and Socket Clients"
   - Socket port: 7497
   - Uncheck "Read-Only API"

### Event Loop Fix Applied

The tests use `ib_async.util.patchAsyncio()` in `conftest.py` to allow nested event loops. This resolves compatibility issues between ib_async and pytest-asyncio.

### Running Tests

**All tests should now run together:**

```bash
# Run single test (RECOMMENDED)
pytest tests/integration/test_shadow_mode.py::test_ib_connection -v -s

# Run all tests individually (ALL PASS)
pytest tests/integration/test_shadow_mode.py::test_ib_connection -v
pytest tests/integration/test_shadow_mode.py::test_position_sync -v
pytest tests/integration/test_shadow_mode.py::test_shadow_mode_basic -v
pytest tests/integration/test_shadow_mode.py::test_shadow_mode_prevents_infinite_buy_loop -v
pytest tests/integration/test_shadow_mode.py::test_shadow_mode_risk_limits -v

# Or run in small groups with delays
pytest tests/integration/test_shadow_mode.py -k "connection or sync" -v
```

### Known Issues

#### 1. TWS Connection Limits

TWS has a limit on concurrent/recent connections. Running many tests quickly can exhaust this limit, causing timeouts.

**Symptoms**:
- First few tests pass, then timeouts occur
- `netstat -tn | grep 7497` shows CLOSE_WAIT connections
- Tests work individually but fail when run together

**Solutions**:
1. **Restart TWS** if connection limit exceeded (clears zombie connections)
2. **Run tests individually** with delays between runs
3. **Use the delay fixture** - `conftest.py` now adds 0.5s delay after each test
4. **Kill zombie connections**: `pkill -f "java.*Jts"` then restart TWS

#### 2. Event Loop Management

ib_async + pytest-asyncio can have event loop conflicts when running tests together.

**Root cause**: ib_async holds event loop references between pytest test functions.

**Solution**: `ib_async.util.patchAsyncio()` is applied in `conftest.py` to mitigate this, plus delays between tests.

### Test Coverage

All tests pass individually:
- ✅ `test_ib_connection` - Connects to TWS, gets account info
- ✅ `test_position_sync` - Syncs positions from IB
- ✅ `test_shadow_mode_basic` - Orders NOT sent to IB, VirtualPortfolio works
- ✅ `test_shadow_mode_prevents_infinite_buy_loop` - Gemini Critical Issue A fixed
- ✅ `test_shadow_mode_risk_limits` - Risk limits enforced
- ✅ `test_shadow_mode_with_aggregator` - Bar aggregation works

## CI/CD Recommendations

For automated testing:
```yaml
# Run each integration test separately
- pytest tests/integration/test_shadow_mode.py::test_ib_connection
- pytest tests/integration/test_shadow_mode.py::test_position_sync
# ... etc
```

Or use pytest-xdist to run in separate processes:
```bash
pytest tests/integration/ -n auto --dist loadfile
```
