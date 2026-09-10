// 全局确认卡组件：全站危险操作统一走「确认卡」而不是原生 confirm() 弹窗，
// 视觉与对话内的两步确认卡（chat/cards/confirm_card.html）保持一致。
//
// 用法一（表单）：
//   <form method="post" data-confirm="确定删除这条备忘录吗？"
//         data-confirm-tone="danger" data-confirm-label="确认删除">…</form>
//   首次 submit 被拦截并弹确认卡，点「确认删除」后原样提交表单，点「取消」/Esc/点遮罩关闭。
//
// 用法二（fetch 等自定义流程）：
//   window.paConfirmCard('确定执行吗？', { tone: 'danger', label: '确认删除' })
//     .then(function (ok) { if (ok) { … } });
(function () {
    'use strict';

    var open = null; // { overlay, resolve, prevFocus }

    function close(result) {
        if (!open) return;
        document.removeEventListener('keydown', onKey, true);
        var entry = open;
        open = null;
        if (entry.prevFocus && entry.prevFocus.isConnected) entry.prevFocus.focus();
        entry.overlay.remove();
        entry.resolve(result);
    }

    function onKey(e) {
        if (e.key === 'Escape') {
            e.preventDefault();
            close(false);
        }
    }

    function show(message, opts) {
        opts = opts || {};
        if (open) close(false);

        var danger = opts.tone === 'danger';
        var label = opts.label || (danger ? '确认删除' : '确认执行');

        var overlay = document.createElement('div');
        overlay.className = 'fixed inset-0 z-[80] flex items-center justify-center p-4';
        overlay.style.background = 'rgba(0,0,0,0.4)';
        overlay.innerHTML =
            '<div role="alertdialog" aria-modal="true" ' +
            'class="w-full max-w-xs rounded-xl border border-zinc-200 bg-white overflow-hidden text-left">' +
            '<div class="h-1 ' + (danger ? 'bg-zinc-400' : 'bg-zinc-900') + '"></div>' +
            '<div class="p-3">' +
            '<p class="text-sm font-semibold text-zinc-800 flex items-center gap-1.5">' +
            '<svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"/></svg>' +
            '<span data-cc-title>请确认</span>' +
            '</p>' +
            '<p class="mt-1.5 text-sm leading-relaxed text-zinc-700" data-cc-text></p>' +
            '<div class="mt-3 flex justify-end gap-2">' +
            '<button type="button" data-cc-cancel class="rounded-full bg-zinc-100 text-zinc-600 hover:bg-zinc-200 px-3.5 py-1 text-xs font-medium transition-colors">取消</button>' +
            '<button type="button" data-cc-confirm class="rounded-full bg-zinc-900 hover:bg-black text-white px-3.5 py-1 text-xs font-medium transition-colors" data-cc-label></button>' +
            '</div>' +
            '</div>' +
            '</div>';
        // 文案与按钮文字走 textContent 注入，避免模板里的引号/尖括号破坏结构
        overlay.querySelector('[data-cc-text]').textContent = message;
        overlay.querySelector('[data-cc-label]').textContent = label;
        if (!danger) {
            // 非删除类操作不带警示图标，标题回落为纯文本
            var title = overlay.querySelector('[data-cc-title]');
            title.parentNode.previousElementSibling.remove();
        }

        var prevFocus = document.activeElement;
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) close(false);
        });
        overlay.querySelector('[data-cc-cancel]').addEventListener('click', function () { close(false); });
        overlay.querySelector('[data-cc-confirm]').addEventListener('click', function () { close(true); });

        document.body.appendChild(overlay);
        overlay.querySelector('[data-cc-confirm]').focus();
        document.addEventListener('keydown', onKey, true);

        return new Promise(function (resolve) {
            open = { overlay: overlay, resolve: resolve, prevFocus: prevFocus };
        });
    }

    // 事件委托（capture）：动态插入的表单也天然生效。
    // 确认后用 form.submit() 原样提交（它不再触发 submit 事件，因此不会二次拦截）。
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || form.nodeType !== 1 || !form.matches('form[data-confirm]')) return;
        if (form.dataset.ccConfirmed === '1') {
            delete form.dataset.ccConfirmed;
            return;
        }
        e.preventDefault();
        e.stopImmediatePropagation();
        show(form.getAttribute('data-confirm'), {
            tone: form.getAttribute('data-confirm-tone') || undefined,
            label: form.getAttribute('data-confirm-label') || undefined
        }).then(function (ok) {
            if (!ok) return;
            form.dataset.ccConfirmed = '1';
            form.submit();
        });
    }, true);

    window.paConfirmCard = show;
})();
