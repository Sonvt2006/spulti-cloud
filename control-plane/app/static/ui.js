// All API strings pass through escapeHtml before entering HTML templates.
export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;'
} [char]));
export const money = (value, hourly = false) => value == null ? '—' : new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: hourly ? 5 : 4
}).format(Number(value));
export const number = value => new Intl.NumberFormat('vi-VN', {
  maximumFractionDigits: 2
}).format(Number(value || 0));
export const date = value => value ? new Intl.DateTimeFormat('vi-VN', {
  dateStyle: 'short',
  timeStyle: 'short'
}).format(new Date(/Z$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)) : '—';
export const modes = {
  SAVING: 'Tiết kiệm',
  BALANCED: 'Cân bằng',
  STABLE: 'Ưu tiên ổn định'
};
export const states = {
  PENDING: 'Chờ khởi tạo',
  PROVISIONING: 'Đang khởi tạo',
  RUNNING: 'Đang chạy',
  RECOVERING: 'Đang khôi phục',
  DELETED: 'Đã xóa'
};
const paths = {
  grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  server: 'M4 3h16v7H4z M4 14h16v7H4z M7 6h.01 M7 17h.01 M11 6h6 M11 17h6',
  plus: 'M12 5v14 M5 12h14',
  key: 'M14 7a5 5 0 1 1-3 9L4 23l-3-3 7-7A5 5 0 0 1 14 7z M16 10h.01',
  arrow: 'M7 17 17 7 M7 7h10v10',
  refresh: 'M20 7v5h-5 M4 17v-5h5 M6 6a8 8 0 0 1 13 3 M18 18a8 8 0 0 1-13-3',
  clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2',
  logout: 'M9 4H4v16h5 M10 12h11 M17 8l4 4-4 4',
  menu: 'M4 6h16 M4 12h16 M4 18h16',
  check: 'm5 12 4 4L19 6',
  search: 'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6',
  cloud: 'M6 18a5 5 0 0 1-1-10 7 7 0 0 1 13-1 5.5 5.5 0 0 1 0 11z',
  copy: 'M8 8h12v13H8z M16 8V3H3v13h5',
  back: 'M20 12H4 m6-6-6 6 6 6',
};
export const icon = (name, extra = '') => `<svg class="icon ${extra}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.cloud}"/></svg>`;
export function badge(machine) {
  const deleted = machine.status === 'DELETED';
  const label = deleted
    ? states.DELETED
    : machine.delete_requested
      ? 'Đang yêu cầu xóa'
      : machine.needs_attention
        ? 'Cần xử lý'
        : states[machine.status] || machine.status;
  const css = deleted
    ? 'deleted'
    : machine.needs_attention
      ? 'warning'
      : machine.delete_requested
        ? 'pending'
        : String(machine.status).toLowerCase();
  return `<span class="badge ${escapeHtml(css)}"><span class="dot"></span>${escapeHtml(label)}</span>`;
}
export const provider = name => `<span class="provider ${name === 'AWS' ? 'aws' : 'gcp'}">${escapeHtml(name || '—')}</span>`;
export const empty = (title, description, action = '') => `<div class="empty">${icon('server')}<h3>${escapeHtml(title)}</h3><p>${escapeHtml(description)}</p>${action}</div>`;
export function costNote(cost) {
  return `<p class="cost-note">Ước tính chi phí compute từ thời gian chạy và lịch sử giá, theo USD. Chưa gồm ổ đĩa, mạng, IP, EC2 control-plane, RDS, S3, thuế hoặc ưu đãi.${cost && !cost.complete ? ` <strong>Thiếu giá cho ${number(cost.missing_spot_hours)} giờ Spot / ${number(cost.missing_on_demand_hours)} giờ On-demand; tổng tương ứng chưa xác định.</strong>` : ''}</p>`;
}
export function stats(cost, first) {
  return `<div class="stats">${first || `<article class="stat"><span>Thời gian compute</span><strong>${number(cost.runtime_hours)} <small>giờ</small></strong><p>Cộng tất cả instance trong kỳ</p></article>`}
    <article class="stat"><span>Chi phí Spot ước tính</span><strong>${money(cost.spot_cost)}</strong><p>${cost.spot_cost == null ? `Phần đã biết: ${money(cost.known_spot_cost)}` : 'Theo lịch sử giá Spot'}</p></article>
    <article class="stat"><span>Nếu chạy On-demand</span><strong>${money(cost.on_demand_cost)}</strong><p>Cùng cấu hình và thời gian chạy</p></article>
    <article class="stat accent"><span>Chênh lệch chi phí</span><strong>${money(cost.savings)}</strong><p>${cost.savings_percent == null ? 'Cần đủ dữ liệu giá để so sánh' : `${number(cost.savings_percent)}% so với On-demand`}</p></article></div>`;
}
export function chart(days, title = 'Chi phí theo ngày', subtitle = 'Spot và On-demand · ngày UTC') {
  const series = [
    ['on_demand_cost', 'demand-line'],
    ['spot_cost', 'spot-line']
  ];
  const max = Math.max(0.01, ...days.flatMap(day => series.map(([key]) => Number(day[key] || 0))));
  const x = i => 56 + i * 650 / Math.max(1, days.length - 1),
    y = n => 190 - Number(n) / max * 154;
  const line = key => {
    let started = false;
    return days.map((day, i) => {
      if (day[key] == null) {
        started = false;
        return '';
      }
      const part = `${started ? 'L' : 'M'}${x(i).toFixed(2)},${y(day[key]).toFixed(2)}`;
      started = true;
      return part;
    }).join(' ');
  };
  const labels = [...new Set([0, Math.floor((days.length - 1) / 2), days.length - 1])].filter(i => days[i]);
  return `<section class="panel chart-panel"><div class="panel-heading"><div><h2>${escapeHtml(title)}</h2><p>${escapeHtml(subtitle)}</p></div><div class="legend"><span><i class="spot-swatch"></i>Spot</span><span><i class="demand-swatch"></i>On-demand</span></div></div>
    ${days.length ? `<svg class="chart" viewBox="0 0 740 232" role="img" aria-label="Biểu đồ chi phí compute theo ngày; số liệu chi tiết ở bảng bên dưới">
    ${[0, 0.5, 1].map(r => `<line class="chart-grid" x1="56" x2="706" y1="${y(max*r)}" y2="${y(max*r)}"/><text x="46" y="${y(max*r)+4}" text-anchor="end">${money(max*r)}</text>`).join('')}
    ${series.map(([key, css]) => `<path class="${css}" d="${line(key)}"/>${days.map((day,i) => day[key] == null ? '' : `<circle class="${css}-point" cx="${x(i)}" cy="${y(day[key])}" r="3"><title>${escapeHtml(day.date)}: ${money(day[key])}</title></circle>`).join('')}`).join('')}
    ${labels.map(i => `<text x="${x(i)}" y="220" text-anchor="middle">${escapeHtml(days[i].date.slice(5))}</text>`).join('')}</svg>
    <details class="chart-data"><summary>Xem số liệu theo ngày</summary><div class="table-scroll"><table><thead><tr><th>Ngày (UTC)</th><th>Spot</th><th>On-demand</th><th>Giờ compute</th></tr></thead><tbody>${days.map(d => `<tr><td>${escapeHtml(d.date)}</td><td>${money(d.spot_cost)}</td><td>${money(d.on_demand_cost)}</td><td>${number(d.runtime_hours)}</td></tr>`).join('')}</tbody></table></div></details>` : empty('Chưa có dữ liệu chi phí', 'Số liệu xuất hiện khi máy bắt đầu chạy.')}</section>`;
}
export function machineTable(items) {
  if (!items.length) return empty('Chưa có máy phù hợp', 'Tạo máy đầu tiên hoặc thay đổi bộ lọc.', '<a class="button primary" href="#/create">Tạo máy mới</a>');
  return `<div class="table-scroll"><table class="machine-table"><thead><tr><th>Máy / cấu hình</th><th>Trạng thái</th><th>Vị trí</th><th>Spot / giờ</th><th>On-demand / giờ</th><th>Spot tích lũy</th><th><span class="sr-only">Chi tiết</span></th></tr></thead><tbody>${items.map(m => `<tr><td><a class="machine-name" href="#/machines/${m.id}"><span class="machine-icon">${icon('server')}</span><span><b>${escapeHtml(m.name)}</b><small>${escapeHtml(m.compute?.machine_type || `${m.requested_vcpu} vCPU · ${number(m.requested_ram_gib)} GiB yêu cầu`)}</small></span></a></td><td>${badge(m)}</td><td>${m.compute ? `${provider(m.compute.provider)}<small class="cell-note">${escapeHtml(m.compute.zone)}</small>` : '—'}</td><td class="numeric">${money(m.compute?.spot_price_per_hour, true)}</td><td class="numeric muted">${money(m.compute?.on_demand_price_per_hour, true)}</td><td class="numeric">${money(m.cost.spot_cost)}</td><td><a class="icon-button" href="#/machines/${m.id}" aria-label="Chi tiết ${escapeHtml(m.name)}">${icon('arrow')}</a></td></tr>`).join('')}</tbody></table></div>`;
}
export const field = (label, name, value = '', attributes = '', hint = '') => `<label class="field"><span>${label}</span><input name="${name}" value="${escapeHtml(value)}" ${attributes}>${hint ? `<small>${hint}</small>` : ''}</label>`;
export function notify(message, error = false) {
  const item = document.createElement('div');
  item.className = `toast ${error ? 'error' : ''}`;
  item.textContent = message;
  document.querySelector('#notifications').append(item);
  setTimeout(() => item.remove(), 7000);
}
