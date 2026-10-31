import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

ID = re.compile(r'[A-Za-z0-9_-]{10,200}')
REMOTE = re.compile(r'[\w][\w. -]*')


class CopyError(Exception):
    pass


def source_link(value):
    text = (value or '').strip()
    if ID.fullmatch(text):
        return text, ''
    try:
        url = urlsplit(text)
        if (url.scheme not in ('http', 'https') or url.netloc.lower() != 'drive.google.com'
                or len(text) > 4096):
            raise ValueError()
        query = parse_qs(url.query)
        path = url.path.rstrip('/')
        match = re.fullmatch(r'/(?:drive/(?:u/\d+/)?)?folders/([\w-]+)', path)
        folder_id = match.group(1) if match else query.get('id', [''])[0] if path == '/open' else ''
        key = query.get('resourcekey', [''])[0]
        if not ID.fullmatch(folder_id) or (key and not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', key)):
            raise ValueError()
        return folder_id, key
    except ValueError:
        raise CopyError('Google Drive 폴더 링크 또는 소스 ID를 입력하세요.') from None


def message_links(message):
    texts = [message.get('text', ''), message.get('caption', '')]
    for group in ('entities', 'caption_entities'):
        texts.extend(e.get('url', '') for e in message.get(group, []))
    for row in message.get('reply_markup', {}).get('inline_keyboard', []):
        texts.extend(button.get('url', '') for button in row)
    result = {}
    for text in texts:
        for token in re.findall(r'https?://[^\s<>"\u200b]+', unescape(text)):
            try:
                fid, key = source_link(token.rstrip('.,;:!?)]}。、'))
                result[fid] = key or result.get(fid, '')
            except CopyError:
                continue
    return list(result.items())


def base_path(value):
    remote, sep, path = (value or '').strip().partition(':')
    if not sep or not REMOTE.fullmatch(remote) or remote != remote.rstrip():
        raise CopyError('기본 Remote를 리모트명:/폴더/ 형식으로 입력하세요.')
    parts = [p for p in path.split('/') if p]
    if any(p in ('.', '..') or any(ord(c) < 32 for c in p) for p in parts):
        raise CopyError('Remote 경로에 사용할 수 없는 문자가 있습니다.')
    return remote + ':' + ('/' + '/'.join(parts) if parts else '')


def target_path(base, title):
    # 폴더 이름 한 개가 별도 경로로 해석되지 않게 한다.
    if not title or not title.strip() or title in ('.', '..') or any(ord(c) < 32 for c in title):
        raise CopyError('원본 폴더명을 안전하게 경로로 변환할 수 없습니다.')
    name = title.replace('/', '／')
    return base.rstrip('/') + '/' + name
