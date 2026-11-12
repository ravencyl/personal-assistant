/* 站内通知前端（2026-11-12）：PC 顶栏铃铛弹窗 + 移动「我的」未读徽标 + 轮询。
 *
 * 数据源全部来自服务端：#notif-root 的 data-unread-url / data-list-url / data-read-all-url
 * 由 {% url %} 下发，列表 JSON 里每条带 read_url / link（服务端 reverse 产出）。JS 不拼路由。
 * fetch 走 window.paJsonFetch（chat-turn.js 统一补 Accept + 401 整页跳转），缺失时回退原生 fetch。
 * 列表条目一律 createElement + textContent 构建（标题/正文是用户相关数据，绝不拼 HTML 字符串）。
 * 关闭弹窗用「点击外部」而非 document 级 keydown —— AGENTS.md 禁全局键盘快捷键。
 * 徽标轮询 20s + 页面重新可见/聚焦时刷新；优化完成广播 notifications:changed 即时刷新。
 */
(function () {
    'use strict';
    var root = document.getElementById('notif-root');
    if (!root) return;                       // 未登录 / 无通知壳

    var unreadUrl = root.dataset.unreadUrl || '';
    var listUrl = root.dataset.listUrl || '';
    var pcBadge = document.getElementById('notif-badge');
    var mineBadge = document.getElementById('nav-mine-badge');
    var toggle = document.getElementById('notif-toggle');
    var popup = document.getElementById('notif-popup');
    var listEl = document.getElementById('notif-list');
    var emptyEl = document.getElementById('notif-empty');
    var loadingEl = document.getElementById('notif-loading');

    function csrfToken() {
        var m = document.querySelector('meta[name="csrf-token"]');
        return m ? m.content : '';
    }
    function jsonFetch(url, opts) {
        opts = opts || {};
        opts.headers = opts.headers || {};
        if (!opts.headers['Accept']) opts.headers['Accept'] = 'application/json';
        return window.paJsonFetch ? window.paJsonFetch(url, opts) : fetch(url, opts);
    }
    function post(url) {
        return jsonFetch(url, { method: 'POST', headers: { 'X-CSRFToken': csrfToken() } });
    }

    /* ── 徽标 ── */
    function setBadges(n) {
        n = Math.max(0, n | 0);
        var text = n > 99 ? '99+' : String(n);
        [pcBadge, mineBadge].forEach(function (b) {
            if (!b) return;
            b.textContent = text;
            b.classList.toggle('hidden', n === 0);
        });
        if (toggle) toggle.setAttribute('aria-label', n > 0 ? '通知（' + n + ' 条未读）' : '通知');
    }
    function refreshUnread() {
        if (!unreadUrl) return;
        jsonFetch(unreadUrl, { method: 'GET' }).then(function (r) {
            if (!r.ok) throw new Error('http ' + r.status);
            return r.json();
        }).then(function (d) {
            if (d && d.ok) setBadges(d.unread || 0);
        }).catch(function () { /* 轮询失败静默，下一拍再试 */ });
    }

    /* ── 列表渲染（createElement + textContent） ── */
    function kindLabel(kind) {
        return { desc_optimized: '描述优化', desc_optimize_failed: '优化失败', system: '系统' }[kind] || '通知';
    }
    function renderItem(n) {
        var item = document.createElement('button');
        item.type = 'button';
        item.className = 'w-full text-left px-4 py-3 hover:bg-[var(--accent-light)] transition-colors flex items-start gap-2.5';
        item.setAttribute('data-notif-click', '');
        item.setAttribute('data-read-url', n.read_url || '');
        item.setAttribute('data-link', n.link || '');

        // 未读圆点（已读留透明占位，保持对齐）
        var dot = document.createElement('span');
        dot.className = 'mt-1.5 w-2 h-2 rounded-full shrink-0 ' + (n.is_read ? 'bg-transparent' : 'bg-[var(--accent)]');
        dot.setAttribute('aria-hidden', 'true');
        item.appendChild(dot);

        var body = document.createElement('span');
        body.className = 'flex-1 min-w-0';

        var top = document.createElement('span');
        top.className = 'flex items-center justify-between gap-2';
        var title = document.createElement('span');
        title.className = 'text-sm font-semibold text-[var(--text)] truncate';
        title.textContent = n.title || kindLabel(n.kind);
        var time = document.createElement('span');
        time.className = 'text-[10px] text-[var(--text-muted)] shrink-0';
        time.textContent = n.created_at || '';
        top.appendChild(title);
        top.appendChild(time);
        body.appendChild(top);

        if (n.body) {
            var desc = document.createElement('span');
            desc.className = 'block mt-0.5 text-xs text-[var(--text-secondary)] line-clamp-2';
            desc.textContent = n.body;
            body.appendChild(desc);
        }
        item.appendChild(body);
        return item;
    }
    function loadList() {
        if (!listEl || !listUrl) return;
        if (loadingEl) loadingEl.classList.remove('hidden');
        if (emptyEl) emptyEl.classList.add('hidden');
        jsonFetch(listUrl, { method: 'GET' }).then(function (r) {
            if (!r.ok) throw new Error('http ' + r.status);
            return r.json();
        }).then(function (d) {
            if (!d || !d.ok) throw new Error('bad payload');
            setBadges(d.unread || 0);
            while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
            var items = d.items || [];
            items.forEach(function (n) { listEl.appendChild(renderItem(n)); });
            if (loadingEl) loadingEl.classList.add('hidden');
            if (emptyEl) emptyEl.classList.toggle('hidden', items.length > 0);
        }).catch(function () {
            if (loadingEl) loadingEl.classList.add('hidden');
            if (emptyEl) { emptyEl.textContent = '加载失败，请稍后重试'; emptyEl.classList.remove('hidden'); }
        });
    }

    /* ── 弹窗开合 ── */
    function popupOpen() { return popup && !popup.classList.contains('hidden'); }
    function openPopup() {
        if (!popup) return;
        popup.classList.remove('hidden');
        if (toggle) toggle.setAttribute('aria-expanded', 'true');
        loadList();
    }
    function closePopup() {
        if (!popup) return;
        popup.classList.add('hidden');
        if (toggle) toggle.setAttribute('aria-expanded', 'false');
    }
    if (toggle) {
        toggle.addEventListener('click', function (e) {
            e.stopPropagation();
            if (popupOpen()) closePopup(); else openPopup();
        });
    }
    // 点击通知壳外部关闭（click 监听，不是 keydown 全局快捷键）
    document.addEventListener('click', function (e) {
        if (popupOpen() && !root.contains(e.target)) closePopup();
    });

    /* ── 条目点击 / 全部已读（委托，覆盖弹窗条目与 dashboard 服务端条目） ── */
    document.addEventListener('click', function (e) {
        var readAll = e.target.closest('[data-notif-read-all]');
        if (readAll) {
            e.preventDefault();
            var allUrl = readAll.getAttribute('data-url') || root.dataset.readAllUrl;
            if (!allUrl) return;
            post(allUrl).then(function () {
                setBadges(0);
                if (popupOpen()) loadList(); else location.reload();
            }).catch(function () { /* 失败不阻断 */ });
            return;
        }
        var item = e.target.closest('[data-notif-click]');
        if (item) {
            e.preventDefault();
            var readUrl = item.getAttribute('data-read-url');
            var link = item.getAttribute('data-link');
            var inPopup = !!item.closest('#notif-popup');
            var go = function () {
                if (link) { location.href = link; }
                else if (inPopup) { loadList(); refreshUnread(); }
                else { location.reload(); }
            };
            if (readUrl) post(readUrl).then(go).catch(go);
            else go();
        }
    });

    /* ── 刷新触发：轮询 + 可见性 + 聚焦 + 优化完成广播 ── */
    refreshUnread();
    setInterval(refreshUnread, 20000);
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) refreshUnread();
    });
    window.addEventListener('focus', refreshUnread);
    document.addEventListener('notifications:changed', function () {
        refreshUnread();
        if (popupOpen()) loadList();
    });
})();