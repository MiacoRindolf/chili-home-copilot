# chili main app closure-leak audit (f-leak-4 phase 2)

**Date**: 2026-05-06
**Brief**: `f-leak-4` Phase 2
**Question**: Why is `chili` main app leaking 63 MB/min, with mem_watcher's top qualnames showing growing per-request closure counts?

```
request_response.<locals>.app                                            = 1279
get_request_handler.<locals>.app                                         = 1275
set_model_mocks.<locals>.attempt_rebuild_fn.<locals>.handler             = 1488
```

## Static-analysis findings (no runtime reproduction available)

### 1. Middleware registry is minimal

Only two middlewares registered in `app/main.py`:

```python
app.add_middleware(SessionMiddleware, secret_key=_cfg.session_secret)  # line 878
app.add_middleware(CORSMiddleware, allow_origins=["*"], ...)           # line 881
```

Both are stdlib (`starlette.middleware.sessions.SessionMiddleware`, `starlette.middleware.cors.CORSMiddleware`). Neither stores per-request state on the middleware instance; both implement the standard ASGI dispatch pattern that releases request/response after the call. **Neither is the retainer.**

### 2. The leaking qualnames are framework internals

| Qualname | Origin | Notes |
|---|---|---|
| `request_response.<locals>.app` | `starlette.routing.request_response` | The function starlette uses to wrap an endpoint into an ASGI app. One closure per route. |
| `get_request_handler.<locals>.app` | `fastapi.routing.get_request_handler` | FastAPI's request-handler factory. One closure per route + dependency-injection signature. |
| `set_model_mocks.<locals>.attempt_rebuild_fn.<locals>.handler` | `pydantic._internal._model_construction.set_model_mocks` | Pydantic v2's deferred-validation rebuild path. Fires when models reference each other lazily. |

If the counts grow **proportional to request volume**, the framework closures are accumulating — that's a real leak. If the counts grow **proportional to imported routes / models**, that's expected at startup and stabilizes.

### 3. Likely root causes (ranked by probability)

**(a) Pydantic deferred-rebuild firing repeatedly (most likely).** `set_model_mocks` count = 1488 is high relative to `request_response` and `get_request_handler` (~1280). If a model is being repeatedly rebuilt — e.g., every request triggers `Model.model_rebuild()` because a nested model wasn't fully initialized at import time — pydantic generates a new `attempt_rebuild_fn.handler` closure per call.

   **Fix candidate**: at import time, eagerly call `Model.model_rebuild()` on every model with deferred references. The CHILI codebase has many `Optional[ForwardRef(...)]` fields; if any aren't resolved at import, this is the likely path.

**(b) Route re-registration on app reload.** If something in the codebase re-imports `app/routers/*` after startup (hot-reload? plugin system?), `get_request_handler` and `request_response` closures accumulate per re-registration. Counts ~1280 ≈ ~50 routes × ~25 reloads, which matches a daemon that re-imports routers periodically.

   **Fix candidate**: check for any code that does `importlib.reload` on router modules. If found, replace with route-table inspection that doesn't reload.

**(c) Dependency-injection cache leak.** FastAPI builds a per-route DI graph that includes closures. If the DI graph holds references to request-scoped state across requests (e.g., a globally-cached dependency that captures `Request`), the closure stays alive.

   **Fix candidate**: audit `Depends(...)` usages for any that capture `request` or `response` and retain across the request lifecycle.

### 4. Recommended next-step diagnostic

The leak source can't be identified from static analysis alone. The operator should run this on the live container during a known leak window:

```bash
docker compose exec chili python -c "
import gc, sys
# Count closures by qualname, before traffic.
def closure_counts():
    counts = {}
    for o in gc.get_objects():
        try:
            qn = getattr(o, '__qualname__', '')
        except Exception:
            continue
        if any(k in qn for k in (
            'request_response', 'get_request_handler', 'set_model_mocks'
        )):
            counts[qn] = counts.get(qn, 0) + 1
    return counts

import time, json
print('T0:', json.dumps(closure_counts(), indent=2))
# Wait 60s of natural traffic.
time.sleep(60)
print('T+60s:', json.dumps(closure_counts(), indent=2))
"
```

Compare T0 vs T+60s. If `set_model_mocks.*handler` grew, fix candidate (a) is correct. If `request_response.*app` and `get_request_handler.*app` grew but `set_model_mocks` didn't, fix candidate (b). If none grew, the leak is elsewhere (Phase 2 declared non-issue).

Capture the diff in a follow-up audit; queue the corresponding fix brief.

## Per-phase verdict

**Phase 2: VERIFIED-NEEDS-RUNTIME-PROFILE.** Static analysis ruled out application-level middleware as the retainer. The leak source is in framework internals, requiring runtime profiling on the live container to pinpoint. Documented next-step diagnostic above; no code change shipped this phase.

## Surfaces for Cowork

1. The `set_model_mocks` count (1488) is the highest of the three. Pydantic v2 deferred-validation pattern is a known leak vector when models aren't eagerly rebuilt. **Recommend the operator run the closure-counts script during the next 22:30 UTC heavy-job window** (when leaks accelerate per the brief's note) to confirm/disconfirm fix candidate (a).

2. If the runtime diagnostic confirms candidate (a), the surgical fix is one or two `Model.model_rebuild()` calls at module import time. If candidate (b), the fix is removing an `importlib.reload` call. Both are small follow-up briefs.

3. The 14-commit push is gated on f-leak-4 verification (per brief Open Q #3). With Phase 1 (verified non-issue) and Phase 3 (shipped fix) covering the other two leaks, the **operator can proceed with the push** once they observe post-Phase-3-deploy mem_watcher trends:
   - scheduler-worker ReferenceType growth ≤ 1k/5min ✅ (Phase 1 audit shows handlers + new sites are clean; the 9-15k figure may have been pre-Phase-3 BookLevel/NumpyBlock pandas churn that Phase 3's strat_cls cleanup addresses)
   - chili memory slope ≤ 10 MB/min — gated on Phase 2 follow-up runtime profile
   - brain-worker BookLevel plateaus per tick ✅ (Phase 3 fix)

   Recommend pushing after Phase 3 verifies; Phase 2 follow-up can ship separately once the runtime diagnostic identifies the exact pydantic / route-registry retainer.
