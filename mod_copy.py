import fcntl
import re
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from flask import abort, jsonify, render_template, session
from framework import F
from plugin import PluginModuleBase

from .drive import Drive, Gds, check_size, inspect_job
from .links import CopyError, REMOTE, base_path, source_link
from .store import Store
from .telegram import check_connection, check_settings, poll


@contextmanager
def file_lock(path):
    with open(path, 'a+b') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
        else:
            yield handle.fileno()


def number(text, low, high, label):
    if not re.fullmatch(r'[0-9]{1,8}', str(text)) or not low <= int(text) <= high:
        raise CopyError('%s 범위는 %d~%d입니다.' % (label, low, high))
    return int(text)


class ModuleCopy(PluginModuleBase):
    def __init__(self, P):
        super().__init__(P, name='copy', first_menu='request')
        self.root = Path(F.config['path_data']) / 'db'
        self.store = Store(self.root / 'gdrive_copy_queue.db')
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.tg_wake = threading.Event()
        self.threads = []

    def plugin_load(self):
        if any(t.is_alive() for t in self.threads):
            return
        self.stop.clear()
        self.threads = [threading.Thread(target=fn, name='gdrive-copy-' + name, daemon=True)
                        for name, fn in [('worker', self.worker), ('telegram', self.receiver)]]
        for thread in self.threads:
            thread.start()

    def plugin_unload(self):
        self.stop.set()
        self.wake.set()
        self.tg_wake.set()

    def drive(self, fd=None):
        rclone = F.PluginManager.get_plugin_instance('rclone')
        if rclone:
            binary = rclone.ModelSetting.get('rclone_path') or 'rclone'
            config = rclone.ModelSetting.get('rclone_config_path')
        else:
            binary, config = 'rclone', None
        config = (config or str(self.root / 'rclone.conf')).replace('{PATH_DATA}', F.config['path_data'])
        return Drive([binary, '--config', config], fd)

    def gds(self, mode):
        if mode == 'gds':
            return Gds(F.PluginManager.get_plugin_instance('gds_tool'))

    def worker(self):
        # 하나의 프로세스만 대기열을 처리한다. rclone에도 잠금을 상속해 FF 재시작 시 중복 복사를 막는다.
        while not self.stop.is_set():
            try:
                with file_lock(self.root / 'gdrive_copy_worker.lock') as fd:
                    if fd is not None:
                        self.store.recover()
                        while not self.stop.is_set():
                            self.wake.clear()
                            job = self.store.claim()
                            if job:
                                self.run_job(job, fd)
                            else:
                                self.wake.wait(5)
            except Exception:
                self.P.logger.exception('복사 대기열 처리 오류')
            self.stop.wait(3)

    def run_job(self, job, fd):
        try:
            with F.app.app_context():
                drive = self.drive(fd)
                gds = self.gds(job['mode'])
                title, dest, count, size = inspect_job(drive, gds, job)
                # 끊긴 복사 재개 시 기존 대상 경로를 유지한다.
                if job['owns_dest'] and job['dest'] != dest:
                    raise CopyError('원본 폴더명이 이전 복사 때와 다릅니다. 대상 경로를 확인한 뒤 새로 요청하세요.')
                self.store.reserve(job['id'], title, dest, count, size)
                check_size(job, count)
                if gds and count > 1000:
                    raise CopyError('구드공 개별 요청의 한도는 파일 1,000개입니다.')
                forbidden = {job['source_id']}
                if not gds:
                    forbidden.update(drive.folder_ids(job))
                drive.check_target(job['base'], dest.rsplit('/', 1)[-1], forbidden, job['owns_dest'])
                self.store.copying(job['id'])
                if gds:
                    drive.run('mkdir', dest)
                    upstream_id, previous = gds.submit(dict(job, count=count, size=size), dest)
                    with self.store.db(True) as db:
                        db.execute("UPDATE jobs SET gds_retry=0,gds_id=?,gds_previous=?,detail=? WHERE id=?",
                                   (upstream_id, previous, '구드공 요청 #%s 처리 중' % upstream_id, job['id']))
                    self.wait_gds(gds, upstream_id, previous)
                    if self.stop.is_set():
                        return
                else:
                    drive.copy(job, dest)
                self.store.finish(job['id'], 'done', '복사 완료')
        except CopyError as exc:
            self.store.finish(job['id'], 'failed', str(exc))
        except Exception:
            self.P.logger.exception('복사 #%s 오류', job['id'])
            self.store.finish(job['id'], 'failed', '예상하지 못한 처리 오류입니다. FF 플러그인 로그를 확인하세요.')

    def wait_gds(self, gds, upstream_id, previous):
        until = time.monotonic() + 24 * 3600
        while not self.stop.is_set():
            # FF의 모델 조회 함수는 새 app_context를 열어 최신 상태를 조회한다.
            row = gds.p.ModelRequestItem.get_by_id(upstream_id)
            if not row:
                raise CopyError('구드공 요청 #%s 이력을 찾지 못했습니다.' % upstream_id)
            status = row.status or ''
            if gds.stamp(row) != previous:
                if status == 'completed':
                    return
                if status.startswith('fail') or status in ('error', 'cancelled'):
                    raise CopyError('구드공 요청 #%s 실패 · %s' % (upstream_id, status))
            if time.monotonic() > until:
                raise CopyError('구드공 요청 #%s 결과 대기 시간이 지났습니다. 구드공 목록을 확인하세요.' % upstream_id)
            self.stop.wait(5)

    def poll_once(self):
        with file_lock(self.root / 'gdrive_copy_telegram.lock') as fd:
            if fd is None:
                return '다른 텔레그램 수신 작업이 진행 중입니다.'
            message = poll(self.store, self.store.settings())
            self.wake.set()
            return message

    def receiver(self):
        while not self.stop.is_set():
            self.tg_wake.clear()
            interval = 1
            try:
                conf = self.store.settings()
                interval = conf['interval']
                if conf['telegram']:
                    self.poll_once()
            except CopyError as exc:
                self.store.log(str(exc), 'ERROR')
            except Exception:
                self.store.log('텔레그램 수신 중 응답 처리에 실패했습니다.', 'ERROR')
            self.tg_wake.wait(interval * 60)

    def process_menu(self, page, req):
        if page not in ('request', 'settings', 'history', 'logs'):
            abort(404)
        conf = self.store.settings()
        conf['has_token'] = bool(conf.pop('token'))
        if 'gdrive_copy_csrf' not in session:
            session['gdrive_copy_csrf'] = secrets.token_urlsafe(32)
        return render_template('gdrive_copy.html', page=page, conf=conf, csrf=session['gdrive_copy_csrf'],
                               version=self.P.plugin_info.get('version', ''), status=self.store.status())

    def save(self, form, section):
        with file_lock(self.root / 'gdrive_copy_settings.lock') as fd:
            if fd is None:
                raise CopyError('설정을 저장 중입니다. 잠시 후 다시 시도하세요.')
            conf = self.store.settings()
            if section in ('settings', 'copy_settings'):
                conf.update(base=base_path(form.get('base')), source=form.get('source', '').strip(),
                            limit=number(form.get('limit'), 1, 100000, '파일 수 한도'), mode=form.get('mode'))
                if conf['source'] and not REMOTE.fullmatch(conf['source']):
                    raise CopyError('소스 리모트에는 콜론 없이 rclone 리모트 이름만 입력하세요.')
                if conf['mode'] not in ('gds', 'rclone'):
                    raise CopyError('복사 방식을 확인하세요.')
            if section in ('settings', 'telegram_settings'):
                conf.update(telegram=form.get('telegram') == '1', chat=form.get('chat', '').strip(),
                            interval=number(form.get('interval'), 1, 1440, '수신 간격(분)'))
                token = form.get('token', '').strip()
                if form.get('clear_token') == '1':
                    if conf['telegram'] or token:
                        raise CopyError('저장된 토큰을 삭제하려면 수신을 끄고 새 토큰 입력란을 비우세요.')
                    conf['token'] = ''
                elif token:
                    conf['token'] = token
            check_settings(conf)
            self.store.save(conf)
        self.tg_wake.set()
        if section == 'telegram_settings':
            return {'has_token': bool(conf['token']), 'message': '텔레그램 설정을 저장했습니다.'}
        return '설정을 저장했습니다.'

    def process_ajax(self, sub, req):
        if req.method != 'POST':
            abort(405)
        expected = session.get('gdrive_copy_csrf', '')
        if not expected or not secrets.compare_digest(expected, req.headers.get('X-GDrive-Copy-CSRF', '')):
            return jsonify(ok=False, error='페이지를 새로고침한 뒤 다시 시도하세요.'), 403
        f = req.form
        try:
            if sub in ('settings', 'copy_settings', 'telegram_settings'):
                result = self.save(f, sub)
            elif sub == 'check_connection':
                result = check_connection(self.store.settings())
            elif sub == 'status':
                result = self.store.status()
            elif sub in ('add', 'preview'):
                conf = self.store.settings()
                fid, key = source_link(f.get('link'))
                base = base_path(f.get('base') or conf['base'])
                if sub == 'add':
                    with self.store.db(True) as db:
                        idx, added = self.store.enqueue(db, fid, key, base, conf, '수동')
                    self.wake.set()
                    result = ('복사 요청 #%s 등록' if added else '같은 요청이 이미 목록에 있습니다 · #%s') % idx
                else:
                    job = {'source_id': fid, 'rkey': key, 'source': conf['source'] or base.split(':')[0], 'base': base}
                    title, dest, count, size = inspect_job(self.drive(), self.gds(conf['mode']), job)
                    result = {'source_id': fid, 'title': title, 'dest': dest, 'count': count, 'size': size}
            elif sub == 'history':
                result = self.store.listing(number(f.get('page', 1), 1, 99999999, '페이지'), f.get('search', ''), f.get('state', ''))
            elif sub == 'logs':
                result = self.store.logs()
            elif sub == 'poll':
                result = self.poll_once()
            elif sub in ('retry', 'delete'):
                idx = number(f.get('id'), 1, 99999999, '요청 번호')
                if sub == 'retry':
                    self.store.retry(idx, self.store.settings())
                    self.wake.set()
                    result = '재요청했습니다.'
                else:
                    self.store.delete(idx)
                    result = '목록에서 요청을 삭제했습니다.'
            else:
                abort(404)
            return jsonify(ok=True, data=result)
        except CopyError as exc:
            return jsonify(ok=False, error=str(exc))
