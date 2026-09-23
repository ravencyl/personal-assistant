// 活动列表置顶切换（2026-09-23）
// 置顶组在服务端按 pinned_at 排序、无视筛选/分页固定在最前，
// 本地翻 DOM 无法还原这套口径，切换成功后整页刷新（与树区/筛选状态一致）。
(function () {
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    if (!csrfMeta) return;

    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.pin-toggle');
        if (!btn) return;
        e.preventDefault();
        if (btn.dataset.busy) return;
        btn.dataset.busy = '1';

        var id = btn.getAttribute('data-activity-id');
        fetch('/activities/' + id + '/pin/', {
            method: 'POST',
            headers: {
                'Accept': 'application/json',
                'X-CSRFToken': csrfMeta.content,
            },
        }).then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        }).then(function (data) {
            if (!data || typeof data.pinned !== 'boolean') throw new Error('bad payload');
            window.location.reload();
        }).catch(function () {
            delete btn.dataset.busy;
            alert('置顶操作失败，请重试');
        });
    });
})();
