import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .links import CopyError

DEFAULTS = {'base': '', 'source': '', 'mode': 'rclone', 'limit': 500,
            'telegram': False, 'chat': '', 'token': '', 'interval': 1}
KST = timezone(timedelta(hours=9))


def now():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


class Store:
    def __init__(self, path):
        self.path = str(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, val TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, rkey TEXT NOT NULL,
                    base TEXT NOT NULL, source TEXT NOT NULL, mode TEXT NOT NULL, max_files INTEGER NOT NULL,
                    origin TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                    title TEXT DEFAULT '', dest TEXT DEFAULT '', owns_dest INTEGER DEFAULT 0,
                    gds_id INTEGER, gds_previous TEXT, gds_retry INTEGER DEFAULT 0, count INTEGER, size INTEGER, detail TEXT DEFAULT '', created TEXT, updated TEXT,
                    UNIQUE(source_id, base));
                CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, job INTEGER, level TEXT, message TEXT, time TEXT);
            """)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                if write:
                    db.execute('BEGIN IMMEDIATE')
                yield db
        finally:
            db.close()

    def value(self, db, key, default):
        row = db.execute('SELECT val FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, db, key, value):
        db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value)))

    def settings(self):
        with self.db() as db:
            return dict(DEFAULTS, **self.value(db, 'settings', {}))

    def save(self, conf):
        with self.db(True) as db:
            old = self.value(db, 'settings', DEFAULTS)
            if conf['token'] != old['token']:
                self.put(db, 'offset', 0)
            self.put(db, 'settings', conf)

    @staticmethod
    def event(db, job, message, level='INFO'):
        db.execute('INSERT INTO events(job,level,message,time) VALUES (?,?,?,?)', (job, level, message, now()))
        db.execute('DELETE FROM events WHERE id <= (SELECT MAX(id)-4000 FROM events)')

    def log(self, message, level='INFO'):
        with self.db(True) as db:
            self.event(db, None, message, level)

    def enqueue(self, db, fid, key, base, conf, origin):
        stamp = now()
        cur = db.execute('''INSERT OR IGNORE INTO jobs(source_id,rkey,base,source,mode,max_files,
            origin,created,updated) VALUES(?,?,?,?,?,?,?,?,?)''',
            (fid, key, base, conf['source'] or base.split(':', 1)[0], conf['mode'],
             conf['limit'], origin, stamp, stamp))
        if cur.rowcount:
            self.event(db, cur.lastrowid, origin + ' 요청을 대기열에 등록했습니다.')
            return cur.lastrowid, True
        row = db.execute('SELECT id FROM jobs WHERE source_id=? AND base=?', (fid, base)).fetchone()
        if key:
            db.execute('UPDATE jobs SET rkey=? WHERE id=?', (key, row[0]))
        return row[0], False

    def recover(self):
        with self.db(True) as db:
            for row in db.execute("SELECT id FROM jobs WHERE state='running'").fetchall():
                self.event(db, row[0], 'FF 재시작으로 중단된 작업을 다시 확인합니다.')
            db.execute("UPDATE jobs SET state='queued', detail='', updated=? WHERE state='running'", (now(),))

    def claim(self):
        with self.db(True) as db:
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='running', detail='원본 확인 중', updated=? WHERE id=?", (now(), row['id']))
                self.event(db, row['id'], '원본 폴더 확인 시작')
                return dict(row)

    def reserve(self, job, title, dest, count, size):
        with self.db(True) as db:
            if db.execute('SELECT 1 FROM jobs WHERE dest=? AND id<>? AND owns_dest=1', (dest, job)).fetchone():
                raise CopyError('다른 소스 ID가 같은 이름의 대상 폴더를 사용 중입니다.')
            db.execute('UPDATE jobs SET title=?,dest=?,count=?,size=?,detail=?,updated=? WHERE id=?',
                       (title, dest, count, size, '대상 경로 확인 중', now(), job))

    def copying(self, job):
        with self.db(True) as db:
            db.execute("UPDATE jobs SET owns_dest=1, detail='파일 복사 중', updated=? WHERE id=?", (now(), job))
            self.event(db, job, '대상 폴더에 복사를 시작합니다.')

    def finish(self, job, state, message):
        with self.db(True) as db:
            db.execute('UPDATE jobs SET state=?,detail=?,updated=? WHERE id=?', (state, message, now(), job))
            self.event(db, job, message, 'ERROR' if state == 'failed' else 'INFO')

    def retry(self, job, conf):
        with self.db(True) as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
            if not row or row['state'] not in ('failed', 'done'):
                raise CopyError('완료 또는 실패한 작업만 재요청할 수 있습니다.')
            db.execute("""UPDATE jobs SET state='queued',detail='',gds_retry=1,max_files=?,source=?,mode=?,updated=?
                          WHERE id=?""", (conf['limit'], conf['source'] or row['base'].split(':',1)[0],
                                          conf['mode'], now(), job))
            self.event(db, job, '재요청을 등록했습니다.')

    def delete(self, job):
        with self.db(True) as db:
            cur = db.execute("DELETE FROM jobs WHERE id=? AND state<>'running'", (job,))
            if not cur.rowcount:
                raise CopyError('진행 중이거나 이미 삭제된 요청입니다.')
            self.event(db, job, '목록에서 요청을 삭제했습니다.')

    def listing(self, page, search, state):
        pattern = '%' + search + '%'
        where = " WHERE (?='' OR state=?) AND (title LIKE ? OR source_id LIKE ? OR base LIKE ?)"
        args = (state, state, pattern, pattern, pattern)
        with self.db() as db:
            total = db.execute('SELECT COUNT(*) FROM jobs' + where, args).fetchone()[0]
            page = min(max(1, page), max(1, (total + 19)//20))
            rows = db.execute('SELECT id,source_id,base,origin,state,title,dest,count,size,detail,created,updated FROM jobs'
                              + where + ' ORDER BY id DESC LIMIT 20 OFFSET ?', (*args, (page - 1)*20))
            return {'rows': [dict(row) for row in rows], 'page': page, 'total': total}

    def logs(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 200')]
