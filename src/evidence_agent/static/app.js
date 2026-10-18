'use strict';
const $ = id => document.getElementById(id),
  node = (tag, text, cls) => {
    const e = document.createElement(tag);
    if (text !== undefined) e.textContent = text;
    if (cls) e.className = cls;
    return e
  };
let token = '',
  approvalToken = '',
  current = null,
  streamController = null,
  cursor = 0,
  events = [],
  refreshTimer = null,
  pendingCreate = null,
  selectionGeneration = 0;
const approvalKeys = new Map(),
  terminal = new Set(['completed', 'failed', 'cancelled', 'rejected']);
const labels = {
  queued: '排队中',
  running: '执行中',
  awaiting_approval: '等待审批',
  approved: '动作待执行',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  rejected: '已拒绝'
};
async function api(path, options = {}, approval = false) {
  const headers = {
    ...(options.headers || {})
  };
  const key = approval ? (approvalToken || token) : token;
  if (key) headers.Authorization = 'Bearer ' + key;
  if (options.body) headers['Content-Type'] = 'application/json';
  const r = await fetch(path, {
    ...options,
    headers,
    cache: 'no-store'
  });
  if (!r.ok) {
    let detail;
    try {
      detail = (await r.json()).detail
    } catch {
      detail = r.statusText
    }
    throw Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return r.json()
}

function message(text = '') {
  $('message').textContent = text
}
async function config() {
  const c = await api('/api/config');
  $('service').textContent = c.openai_enabled ? '真实模型接口已配置' : 'Demo ready · 无 API 消费';
  $('provider').querySelector('option[value="openai"]').disabled = !c.openai_enabled
}
async function runList() {
  const runs = await api('/api/runs');
  $('runs').replaceChildren(...runs.map(r => {
    const b = node('button', r.goal.slice(0, 38), 'run' + (current && current.id === r.id ? ' active' : ''));
    b.append(node('small', `${labels[r.status]||r.status} · ${r.provider} · ${r.id.slice(0,8)}`));
    b.onclick = () => selectRun(r.id).catch(e => message(e.message));
    return b
  }))
}

function render(r) {
  current = r;
  $('run-title').textContent = r.goal.slice(0, 64);
  $('run-id').textContent = `${r.id} · ${r.provider} · revision ${r.version}`;
  $('status').textContent = labels[r.status] || r.status;
  $('metric-steps').textContent = r.steps;
  $('metric-tokens').textContent = r.provider === 'demo' ? '0 · demo' : r.usage.input_tokens + r.usage.output_tokens;
  $('metric-evidence').textContent = r.evidence.length;
  $('metric-errors').textContent = r.tool_errors;
  $('plan').replaceChildren(...(r.plan.length ? r.plan : ['等待 Agent 提交任务计划。']).map(p => node('li', p)));
  $('cancel').disabled = terminal.has(r.status);
  $('replay').disabled = !r.report || r.provider === 'replay';
  $('evidence').replaceChildren(...r.evidence.map(e => {
    const box = node('div', undefined, 'evidence-card');
    box.append(node('strong', e.source));
    const d = node('details');
    d.append(node('summary', `${e.title} · SHA ${e.sha256.slice(0,12)}`), node('p', e.text));
    box.append(d);
    return box
  }));
  $('approval').classList.toggle('hidden', r.status !== 'awaiting_approval');
  if (r.approval) {
    $('approval-preview').textContent = r.approval.payload.body;
    $('approval-digest').textContent = `Action SHA-256: ${r.approval.digest} · 到期 ${new Date(r.approval.expires_at*1000).toLocaleString()}`
  }
  if (r.error) message('运行停止：' + r.error);
  if (r.report) {
    const p = r.report,
      content = [node('h4', p.title), node('span', p.status === 'supported' ? '引用出处检查通过 · 结论待审阅' : '证据不足，已停止断言', 'badge'), node('p', p.recommendation)];
    p.claims.forEach(c => {
      content.push(node('p', c.text));
      c.citations.forEach(q => content.push(node('blockquote', q.quote), node('div', q.chunk_id, 'source')))
    });
    r.calculations.forEach(c => content.push(node('div', `${c.expression} = ${c.value}`, 'calculation')));
    const ul = node('ul');
    p.limitations.forEach(l => ul.append(node('li', l)));
    content.push(node('h3', '限制与待确认项'), ul);
    if (r.ticket_id) content.push(node('p', '已创建本地工单：' + r.ticket_id));
    $('report').replaceChildren(...content)
  } else $('report').replaceChildren(node('p', 'Agent 正在读取证据与验证引用。', 'muted'))
}

function renderTrace() {
  const visible = events.slice(-60);
  $('trace').replaceChildren(...visible.map(e => {
    const box = node('div', undefined, 'event');
    box.append(node('time', new Date(e.timestamp * 1000).toLocaleTimeString()), node('strong', e.type + (e.data.tool ? ' / ' + e.data.tool : '')));
    if (e.data.summary || e.data.error || e.data.reason) box.append(node('p', e.data.summary || e.data.error || e.data.reason));
    return box
  }));
  $('trace').scrollTop = $('trace').scrollHeight
}
async function refresh(id) {
  if (!current || current.id !== id) return;
  const r = await api('/api/runs/' + id);
  if (current && current.id === id) render(r);
  await runList()
}
async function streamRun(id, signal, attempt = 0) {
  if (signal.aborted || !current || current.id !== id) return;
  try {
    const headers = token ? {
      Authorization: 'Bearer ' + token
    } : {};
    const r = await fetch(`/api/runs/${id}/events/stream?after=${cursor}`, {
      headers,
      signal
    });
    if (!r.ok) throw Error('轨迹接口返回 ' + r.status);
    const reader = r.body.getReader(),
      decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const {
        value,
        done
      } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {
        stream: true
      });
      if (buffer.length > 100000) throw Error('轨迹帧过大');
      let split;
      while ((split = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        const line = frame.split('\n').find(l => l.startsWith('data: '));
        if (line) {
          const event = JSON.parse(line.slice(6));
          if (current && current.id === id && event.seq > cursor) {
            cursor = event.seq;
            events.push(event);
            renderTrace();
            clearTimeout(refreshTimer);
            refreshTimer = setTimeout(() => refresh(id).catch(e => message(e.message)), 80)
          }
        }
      }
    }
    await refresh(id);
    if (!signal.aborted && current && current.id === id && !terminal.has(current.status)) setTimeout(() => streamRun(id, signal), 800)
  } catch (e) {
    if (e.name !== 'AbortError') {
      message(e.message);
      if (!signal.aborted && current && current.id === id && !terminal.has(current.status) && attempt < 5) {
        setTimeout(() => streamRun(id, signal, attempt + 1), Math.min(1000 * 2 ** attempt, 16000));
      }
    }
  }
}
async function selectRun(id) {
  const generation = ++selectionGeneration;
  if (streamController) streamController.abort();
  const controller = new AbortController();
  streamController = controller;
  cursor = 0;
  events = [];
  renderTrace();
  message();
  try {
    const r = await api('/api/runs/' + id, {
      signal: controller.signal
    });
    if (generation !== selectionGeneration) return;
    render(r);
    await runList();
    if (generation === selectionGeneration) streamRun(id, controller.signal);
  } catch (e) {
    if (e.name !== 'AbortError') throw e;
  }
}
$('start').onclick = async () => {
  const b = $('start');
  b.disabled = true;
  message();
  try {
    const body = JSON.stringify({
      goal: $('goal').value,
      provider: $('provider').value,
      max_steps: Number($('steps').value),
      create_ticket: $('ticket').checked
    });
    if (!pendingCreate || pendingCreate.body !== body) pendingCreate = {
      body,
      key: crypto.randomUUID()
    };
    const r = await api('/api/runs', {
      method: 'POST',
      body,
      headers: {
        'Idempotency-Key': pendingCreate.key
      }
    });
    pendingCreate = null;
    await selectRun(r.id)
  } catch (e) {
    message(e.message)
  } finally {
    b.disabled = false
  }
};
async function decide(decision) {
  if (!current || !current.approval) return;
  const r = current;
  const body = JSON.stringify({
    expected_version: r.version,
    digest: r.approval.digest,
    decision
  });
  const mapKey = r.id + ':' + decision;
  if (!approvalKeys.has(mapKey)) approvalKeys.set(mapKey, crypto.randomUUID());
  try {
    await api(`/api/runs/${r.id}/approval`, {
      method: 'POST',
      body,
      headers: {
        'Idempotency-Key': approvalKeys.get(mapKey)
      }
    }, true);
    await refresh(r.id)
  } catch (e) {
    message(e.message)
  }
}
$('approve').onclick = () => decide('approve');
$('reject').onclick = () => decide('reject');
$('cancel').onclick = async () => {
  if (!current) return;
  try {
    await api(`/api/runs/${current.id}/cancel`, {
      method: 'POST',
      body: JSON.stringify({
        expected_version: current.version
      })
    });
    await refresh(current.id)
  } catch (e) {
    message(e.message)
  }
};
$('replay').onclick = async () => {
  if (!current) return;
  try {
    const r = await api(`/api/runs/${current.id}/replay`, {
      method: 'POST',
      headers: {
        'Idempotency-Key': crypto.randomUUID()
      }
    });
    await selectRun(r.id)
  } catch (e) {
    message(e.message)
  }
};
$('connect').onclick = async () => {
  token = $('api-token').value;
  approvalToken = $('approval-token').value;
  $('api-token').value = '';
  $('approval-token').value = '';
  try {
    message();
    await config();
    await runList();
    if (current) await selectRun(current.id)
  } catch (e) {
    message(e.message)
  }
};
(async () => {
  try {
    await config();
    await runList()
  } catch (e) {
    $('service').textContent = '等待访问凭据';
    message(e.message)
  }
})();
