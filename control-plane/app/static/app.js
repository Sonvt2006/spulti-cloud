import {
  escapeHtml as esc,
  money,
  number,
  date,
  modes,
  states,
  icon,
  badge,
  provider,
  empty,
  costNote,
  priceCoverage,
  stats,
  chart,
  machineTable,
  field,
  notify
} from './ui.js';

const app = document.querySelector('#app');
let token = sessionStorage.getItem('spulti.session') || '';
let user = null,
  epoch = 0,
  page = '',
  authMode = 'login',
  days = 30,
  offset = 0,
  eventOffset = 0;
let listSearch = '',
  listStatus = '',
  latestDetail = null,
  lastLoaded = null;
let finalResult = null;
let costView = 'cumulative';
let draft = {},
  candidates = [],
  selected = 0,
  step = 1,
  locations = [],
  sshKeys = [];
let routeBusy = false,
  actionBusy = false;
let jobFile = null,
  jobFileName = '',
  jobFileReadVersion = 0,
  jobLaunchConfigured = false,
  jobAgentConfigured = false,
  jobAgentError = '';
let jobSettings = {total_steps: 1, checkpoint_every: 20, step_delay: 0, vcpu: 2, ram_gib: 4, provider: '', optimization_mode: 'SAVING'};

async function api(path, {
  method = 'GET',
  body,
  headers = {}
} = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: {
        ...(token ? {
          Authorization: `Bearer ${token}`
        } : {}),
        ...(body ? {
          'Content-Type': 'application/json'
        } : {}),
        ...headers
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(25000),
      cache: 'no-store'
    });
  } catch {
    throw new Error('Không kết nối được API. Kiểm tra kết nối rồi thử lại.');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && token) {
      token = '';
      user = null;
      finalResult = null;
      latestDetail = null;
      epoch++;
      sessionStorage.removeItem('spulti.session');
      authView();
      notify('Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.', true);
    }
    const detail = Array.isArray(data.detail) ? data.detail.map(item => `${item.loc?.slice(1).join('.') || 'Dữ liệu'}: ${item.msg}`).join('; ') : data.detail;
    const error = new Error(typeof detail === 'string' ? detail : `API trả về lỗi ${response.status}. Vui lòng thử lại.`);
    error.status = response.status;
    error.submissionRejected = response.headers.get('X-Spulti-Submission-Outcome') === 'rejected';
    throw error;
  }
  return data;
}

function authView() {
  jobFile = null;
  jobFileName = '';
  jobFileReadVersion++;
  const register = authMode === 'register';
  app.innerHTML = `<main class="auth-page" id="content"><section class="auth-story"><a class="brand" href="#/overview"><img src="./mark.svg" alt=""><span>spulti<span>cloud</span></span></a><div class="auth-copy"><div class="eyebrow">SPOT COMPUTE · MULTI-CLOUD</div><h1>Tài nguyên linh hoạt.<br><em>Chi phí trong tầm tay.</em></h1><p>Chọn cấu hình phù hợp, theo dõi máy và tiếp tục công việc từ checkpoint.</p><div class="auth-features"><span>AWS & GCP</span><span>Spot instances</span><span>Checkpoint recovery</span></div></div><p class="auth-footer">Spulti Cloud / Control-plane console</p></section><section class="auth-form-area"><form id="auth-form" class="auth-form"><div class="eyebrow">CHÀO MỪNG ĐẾN SPULTI</div><br><h2>${register ? 'Tạo tài khoản' : 'Đăng nhập workspace'}</h2><p>${register ? 'Bắt đầu quản lý tài nguyên của bạn.' : 'Theo dõi tài nguyên và chi phí tại một nơi.'}</p>
  ${register ? field('Họ và tên', 'name', '', 'required maxlength="50" autocomplete="name"') + field('Email', 'email', '', 'type="email" required autocomplete="email"') : ''}
  ${field(register ? 'Tên đăng nhập' : 'Tên đăng nhập hoặc email', 'username', '', `required autocomplete="username" ${register ? 'minlength="3" maxlength="64" pattern="[A-Za-z0-9_-]+"' : 'maxlength="255"'}`)}
  ${field('Mật khẩu', 'password', '', `type="password" required ${register ? 'minlength="8"' : ''} maxlength="128" autocomplete="${register ? 'new-password' : 'current-password'}"`)}
  <div id="form-error" role="alert"></div><button class="button primary" type="submit">${register ? 'Tạo tài khoản' : 'Đăng nhập'} ${icon('arrow')}</button><div class="auth-switch">${register ? 'Đã có tài khoản?' : 'Chưa có tài khoản?'} <button class="text-link" type="button" data-action="auth-switch">${register ? 'Đăng nhập' : 'Đăng ký'}</button></div><div class="auth-footnote">Phiên đăng nhập chỉ được giữ trong tab trình duyệt này.</div></form></section></main>`;
}

function shell() {
  app.innerHTML = `<div class="app-shell"><aside class="sidebar" id="sidebar"><a class="brand" href="#/overview"><img src="./mark.svg" alt=""><span>spulti<span>cloud</span></span></a><div class="workspace"><span class="workspace-icon">${icon('cloud')}</span><div>Cloud workspace<small>Hạ tầng của bạn</small></div></div><p class="nav-label">WORKSPACE</p><nav class="nav" aria-label="Điều hướng chính"><a href="#/overview" data-nav="overview">${icon('grid')} Tổng quan</a><a href="#/machines" data-nav="machines">${icon('server')} Danh sách máy</a><a href="#/ssh-keys" data-nav="ssh-keys">${icon('key')} SSH public keys</a><a href="#/create" data-nav="create" class="nav-create">${icon('plus')} Tạo máy mới</a><a href="#/submit-job" data-nav="submit-job" class="nav-create">${icon('upload')} Tạo job mới</a></nav><div class="sidebar-bottom"><div class="infra-note"><b>Thiết kế cho công việc liên tục</b>Checkpoint lưu trạng thái công việc để tiếp tục khi máy Spot bị thu hồi.</div><div class="user"><span class="avatar">${esc(user.name.slice(0,1).toUpperCase())}</span><div class="user-info"><b>${esc(user.name)}</b><small>${esc(user.email)}</small></div><button class="icon-button" data-action="logout" aria-label="Đăng xuất">${icon('logout')}</button></div></div></aside><div class="main-column"><header class="topbar"><div class="breadcrumb"><button class="icon-button mobile-toggle" data-action="menu" aria-label="Mở menu" aria-controls="sidebar" aria-expanded="false">${icon('menu')}</button><span>Workspace</span><span>/</span><b id="breadcrumb">Tổng quan</b></div><div class="topbar-right"><span class="api-indicator"><i class="dot"></i><span id="last-updated">Đang tải dữ liệu</span></span><span class="avatar">${esc(user.name.slice(0,1).toUpperCase())}</span></div></header><main id="content" tabindex="-1"></main></div></div>`;
}
const content = () => document.querySelector('#content');
const period = () => `<select class="period-select" data-control="period" aria-label="Khoảng thời gian">${[7,30,90].map(d => `<option value="${d}" ${days===d?'selected':''}>${d} ngày gần đây</option>`).join('')}</select>`;
const refresh = () => `<button class="icon-button" data-action="refresh" aria-label="Làm mới dữ liệu">${icon('refresh')}</button>`;
const createLink = () => `<a href="#/create" class="button primary">${icon('plus')} Tạo máy mới</a>`;

function heading(title, description, actions = '') {
  return `<div class="page-heading"><div><p class="eyebrow">CLOUD WORKSPACE</p><h1>${esc(title)}</h1><p>${esc(description)}</p></div><div class="button-row">${actions}</div></div>`;
}

function formError(error) {
  const target = document.querySelector('#form-error');
  if (target) {
    target.innerHTML = `<div class="inline-error">${esc(error.message)}</div>`;
    target.scrollIntoView({
      block: 'nearest'
    });
  } else notify(error.message, true);
}

async function render(quiet = false) {
  if (!user) return;
  const currentEpoch = ++epoch;
  const route = location.hash.slice(1) || '/overview';
  const newPage = route.split('/')[1] || 'overview';
  if (!document.querySelector('.app-shell')) shell();
  if (page !== route) {
    quiet = false;
    if (newPage !== 'machines') offset = 0;
    eventOffset = 0;
  }
  page = route;
  routeBusy = true;
  document.querySelector('#sidebar').classList.remove('open');
  document.querySelector('[data-action="menu"]').setAttribute('aria-expanded', 'false');
  document.querySelectorAll('[data-nav]').forEach(el => {
    const active = el.dataset.nav === newPage;
    el.classList.toggle('active', active);
    if (active) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
  document.querySelector('#breadcrumb').textContent = {
    overview: 'Tổng quan',
    machines: 'Danh sách máy',
    create: 'Tạo máy mới',
    'ssh-keys': 'SSH public keys',
    'submit-job': 'Tạo job mới'
  } [newPage] || 'Workspace';
  if (!quiet) content().innerHTML = '<div class="loading" role="status">Đang tải dữ liệu…</div>';
  try {
    let html;
    if (route === '/overview' || route === '/') {
      const [overview, list] = await Promise.all([api(`/api/dashboard?days=${days}`), api('/api/dashboard/machines?limit=5')]);
      html = overviewView(overview, list);
      lastLoaded = overview.as_of;
    } else if (route === '/machines') {
      const query = new URLSearchParams({
        limit: '20',
        offset: String(offset),
        search: listSearch
      });
      if (listStatus) query.set('status', listStatus);
      const data = await api(`/api/dashboard/machines?${query}`);
      html = listView(data);
      lastLoaded = data.as_of;
    } else if (/^\/machines\/\d+$/.test(route)) {
      const id = route.split('/')[2];
      const [data, events] = await Promise.all([api(`/api/dashboard/machines/${id}?days=${days}`), api(`/api/machines/${id}/events?limit=10&offset=${eventOffset}`)]);
      if (currentEpoch !== epoch || !user) return;
      if (data.job?.status === 'COMPLETED') {
        try {
          const cached = finalResult?.job_id === data.job.job_id && finalResult.updated_at === data.job.updated_at;
          const result = cached ? finalResult : await api(`/api/dashboard/jobs/${data.job.job_id}/result`);
          if (currentEpoch !== epoch || !user) return;
          finalResult = {...result, updated_at: data.job.updated_at};
          data.final_result = result;
        } catch (error) {
          if (currentEpoch !== epoch || !user) return;
          data.result_error = error.message;
        }
      }
      latestDetail = data;
      html = detailView(data, events);
      lastLoaded = data.as_of;
    } else if (route === '/create') {
      const [options, keys] = await Promise.all([api('/api/dashboard/options'), api('/api/ssh-keys')]);
      if (currentEpoch !== epoch || !user) return;
      locations = options.locations;
      sshKeys = keys;
      html = createView();
    } else if (route === '/ssh-keys') {
      sshKeys = await api('/api/ssh-keys');
      html = keysView();
    } else if (route === '/submit-job') {
      const [options, keys] = await Promise.all([api('/api/dashboard/options'), api('/api/ssh-keys')]);
      if (currentEpoch !== epoch || !user) return;
      jobLaunchConfigured = options.launch_configured;
      jobAgentConfigured = options.managed_agent_configured === true;
      jobAgentError = options.managed_agent_error || 'Chạy job tự động cần API HTTPS và bản phát hành agent hợp lệ. Quản trị viên cần hoàn tất cấu hình.';
      sshKeys = keys;
      html = submitJobView();
    } else html = empty('Không tìm thấy trang', 'Chọn một trang trong menu.', '<a class="button" href="#/overview">Về tổng quan</a>');
    if (currentEpoch !== epoch || !user) return;
    const expanded = quiet ? [...content().querySelectorAll('details')].map(el => el.open) : [];
    const summaryFocus = quiet ? [...content().querySelectorAll('details > summary')].indexOf(document.activeElement) : -1;
    content().innerHTML = html;
    if (quiet) content().querySelectorAll('details').forEach((el, i) => {
      el.open = Boolean(expanded[i]);
    });
    if (summaryFocus >= 0) content().querySelectorAll('details > summary')[summaryFocus]?.focus({preventScroll: true});
    document.querySelector('#last-updated').textContent = lastLoaded ? `Dữ liệu lúc ${date(lastLoaded)}` : 'Đã kết nối API';
    document.title = `${document.querySelector('#breadcrumb').textContent} · Spulti Cloud`;
    if (!quiet) content().focus({
      preventScroll: true
    });
  } catch (error) {
    if (currentEpoch !== epoch || !user) return;
    if (quiet) notify(error.message, true);
    else content().innerHTML = empty('Chưa tải được dữ liệu', error.message, '<button class="button" data-action="refresh">Thử lại</button>');
  } finally {
    if (currentEpoch === epoch) routeBusy = false;
  }
}

function overviewView(data, list) {
  const c = data.counts;
  return heading('Tổng quan workspace', 'Tài nguyên đang chạy và bức tranh chi phí của bạn.', period() + refresh() + createLink()) +
    stats(data.cost, `<article class="stat"><span>Máy đang quản lý</span><strong>${c.active} <small>máy</small></strong><p>${c.running} đang chạy · ${c.pending} chờ tạo · ${c.recovering} khôi phục</p></article>`) + priceCoverage(data) +
    `<div class="dashboard-grid">${chart(data.daily, {mode: costView, total: data.cost})}<section class="panel activity-panel"><div class="panel-heading"><div><h2>Hoạt động khôi phục</h2><p>Trong ${days} ngày gần đây</p></div>${icon('clock')}</div><div class="activity-summary"><div><strong>${c.recoveries}</strong><small>Lần khôi phục</small></div><div><strong>${c.needs_attention}</strong><small>Máy cần xử lý</small></div></div>${data.recent_events.length ? data.recent_events.map(e=>`<div class="activity-item">${icon('refresh')}<div><a href="#/machines/${e.machine_id}">Máy #${e.machine_id}</a> · ${esc(e.state)}<p>${esc(e.reason)}<br>${date(e.started_at)}</p></div></div>`).join('') : `<div class="activity-item">${icon('check')}<div><b>Chưa có lần khôi phục nào</b><p>Lịch sử sẽ xuất hiện khi worker xử lý sự cố.</p></div></div>`}</section></div>
    <section class="panel"><div class="panel-heading"><div><h2>Máy gần đây <span class="muted">(${list.total})</span></h2><p>Trạng thái đồng bộ từ control-plane</p></div><a href="#/machines">Xem tất cả ${icon('arrow')}</a></div>${machineTable(list.items)}</section>${costNote(data.cost)}`;
}

function listView(data) {
  return heading('Danh sách máy', `${data.total} máy phù hợp · trạng thái tự cập nhật mỗi 15 giây.`, refresh() + createLink()) + `<section class="panel"><form class="filters" id="filter-form"><label class="search-input">${icon('search')}<input name="search" aria-label="Tìm theo tên máy" placeholder="Tìm theo tên máy…" maxlength="100" value="${esc(listSearch)}"></label><select class="period-select" name="status" aria-label="Lọc trạng thái"><option value="">Tất cả trạng thái</option>${Object.entries(states).map(([key,label])=>`<option value="${key}" ${listStatus===key?'selected':''}>${label}</option>`).join('')}</select><button class="button" type="submit">Lọc</button></form>${machineTable(data.items)}<div class="pagination"><span>${data.total ? `${offset+1}–${Math.min(offset+data.items.length,data.total)}` : '0'} / ${data.total} máy</span><div class="button-row"><button class="button" data-action="prev" ${offset===0?'disabled':''}>Trước</button><button class="button" data-action="next" ${offset+data.items.length>=data.total?'disabled':''}>Sau</button></div></div></section>${costNote()}`;
}

function resultView(m) {
  if (!m.job) return '';
  const result = m.final_result;
  let body;
  if (m.result_error) {
    body = `<div class="inline-error" role="alert">${esc(m.result_error)}</div><button class="button" data-action="refresh">Thử tải lại kết quả</button>`;
  } else if (result) {
    const hasState = result.result_json !== null && result.result_json !== '{}';
    body = `${hasState ? `<pre class="result-json" tabindex="0" aria-label="Kết quả cuối cùng">${esc(result.result_json)}</pre>` : '<p class="muted">Job đã hoàn thành nhưng không có kết quả trong spulti.state. Nội dung print() không được lưu trong checkpoint JSON.</p>'}
      <p class="muted">Kết quả vẫn có thể tải sau khi instance đã bị xóa.</p>
      <details class="result-checkpoint"><summary>Xem checkpoint cuối cùng</summary><pre class="result-json" tabindex="0" aria-label="Checkpoint cuối cùng">${esc(result.checkpoint_json)}</pre></details>
      <p class="muted"><small>File JSON chứa trạng thái và thông tin checkpoint. Nếu job lưu file, JSON chỉ chứa tham chiếu tới bản lưu file.</small></p>`;
  } else {
    body = '<p class="muted">Kết quả sẽ xuất hiện khi job hoàn thành và checkpoint cuối cùng được lưu thành công.</p>';
  }
  return `<section class="panel section-gap"><div class="panel-heading"><h2>Kết quả cuối cùng</h2>${result ? '<button class="button primary" data-action="download-result">Tải kết quả JSON</button>' : ''}</div><div class="panel-body">${body}</div></section>`;
}

function detailView(m, events) {
  const compute = m.compute,
    job = m.job;
  const ssh = m.instance?.public_ip ? `ssh ${m.ssh_username}@${m.instance.public_ip}` : null;
  return `<a class="back-link" href="#/machines">${icon('back')} Danh sách máy</a><div class="page-heading"><div><div class="detail-title"><h1>${esc(m.name)}</h1>${badge(m)}</div><p>Máy #${m.id} · Tạo lúc ${date(m.created_at)}</p></div><div class="button-row">${refresh()}${m.needs_attention && !m.delete_requested && m.status!=='DELETED' ? '<button class="button" data-action="retry">Thử lại</button>' : ''}${m.status!=='DELETED'&&!m.delete_requested ? '<button class="button danger" data-action="delete">Xóa máy</button>':''}</div></div>
    ${m.last_error ? `<div class="inline-error"><b>Thông báo từ worker:</b> ${esc(m.last_error)}</div>`:''}
    ${m.delete_requested && m.status !== 'DELETED' ? '<div class="inline-info">Yêu cầu xóa đã được ghi nhận. Worker đang xử lý; chi phí vẫn có thể phát sinh đến khi instance thực sự dừng.</div>':''}
    ${m.status === 'DELETED' ? '<div class="inline-info">Hệ thống đã xác nhận máy không còn hoạt động trên cloud. Bản ghi được giữ lại để tra cứu lịch sử.</div>':''}
    ${stats(m.cost)}<p class="muted"><small>Chi phí tích lũy toàn bộ vòng đời, bao gồm các instance trước khi khôi phục.</small></p>
    <div class="detail-grid"><section class="panel"><div class="panel-heading"><h2>Instance hiện tại</h2>${compute?provider(compute.provider):''}</div><div class="panel-body">${compute ? `<dl class="kv"><dt>Loại máy</dt><dd>${esc(compute.machine_type)}</dd><dt>Tài nguyên</dt><dd>${compute.vcpu} vCPU · ${number(compute.ram_gib)} GiB RAM · ${compute.gpu_count} GPU</dd><dt>Vùng / zone</dt><dd>${esc(compute.region)} / ${esc(compute.zone)}</dd><dt>Spot / giờ</dt><dd>${money(compute.spot_price_per_hour,true)}</dd><dt>On-demand / giờ</dt><dd>${money(compute.on_demand_price_per_hour,true)}</dd><dt>Giá Spot ghi nhận</dt><dd>${date(compute.spot_price_recorded_at)}</dd><dt>Public IP</dt><dd>${esc(m.instance?.public_ip||'Chưa có')}</dd><dt>Đồng bộ gần nhất</dt><dd>${date(m.checked_at)}</dd></dl>${ssh?`<div class="code-line"><code>${esc(ssh)}</code><button class="icon-button" data-action="copy-ssh" aria-label="Sao chép lệnh SSH">${icon('copy')}</button></div>`:''}` : '<p class="muted">Chưa có instance đang hoạt động. Theo dõi trạng thái và thông báo của worker.</p>'}</div></section>
    <section class="panel"><div class="panel-heading"><h2>Cấu hình yêu cầu</h2></div><div class="panel-body"><dl class="kv"><dt>CPU tối thiểu</dt><dd>${m.requested_vcpu} vCPU</dd><dt>RAM tối thiểu</dt><dd>${number(m.requested_ram_gib)} GiB</dd><dt>GPU tối thiểu</dt><dd>${m.requested_gpu_count}</dd><dt>Ổ đĩa</dt><dd>${number(m.requested_disk_gib)} GiB</dd><dt>Vùng / zone</dt><dd>${esc(locationSummary({providers: m.requested_providers, regions: m.requested_regions, zones: m.requested_zones, region: m.requested_region, zone: m.requested_zone}))}</dd><dt>Tối ưu</dt><dd>${esc(modes[m.optimization_mode])}</dd></dl><div class="inline-info">Hệ thống có thể chuyển sang cấu hình phù hợp khác khi khôi phục. Máy luôn sử dụng Spot.</div></div></section></div>
    <div class="page-heading section-gap"><div><h2>Chi phí trong kỳ</h2><p>Spot ${money(m.period_cost.spot_cost)} · On-demand ${money(m.period_cost.on_demand_cost)}</p></div>${period()}</div>${chart(m.daily, {mode: costView, total: m.period_cost})}${costNote(m.period_cost)}
    <section class="panel section-gap"><div class="panel-heading"><h2>Job & checkpoint</h2>${job?`<span class="badge">${esc(job.status)}</span>`:''}</div><div class="panel-body">${job?`<dl class="kv"><dt>Job ID</dt><dd><code>${esc(job.job_id)}</code></dd><dt>Tiến độ agent</dt><dd>${number(job.current_step)} / ${number(job.total_steps)} bước</dd><dt>Checkpoint</dt><dd>Bước ${number(job.checkpoint_step)}</dd><dt>Heartbeat gần nhất</dt><dd>${date(job.last_heartbeat_at)}</dd><dt>Nơi lưu checkpoint</dt><dd>${job.checkpoint?.storage==='s3'?`<code>${esc(`s3://${job.checkpoint.bucket}/${job.checkpoint.key}`)}</code>`:job.checkpoint?'Database (dữ liệu cũ)':'Chưa ghi nhận checkpoint'}</dd></dl><progress class="progress" value="${Number(job.current_step)}" max="${Number(job.total_steps)||1}" aria-label="Tiến độ công việc"></progress>`:'<p class="muted">Máy chưa gắn job. Có thể liên kết job đã tạo bằng CLI khi tạo máy mới.</p>'}</div></section>
    ${resultView(m)}
    <section class="panel section-gap"><div class="panel-heading"><div><h2>Lịch sử instance</h2><p>Mỗi lần khôi phục có thể tạo một instance mới</p></div></div>${m.instances.length?`<div class="table-scroll"><table><thead><tr><th>Instance / loại máy</th><th>Vị trí</th><th>Bắt đầu</th><th>Kết thúc</th><th>Spot</th><th>On-demand</th></tr></thead><tbody>${m.instances.map(i=>`<tr><td>${esc(i.machine_type)}<small class="cell-note">${esc(i.provider_instance_id)}</small></td><td>${provider(i.provider)}<small class="cell-note">${esc(i.zone)}</small></td><td>${date(i.started_at)}</td><td>${i.ended_at?date(i.ended_at):'<span class="badge running">Đang hoạt động</span>'}</td><td>${money(i.cost.spot_cost)}</td><td>${money(i.cost.on_demand_cost)}</td></tr>`).join('')}</tbody></table></div>`:empty('Chưa có instance','Worker sẽ ghi nhận instance sau khi tạo thành công.')}</section>
    <section class="panel section-gap"><div class="panel-heading"><h2>Lịch sử khôi phục</h2></div>${events.length?`<ul class="event-list">${events.map(e=>`<li>${icon('refresh')}<div><b>${esc(e.state)}</b> · ${esc(e.reason)}<small>${date(e.started_at)}${e.downtime_seconds!=null?` · Gián đoạn ${number(e.downtime_seconds)} giây`:''}${e.last_error?`<br>${esc(e.last_error)}`:''}</small></div></li>`).join('')}</ul>`:empty('Không có sự kiện','Các lần khôi phục được lưu tại đây.')}<div class="pagination"><span>Từ sự kiện ${eventOffset+1}</span><div class="button-row"><button class="button" data-action="events-prev" ${!eventOffset?'disabled':''}>Trước</button><button class="button" data-action="events-next" ${events.length<10?'disabled':''}>Sau</button></div></div></section>${costNote(m.cost)}`;
}

function pendingRequest() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('spulti.pending-create') || 'null');
    return saved?.userId === user.id ? saved : null;
  } catch {
    return null;
  }
}

function createView() {
  const pending = pendingRequest();
  if (pending) return heading('Xác minh yêu cầu tạo máy', 'Một yêu cầu trước đó chưa nhận được phản hồi chắc chắn.') + `<section class="panel form-panel"><h2>${esc(pending.body.name)}</h2><p>${pending.body.vcpu} vCPU · ${number(pending.body.ram_gib)} GiB RAM</p><div class="inline-info">Gửi lại cùng mã yêu cầu để lấy kết quả hoặc tiếp tục tạo máy, tránh tạo trùng sau lỗi mạng. Chỉ bỏ yêu cầu sau khi đã kiểm tra danh sách máy.</div><div id="form-error" role="alert"></div><div class="button-row"><button class="button primary" data-action="resume-create">Kiểm tra / gửi lại yêu cầu</button><a class="button" href="#/machines">Xem danh sách máy</a><button class="text-link" data-action="discard-pending">Bỏ yêu cầu đã lưu</button></div></section>`;
  const side = `<aside class="aside-note">${icon('cloud')}<h3>Cấu hình theo nhu cầu</h3><p>Nhập tài nguyên tối thiểu. Hệ thống xếp hạng các loại máy đáp ứng yêu cầu trong vùng đã được cấu hình.</p><hr><p><b>Spot có thể bị thu hồi.</b><br>Liên kết job để node-agent lưu checkpoint và tiếp tục công việc khi khôi phục.</p><p>Giá On-demand dùng để so sánh. Cả hai chế độ tối ưu đều tạo máy Spot.</p></aside>`;
  return heading('Tạo máy mới', 'Bắt đầu từ nhu cầu, tìm cấu hình phù hợp.') + `<ol class="steps">${['Nhu cầu tài nguyên','Chọn đề xuất','Xác nhận tạo máy'].map((label,i)=>`<li class="${step===i+1?'active':''}" ${step===i+1?'aria-current="step"':''}><span>${i+1}</span>${label}</li>`).join('')}</ol><div class="create-grid"><div>${step===1?requirementsView():step===2?recommendationsView():reviewView()}</div>${side}</div>`;
}

function locationSummary(selection) {
  return [
    selection.providers?.join(', '),
    selection.regions?.map(r => `${r.provider} · ${r.region}`).join(', ') || selection.region,
    selection.zones?.map(z => `${z.provider} · ${z.zone}`).join(', ') || selection.zone
  ].filter(Boolean).join(' / ') || 'Tự động';
}

function locationSelections(form) {
  const values = new FormData(form);
  return {
    providers: values.getAll('providers'),
    regions: values.getAll('regions').map(value => JSON.parse(value)),
    zones: values.getAll('zones').map(value => JSON.parse(value))
  };
}

function locationCheckboxes(name, items, selected, placeholder) {
  const keys = new Set(selected.map(item => JSON.stringify(item)));
  return items.length ? items.map(({value, label}) => {
    const key = JSON.stringify(value);
    return `<label class="location-choice"><input type="checkbox" name="${name}" value="${esc(typeof value === 'string' ? value : key)}" ${keys.has(key) ? 'checked' : ''}><span>${esc(label)}</span></label>`;
  }).join('') : `<p class="muted location-placeholder">${placeholder}</p>`;
}

function locationOptions(selection) {
  const providers = selection.providers || [];
  const available = locations.filter(l => providers.includes(l.provider));
  const regionItems = [...new Map(available.map(l => {
    const value = {provider: l.provider, region: l.region};
    return [JSON.stringify(value), {value, label: `${l.provider} · ${l.region}`}];
  })).values()];
  const regions = (selection.regions || []).filter(r => available.some(l => l.provider === r.provider && l.region === r.region));
  const zoneItems = [...new Map(available.filter(l => regions.some(r => r.provider === l.provider && r.region === l.region)).map(l => {
    const value = {provider: l.provider, region: l.region, zone: l.zone};
    return [JSON.stringify(value), {value, label: `${l.provider} · ${l.zone} (${l.region})`}];
  })).values()];
  const zones = (selection.zones || []).filter(z => zoneItems.some(({value}) => value.provider === z.provider && value.region === z.region && value.zone === z.zone));
  return {
    providers: locationCheckboxes('providers', ['AWS', 'GCP'].map(p => ({value: p, label: p})), providers, ''),
    regions: locationCheckboxes('regions', regionItems, regions, providers.length ? 'Chưa có khu vực được cấu hình.' : 'Chọn nhà cung cấp trước.'),
    zones: locationCheckboxes('zones', zoneItems, zones, regions.length ? 'Chưa có AZ được cấu hình.' : 'Chọn khu vực trước.')
  };
}

function requirementsView() {
  const options = locationOptions(draft);
  return `<form class="panel form-panel" id="requirements-form"><h2>Bạn cần bao nhiêu tài nguyên?</h2><p>CPU, RAM và GPU là mức tối thiểu cho mỗi máy.</p>${!locations.length?'<div class="inline-info">Chưa có cấu hình launch. Quản trị viên cần cấu hình vùng, image và mạng cho worker.</div>':''}<div class="form-grid">
    ${field('Tên máy','name',draft.name||'','required maxlength="100" placeholder="Ví dụ: training-worker-01" class="full"')}
    ${field('Ổ đĩa (GiB)','disk_size_gb',draft.disk_size_gb??30,'type="number" required min="1" max="65536" step="1"')}
    ${field('CPU tối thiểu (vCPU)','vcpu',draft.vcpu??2,'type="number" required min="1" step="1"')}
    ${field('RAM tối thiểu (GiB)','ram_gib',draft.ram_gib??4,'type="number" required min="0.25" step="0.25"')}
    ${field('Số GPU tối thiểu','gpu_count',draft.gpu_count??0,'type="number" required min="0" step="1"','Catalog hiện chưa hỗ trợ chọn model GPU.')}
    ${field('Số giờ dự kiến để so sánh','hours',draft.hours??24,'type="number" required min="1" max="8760" step="1"','Chỉ dùng để ước tính, không tự động dừng máy.')}
    <fieldset class="location-group"><legend>Nhà cung cấp</legend><div class="location-options" id="provider-options">${options.providers}</div><small>Chọn ít nhất một nhà cung cấp.</small></fieldset>
    <fieldset class="location-group"><legend>Khu vực</legend><div class="location-options" id="region-options">${options.regions}</div><small>Không tick khu vực: tìm trong mọi khu vực của nhà cung cấp đã chọn.</small></fieldset>
    <fieldset class="location-group full"><legend>Availability zone</legend><div class="location-options" id="zone-options">${options.zones}</div><small>Không tick AZ: tìm trong mọi AZ thuộc phạm vi đã chọn.</small></fieldset></div>
    <div class="form-section"><h3>Mục tiêu tối ưu</h3><div class="mode-options">${Object.entries(modes).map(([key,label])=>`<label class="mode-option"><input type="radio" name="optimization_mode" value="${key}" ${(draft.optimization_mode||'SAVING')===key?'checked':''}>${label}<small>${{SAVING:'Ưu tiên giá Spot thấp nhất',STABLE:'Ưu tiên chỉ số rủi ro thấp'}[key]}</small></label>`).join('')}</div></div>
    <details class="form-section" ${draft.job_id||draft.startup_script?'open':''}><summary>Tùy chọn nâng cao · job và startup script</summary><div class="form-grid">${field('Job ID đã tạo bằng CLI (không bắt buộc)','job_id',draft.job_id||'','class="full" placeholder="UUID của job thuộc tài khoản này"','Startup script tùy chọn chạy trước khi cài managed agent.')}<label class="field full"><span>Startup script (không bắt buộc)</span><textarea name="startup_script" maxlength="8000" placeholder="#!/bin/bash">${esc(draft.startup_script||'')}</textarea><small>Script chạy trên máy mới; phải bắt đầu bằng shebang. Không nhập khóa truy cập cloud.</small></label></div></details><div id="form-error" role="alert"></div><div class="form-actions"><a href="#/machines">Hủy</a><button type="submit" class="button primary" ${!locations.length?'disabled':''}>Tìm máy phù hợp ${icon('arrow')}</button></div></form>`;
}

function recommendationsView() {
  return `<section><div class="panel form-panel"><h2>${candidates.length} đề xuất phù hợp</h2><p>${draft.vcpu} vCPU trở lên · ${number(draft.ram_gib)} GiB RAM trở lên · ${modes[draft.optimization_mode]}</p>${candidates.length?`<div class="candidate-list">${candidates.map((c,i)=>`<label class="candidate"><div class="candidate-head"><input type="radio" name="candidate" value="${i}" ${selected===i?'checked':''}><b>${esc(c.machine_type)}</b>${provider(c.provider)}${i===0?'<span class="rank">Xếp hạng #1</span>':''}</div><div class="candidate-body"><div><p>${c.vcpu} vCPU · ${number(c.ram_gib)} GiB RAM · ${c.gpu_count} GPU</p><p>${esc(c.zone)}</p></div><div class="candidate-price">${money(c.spot_price_per_hour,true)}<small> / giờ</small></div></div><div class="candidate-foot"><span>On-demand ${money(c.on_demand_price_per_hour,true)} / giờ</span><span>${c.savings_percent==null?'Chưa có giá so sánh':`Chênh lệch ${number(c.savings_percent)}%`}</span></div><div class="candidate-meta">Giá Spot: ${date(c.price_recorded_at)} · Điểm rủi ro: ${number(c.risk)} (${esc(c.risk_source)})<br>Chỉ số: ${esc(c.risk_metric)} · Quan sát: ${date(c.risk_observed_at)}</div></label>`).join('')}</div><div class="inline-info">Chỉ số rủi ro giữa AWS và GCP có kỳ đo khác nhau. Đề xuất không giữ chỗ tài nguyên và chưa xác minh quota; giá có thể thay đổi.</div>`:empty('Không có máy đáp ứng yêu cầu','Thử giảm tài nguyên, mở rộng vùng. Quản trị viên cần kiểm tra catalog, giá và cấu hình launch.')}<div id="form-error" role="alert"></div><div class="form-actions"><button class="button" data-action="requirements-back">${icon('back')} Sửa nhu cầu</button>${candidates.length?'<button class="button primary" data-action="review">Tiếp tục</button>':''}</div></div></section>`;
}

function reviewView() {
  const c = candidates[selected];
  return `<section class="panel form-panel"><h2>Xác nhận cấu hình</h2><p>Máy ưu tiên sẽ được thử đầu tiên. Worker có thể dùng đề xuất khác khi thiếu tài nguyên Spot.</p><dl class="kv"><dt>Tên máy</dt><dd><b>${esc(draft.name)}</b></dd><dt>Cấu hình ưu tiên</dt><dd>${esc(c.machine_type)} · ${esc(c.provider)}</dd><dt>Tài nguyên</dt><dd>${c.vcpu} vCPU · ${number(c.ram_gib)} GiB RAM · ${c.gpu_count} GPU</dd><dt>Zone ưu tiên</dt><dd>${esc(c.zone)}</dd><dt>Phạm vi đã chọn</dt><dd>${esc(locationSummary(draft))}</dd><dt>Ổ đĩa</dt><dd>${draft.disk_size_gb} GiB</dd><dt>Chi phí ${draft.hours} giờ</dt><dd>Spot <b>${money(Number(c.spot_price_per_hour)*draft.hours)}</b><br>On-demand ${c.on_demand_price_per_hour==null?'—':money(Number(c.on_demand_price_per_hour)*draft.hours)}</dd><dt>Job liên kết</dt><dd>${esc(draft.job_id||'Không có')}</dd><dt>SSH public keys</dt><dd>${sshKeys.length} key thuộc tài khoản sẽ được cài trên máy</dd></dl>
    ${!sshKeys.length?`<div class="inline-info">Cần thêm SSH public key trước khi tạo máy. <a href="#/ssh-keys">Thêm public key</a>, sau đó quay lại trang này.</div>`:''}<div class="inline-info">Phạm vi khôi phục giữ nguyên nhu cầu bạn đã nhập; có thể chuyển zone hoặc cloud nếu không giới hạn. Chi phí trên chỉ là dự tính compute theo giá hiện tại.</div><div id="form-error" role="alert"></div><div class="form-actions"><button class="button" data-action="recommendations-back">Quay lại đề xuất</button><button class="button primary" data-action="create" ${!sshKeys.length?'disabled':''}>${icon('plus')} Tạo máy Spot</button></div>${costNote()}</section>`;
}

function keysView() {
  return heading('SSH public keys', 'Public key được đưa vào máy khi khởi tạo. Giữ private key trên máy của bạn.') + `<div class="detail-grid"><section class="panel form-panel"><h2>Thêm public key</h2><p>Dán nội dung file .pub của bạn.</p><form id="ssh-form">${field('Tên key','name','','required maxlength="100" placeholder="Ví dụ: laptop-ca-nhan"')}<br><label class="field"><span>SSH public key</span><textarea name="public_key" required maxlength="16384" placeholder="ssh-ed25519 AAAA…"></textarea></label><div id="form-error" role="alert"></div><div class="form-actions"><a href="#/create">Về tạo máy</a><button class="button primary" type="submit">Thêm key</button></div></form></section><section class="panel"><div class="panel-heading"><h2>Public keys của bạn (${sshKeys.length})</h2></div>${sshKeys.length?sshKeys.map(key=>`<div class="key-row"><b>${esc(key.name)}</b><code>${esc(key.fingerprint)}</code><small class="cell-note">Thêm lúc ${date(key.created_at)}</small></div>`).join(''):empty('Chưa có SSH key','Thêm key để có thể tạo và truy cập máy.')}</section></div>`;
}

function submitJobView() {
  const pending = pendingJob();
  const setupNotice = !jobAgentConfigured ? `<div class="inline-info" role="status">${esc(jobAgentError)} <a href="#/create">Tạo máy để SSH</a></div>` : '';
  if (pending) return heading('Xác minh yêu cầu tạo job', 'Yêu cầu được giữ trong tab này để gửi lại an toàn khi mất kết nối.') +
    `<section class="panel form-panel"><h2>${esc(pending.body.filename)}</h2><p>Kết quả yêu cầu trước chưa được xác nhận. Kiểm tra để mở job/máy đã tạo hoặc nhận kết quả từ chối.</p>${setupNotice}<form id="submit-job-form"><div id="form-error" role="alert"></div><button type="submit" class="button primary">Kiểm tra / gửi lại job</button></form></section>`;
  const hasFile = jobFile && jobFileName;
  return heading('Tạo job mới', 'Upload file Python, hệ thống tự tạo job và khởi chạy trên Spot.') +
    `<div class="create-grid"><div><section class="panel form-panel"><h2>Upload & tạo job</h2><p>Upload file Python (UTF-8, tối đa 16 KiB), chọn cấu hình và triển khai trên Spot.</p>
    <form id="submit-job-form">
    <div class="upload-zone" id="upload-zone">
      <input type="file" name="file" id="job-file-input" accept=".py" hidden>
      <button type="button" class="upload-zone-content" data-action="pick-file" aria-label="Chọn file Python">
        ${hasFile
          ? `${icon('file')}<b>${esc(jobFileName)}</b><small>${number(jobFile.length)} ký tự · Nhấp để đổi file</small>`
          : `${icon('upload')}<b>Upload file Python</b><small>Ví dụ: file_training.py</small>`}
      </button>
    </div>
    <div class="form-grid job-settings">
      ${field('Số bước chạy', 'total_steps', jobSettings.total_steps, 'type="number" required min="1" max="10000000" step="1"', '1 = chạy toàn bộ file một lượt. Mỗi bước sẽ chạy lại file.')}
      ${field('Checkpoint mỗi', 'checkpoint_every', jobSettings.checkpoint_every, 'type="number" required min="0" step="1"', 'Số bước giữa các checkpoint. 0 = chỉ lưu theo thời gian và khi dừng.')}
      ${field('vCPU tối thiểu', 'vcpu', jobSettings.vcpu, 'type="number" required min="1" step="1"')}
      ${field('RAM tối thiểu (GiB)', 'ram_gib', jobSettings.ram_gib, 'type="number" required min="0.1" step="0.1"')}
      <label class="field"><span>Nhà cung cấp</span><select name="provider">${[['', 'Tự động (AWS / GCP)'], ['AWS', 'AWS'], ['GCP', 'GCP']].map(([v, label]) => `<option value="${v}" ${jobSettings.provider === v ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
      <label class="field"><span>Tối ưu</span><select name="optimization_mode">${Object.entries(modes).map(([v, label]) => `<option value="${v}" ${jobSettings.optimization_mode === v ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select></label>
      ${field('Nghỉ giữa các bước (giây)', 'step_delay', jobSettings.step_delay, 'type="number" required min="0" max="60" step="0.001"')}
    </div>
    <div class="job-flow">
      <div class="flow-steps">
        <span class="flow-step ${hasFile ? 'done' : 'active'}">Upload file</span>
        <span class="flow-arrow">→</span>
        <span class="flow-step">Đăng ký job UUID</span>
        <span class="flow-arrow">→</span>
        <span class="flow-step">Chọn máy Spot</span>
        <span class="flow-arrow">→</span>
        <span class="flow-step">Tạo máy</span>
        <span class="flow-arrow">→</span>
        <span class="flow-step">Node Agent tự chạy</span>
      </div>
    </div>
    ${!sshKeys.length ? '<p class="inline-error">Cần thêm SSH public key trước khi tạo máy. <a href="#/ssh-keys">Thêm public key</a></p>' : ''}
    ${!jobLaunchConfigured ? '<p class="inline-error">Hệ thống chưa cấu hình vùng và image để tạo máy.</p>' : ''}
    ${setupNotice}
    <div id="form-error" role="alert"></div>
    <div class="form-actions">
      <a href="#/overview">Hủy</a>
      <button type="submit" class="button primary" ${!hasFile || !sshKeys.length || !jobLaunchConfigured || !jobAgentConfigured ? 'disabled' : ''}>${icon('upload')} Upload & Tạo job</button>
    </div>
    </form>
    </section></div>
    <aside class="aside-note">${icon('cloud')}<h3>Luồng tự động</h3><p>Upload file → Đăng ký job → Chọn máy Spot → Tạo máy → Node Agent tự chạy.</p><hr><p>Hệ thống chọn máy theo tài nguyên và chế độ tối ưu của bạn. Đĩa mặc định 30 GiB.</p><p>Dùng <code>spulti.state</code> cho trạng thái JSON hoặc lưu file trong <code>SPULTI_CHECKPOINT_DIR</code> để khôi phục từ checkpoint gần nhất. File được đồng bộ định kỳ và khi dừng.</p><p>Thư viện bên ngoài cần có sẵn trong image của máy.</p></aside>
    </div>`;
}

function pendingJob() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('spulti.pending-job'));
    return saved?.userId === user?.id ? saved : null;
  } catch { return null; }
}

async function selectJobFile(file) {
  if (actionBusy || pendingJob()) return;
  const version = ++jobFileReadVersion, owner = user?.id;
  try {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*\.py$/.test(file.name) || file.name.length > 128) throw new Error('Tên file .py chỉ gồm chữ không dấu, số, dấu chấm, gạch ngang hoặc gạch dưới.');
    if (!file.size || file.size > 16384) throw new Error('File phải có nội dung và không quá 16 KiB.');
    const source = new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer());
    if (version !== jobFileReadVersion || owner !== user?.id || actionBusy) return;
    jobFile = source;
    jobFileName = file.name;
    if (page === '/submit-job') {
      const zone = document.querySelector('.upload-zone-content');
      if (zone) zone.innerHTML = `${icon('file')}<b>${esc(jobFileName)}</b><small>${number(file.size)} byte · Nhấp để đổi file</small>`;
      const submit = document.querySelector('#submit-job-form [type="submit"]');
      if (submit) submit.disabled = !sshKeys.length || !jobLaunchConfigured || !jobAgentConfigured;
    }
  } catch (error) {
    if (owner === user?.id) notify(error instanceof TypeError ? 'File cần dùng mã hóa UTF-8.' : error.message, true);
  }
}

function readRequirements(form) {
  const data = Object.fromEntries(new FormData(form));
  Object.assign(data, locationSelections(form));
  if (!data.providers.length) throw new Error('Vui lòng chọn ít nhất một nhà cung cấp.');
  for (const key of ['vcpu', 'gpu_count', 'disk_size_gb', 'hours']) data[key] = Number(data[key]);
  for (const key of ['provider', 'region', 'zone', 'job_id']) data[key] = data[key] || null;
  data.name = data.name.trim();
  if (!data.name) throw new Error('Vui lòng nhập tên máy.');
  if (data.job_id && !/^[\da-f]{8}-([\da-f]{4}-){3}[\da-f]{12}$/i.test(data.job_id)) throw new Error('Job ID phải có định dạng UUID.');
  if (data.startup_script && !data.startup_script.startsWith('#!')) throw new Error('Startup script phải bắt đầu bằng shebang, ví dụ #!/bin/bash.');
  return {
    ...data,
    limit: 20
  };
}

function placementBody(data) {
  return Object.fromEntries(['vcpu', 'ram_gib', 'gpu_count', 'provider', 'region', 'zone', 'providers', 'regions', 'zones', 'optimization_mode', 'limit'].map(key => [key, data[key]]));
}

function uniqueId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
async function sendCreate(saved = null) {
  if (!saved) {
    const c = candidates[selected];
    const {
      hours,
      ...body
    } = draft;
    body.preferred_candidate = {
      machine_type_id: c.machine_type_id,
      region: c.region,
      zone: c.zone
    };
    saved = {
      userId: user.id,
      key: uniqueId(),
      body
    };
    sessionStorage.setItem('spulti.pending-create', JSON.stringify(saved));
  }
  try {
    const machine = await api('/api/machines', {
      method: 'POST',
      body: saved.body,
      headers: {
        'Idempotency-Key': saved.key
      }
    });
    sessionStorage.removeItem('spulti.pending-create');
    draft = {};
    candidates = [];
    step = 1;
    notify('Đã ghi nhận yêu cầu. Worker đang khởi tạo máy.');
    location.hash = `/machines/${machine.id}`;
  } catch (error) {
    // Validation/rejection happened before a launch intent was committed. Preserve
    // the key for network/5xx errors, whose outcome may be ambiguous.
    if ([400, 403, 404, 409, 422].includes(error.status)) {
      sessionStorage.removeItem('spulti.pending-create');
      step = 1;
    }
    if (user && page === '/create') {
      content().innerHTML = createView();
      formError(error);
    }
  }
}
async function confirmAction(title, description, label) {
  const dialog = document.querySelector('#confirm-dialog');
  dialog.innerHTML = `<form method="dialog"><h2 id="dialog-title">${esc(title)}</h2><p>${esc(description)}</p><div class="button-row"><button class="button" value="cancel" autofocus>Hủy</button><button class="button danger" value="confirm">${esc(label)}</button></div></form>`;
  dialog.returnValue = 'cancel';
  dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), {
    once: true
  }));
}

app.addEventListener('submit', async event => {
  event.preventDefault();
  if (actionBusy) return;
  const form = event.target,
    submit = form.querySelector('[type="submit"]');
  const startedPage = page,
    startedUser = user?.id;
  actionBusy = true;
  if (submit) submit.disabled = true;
  try {
    if (form.id === 'auth-form') {
      const body = Object.fromEntries(new FormData(form));
      if (authMode === 'register') await api('/api/auth/register', {
        method: 'POST',
        body
      });
      const session = await api('/api/auth/login', {
        method: 'POST',
        body: {
          username: body.username,
          password: body.password
        }
      });
      token = session.access_token;
      sessionStorage.setItem('spulti.session', token);
      user = await api('/api/auth/me');
      authMode = 'login';
      shell();
      await render();
    } else if (form.id === 'filter-form') {
      const data = new FormData(form);
      listSearch = String(data.get('search'));
      listStatus = String(data.get('status'));
      offset = 0;
      await render();
    } else if (form.id === 'requirements-form') {
      draft = readRequirements(form);
      candidates = [];
      const results = await api('/api/placement/recommendations?launchable_only=true', {
        method: 'POST',
        body: placementBody(draft)
      });
      if (page !== startedPage || user?.id !== startedUser) return;
      candidates = results;
      selected = 0;
      step = 2;
      content().innerHTML = createView();
      content().focus();
      window.scrollTo(0, 0);
    } else if (form.id === 'ssh-form') {
      const body = Object.fromEntries(new FormData(form));
      if (body.public_key.includes('PRIVATE KEY')) throw new Error('Đây là private key. Chỉ nhập nội dung file public key (.pub).');
      await api('/api/ssh-keys', {
        method: 'POST',
        body
      });
      notify('Đã thêm SSH public key.');
      await render();
    } else if (form.id === 'submit-job-form') {
      let saved = pendingJob();
      if (!saved) {
        if (!jobAgentConfigured || !jobLaunchConfigured) throw new Error(jobAgentError || 'Hệ thống chưa sẵn sàng chạy job tự động.');
        if (!jobFile || !jobFileName) throw new Error('Vui lòng chọn file Python.');
        const data = Object.fromEntries(new FormData(form));
        const body = {filename: jobFileName, source_code: jobFile, provider: data.provider || null, optimization_mode: data.optimization_mode};
        for (const name of ['total_steps', 'checkpoint_every', 'step_delay', 'vcpu', 'ram_gib']) body[name] = Number(data[name]);
        saved = {userId: user.id, key: uniqueId(), body};
        sessionStorage.setItem('spulti.pending-job', JSON.stringify(saved));
      }
      if (page === startedPage) content().innerHTML = submitJobView();
      const retry = document.querySelector('#submit-job-form [type="submit"]');
      if (retry) retry.disabled = true;
      const result = await api('/api/dashboard/jobs', {
        method: 'POST',
        body: saved.body,
        headers: {'Idempotency-Key': saved.key}
      });
      sessionStorage.removeItem('spulti.pending-job');
      if (user?.id !== startedUser) return;
      jobFile = null;
      jobFileName = '';
      notify(`Đã ghi nhận job trên ${result.candidate.provider}. Worker đang xử lý yêu cầu tạo máy.`);
      if (page === startedPage) location.hash = `/machines/${result.machine.id}`;
    }
  } catch (error) {
    if (form.id === 'submit-job-form' && (error.submissionRejected || [400, 403, 404, 409, 422].includes(error.status))) sessionStorage.removeItem('spulti.pending-job');
    if (form.id === 'submit-job-form' && user?.id === startedUser && page === startedPage) content().innerHTML = submitJobView();
    formError(error);
  } finally {
    actionBusy = false;
    if (submit?.isConnected) submit.disabled = false;
    if (form.id === 'submit-job-form' && pendingJob()) {
      const retry = document.querySelector('#submit-job-form [type="submit"]');
      if (retry) retry.disabled = false;
    }
  }
});

app.addEventListener('click', async event => {
  const sidebar = document.querySelector('#sidebar');
  if (sidebar?.classList.contains('open') && !event.target.closest('#sidebar,[data-action="menu"]')) {
    event.preventDefault();
    sidebar.classList.remove('open');
    document.querySelector('[data-action="menu"]').setAttribute('aria-expanded', 'false');
    return;
  }
  const button = event.target.closest('[data-action]');
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  if (action === 'menu') {
    const open = document.querySelector('#sidebar').classList.toggle('open');
    button.setAttribute('aria-expanded', String(open));
    return;
  }
  if (action === 'auth-switch') {
    authMode = authMode === 'login' ? 'register' : 'login';
    authView();
    return;
  }
  if (actionBusy) return;
  if (action === 'logout') {
    token = '';
    user = null;
    finalResult = null;
    latestDetail = null;
    epoch++;
    sessionStorage.removeItem('spulti.session');
    jobFile = null;
    jobFileName = '';
    jobFileReadVersion++;
    draft = {};
    candidates = [];
    step = 1;
    page = '';
    authView();
    return;
  }
  actionBusy = true;
  button.disabled = true;
  try {
    if (action === 'refresh') await render();
    if (action === 'download-result' && latestDetail?.final_result) {
      const result = latestDetail.final_result;
      const url = URL.createObjectURL(new Blob([result.checkpoint_json], {type: 'application/json;charset=utf-8'}));
      const link = document.createElement('a');
      link.href = url;
      link.download = result.filename;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    if (action === 'next' || action === 'prev') {
      offset = Math.max(0, offset + (action === 'next' ? 20 : -20));
      await render();
    }
    if (action === 'events-next' || action === 'events-prev') {
      eventOffset = Math.max(0, eventOffset + (action === 'events-next' ? 10 : -10));
      await render();
    }
    if (action === 'requirements-back') {
      step = 1;
      content().innerHTML = createView();
    }
    if (action === 'recommendations-back') {
      step = 2;
      content().innerHTML = createView();
    }
    if (action === 'review') {
      step = 3;
      content().innerHTML = createView();
      window.scrollTo(0, 0);
    }
    if (action === 'create') await sendCreate();
    if (action === 'resume-create') await sendCreate(pendingRequest());
    if (action === 'discard-pending' && await confirmAction('Bỏ yêu cầu đã lưu?', 'Nếu máy đã được tạo, thao tác này không xóa máy đó. Kiểm tra danh sách máy trước khi tạo yêu cầu khác.', 'Bỏ yêu cầu')) {
      sessionStorage.removeItem('spulti.pending-create');
      step = 1;
      content().innerHTML = createView();
    }
    if (action === 'delete') {
      const id = latestDetail.id;
      if (await confirmAction(`Xóa máy ${latestDetail.name}?`, 'Worker sẽ dừng instance đang chạy. Máy đã xóa không thể khôi phục bằng nút Thử lại.', 'Xóa máy')) {
        await api(`/api/machines/${id}`, {
          method: 'DELETE'
        });
        notify('Đã ghi nhận yêu cầu xóa.');
        await render();
      }
    }
    if (action === 'retry') {
      await api(`/api/machines/${latestDetail.id}/retry`, {
        method: 'POST'
      });
      notify('Đã yêu cầu worker thử lại.');
      await render();
    }
    if (action === 'copy-ssh') {
      const text = `ssh ${latestDetail.ssh_username}@${latestDetail.instance.public_ip}`;
      if (!navigator.clipboard) throw new Error('Trình duyệt cần HTTPS để sao chép. Bạn có thể chọn và sao chép lệnh SSH trực tiếp.');
      await navigator.clipboard.writeText(text);
      notify('Đã sao chép lệnh SSH.');
    }
    if (action === 'pick-file') {
      const input = document.querySelector('#job-file-input');
      if (input) input.click();
    }
  } catch (error) {
    formError(error);
  } finally {
    actionBusy = false;
    if (button.isConnected) button.disabled = false;
  }
});
app.addEventListener('change', event => {
  if (event.target.dataset.control === 'cost-view') {
    costView = event.target.value;
    render();
  }
  if (event.target.dataset.control === 'period') {
    days = Number(event.target.value);
    render();
  }
  if (event.target.name === 'candidate') selected = Number(event.target.value);
  if (event.target.id === 'job-file-input' && event.target.files[0]) {
    selectJobFile(event.target.files[0]);
    event.target.value = '';
  }
  if (['providers', 'regions'].includes(event.target.name) && event.target.form?.id === 'requirements-form') {
    const form = event.target.form;
    const options = locationOptions(locationSelections(form));
    if (event.target.name === 'providers') form.querySelector('#region-options').innerHTML = options.regions;
    form.querySelector('#zone-options').innerHTML = options.zones;
  }
});
window.addEventListener('hashchange', () => render());
app.addEventListener('input', event => {
  if (event.target.form?.id === 'submit-job-form' && event.target.name in jobSettings) jobSettings[event.target.name] = event.target.value;
});
app.addEventListener('dragover', event => {
  const zone = event.target.closest('#upload-zone');
  if (zone) {
    event.preventDefault();
    zone.classList.add('dragover');
  }
});
app.addEventListener('dragleave', event => {
  const zone = event.target.closest('#upload-zone');
  if (zone) zone.classList.remove('dragover');
});
app.addEventListener('drop', event => {
  const zone = event.target.closest('#upload-zone');
  if (!zone) return;
  event.preventDefault();
  zone.classList.remove('dragover');
  const file = event.dataTransfer?.files[0];
  if (!file) return;
  selectJobFile(file);
});
document.querySelector('.skip-link').addEventListener('click', event => {
  event.preventDefault();
  const target = content();
  if (target) {
    target.tabIndex = -1;
    target.focus();
  }
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    document.querySelector('#sidebar')?.classList.remove('open');
    document.querySelector('[data-action="menu"]')?.setAttribute('aria-expanded', 'false');
  }
});
setInterval(() => {
  // Never interrupt editing, dialogs, or selections during automatic refresh.
  const focused = document.activeElement;
  const pollable = ['/overview', '/machines'].includes(page) || /^\/machines\/\d+$/.test(page);
  if (user && !document.hidden && !routeBusy && !actionBusy && pollable) {
    if (!focused?.closest('form,button,select,a') && !window.getSelection()?.toString() && !document.querySelector('dialog[open]')) render(true);
  }
}, 15000);

async function boot() {
  if (!token) {
    authView();
    return;
  }
  try {
    user = await api('/api/auth/me');
    shell();
    await render();
  } catch (error) {
    if (token) {
      app.innerHTML = empty('Không kết nối được workspace', error.message, '<button class="button" id="boot-retry">Thử lại</button>');
      document.querySelector('#boot-retry').addEventListener('click', boot);
    } else authView();
  }
}
boot();
