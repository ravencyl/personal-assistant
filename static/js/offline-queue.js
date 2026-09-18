/**
 * 离线操作队列：IndexedDB 暂存写操作，联网后自动重放
 *
 * 用法：
 *   PaOfflineQueue.enqueue({ url: '/activities/123/status/', method: 'POST', body: { status: 'done' } })
 *
 * 联网时（online 事件 + 启动时检查）自动逐条重放，成功后从队列移除。
 */
(function () {
    'use strict';

    var DB_NAME = 'pa_offline_queue';
    var STORE_NAME = 'operations';
    var DB_VERSION = 1;

    function openDB() {
        return new Promise(function (resolve, reject) {
            var req = indexedDB.open(DB_NAME, DB_VERSION);
            req.onupgradeneeded = function () {
                var db = req.result;
                if (!db.objectStoreNames.contains(STORE_NAME)) {
                    db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
                }
            };
            req.onsuccess = function () { resolve(req.result); };
            req.onerror = function () { reject(req.error); };
        });
    }

    function getAll() {
        return openDB().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(STORE_NAME, 'readonly');
                var store = tx.objectStore(STORE_NAME);
                var req = store.getAll();
                req.onsuccess = function () { resolve(req.result || []); };
                req.onerror = function () { reject(req.error); };
            });
        });
    }

    function removeItem(id) {
        return openDB().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(STORE_NAME, 'readwrite');
                var store = tx.objectStore(STORE_NAME);
                var req = store.delete(id);
                req.onsuccess = function () { resolve(); };
                req.onerror = function () { reject(req.error); };
            });
        });
    }

    function addItem(op) {
        return openDB().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(STORE_NAME, 'readwrite');
                var store = tx.objectStore(STORE_NAME);
                var req = store.add(op);
                req.onsuccess = function () { resolve(req.result); };
                req.onerror = function () { reject(req.error); };
            });
        });
    }

    function getCsrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    }

    /** 重放队列中的所有操作 */
    function flush() {
        if (!navigator.onLine) return Promise.resolve();

        return getAll().then(function (ops) {
            if (!ops.length) return;

            var chain = Promise.resolve();
            ops.sort(function (a, b) { return a.id - b.id; });

            ops.forEach(function (op) {
                chain = chain.then(function () {
                    var headers = {
                        'Accept': 'application/json',
                        'X-CSRFToken': getCsrfToken(),
                    };
                    if (op.body) {
                        headers['Content-Type'] = 'application/json';
                    }

                    return fetch(op.url, {
                        method: op.method || 'POST',
                        headers: headers,
                        body: op.body ? JSON.stringify(op.body) : undefined,
                    }).then(function (res) {
                        if (res.ok || res.status === 302) {
                            return removeItem(op.id);
                        }
                        if (res.status >= 400 && res.status < 500) {
                            return removeItem(op.id);
                        }
                        console.warn('离线队列重放失败:', op.url, res.status);
                    }).catch(function () {
                        // 网络错误：保留在队列
                    });
                });
            });

            return chain;
        });
    }

    /** 入队一个离线操作 */
    function enqueue(op) {
        op.createdAt = Date.now();
        return addItem(op).then(function () {
            updateBadge();
        });
    }

    /** 更新离线队列角标 */
    function updateBadge() {
        getAll().then(function (ops) {
            var badges = document.querySelectorAll('[data-outbox-badge]');
            badges.forEach(function (badge) {
                badge.textContent = ops.length;
                badge.classList.toggle('hidden', ops.length === 0);
            });
        });
    }

    // 联网时自动重放
    window.addEventListener('online', function () {
        setTimeout(flush, 1000);
    });

    // 启动时检查
    if (navigator.onLine) {
        setTimeout(flush, 2000);
    }

    updateBadge();
    setInterval(updateBadge, 10000);

    // 暴露全局 API
    window.PaOfflineQueue = {
        enqueue: enqueue,
        flush: flush,
        count: function () { return getAll().then(function (ops) { return ops.length; }); },
    };
})();
