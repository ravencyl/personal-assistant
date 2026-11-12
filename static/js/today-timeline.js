/* 今日页「接下来」7 天时间线交互（2026-11-12，还原 demo 变体A）。
 *
 * 只负责五件事，全部客户端渐进增强（无 JS 时服务端渲染的分组/详情标记仍在，
 * 只是不能折叠/展开/标记完成/编辑描述）：
 *   1. 入场 reveal（IntersectionObserver 进入视口加 .in，无 IO 直接全加）
 *   2. 点日期头折叠/展开整天（.tl-day.collapsed + aria-expanded）
 *   3. 点活动行单开内联详情（.tl-detail-wrap.open，同时只开一个）
 *   4. 「标记完成/取消完成」fetch POST 状态端点（JSON 通道），就地更新行与详情，
 *      不整页刷新（保住滚动位置与展开态），成功后广播 activities:changed。
 *   5. 「编辑描述」点开共享弹窗：GET 取描述全文填充（卡片上的 desc 是 200 字截断
 *      预览，编辑必须拿全文，否则一保存就把 200 字之后的内容冲掉），POST 落库后就地
 *      更新卡片预览（与服务端同口径：折叠空白 + 200 字截断），成功后广播 activities:changed。
 *
 * 状态/描述端点复用 activities:activity_set_status / activity_edit_description
 * （Accept: application/json 回 JSON），每个按钮自带 data-url，不在 JS 里拼路由。
 * fetch 走 window.paJsonFetch（chat-turn.js 统一补 Accept + 401 整页跳转），缺失时回退原生 fetch。
 * 弹窗 Escape 监听挂在弹窗自身（焦点在弹窗内才生效），不挂 document 全局——AGENTS.md 禁全局键盘快捷键。
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

    /* ── 5. 编辑描述弹窗（共享单实例） ── */
    var modal = document.getElementById('desc-edit-modal');
    var editPanel = document.getElementById('desc-edit-panel');
    var editName = document.getElementById('desc-edit-name');
    var editForm = document.getElementById('desc-edit-form');
    var editInput = document.getElementById('desc-edit-input');
    var editStatus = document.getElementById('desc-edit-status');
    var editError = document.getElementById('desc-edit-error');
    var editSubmit = document.getElementById('desc-edit-submit');
    var editState = null;        // { id, url, trigger }：当前编辑的活动 + 触发按钮（关闭后还焦点）
    var editLoaded = false;      // GET 成功前禁止保存，避免拿空值冲掉真实描述
    var editLastFocus = null;

    // 与服务端 daily_views 同口径：折叠空白 + 200 字截断（卡片预览，不是全文）
    function descPreview(text) {
        var s = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
        return s.length > 200 ? s.slice(0, 200) + '…' : s;
    }
    function editStatusMsg(msg) {
        if (!editStatus) return;
        editStatus.textContent = msg || '';
        editStatus.classList.toggle('hidden', !msg);
    }
    function editErrorMsg(msg) {
        if (!editError) return;
        editError.textContent = msg || '';
        editError.classList.toggle('hidden', !msg);
    }

    function openEditModal(btn) {
        if (!modal) return;
        var id = btn.dataset.editDesc;
        var url = btn.dataset.url;
        if (!id || !url) return;
        editState = { id: id, url: url, trigger: btn };
        editLastFocus = document.activeElement;
        editLoaded = false;
        // 先用卡片上的名字兜底，GET 回来再覆盖为权威值
        var wrap = root.querySelector('.tl-detail-wrap[data-detail="' + id + '"]');
        var nameEl = wrap ? wrap.querySelector('.td-name') : null;
        if (editName) editName.textContent = nameEl ? nameEl.textContent : '';
        if (editInput) { editInput.value = ''; editInput.disabled = true; }
        if (editSubmit) { editSubmit.disabled = true; editSubmit.textContent = '保存'; }
        editErrorMsg('');
        editStatusMsg('加载中…');
        modal.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
        // 焦点给弹窗容器而非 textarea：移动端不自动弹起键盘（与新建活动弹窗同口径）
        if (editPanel) editPanel.focus();

        var opts = { method: 'GET', headers: { 'Accept': 'application/json' } };
        var doFetch = window.paJsonFetch ? window.paJsonFetch(url, opts) : fetch(url, opts);
        doFetch.then(function (r) {
            if (!r.ok) throw new Error('http ' + r.status);
            return r.json();
        }).then(function (d) {
            if (!editState || editState.id !== id) return;   // 慢响应不得盖掉已切换的活动
            if (!d || !d.ok) throw new Error('bad payload');
            if (editName) editName.textContent = d.name || '';
            if (editInput) { editInput.value = d.description || ''; editInput.disabled = false; }
            editLoaded = true;
            if (editSubmit) editSubmit.disabled = false;
            editStatusMsg('');
        }).catch(function () {
            if (!editState || editState.id !== id) return;
            editStatusMsg('');
            editErrorMsg('加载描述失败，请关闭后重试');   // 保持禁用，绝不让用户在空值上保存
        });
    }

    function closeEditModal() {
        if (!modal || modal.classList.contains('hidden')) return;
        modal.classList.add('hidden');
        document.body.style.overflow = '';
        var trigger = editState && editState.trigger;
        editState = null;
        editLoaded = false;
        editErrorMsg('');
        editStatusMsg('');
        if (editSubmit) { editSubmit.disabled = false; editSubmit.textContent = '保存'; }
        if (editInput) editInput.disabled = false;
        // 焦点还给触发按钮（无障碍）；按钮已不在 DOM 就还给开弹窗前的焦点元素
        if (trigger && document.contains(trigger)) trigger.focus();
        else if (editLastFocus && document.contains(editLastFocus)) editLastFocus.focus();
    }

    // 就地更新卡片描述预览，DOM 顺序与服务端一致（.td-desc 在 .td-actions 之前）
    function updateCardDesc(id, text) {
        var wrap = root.querySelector('.tl-detail-wrap[data-detail="' + id + '"]');
        var detail = wrap ? wrap.querySelector('.tl-detail') : null;
        if (!detail) return;
        var preview = descPreview(text);
        var descEl = detail.querySelector('.td-desc');
        if (!preview) {                       // 清空：移除预览块（对齐 {% if a.desc %}）
            if (descEl) descEl.remove();
            return;
        }
        if (descEl) {
            descEl.textContent = preview;     // 用户数据一律 textContent，绝不拼 HTML 字符串
        } else {
            descEl = document.createElement('div');
            descEl.className = 'td-desc';
            descEl.textContent = preview;
            detail.insertBefore(descEl, detail.querySelector('.td-actions') || null);
        }
    }

    function saveEdit() {
        if (!editState || !editLoaded) return;
        var id = editState.id;
        var url = editState.url;
        var value = editInput ? editInput.value : '';
        editErrorMsg('');
        if (editSubmit) { editSubmit.disabled = true; editSubmit.textContent = '保存中…'; }
        if (editInput) editInput.disabled = true;

        var body = new URLSearchParams();
        body.set('description', value);
        var opts = {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken(), 'Accept': 'application/json' },
            body: body
        };
        var doFetch = window.paJsonFetch ? window.paJsonFetch(url, opts) : fetch(url, opts);
        doFetch.then(function (r) {
            return r.json().catch(function () { return null; }).then(function (d) {
                if (r.status === 404) { var e = new Error('404'); e.user = (d && d.error) || '活动不存在或无权访问'; throw e; }
                if (!r.ok || !d || !d.ok) throw new Error('save failed');
                return d;
            });
        }).then(function (d) {
            updateCardDesc(id, d.description || '');
            document.dispatchEvent(new CustomEvent('activities:changed'));
            closeEditModal();
        }).catch(function (err) {
            if (editSubmit) { editSubmit.disabled = false; editSubmit.textContent = '保存'; }
            if (editInput) editInput.disabled = false;
            editErrorMsg((err && err.user) || '保存失败，请稍后重试');
        });
    }

    if (modal) {
        var closeBtn = document.getElementById('desc-edit-close');
        var cancelBtn = document.getElementById('desc-edit-cancel');
        if (closeBtn) closeBtn.addEventListener('click', closeEditModal);
        if (cancelBtn) cancelBtn.addEventListener('click', closeEditModal);
        // 点遮罩空白处关闭（点面板内部不关）
        modal.addEventListener('click', function (e) { if (e.target === modal) closeEditModal(); });
        if (editForm) editForm.addEventListener('submit', function (e) { e.preventDefault(); saveEdit(); });
        // Escape 挂在弹窗自身：焦点在弹窗内才生效，不是 document 全局快捷键
        modal.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') { e.preventDefault(); closeEditModal(); }
        });
    }

    /* ── 委托点击 ── */
    root.addEventListener('click', function (e) {
        var close = e.target.closest('[data-close]');
        if (close) { setOpen(null); return; }
        var done = e.target.closest('[data-done]');
        if (done) { markComplete(done); return; }
        var editBtn = e.target.closest('[data-edit-desc]');
        if (editBtn) { openEditModal(editBtn); return; }
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