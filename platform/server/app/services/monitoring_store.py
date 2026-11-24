"""Durable history, central request logs, and alert delivery state."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


SCHEMA = '''
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY, sampled_at REAL NOT NULL, node TEXT NOT NULL, cluster TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_node ON samples(node, id);
CREATE TABLE IF NOT EXISTS node_logs (
    node TEXT NOT NULL, boot TEXT NOT NULL, seq INTEGER NOT NULL, seen REAL NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(node, boot, seq)
);
CREATE INDEX IF NOT EXISTS node_logs_seen ON node_logs(seen);
CREATE TABLE IF NOT EXISTS cursors (
    node TEXT PRIMARY KEY, boot TEXT NOT NULL, seq INTEGER NOT NULL, dropped INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS active_alerts (
    node TEXT NOT NULL, code TEXT NOT NULL, level TEXT NOT NULL, PRIMARY KEY(node, code)
);
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL, due REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, delivered REAL
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(delivered, due);
'''


def connect(path, *, readonly=False):
    target = Path(path).resolve()
    if readonly:
        db = sqlite3.connect(target.as_uri() + '?mode=ro', uri=True, timeout=3)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(target, timeout=3)
    db.row_factory = sqlite3.Row
    return db


def initialize(path):
    with closing(connect(path)) as db:
        db.execute('PRAGMA journal_mode=DELETE')
        db.executescript(SCHEMA)


def cursors(path):
    with closing(connect(path, readonly=True)) as db:
        return {row['node']: (row['boot'], row['seq']) for row in db.execute('SELECT node, boot, seq FROM cursors')}


def _delivery(db, node, cluster, code, level, phase, now):
    identifier = uuid4().hex
    payload = {'schema': 'vibeflow.monitor-alert.v1', 'event_id': identifier, 'node': node, 'cluster': cluster,
               'code': code, 'level': level, 'phase': phase, 'time': now}
    db.execute('INSERT INTO deliveries (id, created, payload, due) VALUES (?, ?, ?, ?)',
               (identifier, now, json.dumps(payload, separators=(',', ':')), now))


def ingest(path, node, cluster, sample, log, *, now=None):
    now = time.time() if now is None else now
    with closing(connect(path)) as db, db:
        db.execute('INSERT INTO samples (sampled_at, node, cluster, payload) VALUES (?, ?, ?, ?)',
                   (now, node, cluster, json.dumps(sample, separators=(',', ':'))))
        if log is not None:
            db.executemany('INSERT OR IGNORE INTO node_logs (node, boot, seq, seen, payload) VALUES (?, ?, ?, ?, ?)',
                           [(node, log['boot'], row['seq'], now, json.dumps(row, separators=(',', ':'))) for row in log['records']])
            db.execute('''INSERT INTO cursors (node, boot, seq, dropped) VALUES (?, ?, ?, ?)
                ON CONFLICT(node) DO UPDATE SET boot=excluded.boot, seq=excluded.seq, dropped=cursors.dropped+excluded.dropped''',
                (node, log['boot'], log['after'], log['lost']))
        current = {alert['code']:alert['level'] for alert in sample['alerts']}
        previous = dict(db.execute('SELECT code, level FROM active_alerts WHERE node=?', (node,)))
        # An unreachable peer gives no evidence that its last known fault recovered.
        if not sample['reachable']:
            current.update({code: level for code, level in previous.items() if code not in current})
        for code, level in current.items():
            if previous.get(code) != level:
                _delivery(db, node, cluster, code, level, 'firing', now)
                db.execute('''INSERT INTO active_alerts (node, code, level) VALUES (?, ?, ?)
                    ON CONFLICT(node, code) DO UPDATE SET level=excluded.level''', (node, code, level))
        for code in previous.keys() - current.keys():
            _delivery(db, node, cluster, code, previous[code], 'recovered', now)
            db.execute('DELETE FROM active_alerts WHERE node=? AND code=?', (node, code))


def next_deliveries(path, now):
    with closing(connect(path, readonly=True)) as db:
        return [(row['id'],json.loads(row['payload']),row['attempts']) for row in db.execute(
            'SELECT id, payload, attempts FROM deliveries WHERE delivered IS NULL AND due <= ? ORDER BY created, rowid LIMIT 32', (now,))]


def record_delivery(path, identifier, now, attempts, success):
    with closing(connect(path)) as db, db:
        delay = min(300, 2 ** min(attempts, 9))
        db.execute('UPDATE deliveries SET delivered=?, attempts=?, due=? WHERE id=? AND delivered IS NULL',
                   (now if success else None, attempts+1, now+delay, identifier))


def prune(path, before):
    with closing(connect(path)) as db, db:
        db.execute('DELETE FROM samples WHERE sampled_at<? AND id NOT IN (SELECT max(id) FROM samples GROUP BY node)', (before,))
        db.execute('DELETE FROM node_logs WHERE seen<?', (before,))
        db.execute('DELETE FROM deliveries WHERE delivered<?', (before,))


def _sample(row):
    return {'node':row['node'], 'cluster':row['cluster'], 'sampled_at':row['sampled_at'], **json.loads(row['payload'])}


def _aggregate(latest):
    cluster_samples = {}
    for row in latest:
        if row['reachable'] and row['database']['ok']:
            previous = cluster_samples.get(row['cluster'])
            if previous is None or row['sampled_at'] > previous['sampled_at']:
                cluster_samples[row['cluster']] = row
    totals = {}
    for sample in cluster_samples.values():
        for state,count in sample['jobs'].items():
            totals[state] = totals.get(state,0)+count
    return {'nodes_reachable':sum(row['reachable'] for row in latest), 'nodes_configured':len(latest),
            'reporting_database_groups':len(cluster_samples), 'jobs_observed':totals}


def history(path, limit):
    with closing(connect(path, readonly=True)) as db:
        samples = [_sample(row) for row in db.execute('SELECT * FROM samples ORDER BY id DESC LIMIT ?', (limit,))]
        latest = [_sample(row) for row in db.execute('SELECT * FROM samples WHERE id IN (SELECT max(id) FROM samples GROUP BY node) ORDER BY node')]
        alerts = [dict(row) for row in db.execute('SELECT * FROM active_alerts ORDER BY node, code')]
        pending = db.execute('SELECT count(*) FROM deliveries WHERE delivered IS NULL').fetchone()[0]
    return {'schema':'vibeflow.monitoring-history.v1', 'samples':samples[::-1], 'latest':latest,
            'aggregate':_aggregate(latest), 'active_alerts':alerts, 'pending_deliveries':pending}


def logs(path, limit):
    with closing(connect(path, readonly=True)) as db:
        rows = [{'node':row['node'], **json.loads(row['payload'])} for row in db.execute(
            'SELECT * FROM node_logs ORDER BY seen DESC, seq DESC LIMIT ?', (limit,))]
        lost = {row['node']:row['dropped'] for row in db.execute('SELECT node,dropped FROM cursors')}
    return {'schema':'vibeflow.monitoring-logs.v1', 'records':rows, 'lost_by_node':lost}
