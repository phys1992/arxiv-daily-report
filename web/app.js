/* ==========================================================================
 * arxiv-daily-report 配置界面 —— 纯原生 JavaScript（无框架 / 无构建 / 无 CDN）
 *
 * 模块结构：
 *   utils   通用工具（DOM、路径读写、模板替换）
 *   api     /api/* 请求封装（统一 try/catch 与错误对象）
 *   i18n    中英文案字典加载、查询与批量应用
 *   state   全部可变状态（config / providers / categories / subscriptions …）
 *   render  纯渲染函数（输入绑定、分类树、订阅卡片、状态页、日志）
 *   actions 保存、测试连接、启动任务、语言切换等有副作用的操作
 *   boot    启动流程与事件绑定
 * ========================================================================== */
(function () {
  'use strict';

  /* =====================================================================
   * utils
   * ===================================================================== */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  /** 创建元素：el('div', {class:'x', text:'hi', onclick:fn}, [子元素…]) */
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    var a = attrs || {};
    Object.keys(a).forEach(function (k) {
      var v = a[k];
      if (v === undefined || v === null) return;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k === 'dataset') Object.keys(v).forEach(function (d) { node.dataset[d] = v[d]; });
      else if (k === 'checked' || k === 'value' || k === 'disabled' || k === 'selected') node[k] = v;
      else if (k.indexOf('on') === 0) node.addEventListener(k.slice(2).toLowerCase(), v);
      else node.setAttribute(k, v);
    });
    (children || []).forEach(function (c) { if (c !== null && c !== undefined && c !== false) node.appendChild(c); });
    return node;
  }

  /** 读取形如 a.b.c 的配置路径 */
  function getPath(obj, path) {
    return String(path).split('.').reduce(function (o, k) {
      return (o === null || o === undefined) ? undefined : o[k];
    }, obj);
  }

  /** 写入形如 a.b.c 的配置路径（中间层缺失时自动创建） */
  function setPath(obj, path, val) {
    var parts = String(path).split('.');
    var cur = obj;
    for (var i = 0; i < parts.length - 1; i++) {
      if (typeof cur[parts[i]] !== 'object' || cur[parts[i]] === null) cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = val;
  }

  function deepClone(o) { return JSON.parse(JSON.stringify(o === undefined ? null : o)); }

  /** 拼接 home 与相对路径，用于展示 publish_dir 的解析结果 */
  function joinPath(home, value) {
    var v = String(value || '');
    if (!v) return '';
    if (v.charAt(0) === '/' || v.charAt(0) === '~') return v;
    return String(home || '').replace(/\/+$/, '') + '/' + v;
  }

  /* =====================================================================
   * state
   * ===================================================================== */
  var state = {
    config: null,          // 服务端配置对象（编辑中的唯一真相）
    meta: {},              // _meta：解析后的真实路径等
    providers: [],         // /api/providers
    providerMap: {},
    modelOptions: [],      // 连接测试返回的模型列表
    catGroups: [],         // /api/categories 的分组
    catNames: {},          // code -> 官方名称
    catTotal: 0,
    themes: [],            // /api/themes
    subs: new Map(),       // code -> {category, enabled, report_name, out_dir}
    subOrder: [],          // 订阅顺序（决定卡片顺序）
    masked: {},            // provider -> 掩码
    keySource: {},         // provider -> 密钥来源
    apiKeys: {},           // 用户新输入的明文密钥（仅内存，绝不回填到界面）
    status: null,
    job: null,
    view: 'llm',
    lang: 'zh-CN',
    dirty: false,
    saving: false,
    pollTimer: null,
    searching: false
  };

  /* =====================================================================
   * i18n
   * ===================================================================== */
  // 兜底字典：i18n.json 加载失败时，动态渲染仍能显示中文
  var FALLBACK_ZH = {
    'common.none': '（无）', 'common.unset': '（未设置）', 'action.remove': '移除',
    'llm.api_key_placeholder': '未设置', 'llm.api_key_keep': '留空 = 保持当前密钥不变',
    'llm.api_key_source_env': '当前密钥来自环境变量：{src}',
    'llm.api_key_source_file': '当前密钥来自 secrets.json，掩码：{mask}',
    'llm.api_key_source_none': '当前未配置密钥',
    'llm.base_url_default': '默认：{url}', 'llm.base_url_hint_empty': '留空则使用该服务商的默认地址',
    'llm.testing': '正在测试连接…', 'llm.models_none': '接口未返回模型列表',
    'llm.test_ok': '连接成功 · 延迟 {ms} ms · {msg}', 'llm.test_fail': '连接失败：{msg}',
    'subs.empty': '尚未订阅任何分类，请在上方的分类树中勾选。',
    'subs.count': '已选 {n} / {total}', 'subs.name': '报告名称（留空 = 分类官方全称）',
    'subs.name_ph': '例如：引力与量子宇宙学日报',
    'subs.out_dir': '输出目录（留空 = {default}）', 'subs.out_dir_ph': '例如：{default}',
    'subs.enabled': '启用',
    'status.target_day': '目标报告日', 'status.categories': '订阅分类', 'status.papers_today': '当日论文',
    'status.papers_total': '库内累计论文', 'status.math_cache': '公式缓存', 'status.next_run': '下次运行',
    'status.pdf_backends': 'PDF 后端', 'status.usage': '本月模型调用',
    'status.usage_tokens': '输入 {p} / 输出 {c} tokens',
    'status.home': '配置根目录', 'status.reports_dir': '报告目录', 'status.data_dir': '数据目录',
    'status.publish_dir': '发布目录', 'status.no_reports': '暂无报告记录。', 'status.open_pdf': '打开',
    'status.no_runs': '暂无运行记录。',
    'job.running': '运行中 · {kind}', 'job.idle': '空闲', 'job.done_ok': '已完成',
    'job.done_fail': '失败', 'job.empty': '（暂无日志）',
    'job.kind.daily': '日报', 'job.kind.monthly': '月报',
    'report.themes_empty': '未获取到主题表。',
    'toast.saved': '已保存', 'toast.save_failed': '保存失败', 'toast.run_started': '任务已启动',
    'toast.run_failed': '启动任务失败', 'toast.job_busy': '已有任务在运行',
    'toast.models_ok': '模型列表已更新', 'toast.models_fail': '获取模型列表失败',
    'toast.conn_ok': '连接正常', 'toast.conn_fail': '连接失败',
    'err.load': '加载配置失败', 'err.network': '网络请求失败',
    'err.unauthorized': '未授权：请使用带 ?token=... 的地址重新打开页面'
  };

  var i18n = {
    dict: { 'zh-CN': {}, 'en': {} },

    /** 加载 i18n.json；失败不抛异常，仅使用内置兜底 */
    load: function () {
      return fetch('/i18n.json', { credentials: 'same-origin' })
        .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
        .then(function (data) {
          if (data && typeof data === 'object') i18n.dict = data;
          return true;
        })
        .catch(function (err) {
          showError('i18n.json 加载失败 / failed to load i18n.json: ' + err.message);
          return false;
        });
    },

    /** 查字典：当前语言 -> 中文 -> 内置兜底；都没有则返回 undefined */
    lookup: function (key) {
      var cur = i18n.dict[state.lang] || {};
      if (cur[key] !== undefined) return cur[key];
      var zh = i18n.dict['zh-CN'] || {};
      if (zh[key] !== undefined) return zh[key];
      return FALLBACK_ZH[key];
    }
  };

  /** 取文案并做 {name} 变量替换 */
  function t(key, vars) {
    var s = i18n.lookup(key);
    if (s === undefined) return key;
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        s = s.split('{' + k + '}').join(String(vars[k]));
      });
    }
    return s;
  }

  /** 把 data-i18n / data-i18n-placeholder / data-i18n-title 批量应用到 DOM */
  function applyI18n(root) {
    var scope = root || document;
    $$('[data-i18n]', scope).forEach(function (n) {
      var v = i18n.lookup(n.dataset.i18n);
      if (v !== undefined) n.textContent = v;
    });
    $$('[data-i18n-placeholder]', scope).forEach(function (n) {
      var v = i18n.lookup(n.dataset.i18nPlaceholder);
      if (v !== undefined) n.setAttribute('placeholder', v);
    });
    $$('[data-i18n-title]', scope).forEach(function (n) {
      var v = i18n.lookup(n.dataset.i18nTitle);
      if (v !== undefined) n.setAttribute('title', v);
    });
  }

  /* =====================================================================
   * api —— 所有请求统一封装，错误带上 HTTP 状态码
   * ===================================================================== */
  var api = {
    request: function (method, url, body) {
      var init = { method: method, credentials: 'same-origin' };
      if (body !== undefined) {
        init.headers = { 'Content-Type': 'application/json' };
        init.body = JSON.stringify(body);
      }
      return fetch(url, init).then(function (res) {
        return res.text().then(function (text) {
          var data = null;
          if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
          if (!res.ok) {
            var msg = (data && (data.error || data.message)) || ('HTTP ' + res.status);
            var err = new Error(msg);
            err.status = res.status;
            err.data = data;
            throw err;
          }
          return data;
        });
      }, function (netErr) {
        var err = new Error(t('err.network') + ': ' + netErr.message);
        err.status = 0;
        throw err;
      });
    },
    get: function (url) { return api.request('GET', url); },
    post: function (url, body) { return api.request('POST', url, body); }
  };

  /* =====================================================================
   * 界面反馈：错误条 / toast / 脏标记
   * ===================================================================== */
  function showError(msg) {
    var bar = $('#error-bar');
    if (!bar) return;
    bar.textContent = String(msg);
    bar.classList.remove('hidden');
  }
  function clearError() {
    var bar = $('#error-bar');
    if (bar) { bar.textContent = ''; bar.classList.add('hidden'); }
  }
  function toast(msg, kind) {
    var wrap = $('#toast-wrap');
    if (!wrap) return;
    var node = el('div', { class: 'toast' + (kind ? ' ' + kind : ''), text: msg });
    wrap.appendChild(node);
    setTimeout(function () {
      node.style.opacity = '0';
      node.style.transition = 'opacity .25s';
      setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 260);
    }, 2800);
  }
  function markDirty() {
    state.dirty = true;
    var flag = $('#dirty-flag');
    if (flag) flag.classList.remove('hidden');
  }
  function markClean() {
    state.dirty = false;
    var flag = $('#dirty-flag');
    if (flag) flag.classList.add('hidden');
  }

  /* =====================================================================
   * render —— 输入绑定（data-path <-> state.config）
   * ===================================================================== */
  /** 把 state.config 的值写回所有 [data-path] 控件 */
  function applyValues() {
    $$('[data-path]').forEach(function (node) {
      var v = getPath(state.config, node.dataset.path);
      if (v === undefined || v === null) return;
      if (node.type === 'checkbox') node.checked = !!v;
      else node.value = String(v);
    });
  }

  /** 读取单个控件的值（按类型转换）并写回 state.config */
  function writeControl(node) {
    var path = node.dataset.path;
    var v;
    if (node.type === 'checkbox') v = node.checked;
    else if (node.type === 'number') v = node.value === '' ? 0 : Number(node.value);
    else v = node.value;
    setPath(state.config, path, v);
  }

  function bindInputs() {
    $$('[data-path]').forEach(function (node) {
      if (node.dataset.bound) return;   // 幂等：重复调用不会重复绑定
      node.dataset.bound = '1';
      var handler = function () { writeControl(node); markDirty(); };
      node.addEventListener('change', handler);
      node.addEventListener('input', handler);
    });
  }

  /* =====================================================================
   * render —— 大模型设置
   * ===================================================================== */
  function renderProviderOptions() {
    var sel = $('#llm-provider');
    var fb = $('#fallback-provider');
    sel.textContent = '';
    fb.textContent = '';
    fb.appendChild(el('option', { value: '', text: t('common.none') }));
    state.providers.forEach(function (p) {
      sel.appendChild(el('option', { value: p.key, text: p.label + ' (' + p.key + ')' }));
      fb.appendChild(el('option', { value: p.key, text: p.label }));
    });
    // 配置里的服务商若不在预设表中，补一个选项，避免被静默改掉
    var want = state.config && state.config.llm ? state.config.llm.provider : '';
    if (want && !state.providerMap[want]) {
      sel.appendChild(el('option', { value: want, text: want }));
    }
  }

  function currentProvider() {
    return state.providerMap[$('#llm-provider').value] || null;
  }

  /** 服务商相关的提示、占位符与密钥框状态 */
  function syncProviderUI() {
    var p = currentProvider();
    $('#provider-note').textContent = (p && p.note) ? p.note : '';
    var base = $('#llm-base-url');
    base.placeholder = (p && p.base_url) ? p.base_url : '';
    $('#base-url-hint').textContent = (p && p.base_url)
      ? t('llm.base_url_default', { url: p.base_url })
      : t('llm.base_url_hint_empty');
    var modelInput = $('#llm-model');
    modelInput.placeholder = (p && p.default_model) ? p.default_model : '';
    syncApiKeyField();
  }

  /** API Key 输入框：只显示掩码占位符，绝不回填明文 */
  function syncApiKeyField() {
    var provider = $('#llm-provider').value;
    var input = $('#api-key');
    var mask = state.masked[provider] || '';
    var src = state.keySource[provider] || '';
    input.placeholder = mask || t('llm.api_key_placeholder');
    input.value = state.apiKeys[provider] || '';
    var hint;
    if (src.indexOf('env:') === 0) hint = t('llm.api_key_source_env', { src: src.slice(4) });
    else if (src) hint = t('llm.api_key_source_file', { mask: mask });
    else hint = t('llm.api_key_source_none');
    $('#api-key-hint').textContent = hint + ' · ' + t('llm.api_key_keep');
  }

  function renderModelOptions() {
    var dl = $('#model-options');
    dl.textContent = '';
    var seen = {};
    state.modelOptions.forEach(function (m) {
      if (!m || seen[m]) return;
      seen[m] = 1;
      dl.appendChild(el('option', { value: m }));
    });
  }

  /* =====================================================================
   * render —— 分类订阅
   * ===================================================================== */
  function renderCategoryTree() {
    var tree = $('#cat-tree');
    tree.textContent = '';
    state.catGroups.forEach(function (g) {
      var details = el('details', { class: 'cat-group', dataset: { archive: g.archive } });
      var badge = el('span', { class: 'pill group-count' });
      details.appendChild(el('summary', {}, [
        el('span', { text: g.archive }),
        badge
      ]));
      var box = el('div', { class: 'cat-items' });
      (g.items || []).forEach(function (it) {
        var cb = el('input', { type: 'checkbox', checked: state.subs.has(it.code) });
        cb.dataset.code = it.code;
        var label = el('label', { class: 'cat-item', dataset: { code: it.code, name: String(it.name || '').toLowerCase() } }, [
          cb,
          el('code', { text: it.code }),
          el('span', { class: 'cat-name', text: it.name || '' })
        ]);
        box.appendChild(label);
      });
      details.appendChild(box);
      tree.appendChild(details);
    });
    // 默认展开「已订阅」所在分组，若没有则展开第一组
    var opened = false;
    $$('#cat-tree details').forEach(function (d) {
      if (d.querySelector('input[type="checkbox"]:checked')) { d.open = true; opened = true; }
    });
    var first = $('#cat-tree details');
    if (!opened && first) first.open = true;
    updateGroupCounts();
  }

  /** 分组标题上的数量徽标：已选/总数，搜索时显示 命中/总数 */
  function updateGroupCounts() {
    $$('#cat-tree details').forEach(function (d) {
      var items = $$('.cat-item', d);
      var sel = 0, visible = 0;
      items.forEach(function (it) {
        if (!$('input', it).checked) return;
        sel++;
      });
      items.forEach(function (it) { if (!it.classList.contains('hidden')) visible++; });
      var badge = $('.group-count', d);
      if (!badge) return;
      badge.textContent = state.searching ? (visible + '/' + items.length)
        : (sel > 0 ? (sel + '/' + items.length) : String(items.length));
      badge.classList.toggle('pill-ok', sel > 0 && !state.searching);
    });
  }

  function updateSelectedCount() {
    var node = $('#cat-count');
    if (node) node.textContent = t('subs.count', { n: state.subOrder.length, total: state.catTotal });
  }

  function applyCategoryFilter(raw) {
    var q = String(raw || '').trim().toLowerCase();
    var searching = !!q;
    var groups = $$('#cat-tree details');
    if (searching && !state.searching) {
      groups.forEach(function (d) { d.dataset.prevOpen = d.open ? '1' : '0'; });
    }
    groups.forEach(function (d) {
      var visible = 0;
      $$('.cat-item', d).forEach(function (it) {
        var hit = !searching
          || String(it.dataset.code || '').toLowerCase().indexOf(q) >= 0
          || String(it.dataset.name || '').indexOf(q) >= 0;
        it.classList.toggle('hidden', !hit);
        if (hit) visible++;
      });
      d.classList.toggle('hidden', searching && visible === 0);
      if (searching) d.open = visible > 0;
      else if (d.dataset.prevOpen !== undefined) {
        d.open = d.dataset.prevOpen === '1';
        delete d.dataset.prevOpen;
      }
    });
    state.searching = searching;
    updateGroupCounts();
  }

  /** 勾选 / 取消勾选：驱动 state.subs 与订阅卡片 */
  function setSubscribed(code, on) {
    if (on) {
      if (!state.subs.has(code)) {
        state.subs.set(code, { category: code, enabled: true, report_name: '', out_dir: '' });
        state.subOrder.push(code);
      }
    } else {
      state.subs.delete(code);
      state.subOrder = state.subOrder.filter(function (c) { return c !== code; });
    }
    var cb = $('#cat-tree input[data-code="' + code + '"]');
    if (cb) cb.checked = on;
    renderSubCards();
    updateGroupCounts();
    updateSelectedCount();
    markDirty();
  }

  function renderSubCards() {
    var box = $('#sub-cards');
    box.textContent = '';
    if (!state.subOrder.length) {
      box.appendChild(el('div', { class: 'empty', text: t('subs.empty') }));
      return;
    }
    state.subOrder.forEach(function (code) { box.appendChild(buildSubCard(code)); });
  }

  function buildSubCard(code) {
    var sub = state.subs.get(code) || { category: code, enabled: true, report_name: '', out_dir: '' };
    var name = state.catNames[code] || '';
    var defOut = 'reports/' + code;

    var head = el('div', { class: 'sub-card-head' }, [
      el('code', { text: code }),
      el('span', { class: 'sub-name', text: name }),
      el('button', {
        class: 'btn btn-danger', text: t('action.remove'),
        onclick: function () { setSubscribed(code, false); }
      })
    ]);

    var enabled = el('input', { type: 'checkbox', checked: sub.enabled !== false });
    enabled.addEventListener('change', function () { sub.enabled = enabled.checked; markDirty(); });
    var enabledRow = el('label', { class: 'field field-check' }, [
      enabled, el('span', { class: 'label', text: t('subs.enabled') })
    ]);

    var nameInput = el('input', { type: 'text', value: sub.report_name || '', placeholder: t('subs.name_ph') });
    nameInput.addEventListener('input', function () { sub.report_name = nameInput.value; markDirty(); });

    var outInput = el('input', { type: 'text', value: sub.out_dir || '', placeholder: t('subs.out_dir_ph', { 'default': defOut }) });
    outInput.addEventListener('input', function () { sub.out_dir = outInput.value; markDirty(); });

    return el('div', { class: 'sub-card', dataset: { code: code } }, [
      head,
      enabledRow,
      el('label', { class: 'field' }, [
        el('span', { class: 'label', text: t('subs.name') }), nameInput
      ]),
      el('label', { class: 'field' }, [
        el('span', { class: 'label', text: t('subs.out_dir', { 'default': defOut }) }), outInput
      ])
    ]);
  }

  /* =====================================================================
   * render —— 报告主题 / 路径 / 状态页
   * ===================================================================== */
  function renderThemes() {
    var box = $('#theme-list');
    box.textContent = '';
    if (!state.config.report) { box.appendChild(el('div', { class: 'muted', text: t('report.themes_empty') })); return; }
    var selected = state.config.report.themes || [];
    if (!state.themes.length) {
      box.appendChild(el('div', { class: 'muted', text: t('report.themes_empty') }));
      return;
    }
    state.themes.forEach(function (th) {
      var cb = el('input', { type: 'checkbox', checked: selected.indexOf(th.code) >= 0 });
      cb.addEventListener('change', function () {
        var cur = (state.config.report.themes || []).slice();
        var next;
        if (cb.checked) next = cur.concat([th.code]);
        else next = cur.filter(function (c) { return c !== th.code; });
        state.config.report.themes = next;
        markDirty();
      });
      var label = state.lang === 'en' ? (th.en || th.zh || th.code) : (th.zh || th.en || th.code);
      box.appendChild(el('label', {}, [cb, el('span', { text: label })]));
    });
  }

  function renderPaths() {
    var m = state.meta || {};
    var cfg = state.config || {};
    var publishValue = (cfg.paths && cfg.paths.publish_dir) || '';
    var publish = publishValue ? joinPath(m.home, publishValue) : '';
    var list = $('#paths-resolved');
    list.textContent = '';
    var rows = [
      ['status.home', m.home],
      ['status.reports_dir', m.reports_dir],
      ['status.data_dir', m.data_dir],
      ['status.publish_dir', publish || t('common.none')]
    ];
    rows.forEach(function (row) {
      list.appendChild(el('li', {}, [
        el('span', { class: 'k', text: t(row[0]) }),
        el('span', { class: 'v', text: row[1] || t('common.unset') })
      ]));
    });
  }

  function renderStatus() {
    var s = state.status;
    if (!s) return;

    var grid = $('#stat-grid');
    grid.textContent = '';
    function stat(labelKey, value, sub, small) {
      return el('div', { class: 'stat' }, [
        el('div', { class: 'stat-label', text: t(labelKey) }),
        el('div', { class: 'stat-value' + (small ? ' small' : ''), text: value }),
        sub ? el('div', { class: 'stat-sub', text: sub }) : null
      ]);
    }
    var cats = s.categories || [];
    grid.appendChild(stat('status.target_day', s.target_day || '—'));
    grid.appendChild(stat('status.categories', String(s.category_count || 0), cats.join(', ') || t('common.none'), true));
    grid.appendChild(stat('status.papers_today', String(s.papers_today === undefined ? '—' : s.papers_today),
      t('status.papers_total') + ': ' + (s.papers_total === undefined ? '—' : s.papers_total)));
    grid.appendChild(stat('status.math_cache', String(s.math_cache === undefined ? '—' : s.math_cache)));
    grid.appendChild(stat('status.next_run', s.next_run || '—', '', true));
    var usage = s.usage || {};
    grid.appendChild(stat('status.usage',
      String(usage.calls || 0) + ' · ¥' + (usage.cost_cny === undefined ? 0 : usage.cost_cny),
      t('status.usage_tokens', { p: usage.prompt_tokens || 0, c: usage.completion_tokens || 0 }), true));
    grid.appendChild(stat('status.pdf_backends', (s.pdf_backends || []).join(', ') || t('common.none'), '', true));

    // 最近报告表格
    var tbody = $('#reports-table tbody');
    tbody.textContent = '';
    var reports = s.reports || [];
    if (!reports.length) {
      tbody.appendChild(el('tr', {}, [el('td', { colspan: '6', class: 'muted', text: t('status.no_reports') })]));
    }
    reports.forEach(function (r) {
      tbody.appendChild(el('tr', {}, [
        el('td', { class: 'mono', text: r.day || '' }),
        el('td', { class: 'mono', text: r.category || '' }),
        el('td', { class: 'col-num', text: String(r.count === undefined ? 0 : r.count) }),
        el('td', { class: 'col-num', text: 'v' + (r.revision === undefined ? 1 : r.revision) }),
        el('td', { class: 'mono', text: r.generated_at || '' }),
        el('td', {}, [el('a', {
          href: reportUrl(r.path), target: '_blank', rel: 'noopener',
          text: t('status.open_pdf')
        })])
      ]));
    });

    // 最近运行
    var runsBox = $('#runs-list');
    runsBox.textContent = '';
    var runs = s.runs || [];
    if (!runs.length) runsBox.appendChild(el('li', { class: 'muted', text: t('status.no_runs') }));
    runs.forEach(function (r) {
      var cls = 'pill';
      if (r.status === 'ok' || r.status === 'success') cls += ' pill-ok';
      else if (r.status === 'error' || r.status === 'failed') cls += ' pill-err';
      else if (r.status === 'running') cls += ' pill-warn';
      runsBox.appendChild(el('li', {}, [
        el('span', { class: 'v plain', text: r.started_at || '' }),
        el('span', { class: cls, text: r.status || '' }),
        el('span', { class: 'k', text: r.detail || '' })
      ]));
    });
  }

  /**
   * 由后端绝对路径推导 /reports/ 相对 URL：
   * 优先减去 reports_dir 前缀，否则退化为「最后一段目录 + 文件名」。
   */
  function reportUrl(p) {
    if (!p) return '#';
    var rel = String(p).replace(/\\/g, '/');
    var root = String((state.meta && state.meta.reports_dir) || '').replace(/\\/g, '/').replace(/\/+$/, '');
    if (root && rel.indexOf(root + '/') === 0) {
      rel = rel.slice(root.length + 1);
    } else {
      var parts = rel.split('/').filter(Boolean);
      rel = parts.slice(-2).join('/');
    }
    return '/reports/' + rel.split('/').map(encodeURIComponent).join('/');
  }

  function renderJob(job) {
    state.job = job;
    var lines = (job && job.lines) || [];
    var log = $('#job-log');
    log.textContent = lines.join('\n');
    log.dataset.empty = t('job.empty');
    log.scrollTop = log.scrollHeight;

    var pill = $('#job-state');
    pill.className = 'pill';
    var text;
    if (job && job.running) {
      pill.classList.add('pill-warn');
      text = t('job.running', { kind: t('job.kind.' + (job.kind || 'daily')) });
    } else if (job && job.ok === true) {
      pill.classList.add('pill-ok');
      text = t('job.done_ok');
    } else if (job && job.ok === false) {
      pill.classList.add('pill-err');
      text = t('job.done_fail') + (job.error ? ': ' + job.error : '');
    } else {
      text = t('job.idle');
    }
    pill.textContent = text;
  }

  /* =====================================================================
   * render —— 顶部信息与视图切换
   * ===================================================================== */
  function renderHeader() {
    var m = state.meta || {};
    var cfgDir = m.config_dir || m.home || '—';
    $('#config-root').textContent = cfgDir;
    $('#config-root').title = cfgDir;
    // 副行：模式 + 数据目录（安装形态下与配置目录分离）
    var bits = [];
    if (m.mode) bits.push(m.mode === 'portable' ? t('meta.portable') : t('meta.system'));
    if (m.data_dir && m.data_dir !== cfgDir) bits.push(t('meta.data') + ' ' + m.data_dir);
    $('#project-root').textContent = bits.length ? ('· ' + bits.join(' · ')) : '';
  }

  function switchView(view, skipHash) {
    state.view = view;
    $$('.nav-item').forEach(function (b) { b.classList.toggle('active', b.dataset.view === view); });
    $$('.view').forEach(function (s) { s.classList.toggle('active', s.id === 'view-' + view); });
    // 写入 URL hash，便于直达某页与收藏（不触发滚动）
    if (!skipHash && window.location.hash !== '#' + view) {
      try { window.history.replaceState(null, '', '#' + view); }
      catch (e) { window.location.hash = view; }
    }
    if (view === 'status') refreshStatus();
    if (view === 'paths') renderPaths();
  }

  function viewFromHash() {
    var v = (window.location.hash || '').replace(/^#/, '');
    var valid = $$('.nav-item').map(function (b) { return b.dataset.view; });
    return valid.indexOf(v) >= 0 ? v : 'llm';
  }

  function renderAll() {
    renderHeader();
    renderProviderOptions();
    renderModelOptions();
    applyValues();
    syncProviderUI();
    renderCategoryTree();
    applyCategoryFilter($('#cat-search').value);
    renderSubCards();
    updateSelectedCount();
    renderThemes();
    renderPaths();
    $('#run-day').value = (state.status && state.status.target_day) || '';
  }

  /** 语言切换后重新渲染所有含动态文案的区块 */
  function rerenderDynamic() {
    syncProviderUI();
    renderSubCards();
    updateSelectedCount();
    renderThemes();
    renderPaths();
    renderStatus();
    renderJob(state.job);
    updateGroupCounts();
  }

  /* =====================================================================
   * actions —— 加载 / 保存
   * ===================================================================== */
  function loadAll() {
    clearError();
    return Promise.all([
      api.get('/api/config'),
      api.get('/api/providers'),
      api.get('/api/categories'),
      api.get('/api/themes')
    ]).then(function (res) {
      var cfg = res[0] || {};
      state.config = cfg;
      state.meta = cfg._meta || {};
      state.providers = (res[1] && res[1].providers) || [];
      state.providerMap = {};
      state.providers.forEach(function (p) { state.providerMap[p.key] = p; });

      var cat = res[2] || {};
      state.catGroups = cat.groups || [];
      state.catTotal = cat.total || 0;
      state.catNames = {};
      state.catGroups.forEach(function (g) {
        (g.items || []).forEach(function (it) { state.catNames[it.code] = it.name; });
      });
      state.themes = (res[3] && res[3].themes) || [];

      var llm = cfg.llm || {};
      state.masked = llm.api_key_masked || {};
      state.keySource = llm.api_key_source || {};

      // 订阅表 -> Map（保留自定义字段与顺序）
      state.subs = new Map();
      state.subOrder = [];
      (cfg.subscriptions || []).forEach(function (s) {
        if (!s || !s.category || state.subs.has(s.category)) return;
        state.subs.set(s.category, {
          category: s.category,
          enabled: s.enabled !== false,
          report_name: s.report_name || '',
          out_dir: s.out_dir || ''
        });
        state.subOrder.push(s.category);
      });

      state.lang = cfg.ui_language === 'en' ? 'en' : 'zh-CN';
      document.documentElement.lang = state.lang;
      applyI18n();
      renderAll();
      markClean();
      $('#loading').classList.add('hidden');
      return true;
    });
  }

  /** 组装提交体：剔除只读字段，api_keys 只在用户真的输入时提交 */
  function buildPayload() {
    var cfg = deepClone(state.config);
    delete cfg._meta;
    if (cfg.llm) {
      delete cfg.llm.api_key_masked;
      delete cfg.llm.api_key_source;
    }
    cfg.subscriptions = state.subOrder.map(function (code) {
      var s = state.subs.get(code) || {};
      return {
        category: code,
        enabled: s.enabled !== false,
        report_name: s.report_name || '',
        out_dir: s.out_dir || ''
      };
    });
    var payload = { config: cfg };
    var keys = {};
    Object.keys(state.apiKeys).forEach(function (p) {
      var v = state.apiKeys[p];
      if (!v || v.indexOf('••') === 0 || v === (state.masked[p] || '')) return;
      keys[p] = v;
    });
    if (Object.keys(keys).length) payload.api_keys = keys;
    return payload;
  }

  function save() {
    if (!state.config || state.saving) return;
    state.saving = true;
    var fab = $('#save-fab');
    fab.disabled = true;
    fab.textContent = t('common.saving');
    api.post('/api/config', buildPayload()).then(function (res) {
      if (res && res.config) {
        state.config = res.config;
        state.meta = res.config._meta || state.meta;
        var llm = res.config.llm || {};
        state.masked = llm.api_key_masked || {};
        state.keySource = llm.api_key_source || {};
      }
      state.apiKeys = {};            // 已保存的明文不再保留在内存
      applyValues();
      renderHeader();
      syncProviderUI();
      renderPaths();
      markClean();
      clearError();
      toast(t('toast.saved'), 'ok');
    }).catch(function (err) {
      var msg = t('toast.save_failed') + ': ' + err.message;
      toast(msg, 'err');
      showError(t('err.save') + ': ' + err.message + (err.status ? ' (HTTP ' + err.status + ')' : ''));
    }).then(function () {
      state.saving = false;
      fab.disabled = false;
      fab.textContent = t('action.save');
    });
  }

  /* =====================================================================
   * actions —— 连接测试 / 模型列表
   * ===================================================================== */
  function testPayload() {
    var provider = $('#llm-provider').value;
    var body = {
      provider: provider,
      base_url: $('#llm-base-url').value,
      model: $('#llm-model').value
    };
    var typed = state.apiKeys[provider];
    if (typed && typed.indexOf('••') !== 0) body.api_key = typed;
    return body;
  }

  function showTestBox(text, kind) {
    var box = $('#test-result');
    box.className = 'result-box' + (kind ? ' ' + kind : '');
    box.classList.remove('hidden');
    box.textContent = text;
    return box;
  }

  function testConnection() {
    var btn = $('#btn-test');
    btn.disabled = true;
    showTestBox(t('llm.testing'));
    api.post('/api/test-connection', testPayload()).then(function (res) {
      res = res || {};
      if (res.ok) {
        showTestBox(t('llm.test_ok', { ms: res.latency_ms, msg: res.message || '' }), 'ok');
        toast(t('toast.conn_ok'), 'ok');
        if (res.models && res.models.length) {
          state.modelOptions = res.models;
          renderModelOptions();
          toast(t('toast.models_ok'), 'ok');
        } else {
          toast(t('llm.models_none'), 'warn');
        }
      } else {
        showTestBox(t('llm.test_fail', { msg: res.message || '' }), 'err');
        toast(t('toast.conn_fail'), 'err');
      }
    }).catch(function (err) {
      showTestBox(t('llm.test_fail', { msg: err.message }), 'err');
      toast(t('toast.conn_fail'), 'err');
      showError(t('err.load') + ': ' + err.message);
    }).then(function () {
      btn.disabled = false;
    });
  }

  function fetchModels() {
    var btn = $('#btn-models');
    btn.disabled = true;
    api.post('/api/test-connection', testPayload()).then(function (res) {
      res = res || {};
      if (res.ok && res.models && res.models.length) {
        state.modelOptions = res.models;
        renderModelOptions();
        toast(t('toast.models_ok') + ' · ' + t('llm.models_loaded', { n: res.models.length }), 'ok');
        showTestBox(t('llm.test_ok', { ms: res.latency_ms, msg: res.message || '' }), 'ok');
      } else if (res.ok) {
        toast(t('llm.models_none'), 'warn');
      } else {
        toast(t('toast.models_fail') + ': ' + (res.message || ''), 'err');
        showTestBox(t('llm.test_fail', { msg: res.message || '' }), 'err');
      }
    }).catch(function (err) {
      toast(t('toast.models_fail'), 'err');
      showError(t('err.load') + ': ' + err.message);
    }).then(function () {
      btn.disabled = false;
    });
  }

  /* =====================================================================
   * actions —— 任务运行与日志轮询
   * ===================================================================== */
  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function pollJob() {
    api.get('/api/job').then(function (job) {
      renderJob(job);
      if (!job || !job.running) stopPolling();
    }).catch(function (err) {
      stopPolling();
      showError(t('err.load') + ': ' + err.message);
    });
  }

  function startPolling() {
    stopPolling();
    pollJob();
    state.pollTimer = setInterval(pollJob, 1500);
  }

  function runNow() {
    var day = $('#run-day').value || null;
    var useLlm = $('#run-use-llm').checked;
    var btn = $('#btn-run');
    btn.disabled = true;
    api.post('/api/run', { kind: 'daily', day: day, use_llm: useLlm }).then(function () {
      toast(t('toast.run_started'), 'ok');
      startPolling();
    }).catch(function (err) {
      if (err.status === 409) {
        toast(t('toast.job_busy'), 'warn');
        startPolling();          // 已在运行，直接接管日志
      } else {
        toast(t('toast.run_failed') + ': ' + err.message, 'err');
        showError(t('toast.run_failed') + ': ' + err.message);
      }
    }).then(function () {
      btn.disabled = false;
    });
  }

  function refreshStatus() {
    return api.get('/api/status').then(function (s) {
      state.status = s;
      renderStatus();
      if (!$('#run-day').value && s && s.target_day) $('#run-day').value = s.target_day;
    }).catch(function (err) {
      showError(t('err.load') + ': ' + err.message);
    });
  }

  /* =====================================================================
   * actions —— 语言切换
   * ===================================================================== */
  function onLanguageChange() {
    var lang = $('#ui-language').value;
    state.lang = lang === 'en' ? 'en' : 'zh-CN';
    document.documentElement.lang = state.lang;
    applyI18n();
    rerenderDynamic();
    toast(t('toast.lang_switched'), 'ok');
  }

  /* =====================================================================
   * boot —— 事件绑定与启动
   * ===================================================================== */
  function bindEvents() {
    // 左侧导航
    $$('.nav-item').forEach(function (btn) {
      btn.addEventListener('click', function () { switchView(btn.dataset.view); });
    });
    // 浏览器前进/后退
    window.addEventListener('hashchange', function () {
      var v = viewFromHash();
      if (v !== state.view) switchView(v, true);
    });

    // 全局保存 / 重新载入
    $('#save-fab').addEventListener('click', save);
    $('#btn-reload').addEventListener('click', function () {
      if (state.dirty && !window.confirm(t('common.unsaved') + ' — ' + t('action.reload') + '?')) return;
      $('#loading').classList.remove('hidden');
      i18n.load().then(loadAll).then(afterLoad).catch(handleLoadError);
    });

    // 大模型
    $('#btn-test').addEventListener('click', testConnection);
    $('#btn-models').addEventListener('click', fetchModels);
    $('#llm-provider').addEventListener('change', function () {
      var p = state.providerMap[$('#llm-provider').value];
      if (p) {
        // 选中服务商后自动填充 base_url 与默认 model
        if (p.base_url) { $('#llm-base-url').value = p.base_url; setPath(state.config, 'llm.base_url', p.base_url); }
        var model = p.default_model || '';
        $('#llm-model').value = model;
        setPath(state.config, 'llm.model', model);
      }
      syncProviderUI();
    });
    $('#api-key').addEventListener('input', function () {
      var provider = $('#llm-provider').value;
      var v = $('#api-key').value;
      if (!v || v === (state.masked[provider] || '')) delete state.apiKeys[provider];
      else state.apiKeys[provider] = v;
      markDirty();
    });

    // 调度与手动运行
    $('#btn-run').addEventListener('click', runNow);
    $('#btn-refresh').addEventListener('click', refreshStatus);

    // 分类搜索与勾选
    $('#cat-search').addEventListener('input', function () { applyCategoryFilter(this.value); });
    $('#cat-tree').addEventListener('change', function (ev) {
      var target = ev.target;
      if (!target || target.type !== 'checkbox' || !target.dataset.code) return;
      setSubscribed(target.dataset.code, target.checked);
    });

    // 界面语言：切换即生效
    $('#ui-language').addEventListener('change', onLanguageChange);

    // 离开页面前提醒未保存
    window.addEventListener('beforeunload', function (e) {
      if (!state.dirty) return;
      e.preventDefault();
      e.returnValue = '';
    });
  }

  function handleLoadError(err) {
    $('#loading').classList.add('hidden');
    var msg = err && err.status === 403 ? t('err.unauthorized') : (t('err.load') + ': ' + (err ? err.message : ''));
    showError(msg + (err && err.status ? ' (HTTP ' + err.status + ')' : ''));
    toast(t('err.load'), 'err');
  }

  /** 数据加载完成后的收尾：文案、控件绑定、辅助信息（幂等，可重复调用） */
  function afterLoad() {
    applyI18n();
    bindInputs();
    switchView(viewFromHash(), true);
    // 状态与任务日志属于辅助信息，失败不阻塞主界面
    refreshStatus();
    api.get('/api/job').then(function (job) {
      renderJob(job);
      if (job && job.running) startPolling();
    }).catch(function () { renderJob(null); });
  }

  function boot() {
    // 先绑定事件：即使数据加载失败，「重新载入」等按钮依然可用
    bindEvents();
    // 加载文案 -> 拉取全部数据 -> 渲染 -> 绑定 data-path 控件
    i18n.load().then(loadAll).then(afterLoad).catch(handleLoadError);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
