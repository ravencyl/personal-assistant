/* ============================================================
   离线提交队列（快记浮层弱网兜底）
   ------------------------------------------------------------
   - enqueue(url, body, headers, follow?)：推入 localStorage['pa-outbox']
     （JSON 数组，元素 {url, body, headers, ts, follow}），返回是否实际入队
   - flush()：遍历队列逐条原生 fetch 重放（带入队时的 csrf 头），成功移除
   - 幂等防护：同 url + 同 body + 同分钟内已入队则跳过重复
   - 触发时机：window 'online' 事件、DOMContentLoaded、每次入队后立即尝试一次
   - 角标：队列非空时在快记 FAB（#quick-outbox-badge）显示灰阶数字，空则隐藏
   - follow（可选）：首跳重放成功后取其响应 JSON 再 POST 至 follow.url
     （活动 Tab「解析 → 创建」两跳链路用）
   - 重放结果处理：2xx 移除；4xx（业务错误，无法自愈）移除；
     5xx / 网络错误保留，等待下次触发重试
   - 不使用 Background Sync（iOS Safari 不支持）；重放全程原生 fetch，
     严禁调用 htmx / htmx.process()
   ============================================================ */
(function () {
    'use strict';

    var STORAGE_KEY = 'pa-outbox';
    var root = typeof window !== 'undefined' ? window : globalThis;

    function load() {
        try {
            var raw = root.localStorage.getItem(STORAGE_KEY);
            var items = raw ? JSON.parse(raw) : [];
            return Array.isArray(items) ? items : [];
        } catch (e) {
            return [];
        }
    }

    function save(items) {
        try {
            root.localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
        } catch (e) {
            /* 存储不可用/写满：仅告警，不阻断主流程 */
        }
        updateBadge();
    }

    function isOnline() {
        return typeof navigator === 'undefined' || navigator.onLine !== false;
    }

    function enqueue(url, body, headers, follow) {
        var items = load();
        var now = Date.now();
        var minute = Math.floor(now / 60000);
        // 幂等防护：同 url + 同 body 且同分钟内已入队 → 跳过重复
        var duplicated = items.some(function (it) {
            return !!it && it.url === url && it.body === body &&
                Math.floor((it.ts || 0) / 60000) === minute;
        });
        if (duplicated) return false;
        items.push({
            url: url,
            body: body == null ? '' : body,
            headers: headers || {},
            ts: now,
            follow: follow || null
        });
        save(items);
        flush(); // 在线则立即尝试重放（离线时 flush 内部直接返回）
        return true;
    }

    function removeItem(item) {
        save(load().filter(function (x) {
            return !(x.ts === item.ts && x.url === item.url);
        }));
    }

    var flushing = false;

    function replay(item, done) {
        // done(result): 'ok' 成功移除 / 'drop' 业务错误丢弃 / 'keep' 保留待下次
        fetch(item.url, {
            method: 'POST',
            headers: item.headers || {},
            body: item.body || ''
        }).then(function (r) {
            if (!r.ok) {
                done(r.status >= 400 && r.status < 500 ? 'drop' : 'keep');
                return;
            }
            if (!item.follow) { done('ok'); return; }
            // 两跳链路：解析响应 JSON 后 POST 到 follow.url
            r.json().then(function (data) {
                if (data && data.source) delete data.source;
                return fetch(item.follow.url, {
                    method: 'POST',
                    headers: item.follow.headers || {},
                    body: JSON.stringify(data)
                });
            }).then(function (r2) {
                done(r2.ok ? 'ok' : (r2.status >= 400 && r2.status < 500 ? 'drop' : 'keep'));
            }).catch(function () { done('keep'); });
        }).catch(function () {
            done('keep'); // 网络仍不可用：保留待下次触发
        });
    }

    function flush() {
        if (flushing || !isOnline()) return;
        var snapshot = load();
        if (!snapshot.length) { updateBadge(); return; }
        flushing = true;
        var i = 0;
        (function step() {
            if (i >= snapshot.length) { flushing = false; updateBadge(); return; }
            var item = snapshot[i++];
            replay(item, function (result) {
                if (result !== 'keep') removeItem(item);
                step();
            });
        })();
    }

    function updateBadge() {
        if (typeof document === 'undefined') return;
        var badge = document.getElementById('quick-outbox-badge');
        if (!badge) return;
        var count = load().length;
        badge.textContent = String(count);
        if (count > 0) badge.classList.remove('hidden');
        else badge.classList.add('hidden');
    }

    root.PaOutbox = {
        enqueue: enqueue,
        flush: flush,
        load: load,
        updateBadge: updateBadge
    };

    // ── 触发时机 ──
    if (typeof window !== 'undefined') {
        window.addEventListener('online', flush);
    }
    if (typeof document !== 'undefined') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function () {
                updateBadge();
                flush();
            });
        } else {
            updateBadge();
            flush();
        }
    }
})();
