import re

import requests

from .links import CopyError, message_links


def check_settings(conf):
    if conf['token'] and not re.fullmatch(r'[0-9]{4,20}:[\w-]{20,120}', conf['token'], re.ASCII):
        raise CopyError('텔레그램 봇 토큰 형식을 확인하세요.')
    if conf['chat'] and not re.fullmatch(r'-?[1-9]\d{0,19}|@[A-Za-z][A-Za-z0-9_]{3,31}', conf['chat']):
        raise CopyError('허용할 대화방의 숫자 ID 또는 공개 채널 @이름을 입력하세요.')
    if conf['telegram'] and not (conf['token'] and conf['chat'] and conf['base']):
        raise CopyError('자동 수신에는 봇 토큰, 허용 대화방, 기본 Remote가 필요합니다.')


def api(token, method, params):
    try:
        res = requests.post('https://api.telegram.org/bot' + token + '/' + method, json=params,
                            timeout=(5, 15), allow_redirects=False)
        if res.status_code == 409:
            raise CopyError('텔레그램 봇 충돌 · 기존 웹훅 또는 다른 수신 프로그램을 확인하세요.')
        if res.status_code != 200:
            raise CopyError('텔레그램 요청 실패 (HTTP %s) · 봇 토큰과 대화방 권한을 확인하세요.' % res.status_code)
        body = res.json()
        if body.get('ok'):
            return body['result']
    except (requests.RequestException, ValueError):
        pass
    # 예외 문자열은 봇 토큰이 포함된 URL을 가질 수 있다.
    raise CopyError('텔레그램 연결 또는 응답 확인에 실패했습니다.')


def poll(store, conf, call=api):
    if not conf['telegram']:
        raise CopyError('텔레그램 수신을 켜고 설정을 저장하세요.')
    if call(conf['token'], 'getWebhookInfo', {}).get('url'):
        raise CopyError('웹훅이 설정된 봇입니다. 수신 전용 봇을 지정하세요.')
    chat = conf['chat']
    if chat.startswith('@'):
        chat = str(call(conf['token'], 'getChat', {'chat_id': chat})['id'])
    with store.db() as db:
        offset = store.value(db, 'offset', 0)
    kinds = ('message', 'channel_post', 'edited_message', 'edited_channel_post')
    updates = call(conf['token'], 'getUpdates', {'offset': offset, 'limit': 100, 'timeout': 0,
                                                 'allowed_updates': list(kinds)})
    added = duplicates = 0
    with store.db(True) as db:
        if store.value(db, 'settings', {}) != conf:
            raise CopyError('수신 중 설정이 변경되어 다음 수신 때 다시 확인합니다.')
        for update in updates:
            if update['update_id'] < offset:
                continue
            msg = next((update[k] for k in kinds if k in update), {})
            if str(msg.get('chat', {}).get('id', '')) == chat:
                for fid, key in message_links(msg):
                    _, new = store.enqueue(db, fid, key, conf['base'], conf, '텔레그램')
                    added += int(new)
                    duplicates += int(not new)
            offset = update['update_id'] + 1
        store.put(db, 'offset', offset)
        if added or duplicates:
            store.event(db, None, '텔레그램 · 신규 %d건 · 중복 %d건' % (added, duplicates))
    return '텔레그램 수신 완료 · 신규 %d건 · 중복 %d건' % (added, duplicates)
