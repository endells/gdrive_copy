(() => {
  'use strict';
  const root = document.getElementById('gc');
  const find = id => document.getElementById(id);
  const page = root.dataset.page;
  const states = {queued: '대기', running: '진행 중', done: '완료', failed: '실패'};
  let current = 1;
  let sequence = 0;
  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== null && text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }
  function note(text, error = false, id = 'gc-note') {
    const output = find(id);
    output.textContent = text;
    output.classList.toggle('gc-error', error);
  }
  async function api(cmd, params = {}) {
    const response = await fetch(root.dataset.endpoint.replace(/COMMAND$/, cmd), {
      method: 'POST', credentials: 'same-origin', headers: {'X-GDrive-Copy-CSRF': root.dataset.csrf},
      body: params instanceof FormData ? params : new URLSearchParams(params)
    });
    if (!response.ok) throw Error('FF 요청 실패 (' + response.status + ')');
    const body = await response.json();
    if (!body.ok) throw Error(body.error || '요청 실패');
    return body.data;
  }
  function action(button, fn, noteId = 'gc-note') {
    return async event => {
      event.preventDefault();
      if (button.disabled) return;
      button.disabled = true;
      try { await fn(); } catch (e) { note(e.message, true, noteId); } finally { button.disabled = false; }
    };
  }
  function bytes(n) {
    if (n == null) return '—';
    const units = ['B','KiB','MiB','GiB','TiB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + ' ' + units[i];
  }
  function card(job, compact) {
    const box = el('article', null, 'gc-row');
    const line = el('div', null, 'gc-line');
    line.append(el('span', states[job.state], 'gc-state gc-' + job.state),
                el('strong', job.title || job.source_id), el('small', '#' + job.id + ' · ' + job.origin));
    box.append(line, el('div', job.dest || job.base, 'gc-path'));
    if (!compact) {
      box.append(el('div', '소스 ID ' + job.source_id + ' · 파일 ' + (job.count ?? '—') + '개 · ' + bytes(job.size), 'gc-meta'),
                 el('div', '등록 ' + job.created + ' · 변경 ' + job.updated + ' (한국시간)', 'gc-meta'));
    }
    if (job.detail) box.append(el('div', job.detail, job.state === 'failed' ? 'gc-error' : 'gc-meta'));
    if (!compact && job.state !== 'running') {
      const buttons = el('div', null, 'gc-actions');
      const command = (label, cmd) => {
        const btn = el('button', label);
        btn.onclick = action(btn, async () => {
          if (cmd === 'delete' && !window.confirm('다운로드 목록에서 이 요청을 삭제할까요? 복사된 파일은 보존됩니다.')) return;
          note(await api(cmd, {id: job.id})); await refresh();
        });
        buttons.append(btn);
      };
      if (['failed', 'done'].includes(job.state)) command('재요청', 'retry');
      command('목록 삭제', 'delete'); box.append(buttons);
    }
    return box;
  }
  async function refresh() {
    const ticket = ++sequence;
    if (page === 'logs') {
      const rows = await api('logs');
      if (ticket !== sequence) return;
      const nodes = rows.map(r => {
        const div = el('div', null, 'gc-log' + (r.level === 'ERROR' ? ' gc-error' : ''));
        div.append(el('time', r.time + ' · ' + r.level + (r.job ? ' · #' + r.job : '') + '　'), el('span', r.message));
        return div;
      });
      find('gc-logs').replaceChildren(...(nodes.length ? nodes : [el('p','기록된 로그가 없습니다.')]));
    } else if (page === 'request' || page === 'history') {
      const compact = page === 'request';
      const query = compact ? {} : Object.fromEntries(new FormData(find('gc-filter')));
      const result = await api('history', {...query, page: current});
      if (ticket !== sequence) return;
      const nodes = (compact ? result.rows.slice(0,5) : result.rows).map(j => card(j, compact));
      find(compact ? 'gc-recent' : 'gc-rows').replaceChildren(...(nodes.length ? nodes : [el('p','등록된 요청이 없습니다.')]));
      if (!compact) {
        current = result.page;
        const last = Math.max(1, Math.ceil(result.total / 20));
        find('gc-pages').textContent = current + ' / ' + last + ' · 총 ' + result.total + '건';
        find('gc-prev').disabled = current <= 1; find('gc-next').disabled = current >= last;
      }
    }
  }
  const load = () => refresh().catch(e => note(e.message,true));
  if (page === 'request') {
    const form = find('gc-request');
    const preview = find('gc-preview');
    form.oninput = () => { preview.hidden = true; };
    find('preview').onclick = action(find('preview'), async () => {
      if (!form.reportValidity()) return;
      const values = new FormData(form); note('원본 폴더를 확인하고 있습니다…');
      const r = await api('preview', values);
      if (values.get('link') !== form.link.value || values.get('base') !== form.base.value) return;
      note('원본 폴더 확인 완료'); preview.hidden = false;
      preview.replaceChildren(el('strong',r.title), el('div','소스 ID ' + r.source_id),
        el('div','복사될 위치　' + r.dest), el('div','파일 ' + r.count + '개 · ' + bytes(r.size)));
    });
    form.onsubmit = action(form.querySelector('[type=submit]'), async () => {
      note(await api('add', new FormData(form))); await refresh();
    });
  } else if (page === 'settings') {
    const copy = find('gc-settings');
    copy.onsubmit = action(copy.querySelector('[type=submit]'), async () => {
      note(await api('copy_settings', new FormData(copy)), false, 'gc-copy-note');
    }, 'gc-copy-note');

    const tg = find('gc-telegram');
    let revision = 0;
    let saved = 0;
    tg.oninput = () => { revision++; note('', false, 'gc-tg-note'); };
    tg.onsubmit = action(tg.querySelector('[type=submit]'), async () => {
      const submitted = revision;
      const result = await api('telegram_settings', new FormData(tg));
      saved = submitted;
      if (saved === revision) {
        tg.token.value = '';
        tg.clear_token.checked = false;
      }
      find('gc-token-state').textContent = result.has_token
        ? '토큰 저장됨 · 빈칸으로 저장하면 유지됩니다.' : '저장된 토큰 없음';
      note(result.message, false, 'gc-tg-note');
    }, 'gc-tg-note');

    function telegramAction(id, command, progress) {
      const button = find(id);
      button.onclick = action(button, async () => {
        if (revision !== saved) throw Error('변경한 텔레그램 설정을 먼저 저장하세요.');
        note(progress, false, 'gc-tg-note');
        note(await api(command), false, 'gc-tg-note');
      }, 'gc-tg-note');
    }
    telegramAction('gc-connect', 'check_connection', '봇과 채널 연결을 확인하고 있습니다…');
    telegramAction('gc-poll', 'poll', '새 링크를 확인하고 있습니다…');
  } else if (page === 'history') {
    find('gc-filter').onsubmit = event => { event.preventDefault(); current = 1; load(); };
    find('gc-prev').onclick = () => { if (current > 1) current--; load(); };
    find('gc-next').onclick = () => { current++; load(); };
  } else { find('gc-refresh').onclick = load; }
  if (page !== 'settings') load();
  let updating = false;
  setInterval(async () => {
    if (document.hidden || updating) return;
    updating = true;
    try {
      const {active, queued} = await api('status');
      find('gc-active').textContent = active
        ? '#' + active.id + ' ' + (active.title || active.source_id) + ' · ' + active.detail
        : '진행 중인 복사 없음';
      find('gc-queued').textContent = '대기 ' + queued + '건';
      if (page !== 'settings') await refresh();
    } catch (e) {
      find('gc-active').textContent = '상태 갱신 실패 · ' + e.message;
      find('gc-queued').textContent = '';
    } finally { updating = false; }
  }, 8000);
})();
