/* 今日页「接下来」7 天时间线交互（2026-11-12，还原 demo 变体A）。
 *
 * 只负责四件事，全部客户端渐进增强（无 JS 时服务端渲染的分组/详情标记仍在，
 * 只是不能折叠/展开/标记完成）：
 *   1. 入场 reveal（IntersectionObserver 进入视口加 .in，无 IO 直接全加）
 *   2. 点日期头折叠/展开整天（.tl-day.collapsed + aria-expanded）
 *   3. 点活动行单开内联详情（.tl-detail-wrap.open，同时只开一个）
 *   4. 「标记完成/取消完成」fetch POST 状态端点（JSON 通道），就地更新行与详情，
 *      不整页刷新（保住滚动位置与展开态），成功后广播 activities:changed。
 *
 * 状态端点复用 activities:activity_set_status（Accept: application/json 回 JSON），
 * 每个按钮自带 data-url，不在 JS 里拼路由。fetch 走 window.paJsonFetch（chat-turn.js
 * 统一补 Accept + 401 整页跳转），缺失时回退原生 fetch。
 */
(function () {
    'use strict';
    var root = document.getElementById('upcoming-timeline');
    if (!root) return;

    function esc(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    /* ── 1. 入场 reveal ── */
    var rev = root.querySelectorAll('.reveal');
    if ('IntersectionObserver' in window) {
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (en) {
                if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); }
            });
        }, { threshold: 0.05 });
        rev.forEach(function (el) { io.observe(el); });
    } else {
        rev.forEach(function (el) { el.classList.add('in'); });
    }

    /* ── 3. 单开内联详情 ── */
    function setOpen(id) {
        root.querySelectorAll('.tl-detail-wrap').forEach(function (w) {
            w.classList.toggle('open', w.dataset.detail === id);
        });
        root.querySelectorAll('.tl-item').forEach(function (row) {
            row.setAttribute('aria-expanded', String(row.dataset.id === id));
        });
    }

    /* ── 2. 折叠整天 ── */
    function toggleDay(head) {
        var day = head.closest('.tl-day');
        if (!day) return;
        var collapsed = day.classList.toggle('collapsed');
        head.setAttribute('aria-expanded', String(!collapsed));
    }

    /* ── 4. 标记完成 / 取消完成 ── */
    function csrfToken() {
        var m = document.querySelector('meta[name="csrf-token"]');
        return m ? m.content : '';
    }
    function setStatusChip(chip, status, label) {
        if (!chip) return;
        chip.className = 'tl-status ' + status;
        chip.innerHTML = '<span class="sd"></span>' + esc(label);
    }
    function markComplete(btn) {
        if (btn.disabled) return;
        var id = btn.dataset.done;
        var url = btn.dataset.url;
        if (!id || !url) return;
        var wasDone = btn.classList.contains('is-done');
        var oldStatus = wasDone ? 'done' : (btn.dataset.prev || 'planned');
        var target = wasDone ? (btn.dataset.prev || 'planned') : 'done';
        var item = root.querySelector('.tl-item[data-id="' + id + '"]');
        var wrap = root.querySelector('.tl-detail-wrap[data-detail="' + id + '"]');
        var restoreLabel = btn.textContent;
        btn.disabled = true;

        var body = new URLSearchParams();
        body.set('status', target);
        var opts = {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken(), 'Accept': 'application/json' },
            body: body
        };
        var doFetch = window.paJsonFetch ? window.paJsonFetch(url, opts) : fetch(url, opts);
        doFetch.then(function (r) {
            if (!r.ok) throw new Error('http ' + r.status);
            return r.json();
        }).then(function (d) {
            if (!d || !d.ok) throw new Error('bad payload');
            var done = !!d.done;
            if (item) {
                item.classList.toggle('done', done);
                var node = item.querySelector('.tl-node--act');
                if (node) node.classList.toggle('in_progress', d.status === 'in_progress');
                setStatusChip(item.querySelector('.tl-status'), d.status, d.status_label);
            }
            if (wrap) setStatusChip(wrap.querySelector('.tl-status'), d.status, d.status_label);
            btn.classList.toggle('is-done', done);
            btn.textContent = done ? '取消完成' : '标记完成';
            btn.dataset.prev = done ? oldStatus : d.status;
            document.dispatchEvent(new CustomEvent('activities:changed'));
        }).catch(function () {
            btn.textContent = '操作失败';
            setTimeout(function () { btn.textContent = restoreLabel; }, 1600);
        }).finally(function () { btn.disabled = false; });
    }

    /* ── 委托点击 ── */
    root.addEventListener('click', function (e) {
        var close = e.target.closest('[data-close]');
        if (close) { setOpen(null); return; }
        var done = e.target.closest('[data-done]');
        if (done) { markComplete(done); return; }
        var head = e.target.closest('[data-head]');
        if (head) { toggleDay(head); return; }
        var item = e.target.closest('.tl-item');
        if (item) { setOpen(root.querySelector('.tl-detail-wrap.open') &&
            root.querySelector('.tl-detail-wrap.open').dataset.detail === item.dataset.id
            ? null : item.dataset.id); return; }
    });

    /* ── 键盘：Enter/Space 触发折叠/展开 ── */
    root.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        var t = e.target.closest('.tl-item,[data-head]');
        if (t) { e.preventDefault(); t.click(); }
    });
})();