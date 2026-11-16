import os,sqlite3,json,time
from pathlib import Path
from app.errors import RuleError

SCHEMA = (
 'CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, reviewed INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL)',
 'CREATE TABLE IF NOT EXISTS ledgers(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, closing_key TEXT NOT NULL UNIQUE, version INTEGER NOT NULL, run_id TEXT NOT NULL, policy TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0, UNIQUE(day,version))',
 'CREATE UNIQUE INDEX IF NOT EXISTS ledgers_active_day ON ledgers(day) WHERE active=1',
 'CREATE INDEX IF NOT EXISTS ledgers_day_version ON ledgers(day,version DESC)',
 'CREATE TABLE IF NOT EXISTS ledger_confirmations(day TEXT NOT NULL, version INTEGER NOT NULL, confirmed_at TEXT NOT NULL, note TEXT NOT NULL, PRIMARY KEY(day,version))',
 'CREATE TABLE IF NOT EXISTS raw_samples(sha256 TEXT PRIMARY KEY, content BLOB NOT NULL, created TEXT NOT NULL)',
)

def connect():
    path=os.environ.get('DB_PATH','/tmp/coldchain.sqlite3')
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(path,timeout=15);con.row_factory=sqlite3.Row
    for statement in SCHEMA:con.execute(statement)
    return con

def save(bundle,raw=None):
    rid=bundle['manifest']['run_id']
    with connect() as c:
        c.execute('INSERT OR IGNORE INTO runs(id,payload,created) VALUES(?,?,?)',(rid,json.dumps(bundle,ensure_ascii=False),bundle['manifest']['created_at']))
        if raw is not None:
            c.execute('INSERT OR IGNORE INTO raw_samples(sha256,content,created) VALUES(?,?,?)',(bundle['manifest']['sha256'],raw,bundle['manifest']['created_at']))
        row=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
    out=json.loads(row['payload']);out['version']=row['version'];out['reviewed']=bool(row['reviewed'])
    return out

def save_raw(raw,digest,created=None):
    stamp=created or time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    with connect() as c:c.execute('INSERT OR IGNORE INTO raw_samples(sha256,content,created) VALUES(?,?,?)',(digest,raw,stamp))

def load_raw(digest):
    with connect() as c:row=c.execute('SELECT content FROM raw_samples WHERE sha256=?',(digest,)).fetchone()
    return None if row is None else bytes(row['content'])

def raw_digests():
    with connect() as c:rows=c.execute('SELECT sha256,created FROM raw_samples ORDER BY created').fetchall()
    return [dict(r) for r in rows]

def load(rid):
    with connect() as c:row=c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
    if not row:return None
    out=json.loads(row['payload']);out['version']=row['version'];out['reviewed']=bool(row['reviewed']);return out

def latest():
    with connect() as c:row=c.execute('SELECT * FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
    if not row:return None
    out=json.loads(row['payload']);out['version']=row['version'];out['reviewed']=bool(row['reviewed']);return out

def history():
    with connect() as c:rows=c.execute('SELECT id,version,reviewed,created FROM runs ORDER BY rowid DESC').fetchall()
    return [dict(r) for r in rows]

def review(rid,version):
    with connect() as c:
        cur=c.execute('UPDATE runs SET version=version+1,reviewed=1 WHERE id=? AND version=?',(rid,version))
        if cur.rowcount!=1:raise RuleError('STALE','记录已变更，请刷新后复核',409)
    return {'run_id':rid,'version':version+1,'reviewed':True}

# ---- 分日关账账本：每个版本都是完整快照，新版本只切换 active，不累加旧版分钟 ----

def _ledger(row):
    if not row:return None
    out=json.loads(row['payload'])
    out.update(version=row['version'],closing_key=row['closing_key'],created=row['created'],active=bool(row['active']))
    return out

def find_ledger(closing_key):
    with connect() as c:row=c.execute('SELECT * FROM ledgers WHERE closing_key=?',(closing_key,)).fetchone()
    return _ledger(row)

def active_ledger(day):
    with connect() as c:row=c.execute('SELECT * FROM ledgers WHERE day=? AND active=1',(day,)).fetchone()
    return _ledger(row)

def active_ledgers():
    with connect() as c:rows=c.execute('SELECT * FROM ledgers WHERE active=1 ORDER BY day DESC').fetchall()
    return [_ledger(r) for r in rows]

def ledger_days():
    with connect() as c:rows=c.execute('SELECT day, MAX(version) AS version, SUM(active) AS active FROM ledgers GROUP BY day ORDER BY day DESC').fetchall()
    return [dict(r,active=bool(r['active'])) for r in rows]

def ledger_versions(day,with_payload=False):
    with connect() as c:rows=c.execute('SELECT * FROM ledgers WHERE day=? ORDER BY version DESC',(day,)).fetchall()
    if with_payload:return [_ledger(r) for r in rows]
    return [{'day':r['day'],'version':r['version'],'closing_key':r['closing_key'],'run_id':r['run_id'],'policy':r['policy'],'created':r['created'],'active':bool(r['active'])} for r in rows]

def confirmations(day):
    with connect() as c:rows=c.execute('SELECT * FROM ledger_confirmations WHERE day=? ORDER BY version DESC',(day,)).fetchall()
    return [dict(r) for r in rows]

def confirm(day,version,note=''):
    stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    con=connect();con.isolation_level=None
    try:
        con.execute('BEGIN IMMEDIATE')
        active=con.execute('SELECT version FROM ledgers WHERE day=? AND active=1',(day,)).fetchone()
        if not active or active['version']!=version:
            raise RuleError('STALE','不是当前有效版本，请刷新后确认',409)
        existing=con.execute('SELECT * FROM ledger_confirmations WHERE day=? AND version=?',(day,version)).fetchone()
        if existing:
            con.rollback();return dict(existing,idempotent=True)
        con.execute('INSERT INTO ledger_confirmations(day,version,confirmed_at,note) VALUES(?,?,?,?)',(day,version,stamp,note))
        con.commit()
        return {'day':day,'version':version,'confirmed_at':stamp,'note':note,'idempotent':False}
    except Exception:
        con.rollback();raise
    finally:con.close()

def publish_ledger(day,closing_key,run_id,policy,finalize):
    """在一个 IMMEDIATE 事务内切换有效版本；finalize 抛错时旧版保持有效。

    同一关账键重试直接返回已记录版本，不新增版本、不重复累计责任分钟；
    不隐式改回旧版，恢复旧版必须走显式 restore。
    """
    con=connect();con.isolation_level=None
    try:
        con.execute('BEGIN IMMEDIATE')
        existing=con.execute('SELECT * FROM ledgers WHERE closing_key=?',(closing_key,)).fetchone()
        if existing:
            con.rollback();return _ledger(existing),True
        previous=con.execute('SELECT * FROM ledgers WHERE day=? AND active=1',(day,)).fetchone()
        version=con.execute('SELECT COALESCE(MAX(version),0)+1 FROM ledgers WHERE day=?',(day,)).fetchone()[0]
        payload=finalize(_ledger(previous),version)
        if payload is None:
            # 内容与当前有效版本完全一致：视为同一关账重试，不生成新版本。
            con.rollback()
            return _ledger(previous),True
        stamp=payload.get('created') or time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        con.execute('UPDATE ledgers SET active=0 WHERE day=? AND active=1',(day,))
        con.execute('INSERT INTO ledgers(day,closing_key,version,run_id,policy,payload,created,active) VALUES(?,?,?,?,?,?,?,1)',(day,closing_key,version,run_id,policy,json.dumps(payload,ensure_ascii=False),stamp))
        con.commit()
        row=con.execute('SELECT * FROM ledgers WHERE closing_key=?',(closing_key,)).fetchone()
        return _ledger(row),False
    except Exception:
        con.rollback();raise
    finally:con.close()

def restore_ledger(day,target_version,run_id='restore'):
    """恢复指定历史版本：生成新的审计版本，分钟数与目标版本完全相同；重复恢复同一目标不再生成版本。"""
    con=connect();con.isolation_level=None
    try:
        con.execute('BEGIN IMMEDIATE')
        target=con.execute('SELECT * FROM ledgers WHERE day=? AND version=?',(day,target_version)).fetchone()
        if not target:raise RuleError('NOT_FOUND','目标账本版本不存在',404)
        active=con.execute('SELECT * FROM ledgers WHERE day=? AND active=1',(day,)).fetchone()
        if active:
            active_payload=json.loads(active['payload'])
            if active['version']==target_version or active_payload.get('restored_from')==target_version:
                con.rollback();return _ledger(active),True
        key='restore:%s:%s:%s'%(day,target_version,active['version'] if active else 0)
        existing=con.execute('SELECT * FROM ledgers WHERE closing_key=?',(key,)).fetchone()
        if existing:
            con.rollback();return _ledger(existing),True
        version=con.execute('SELECT COALESCE(MAX(version),0)+1 FROM ledgers WHERE day=?',(day,)).fetchone()[0]
        payload=json.loads(target['payload'])
        payload['restored_from']=target_version
        payload['previous_version']=active['version'] if active else None
        payload['created']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        con.execute('UPDATE ledgers SET active=0 WHERE day=? AND active=1',(day,))
        con.execute('INSERT INTO ledgers(day,closing_key,version,run_id,policy,payload,created,active) VALUES(?,?,?,?,?,?,?,1)',(day,key,version,run_id,target['policy'],json.dumps(payload,ensure_ascii=False),payload['created']))
        con.commit()
        row=con.execute('SELECT * FROM ledgers WHERE closing_key=?',(key,)).fetchone()
        return _ledger(row),False
    except Exception:
        con.rollback();raise
    finally:con.close()
