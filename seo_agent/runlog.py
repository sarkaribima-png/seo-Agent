"""Tiny run log so the Performance Evaluator can measure workflow success. Any agent can call it; it NEVER raises (logging must not break the work).
    from .runlog import log_run, logged
    log_run("technical_auditor", "kalosa", True, 41.2, "88 urls")
    with logged("keyword_researcher", site_id): ...        # times the block, logs ok/failed, re-raises the error
Needs table agent_runs (schema_v26.sql). If the table is missing the call just prints a note."""
import time
from contextlib import contextmanager
from . import sb

def log_run(agent, site_id, ok, seconds, detail=""):
    """Store one run. Returns True if saved, False if not (never raises)."""
    try:
        sb.insert("agent_runs", [{"agent": str(agent), "site_id": site_id, "ok": bool(ok), "seconds": None if seconds is None else round(float(seconds), 2), "detail": (str(detail or ""))[:300]}])
        return True
    except Exception as e:
        print("run not logged:", str(e)[:100]); return False

@contextmanager
def logged(agent, site_id=None, detail=""):
    t0 = time.time()
    try: yield
    except BaseException as e:
        log_run(agent, site_id, False, time.time() - t0, f"{type(e).__name__}: {e}"); raise
    else: log_run(agent, site_id, True, time.time() - t0, detail)
