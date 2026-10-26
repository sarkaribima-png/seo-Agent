import os, requests
def _h(extra=None):
    k = os.environ["SUPABASE_SERVICE_KEY"]; h = {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"}
    h.update(extra or {}); return h
def _u(t): return os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + t
def select(t, **params):
    r = requests.get(_u(t), headers=_h(), params=params, timeout=30); r.raise_for_status(); return r.json()
def insert(t, rows):
    r = requests.post(_u(t), headers=_h({"Prefer": "return=representation"}), json=rows, timeout=60); r.raise_for_status(); return r.json()
def upsert(t, rows, on, chunk=1000):
    for i in range(0, len(rows), chunk):
        r = requests.post(_u(t), headers=_h({"Prefer": "resolution=merge-duplicates"}), params={"on_conflict": on},
                          json=rows[i:i + chunk], timeout=120); r.raise_for_status()
def update(t, match, data):
    r = requests.patch(_u(t), headers=_h(), params={k: f"eq.{v}" for k, v in match.items()}, json=data, timeout=30); r.raise_for_status()
