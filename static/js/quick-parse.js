/* 快速记一笔前端通道：解析 → 预览 → 确认创建
 *
 * 约定（见 AGENTS.md「前端分端约定」）：本模块只走原生 fetch + JSON，
 * 触发元素上严禁出现 hx-*，也严禁调用 htmx.process()。
 * CSRF 一律读 base.html 的 <meta name="csrf-token">，不再各页自己解析 cookie。
 *
 * 消费方：
 * - templates/activities/activity_list.html   快速记活动（带「编辑详情」跳转）
 * - templates/activities/activity_detail.html 快速记子任务
 * - templates/activities/activity_form.html   解析后回填表单（只用 PaQuickParse.parse）
 */
(function () {
    'use strict';

    // 状态中文名与 _status_badge.html / STATUS_LABELS 保持一致
    var STATUS_LABELS = { planned: '计划', in_progress: '进行中', done: '已完成', cancelled: '已取消' };

    function esc(s) {
        var d = document.createElement('span');
        d.textContent = s;
        return d.innerHTML;
    }

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    }

    // 保留 HTTP 状态码，让调用方能区分「服务端拒绝」与「网络断开」
    function readJson(r) {
        return r.json().then(function (d) { return { ok: r.ok, data: d }; });
    }

    /**
     * POST 一段自然语言 → 解析结果。回调三选一：onOk / onErr（服务端拒绝）/ onNetErr（断网）。
     * opts: { parseUrl, onOk(data), onErr(message), onNetErr() }
     */
    function parse(text, opts) {
        var fd = new FormData();
        fd.append('text', text);
        return fetch(opts.parseUrl, {
            method: 'POST',
            body: fd,
            headers: { 'X-CSRFToken': csrfToken() },
        }).then(readJson).then(function (res) {
            if (!res.ok) { opts.onErr(res.data.error || '解析失败，请重试'); return; }
            opts.onOk(res.data);
        }).catch(opts.onNetErr);
    }

    /**
     * 完整「解析 → 预览 → 确认」交互。
     * opts: {
     *   input, parseBtn, confirmBtn, closeBtn, preview, previewBody, errEl, sourceEl  —— 元素 id
     *   parseUrl, submitUrl                                                           —— 必填端点
     *   confirmText    确认按钮空闲文案（默认「确认创建」）
     *   createdLabel   成功提示前缀（默认「已创建」，子任务页传「已创建子任务」）
     *   editBtn, editUrl                                                              —— 可选「编辑详情」跳转
     * }
     */
    function init(opts) {
        var input = document.getElementById(opts.input);
        var parseBtn = document.getElementById(opts.parseBtn);
        var confirmBtn = document.getElementById(opts.confirmBtn);
        var closeBtn = document.getElementById(opts.closeBtn);
        var preview = document.getElementById(opts.preview);
        var previewBody = document.getElementById(opts.previewBody);
        var errEl = document.getElementById(opts.errEl);
        var sourceEl = document.getElementById(opts.sourceEl);
        if (!input || !parseBtn || !confirmBtn || !preview || !previewBody || !errEl) return;
        var editBtn = opts.editBtn ? document.getElementById(opts.editBtn) : null;

        var confirmText = opts.confirmText || '确认创建';
        var createdLabel = opts.createdLabel || '已创建';
        var parsed = null;

        function showError(msg) { errEl.textContent = msg; errEl.classList.remove('hidden'); }
        function hideError() { errEl.classList.add('hidden'); }
        function hidePreview() { preview.classList.add('hidden'); }

        function setLoading(loading) {
            parseBtn.disabled = loading;
            parseBtn.textContent = loading ? '解析中…' : '解析';
        }

        function resetConfirm() {
            confirmBtn.disabled = false;
            confirmBtn.textContent = confirmText;
        }

        function renderPreview(d) {
            var parts = ['<span class="font-medium text-[var(--text)]">' + esc(d.name) + '</span>'];
            if (d.start_date) {
                parts.push('<span class="text-[var(--text-secondary)]">' + d.start_date +
                    (d.end_date && d.end_date !== d.start_date ? ' ~ ' + d.end_date : '') + '</span>');
            }
            if (d.cost !== undefined && d.cost !== null) {
                parts.push('<span class="text-[var(--text-secondary)]">费用 ¥ ' + esc(String(d.cost)) + '</span>');
            }
            // 预算与费用分列展示：前者是上限（写字段），后者是已花（记支出），混显示会直接误导确认
            if (d.budget !== undefined && d.budget !== null) {
                parts.push('<span class="text-[var(--text-secondary)]">预算 ¥ ' + esc(String(d.budget)) + '</span>');
            }
            if (d.status && STATUS_LABELS[d.status]) {
                parts.push('<span class="inline-flex items-center rounded-full bg-[var(--accent-light)] px-2 py-0.5 text-xs text-[var(--text-secondary)]">' + STATUS_LABELS[d.status] + '</span>');
            }
            (d.tags || []).forEach(function (t) {
                parts.push('<span class="inline-flex items-center rounded-full bg-[var(--accent-light)] border border-[var(--border-strong)] px-2 py-0.5 text-xs text-[var(--text-secondary)]"># ' + esc(t) + '</span>');
            });
            (d.participants || []).forEach(function (p) {
                parts.push('<span class="text-xs text-[var(--text-secondary)]">@ ' + esc(p) + '</span>');
            });
            previewBody.innerHTML = parts.join('');
            if (sourceEl) sourceEl.textContent = d.source === 'ai' ? 'AI 识别' : '规则识别';
            preview.classList.remove('hidden');
        }

        function doParse() {
            var text = input.value.trim();
            if (!text || parseBtn.disabled) return;
            hideError();
            hidePreview();
            setLoading(true);
            // 解析中途必须清空上次结果，否则失败后「确认」还可能提交旧数据
            parsed = null;
            parse(text, {
                parseUrl: opts.parseUrl,
                onOk: function (d) { setLoading(false); parsed = d; renderPreview(d); },
                onErr: function (m) { setLoading(false); showError(m); },
                onNetErr: function () { setLoading(false); showError('网络异常，请重试'); },
            });
        }

        parseBtn.addEventListener('click', doParse);
        input.addEventListener('keydown', function (e) { if (e.key === 'Enter') doParse(); });
        if (closeBtn) {
            closeBtn.addEventListener('click', function () { hidePreview(); hideError(); parsed = null; });
        }

        // 「编辑详情」：携带解析结果跳转完整表单页（由创建页读取填充）
        if (editBtn && opts.editUrl) {
            editBtn.addEventListener('click', function () {
                if (!parsed) return;
                sessionStorage.setItem('quickInputDraft', JSON.stringify(parsed));
                window.location.href = opts.editUrl;
            });
        }

        confirmBtn.addEventListener('click', function () {
            if (!parsed || confirmBtn.disabled) return;
            confirmBtn.disabled = true;
            confirmBtn.textContent = '创建中…';
            fetch(opts.submitUrl, {
                method: 'POST',
                body: JSON.stringify(parsed),
                headers: { 'X-CSRFToken': csrfToken(), 'Content-Type': 'application/json' },
            }).then(readJson).then(function (res) {
                if (!res.ok) {
                    resetConfirm();
                    showError(res.data.error || '创建失败');
                    return;
                }
                previewBody.innerHTML = '<span class="text-zinc-900 font-medium">' + createdLabel + '「'
                    + esc(res.data.name) + '」'
                    + (res.data.note ? '（' + esc(res.data.note) + '）' : '') + '，即将刷新…</span>';
                confirmBtn.textContent = '已创建';
                setTimeout(function () { window.location.reload(); }, 700);
            }).catch(function () {
                resetConfirm();
                showError('网络异常，请重试');
            });
        });
    }

    window.PaQuickParse = {
        parse: parse,
        init: init,
        esc: esc,
        csrfToken: csrfToken,
    };
})();
