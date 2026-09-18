/* 日历拖拽排期：月视图中拖拽活动卡片改日期
 *
 * 活动卡片 draggable=true，日历单元格 droppable。
 * drop 时 fetch POST /activities/<id>/move-date/ 更新 start_date。
 */
(function () {
    'use strict';

    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    if (!csrfMeta) return;

    // 只在月视图生效
    var grid = document.querySelector('.grid.grid-cols-7 .min-h-\\[60px\\]');
    if (!grid) return;

    var dragId = null;

    document.addEventListener('dragstart', function (e) {
        var el = e.target.closest('[data-activity-id]');
        if (!el) return;
        dragId = el.dataset.activityId;
        e.dataTransfer.setData('text/plain', dragId);
        e.dataTransfer.effectAllowed = 'move';
        el.style.opacity = '0.4';
    });

    document.addEventListener('dragend', function (e) {
        var el = e.target.closest('[data-activity-id]');
        if (el) el.style.opacity = '';
        // 清除所有高亮
        document.querySelectorAll('.cal-drop-target').forEach(function (c) {
            c.classList.remove('cal-drop-target');
        });
    });

    document.addEventListener('dragover', function (e) {
        var cell = e.target.closest('[data-date]');
        if (!cell || !dragId) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        cell.classList.add('cal-drop-target');
    });

    document.addEventListener('dragleave', function (e) {
        var cell = e.target.closest('[data-date]');
        if (cell) cell.classList.remove('cal-drop-target');
    });

    document.addEventListener('drop', function (e) {
        var cell = e.target.closest('[data-date]');
        if (!cell || !dragId) return;
        e.preventDefault();
        cell.classList.remove('cal-drop-target');

        var newDate = cell.dataset.date;
        if (!newDate) return;

        var url = '/activities/' + dragId + '/move-date/';
        fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfMeta.content,
            },
            body: JSON.stringify({date: newDate}),
        }).then(function (res) {
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return res.json();
        }).then(function () {
            location.reload();
        }).catch(function (err) {
            alert('拖拽失败：' + err.message);
        });

        dragId = null;
    });
})();
