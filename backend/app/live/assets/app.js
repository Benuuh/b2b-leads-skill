/* Live results page. All source text is inserted with textContent/DOM nodes,
   never as markup; links are limited to http(s) here and again in the core. */
(() => {
  'use strict';

  const POLL_MS = 2000;
  const PAGE_SIZE = 50;
  const STATUS_LABELS = {
    running: '运行中', paused: '已暂停', completed: '已完成', partial: '部分完成',
    failed: '失败/受阻', blocked: '失败/受阻', cancelled: '已取消', unknown: '未知',
  };
  const CHANNEL_LABELS = { email: '邮箱', phone: '电话', contact_form: '联系表单' };

  const state = {
    page: null,
    server: null,
    tab: 'deliverable',
    q: '',
    country: '',
    channel: '',
    sort: 'updated',
    limit: PAGE_SIZE,
    openId: null,
    deferred: false,
    newIds: [],
    offline: false,
    lastPollAt: null,
  };

  const byId = (id) => document.getElementById(id);
  const debounce = (fn, wait) => {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  };

  function build(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, String(value));
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function safeUrl(value) {
    if (typeof value !== 'string' || !value) return null;
    try {
      const parsed = new URL(value);
      return (parsed.protocol === 'http:' || parsed.protocol === 'https:') ? parsed.href : null;
    } catch (error) {
      return null;
    }
  }

  function externalLink(url, label) {
    const safe = safeUrl(url);
    if (!safe) return build('span', { class: 'muted', text: '未找到' });
    return build('a', {
      href: safe, target: '_blank', rel: 'noopener noreferrer', referrerpolicy: 'no-referrer',
      text: label || safe,
    });
  }

  function number(value) {
    return (value === null || value === undefined) ? '未知' : Number(value).toLocaleString('zh-CN');
  }

  function parseTime(value) {
    if (!value) return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function clock(value) {
    const parsed = parseTime(value);
    return parsed ? parsed.toLocaleTimeString('zh-CN', { hour12: false }) : '未知';
  }

  function dateTime(value) {
    const parsed = parseTime(value);
    return parsed ? parsed.toLocaleString('zh-CN', { hour12: false }) : '未知';
  }

  function relative(value) {
    const parsed = parseTime(value);
    if (!parsed) return '未知';
    const seconds = Math.max(0, Math.round((Date.now() - parsed.getTime()) / 1000));
    if (seconds < 5) return '刚刚';
    if (seconds < 60) return seconds + ' 秒前';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟前';
    return Math.floor(seconds / 3600) + ' 小时前';
  }

  function duration(seconds) {
    const total = Number(seconds || 0);
    if (!Number.isFinite(total)) return '未知';
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    return hours ? hours + ' 小时 ' + minutes + ' 分' : minutes + ' 分 ' + Math.round(total % 60) + ' 秒';
  }

  function toast(message) {
    const node = byId('toast');
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.hidden = true; }, 3600);
  }

  async function copy(value, label) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        const area = build('textarea', { class: 'visually-hidden' });
        area.value = value;
        document.body.append(area);
        area.select();
        const ok = document.execCommand('copy');
        area.remove();
        if (!ok) throw new Error('copy rejected');
      }
      toast('已复制' + (label ? '：' + label : ''));
    } catch (error) {
      toast('复制失败；请手动选择文本复制');
    }
  }

  function statusChip(label, kind) {
    return build('span', { class: 'chip chip-' + (kind || 'unknown'), text: label });
  }

  // -- data ------------------------------------------------------------
  function itemsForTab() {
    if (!state.page) return [];
    return state.tab === 'deliverable' ? (state.page.deliverables || []) : (state.page.pending || []);
  }

  function matches(item) {
    const query = state.q.trim().toLowerCase();
    if (query) {
      const haystack = ['company_name', 'domain', 'country_code', 'region', 'city', 'industry_text']
        .map((field) => (item[field] === null || item[field] === undefined) ? '' : String(item[field]))
        .join(' ').toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    if (state.country && String(item.country_code || '').toLowerCase() !== state.country.toLowerCase()) return false;
    if (state.channel && !(item.channels || []).map((value) => String(value).toLowerCase()).includes(state.channel)) return false;
    return true;
  }

  function filteredItems() {
    const result = itemsForTab().filter(matches);
    if (state.sort === 'name') {
      result.sort((a, b) => String(a.company_name || '').localeCompare(String(b.company_name || ''), 'zh-Hans-CN'));
    } else if (state.sort === 'score') {
      result.sort((a, b) => Number(b.lead_score || 0) - Number(a.lead_score || 0));
    } else {
      result.sort((a, b) => {
        const left = parseTime(a.observed_at);
        const right = parseTime(b.observed_at);
        if (!left && !right) return 0;
        if (!left) return 1;
        if (!right) return -1;
        return right - left;
      });
    }
    return result;
  }

  // -- header ----------------------------------------------------------
  function renderHeader() {
    const page = state.page;
    const server = state.server || {};
    if (!page) return;
    const run = page.run || {};
    byId('run-name').textContent = run.name || run.id || '未命名任务';
    const parts = [];
    if ((run.countries || []).length) parts.push(run.countries.join(' / '));
    if (run.industry) parts.push(run.industry);
    parts.push('配置 v' + (run.spec_version || '未知'));
    parts.push('批次 ' + (run.id || '未知'));
    byId('run-meta').textContent = parts.join(' · ');

    const view = server.status_view || { value: 'unknown', label: '未知' };
    const chip = byId('status-chip');
    chip.className = 'chip chip-' + (view.value || 'unknown');
    chip.textContent = view.label || STATUS_LABELS[view.value] || '未知';
    const note = [];
    if (view.note) note.push(view.note);
    if (run.stop_reason_label) note.push('结束原因：' + run.stop_reason_label);
    else if (run.stop_reason && !run.input_open) note.push('结束原因：原因未知');
    const errors = page.errors || [];
    if (errors.length) {
      const latest = errors[errors.length - 1];
      note.push('最近失败：' + (latest.reason || '原因未知') + (latest.retryable ? '（可重试）' : ''));
    }
    byId('status-note').textContent = note.join('；');
    byId('updated-at').textContent = '最后更新：' + dateTime(server.generated_at) + '（' + relative(server.generated_at) + '）';

    const progress = page.progress || {};
    byId('progress-text').textContent = '队列 ' + number(progress.queued) + ' 家 · 运行 ' + number(progress.running) +
      ' 家 · 已处理 ' + number(progress.processed) + ' 家 · 失败 ' + number(progress.failed) + ' 家';
    byId('active-time').textContent = '研究时间：' + duration(run.active_seconds);
    byId('version-text').textContent = '版本：核心 ' + ((run.release || {}).core_version || '未知') +
      ' · 页面 ' + (page.projection_version || '未知') + ' · 修订 ' + String(page.revision || '').slice(0, 8);

    const delivered = page.deliverables || [];
    const total = delivered.length;
    if (total) {
      const covered = (channel) => delivered.filter((item) => (item.channels || []).includes(channel)).length;
      const sourced = delivered.filter((item) => (item.sources || []).length).length;
      byId('coverage-text').textContent = '渠道覆盖（已确认可交付 ' + total + ' 家）：邮箱 ' +
        covered('email') + '/' + total + ' · 电话 ' + covered('phone') + '/' + total +
        ' · 表单 ' + covered('contact_form') + '/' + total + ' · 有来源 ' + sourced + '/' + total +
        '（更新 ' + clock(server.generated_at) + '）';
    } else {
      byId('coverage-text').textContent = '渠道覆盖：暂无可交付企业';
    }

    const pending = server.pending;
    byId('banner-pending').hidden = !pending;
    if (pending) {
      byId('pending-text').textContent = ' ' + (pending.message || '页面未反映最新变更') + '（' + clock(pending.at) + '）。请重新运行查看命令更新页面。';
    }
    const warnings = page.warnings || [];
    byId('banner-warning').hidden = warnings.length === 0;
    byId('banner-warning').textContent = warnings.join('；');
    byId('banner-demo').hidden = !run.demo_only;
  }

  function renderMetrics() {
    const counts = (state.page && state.page.counts) || {};
    byId('m-target').textContent = number(counts.target);
    byId('m-admitted').textContent = number(counts.admitted);
    byId('m-deliverable').textContent = number(counts.deliverable);
    byId('m-pending').textContent = number(counts.pending);
    byId('m-shortfall').textContent = number(counts.shortfall);
    const discovered = counts.discovered;
    byId('m-discovered').textContent = (discovered === null || discovered === undefined) ? '暂不可得' : number(discovered);
    byId('m-discovered-hint').textContent = counts.discovered_note || '本机核心不统计全网发现量';
    byId('m-deliverable-hint').textContent = '满足冻结客群与渠道政策；可导出 ' + number(counts.exportable) + ' 家';
    byId('count-deliverable').textContent = number(counts.deliverable);
    byId('count-pending').textContent = number(counts.pending);
  }

  function renderCountries() {
    const select = byId('country');
    const values = ((state.page && state.page.filters) || {}).countries || [];
    const signature = values.join('|');
    if (select.dataset.signature === signature) return;
    select.dataset.signature = signature;
    const current = state.country;
    select.replaceChildren(build('option', { value: '', text: '全部国家' }));
    for (const code of values) {
      select.append(build('option', { value: code, text: code }));
    }
    select.value = values.includes(current) ? current : '';
    state.country = select.value;
  }

  // -- list ------------------------------------------------------------
  function channelPill(item, contact) {
    const kind = contact.kind === 'phone' ? '电话' : '邮箱';
    const verification = contact.verification && contact.verification !== '已确认' ? '（' + contact.verification + '）' : '';
    const pill = build('span', { class: 'pill' + (contact.verification === '已确认' ? ' pill-ok' : ' pill-warn') }, [
      build('span', { text: kind + verification + '：' }),
      build('span', { class: 'pill-value', text: contact.value, title: contact.value }),
      build('button', {
        class: 'pill-copy', type: 'button', 'aria-label': '复制' + kind + ' ' + contact.value,
        onclick: (event) => { event.stopPropagation(); copy(contact.value, kind); },
      }, '复制'),
    ]);
    return pill;
  }

  function reasonPill(reason) {
    const kind = reason.status === '已确认' ? 'pass' : reason.status === '来源冲突' ? 'conflict'
      : reason.status === '不符合' ? 'fail' : 'unknown';
    return build('span', { class: 'pill pill-' + (kind === 'pass' ? 'ok' : kind === 'unknown' ? 'warn' : 'warn') }, [
      build('span', { class: 'chip chip-condition-' + kind, text: reason.status + '：' }),
      build('span', { class: 'pill-value', text: reason.label + (reason.detail ? ' · ' + reason.detail : ''), title: reason.detail || reason.label }),
    ]);
  }

  function card(item) {
    const meta = [item.country_code, item.city || item.region, item.industry_text].filter(Boolean).join(' · ');
    const contacts = (item.contacts || []).slice(0, 3);
    const pills = contacts.map((contact) => channelPill(item, contact));
    if (item.contact_form_url) {
      pills.push(build('span', { class: 'pill pill-ok' }, [
        build('span', { text: '联系表单：' }),
        externalLink(item.contact_form_url, '打开表单'),
      ]));
    }
    const reasons = (item.match_reasons || []).slice(0, 2).map(reasonPill);
    const pending = (item.pending_reasons || []).slice(0, 2).map((value) =>
      build('span', { class: 'pill pill-warn' }, [build('span', { class: 'pill-value', text: value, title: value })]));

    const side = [];
    if (Number.isFinite(Number(item.lead_score))) side.push(build('span', { text: '优先分 ' + Number(item.lead_score).toFixed(1) }));
    if ((item.channels || []).length) side.push(build('span', { text: (item.channels || []).map((name) => CHANNEL_LABELS[name] || name).join(' / ') }));
    side.push(build('span', { text: '来源 ' + (item.sources || []).length + ' 条' }));
    if (item.observed_at) side.push(build('span', { text: '采集 ' + clock(item.observed_at) }));

    return build('li', {
      class: 'card', 'data-id': item.company_id,
      onclick: () => openDetail(item.company_id),
    }, [
      build('div', { class: 'card-main' }, [
        build('div', { class: 'card-title' }, [
          build('h3', { text: item.company_name }),
          statusChip(item.status_label, item.status === 'deliverable' ? 'deliverable' : 'pending'),
        ]),
        build('p', { class: 'card-meta', text: meta || '所在地与行业待核实' }),
        item.description ? build('p', { class: 'card-desc', text: item.description }) : null,
        (reasons.length || pills.length || pending.length)
          ? build('div', { class: 'pills' }, [...pills, ...reasons, ...pending]) : null,
      ]),
      build('div', { class: 'card-side' }, [
        ...side,
        build('button', {
          class: 'link-button card-open', type: 'button',
          'aria-label': '查看 ' + item.company_name + ' 的详情',
          onclick: (event) => { event.stopPropagation(); openDetail(item.company_id); },
        }, '查看详情'),
      ]),
    ]);
  }

  function renderList(options = {}) {
    const preserve = options.preserveScroll !== false;
    const scrollY = window.scrollY;
    const list = byId('list');
    const all = itemsForTab();
    const filtered = filteredItems();
    const shown = filtered.slice(0, state.limit);
    list.replaceChildren(...shown.map(card));

    const summary = '当前显示 ' + shown.length + ' 家 / 此筛选共 ' + filtered.length + ' 家' +
      (filtered.length !== all.length ? '（' + (state.tab === 'deliverable' ? '可交付' : '待核实') + '共 ' + all.length + ' 家）' : '');
    byId('list-summary').textContent = summary;
    byId('more').hidden = shown.length >= filtered.length;

    const empty = byId('empty');
    if (filtered.length === 0) {
      empty.hidden = false;
      if (!all.length) {
        empty.textContent = state.tab === 'deliverable'
          ? '尚无已确认可交付客户。任务运行中，新结果会持续出现在这里；若任务已结束，请核对目标与证据政策。'
          : '当前没有待核实企业。';
      } else {
        empty.textContent = '没有符合当前筛选的客户。可清除筛选后重试。';
      }
    } else {
      empty.hidden = true;
    }

    if (preserve) window.scrollTo(0, scrollY);
    if (state.openId) {
      if (findItem(state.openId)) renderDrawer(state.openId, true);
      else closeDetail();
    }
    updateExportState();
  }

  function findItem(id) {
    if (!state.page) return null;
    return (state.page.deliverables || []).concat(state.page.pending || []).find((item) => item.company_id === id) || null;
  }

  // -- detail ----------------------------------------------------------
  function kv(term, value) {
    return [build('dt', { text: term }), build('dd', {}, value === null || value === undefined || value === '' ? '未找到' : value)];
  }

  function renderDrawer(id, preserve) {
    const item = findItem(id);
    if (!item) { closeDetail(); return; }
    const body = byId('drawer-body');
    const scrollTop = body.scrollTop;
    byId('drawer-title').textContent = item.company_name;
    const sections = [];

    sections.push(build('section', { class: 'drawer-section' }, [
      build('h3', { text: '基本情况' }),
      build('dl', { class: 'drawer-kv' }, [
        ...kv('状态', statusChip(item.status_label, item.status === 'deliverable' ? 'deliverable' : 'pending')),
        ...kv('官网', item.website_url ? externalLink(item.website_url, item.domain || item.website_url) : null),
        ...kv('所在地', [item.country_code, item.region, item.city].filter(Boolean).join(' · ') || null),
        ...kv('行业', item.industry_text),
        ...kv('产品/业务', (item.products || []).join('、') || item.description),
      ]),
    ]));

    const reasons = (item.match_reasons || []).length
      ? build('div', { class: 'reason-list' }, (item.match_reasons || []).map((reason) => build('div', { class: 'reason' }, [
        build('span', { class: 'chip chip-condition-' + (reason.status === '已确认' ? 'pass' : reason.status === '来源冲突' ? 'conflict' : reason.status === '不符合' ? 'fail' : 'unknown'), text: reason.status }),
        build('span', { text: reason.label + (reason.detail ? '：' + reason.detail : '') }),
      ])))
      : build('p', { class: 'muted small', text: '暂无逐项匹配理由；请以来源证据为准。' });
    sections.push(build('section', { class: 'drawer-section' }, [build('h3', { text: '匹配理由' }), reasons]));

    const contactRows = [];
    for (const contact of item.contacts || []) {
      contactRows.push(build('div', { class: 'reason' }, [
        build('span', { class: 'chip chip-' + (contact.verification === '已确认' ? 'deliverable' : 'pending'), text: contact.verification || '待核实' }),
        build('span', { text: (contact.kind === 'phone' ? '电话' : '邮箱') + '：' + contact.value }),
        build('button', {
          class: 'pill-copy', type: 'button', 'aria-label': '复制 ' + contact.value,
          onclick: () => copy(contact.value, contact.kind === 'phone' ? '电话' : '邮箱'),
        }, '复制'),
        contact.name || contact.title ? build('span', { class: 'muted small', text: '（' + [contact.name, contact.title].filter(Boolean).join('，') + '）' }) : null,
      ]));
    }
    if (item.contact_form_url) {
      contactRows.push(build('div', { class: 'reason' }, [
        build('span', { class: 'chip chip-deliverable', text: '已观察' }),
        build('span', { text: '联系表单：' }), externalLink(item.contact_form_url, item.contact_form_url),
      ]));
    }
    sections.push(build('section', { class: 'drawer-section' }, [
      build('h3', { text: '联系方式' }),
      contactRows.length ? build('div', { class: 'reason-list' }, contactRows) : build('p', { class: 'muted small', text: '未找到公开商务联系方式。' }),
    ]));

    if ((item.pending_reasons || []).length) {
      sections.push(build('section', { class: 'drawer-section' }, [
        build('h3', { text: '待核实原因' }),
        build('div', { class: 'reason-list' }, item.pending_reasons.map((value) =>
          build('div', { class: 'reason' }, [build('span', { class: 'chip chip-pending', text: '待核实' }), build('span', { text: value })]))),
      ]));
    }

    const sources = (item.sources || []).map((source) => build('article', { class: 'source' }, [
      build('div', { class: 'source-head' }, [
        build('strong', { text: source.label + (source.value ? '：' + source.value : '') }),
        externalLink(source.url, '来源链接'),
      ]),
      build('div', { class: 'source-head muted small' }, [
        build('span', { text: (source.source_type || '来源') + (source.retrieved_at ? ' · 采集 ' + dateTime(source.retrieved_at) : '') }),
        build('span', { text: source.field }),
      ]),
      source.excerpt ? build('blockquote', { text: source.excerpt }) : null,
    ]));
    sections.push(build('section', { class: 'drawer-section' }, [
      build('h3', { text: '来源证据（' + (item.sources || []).length + ' 条）' }),
      sources.length ? build('div', {}, sources) : build('p', { class: 'muted small', text: '未找到可追溯来源。' }),
      build('p', { class: 'notice', text: '公开、来源可追溯不代表已实际验证可送达；请仅用于合法 B2B 联系，并遵守当地法规与退订要求。' }),
    ]));

    body.replaceChildren(...sections);
    if (preserve) body.scrollTop = scrollTop;
    else body.scrollTop = 0;
  }

  function openDetail(id) {
    state.openId = id;
    const drawer = byId('drawer');
    drawer.hidden = false;
    renderDrawer(id, false);
    byId('drawer-close').focus({ preventScroll: true });
  }

  function closeDetail() {
    const previous = state.openId;
    state.openId = null;
    byId('drawer').hidden = true;
    if (previous) {
      const escaped = (window.CSS && CSS.escape) ? CSS.escape(previous) : String(previous).replace(/["\\]/g, '\\$&');
      const node = document.querySelector('.card[data-id="' + escaped + '"] .card-open');
      if (node) node.focus({ preventScroll: true });
    }
  }

  // -- interaction state ----------------------------------------------
  function isInteracting() {
    const active = document.activeElement;
    // Text entry must never be interrupted. A select or button keeps its own
    // focus when the list re-renders, so it is not a reason to defer updates.
    if (active && ['INPUT', 'TEXTAREA'].includes(active.tagName)) return true;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) {
      const anchor = selection.anchorNode;
      const host = anchor && (anchor.nodeType === 1 ? anchor : anchor.parentElement);
      if (host && (byId('results').contains(host) || byId('drawer').contains(host))) return true;
    }
    return false;
  }

  function showNewBanner(text, applicable) {
    const banner = byId('banner-new');
    byId('new-text').textContent = text;
    byId('apply-update').hidden = !applicable;
    banner.hidden = false;
  }

  function hideNewBanner() {
    byId('banner-new').hidden = true;
  }

  function applyDeferred() {
    if (!state.deferred) return;
    state.deferred = false;
    hideNewBanner();
    renderList({ preserveScroll: true });
    maybeNewResults();
  }

  function maybeNewResults() {
    if (!state.newIds.length) return;
    const visible = new Set(filteredItems().map((item) => item.company_id));
    const hidden = state.newIds.filter((id) => !visible.has(id));
    if (hidden.length) {
      showNewBanner('新增 ' + hidden.length + ' 家客户，不在当前筛选或当前页范围内。', false);
    } else {
      hideNewBanner();
    }
  }

  // -- export ----------------------------------------------------------
  function activeFilters() {
    const filters = {};
    if (state.q.trim()) filters.q = state.q.trim();
    if (state.country) filters.countries = [state.country];
    if (state.channel) filters.channels = [state.channel];
    return filters;
  }

  function updateExportState() {
    const button = byId('export');
    const hint = byId('export-hint');
    if (state.tab !== 'deliverable') {
      button.disabled = true;
      hint.textContent = '待核实客户不进入正式交付；切换到“已确认可交付”后导出';
      return;
    }
    const count = filteredItems().length;
    const target = Number(((state.page || {}).counts || {}).target || 0);
    const planned = target ? Math.min(count, target) : count;
    button.disabled = state.offline || planned === 0;
    hint.textContent = planned === 0
      ? '当前筛选下暂无可交付企业'
      : '将导出 ' + planned + ' 家（当前筛选；实际数量以导出结果为准）';
  }

  async function exportNow() {
    if (state.tab !== 'deliverable') { toast('待核实客户不进入正式交付'); return; }
    const button = byId('export');
    const original = button.textContent;
    button.disabled = true;
    button.textContent = '正在导出…';
    try {
      const response = await fetch('/api/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Live-Request': '1' },
        body: JSON.stringify({ format: 'xlsx', filters: activeFilters() }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        toast(data.message || '导出未完成，请稍后重试');
        return;
      }
      const result = byId('export-result');
      result.hidden = false;
      result.replaceChildren(
        build('div', {}, [build('strong', { text: '已导出 ' + number(data.count) + ' 家' })]),
        build('div', { text: '快照时间：' + dateTime(data.snapshot_at) }),
        build('div', { text: '文件：' + (data.file || '未知') }),
        build('button', {
          class: 'pill-copy', type: 'button', 'aria-label': '复制导出文件路径',
          onclick: () => copy(data.file || '', '文件路径'),
        }, '复制路径'),
      );
      toast('导出完成：' + number(data.count) + ' 家');
    } catch (error) {
      toast('导出失败：无法连接本机查看服务');
    } finally {
      button.textContent = original;
      updateExportState();
    }
  }

  // -- polling ---------------------------------------------------------
  function setOnline(online) {
    state.offline = !online;
    byId('banner-offline').hidden = online;
    if (!online) {
      const generated = state.server && state.server.generated_at;
      byId('offline-text').textContent = ' 无法连接本机查看服务；当前显示 ' +
        (generated ? dateTime(generated) + ' 的内容' : '最后一次成功读取的内容') + '，任务状态未知。';
      const chip = byId('status-chip');
      chip.className = 'chip chip-unknown';
      chip.textContent = '未知';
      byId('status-note').textContent = '最后更新于 ' + (generated ? dateTime(generated) : '未知') + '，当前状态未知';
    }
    updateExportState();
    if (online && state.page) tick();
  }

  async function poll() {
    try {
      const response = await fetch('/api/projection', { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (response.status === 304) { setOnline(true); return; }
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const data = await response.json();
      setOnline(true);
      apply(data);
    } catch (error) {
      setOnline(false);
    }
  }

  function apply(data) {
    const previous = state.page;
    const previousRecords = (((previous || {}).deliverables) || []).concat(((previous || {}).pending) || []);
    const previousAll = new Set(previousRecords.map((item) => item.company_id));
    const page = data.page || {};
    const changed = !previous || page.revision !== previous.revision ||
      JSON.stringify((data.server || {}).pending || null) !== JSON.stringify((state.server || {}).pending || null);
    state.page = page;
    state.server = data.server || {};
    state.newIds = previous
      ? ((page.deliverables || []).concat(page.pending || []))
        .filter((item) => !previousAll.has(item.company_id)).map((item) => item.company_id)
      : [];

    renderHeader();
    renderMetrics();
    renderCountries();

    if (!changed) { updateExportState(); return; }
    if (isInteracting()) {
      state.deferred = true;
      showNewBanner('有新的已提交结果；完成当前输入或选择后自动更新。', true);
      return;
    }
    hideNewBanner();
    renderList({ preserveScroll: true });
    maybeNewResults();
  }

  function tick() {
    const server = state.server || {};
    const generated = server.generated_at;
    if (generated) {
      byId('updated-at').textContent = '最后更新：' + dateTime(generated) + '（' + relative(generated) + '）';
    }
    const view = server.status_view || {};
    const staleAfter = Number(server.stale_after_seconds || 120);
    const parsed = parseTime(generated);
    const age = parsed ? (Date.now() - parsed.getTime()) / 1000 : null;
    const chip = byId('status-chip');
    if (state.offline) return;
    if (view.value === 'running' && age !== null && age > staleAfter) {
      chip.className = 'chip chip-unknown';
      chip.textContent = '未知';
      byId('status-note').textContent = '最后更新于 ' + dateTime(generated) + '，当前状态未知';
    } else if (view.value) {
      chip.className = 'chip chip-' + view.value;
      chip.textContent = view.label || STATUS_LABELS[view.value] || '未知';
    }
  }

  // -- events ----------------------------------------------------------
  function bind() {
    const debouncedList = debounce(() => renderList({ preserveScroll: false }), 160);
    byId('q').addEventListener('input', (event) => {
      state.q = event.target.value;
      state.limit = PAGE_SIZE;
      debouncedList();
    });
    byId('country').addEventListener('change', (event) => {
      state.country = event.target.value;
      state.limit = PAGE_SIZE;
      renderList({ preserveScroll: false });
    });
    byId('channel').addEventListener('change', (event) => {
      state.channel = event.target.value;
      state.limit = PAGE_SIZE;
      renderList({ preserveScroll: false });
    });
    byId('sort').addEventListener('change', (event) => {
      state.sort = event.target.value;
      state.limit = PAGE_SIZE;
      renderList({ preserveScroll: false });
    });
    byId('clear').addEventListener('click', () => {
      state.q = ''; state.country = ''; state.channel = ''; state.sort = 'updated'; state.limit = PAGE_SIZE;
      byId('q').value = ''; byId('country').value = ''; byId('channel').value = ''; byId('sort').value = 'updated';
      renderList({ preserveScroll: false });
    });
    byId('tab-deliverable').addEventListener('click', () => switchTab('deliverable'));
    byId('tab-pending').addEventListener('click', () => switchTab('pending'));
    byId('more').addEventListener('click', () => {
      state.limit += PAGE_SIZE;
      renderList({ preserveScroll: true });
    });
    byId('export').addEventListener('click', exportNow);
    byId('drawer-close').addEventListener('click', closeDetail);
    byId('apply-update').addEventListener('click', applyDeferred);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && state.openId) closeDetail();
    });
    document.addEventListener('selectionchange', () => {
      if (state.deferred && !isInteracting()) applyDeferred();
    });
    document.addEventListener('focusout', () => {
      setTimeout(() => { if (state.deferred && !isInteracting()) applyDeferred(); }, 120);
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    setInterval(() => { if (!document.hidden) poll(); }, POLL_MS);
    setInterval(tick, 1000);
  }

  function switchTab(tab) {
    if (state.tab === tab) return;
    state.tab = tab;
    state.limit = PAGE_SIZE;
    state.deferred = false;
    hideNewBanner();
    byId('tab-deliverable').classList.toggle('is-active', tab === 'deliverable');
    byId('tab-pending').classList.toggle('is-active', tab === 'pending');
    byId('tab-deliverable').setAttribute('aria-selected', tab === 'deliverable' ? 'true' : 'false');
    byId('tab-pending').setAttribute('aria-selected', tab === 'pending' ? 'true' : 'false');
    renderList({ preserveScroll: false });
  }

  bind();
  poll();
  tick();
})();
