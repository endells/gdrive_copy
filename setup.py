from plugin import create_plugin_instance

from .mod_copy import ModuleCopy

P = create_plugin_instance({
    'filepath': __file__, 'home_module': 'copy', 'use_db': True,
    'use_default_setting': True, 'default_route': 'normal',
    'menu': {'uri': __package__, 'name': '구드공 개별복사', 'list': [
        {'uri': 'copy/request', 'name': '개별 요청'},
        {'uri': 'copy/history', 'name': '다운로드 목록'},
        {'uri': 'copy/settings', 'name': '설정'},
        {'uri': 'copy/logs', 'name': '로그'},
    ]},
})
P.set_module_list([ModuleCopy])
