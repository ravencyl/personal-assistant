/* 批量操作：活动列表多选后批量改状态 / 改标签 / 改日期 */
(function () {
    'use strict';

    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    if (!csrfMeta) return;

    // 只在活动列表页生效
    var listEl = document.getElementById('activity-list');
    if (!listEl) return;

    var selected = new Set();
    var bar = document.getElementById('batch-bar');
    var countEl = document.getElementById('batch-count');

    function updateBar() {
        if (!bar) return;
        if (selected.size === 0) {
            bar.classList.add('hidden');
        } else {
            bar.classList.remove('hidden');
            if (countEl) countEl.textContent = selected.size;
        }
    }

    // 事件委托：checkbox 变更
    document.addEventListener('change', function (e) {
        if (e.target.matches('.batch-check')) {
            var id = e.target.value;
            if (e.target.checked) {
                selected.add(id);
            } else {
                selected.delete(id);
            }
            updateBar();
        }
        // 全选
        if (e.target.matches('#batch-select-all')) {
            var checks = document.querySelectorAll('.batch-check');
            checks.forEach(function (cb) {
                cb.checked = e.target.checked;
                if (e.target.checked) selected.add(cb.value);
                else selected.delete(cb.value);
            });
            updateBar();
        }
    });

    // 批量操作按钮
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-batch-action]');
        if (!btn || selected.size === 0) return;

        var action = btn.dataset.batchAction;
        var value;

        if (action === 'status') {
            value = prompt('批量改为状态（planned/in_progress/done/cancelled）：');
            if (!value || !['planned', 'in_progress', 'done', 'cancelled'].includes(value)) return;
        } else if (action === 'tags') {
            value = prompt('批量设置标签（逗号分隔）：');
            if (!value) return;
        } else if (action === 'date') {
            value = prompt('批量改为日期（YYYY-MM-DD）：');
            if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return;
        } else {
            return;
        }

        var changes = {};
        if (action === 'status') changes.status = value;
        else if (action === 'tags') changes.tags = value;
        else if (action === 'date') changes.date = value;

        fetch('/activities/batch-update/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfMeta.content,
            },
            body: JSON.stringify({ids: Array.from(selected), changes: changes}),
        }).then(function (res) {
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return res.json();
        }).then(function () {
            location.reload();
        }).catch(function (err) {
            alert('批量操作失败：' + err.message);
        });
    });
})();
