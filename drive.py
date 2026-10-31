import configparser
import json
import re
import subprocess
from html.parser import HTMLParser
from urllib.parse import urlencode

import requests

from .links import CopyError, target_path

FOLDER = 'application/vnd.google-apps.folder'


class Drive:
    def __init__(self, command, lock_fd=None):
        self.command = list(command)
        self.lock_fds = () if lock_fd is None else (lock_fd,)

    def run(self, *args, timeout=180):
        try:
            result = subprocess.run(self.command + ['--ask-password=false', '--log-level', 'ERROR', *args],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=timeout, pass_fds=self.lock_fds)
        except FileNotFoundError:
            raise CopyError('FF rclone 실행 파일을 찾지 못했습니다. rclone 플러그인의 설정을 확인하세요.') from None
        except subprocess.TimeoutExpired:
            raise CopyError('rclone 응답 시간이 초과되었습니다. 리모트 연결을 확인하세요.') from None
        if result.returncode:
            msg = result.stderr.decode('utf-8', 'replace').strip().splitlines()
            last = msg[-1][-600:] if msg else '종료 코드 %d' % result.returncode
            last = re.sub(r'(?i)(resource_key=|resourcekey=)[^,:\s&]+', r'\1***', last)
            if args[0] == 'config':
                last = '리모트 설정을 읽지 못했습니다.'
            raise CopyError('rclone %s 실패 · %s' % (args[0], last))
        return result.stdout.decode('utf-8')

    def data(self, *args):
        try:
            return json.loads(self.run(*args))
        except (ValueError, UnicodeError):
            raise CopyError('rclone 응답을 해석할 수 없습니다. 설치 버전을 확인하세요.') from None

    def config(self, remote):
        parser = configparser.RawConfigParser()
        try:
            parser.read_string(self.run('config', 'show', remote))
            return dict(parser[remote])
        except (KeyError, configparser.Error):
            raise CopyError('rclone 설정에서 지정한 리모트를 찾지 못했습니다.') from None

    def folder_info(self, remote, fid, key=''):
        # 기존 FF 설정 파일은 rclone 자신이 갱신한다. 이 플러그인은 자격 증명을 저장하지 않는다.
        self.run('about', remote + ':', '--json')
        conf = self.config(remote)
        if conf.get('type') != 'drive':
            raise CopyError('설정에서 소스 리모트에 Google Drive 리모트 이름을 지정하세요.')
        try:
            token = json.loads(conf.get('token') or '{}').get('access_token')
        except ValueError:
            token = None
        if not token:
            raise CopyError('소스 리모트에 Google Drive OAuth 로그인이 필요합니다.')
        headers = {'Authorization': 'Bearer ' + token}
        if key:
            headers['X-Goog-Drive-Resource-Keys'] = fid + '/' + key
        try:
            response = requests.get('https://www.googleapis.com/drive/v3/files/' + fid,
                                    params={'supportsAllDrives': 'true', 'fields': 'id,name,mimeType,trashed'},
                                    headers=headers, timeout=(8, 20), allow_redirects=False)
            if response.status_code in (403, 404):
                raise CopyError('원본을 열 수 없습니다. 소스 리모트 계정의 공유 권한과 폴더 링크를 확인하세요.')
            if response.status_code != 200:
                raise CopyError('Drive 폴더 정보 조회 실패 (HTTP %s)' % response.status_code)
            data = response.json()
        except (requests.RequestException, ValueError):
            raise CopyError('Google Drive 폴더 정보 요청에 실패했습니다.') from None
        if (not data.get('id') or (fid != 'root' and data['id'] != fid)
                or data.get('mimeType') != FOLDER or data.get('trashed')):
            raise CopyError('복사할 수 있는 Google Drive 폴더가 아닙니다.')
        return data

    def title(self, remote, fid, key):
        return self.folder_info(remote, fid, key)['name']

    @staticmethod
    def source(remote, fid, key):
        # 두 리모트에 전역 --drive-root-folder-id를 적용하면 대상까지 소스를 가리킨다.
        options = ',root_folder_id=' + fid + ",team_drive='',shared_with_me=false"
        return remote + options + (',resource_key=' + key if key else '') + ':'

    def inspect(self, job):
        title = self.title(job['source'], job['source_id'], job['rkey'])
        src = self.source(job['source'], job['source_id'], job['rkey'])
        size = self.data('size', src, '--json')
        return title, int(size['count']), int(size['bytes'])

    def check_target(self, base, name, forbidden, owned):
        remote, path = base.split(':', 1)
        seen = set()
        conf = self.config(remote)
        while conf.get('type') == 'alias' and ':' in conf.get('remote', ''):
            if remote in seen:
                raise CopyError('리모트 별칭이 서로를 가리킵니다.')
            seen.add(remote)
            remote, parent = conf['remote'].split(':', 1)
            path = parent.rstrip('/') + '/' + path.lstrip('/')
            conf = self.config(remote)
        prefix = remote + ':'
        root_id = conf.get('root_folder_id') or conf.get('team_drive')
        if conf.get('type') == 'drive' and not root_id:
            root_id = self.folder_info(remote, 'root')['id']
        if root_id in forbidden:
            raise CopyError('원본 폴더 안에 대상 폴더를 만들 수 없습니다.')
        parts = [p for p in path.split('/') if p] + [name]
        for idx, part in enumerate(parts):
            entries = self.data('lsjson', prefix, '--no-modtime')
            matches = [e for e in entries if e['Name'] == part]
            if not matches:
                return
            if len(matches) != 1 or not matches[0]['IsDir']:
                raise CopyError('대상 경로에 같은 이름의 파일 또는 중복 폴더가 있습니다.')
            item = matches[0]
            if set((item.get('ID') or '').split('\t')) & forbidden:
                raise CopyError('원본 또는 그 하위 폴더로 복사할 수 없습니다.')
            if idx == len(parts) - 1 and not owned:
                raise CopyError('같은 이름의 대상 폴더가 이미 있습니다. 다른 기본 Remote를 선택하세요.')
            prefix += '/' + part

    def copy(self, job, dest):
        src = self.source(job['source'], job['source_id'], job['rkey'])
        self.run('mkdir', dest)
        options = ['--create-empty-src-dirs', '--stats', '0']
        if job['source'] == dest.split(':', 1)[0]:
            options.append('--drive-server-side-across-configs')
        self.run('copy', src, dest, *options, timeout=None)

    def folder_ids(self, job):
        src = self.source(job['source'], job['source_id'], job['rkey'])
        dirs = self.data('lsjson', src, '--recursive', '--dirs-only', '--no-modtime')
        return {d['ID'].split('\t')[0] for d in dirs if d.get('ID')}


class OGTitle(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ''
        self.in_title = False
        self.tab_title = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'title':
            self.in_title = True
        attr = dict(attrs)
        if tag == 'meta' and attr.get('property') == 'og:title':
            self.title = attr.get('content', '')

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.tab_title += data


def public_title(fid, key):
    url = 'https://drive.google.com/drive/folders/' + fid
    if key:
        url += '?' + urlencode({'resourcekey': key})
    parser = OGTitle()
    try:
        with requests.get(url, timeout=(8, 20), stream=True, allow_redirects=False) as res:
            if res.status_code == 200:
                buf = bytearray()
                for chunk in res.iter_content(8192):
                    buf.extend(chunk)
                    if b'</head>' in chunk or len(buf) >= 512 * 1024:
                        break
                parser.feed(buf.decode('utf-8', 'replace'))
    except requests.RequestException:
        pass
    title = parser.title
    if not title:
        for suffix in (' - Google Drive', ' - Google 드라이브'):
            if parser.tab_title.endswith(suffix):
                title = parser.tab_title[:-len(suffix)]
                break
    if not title or title.strip() in ('Google Drive', 'Google 드라이브'):
        raise CopyError('원본 폴더 이름을 확인할 수 없습니다. 폴더 읽기 권한이 있는 소스 리모트를 지정하세요.')
    return title


class Gds:
    """원본 플러그인의 공개 요청 인터페이스와 이력만 이용한다."""
    def __init__(self, plugin):
        if not plugin or not getattr(plugin, 'SupportRcloneWorker', None):
            raise CopyError('구드공 연동 모드에는 실행 중인 원본 gds_tool 플러그인이 필요합니다.')
        self.p = plugin

    def inspect(self, drive, job):
        try:
            title = drive.title(job['source'], job['source_id'], job['rkey'])
        except CopyError:
            title = public_title(job['source_id'], job['rkey'])
        size = self.p.SupportRcloneWorker.size('worker:{' + job['source_id'] + '}')
        if not size or size.get('count') is None:
            raise CopyError('구드공 워커에서 원본 크기를 조회하지 못했습니다. 공유 권한을 확인하세요.')
        return title, int(size['count']), int(size['bytes'])

    @staticmethod
    def stamp(row):
        return '%s|%s' % (row.status, row.completed_time)

    def submit(self, job, dest):
        if job['gds_id'] and not job['gds_retry']:
            return job['gds_id'], job['gds_previous']
        old = self.p.ModelRequestItem.get_by_source_id(job['source_id'])
        if old:
            if old.remote_path.rstrip('/') != dest.rstrip('/'):
                raise CopyError('구드공에 같은 ID가 다른 경로로 등록되어 있습니다. 원본 구드공 목록을 확인하세요.')
            previous = ''
            if old.status == 'completed' or (old.status or '').startswith('fail'):
                # 원본 워커가 재실행되는 동안 기존 '완료' 상태가 그대로 보일 수 있다.
                previous = self.stamp(old)
                result = self.p.logic.get_module('request').add_copy_force(old.id)
                if result.get('ret') != 'success':
                    raise CopyError('구드공 재요청 실패 · ' + str(result.get('ret')))
            return old.id, previous
        result = self.p.add_copy(job['source_id'], '', 'direct', '', job['size'], job['count'],
                                 copy_type='folder', remote_path=dest)
        if result.get('ret') == 'already':
            old = self.p.ModelRequestItem.get_by_source_id(job['source_id'])
            if old and old.remote_path.rstrip('/') == dest.rstrip('/'):
                return old.id, ''
        if result.get('ret') != 'success' or not result.get('request_db_id'):
            raise CopyError('구드공 요청 실패 · ' + str(result.get('ret')))
        return result['request_db_id'], ''


def check_size(job, count):
    if count > job['max_files']:
        raise CopyError('원본 파일 %s개가 설정 한도 %s개를 초과합니다.' % (count, job['max_files']))


def inspect_job(drive, gds, job):
    title, count, size = gds.inspect(drive, job) if gds else drive.inspect(job)
    return title, target_path(job['base'], title), count, size
