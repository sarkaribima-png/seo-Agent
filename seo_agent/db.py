import sqlite3, json, time, os
P = os.environ.get("SEO_DB", "data/state.db")
def conn():
    c = sqlite3.connect(P); c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS gsc(site TEXT,date TEXT,page TEXT,device TEXT,clicks REAL,impr REAL,pos REAL,
        PRIMARY KEY(site,date,page,device));
    CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY,site TEXT,page TEXT,ts REAL,kind TEXT,
        old TEXT,new TEXT,status TEXT,reason TEXT);
    """); return c
def log_change(c, site, page, kind, old, new, status, reason=""):
    c.execute("INSERT INTO changes(site,page,ts,kind,old,new,status,reason) VALUES(?,?,?,?,?,?,?,?)",
              (site, page, time.time(), kind, json.dumps(old), json.dumps(new), status, reason)); c.commit()
def changed_today(c, site):
    t = time.time() - 86400
    return c.execute("SELECT COUNT(DISTINCT page) n FROM changes WHERE site=? AND ts>? AND status IN('applied','dry-run')",(site,t)).fetchone()["n"]
def recently_changed(c, site, page, days=21):
    t = time.time() - days*86400
    return c.execute("SELECT 1 FROM changes WHERE site=? AND page=? AND ts>? AND status IN('applied','dry-run')",(site,page,t)).fetchone() is not None
