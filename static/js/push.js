/* Web Push 订阅控制：顶栏铃铛按钮 → 授权 → 订阅 → 立即收到测试推送
 *
 * 流程（点击 toggle）：
 *   未订阅 → requestPermission → pushManager.subscribe(VAPID 公钥)
 *          → POST /api/push/subscribe/ → POST /api/push/test/（当场验证全链路）
 *   已订阅 → pushManager.unsubscribe() → POST /api/push/unsubscribe/
 *
 * iOS 限制：16.4+ 仅在添加到主屏幕（standalone 模式）后才允许推送授权，
 * 检测到 iOS Safari 普通标签页时引导先添加主屏幕，而不是让授权静默失败。
 *
 * 事件驱动脚本，defer 加载；无 #push-toggle-btn（未配置 VAPID）时静默退出。
 */
(function () {
    'use strict';

    var btn = document.getElementById('push-toggle-btn');
    if (!btn) return;

    var keyMeta = document.querySelector('meta[name="vapid-key"]');
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    if (!keyMeta || !keyMeta.content || !csrfMeta) return;
    var VAPID_KEY = keyMeta.content;

    function isStandalone() {
        return window.matchMedia('(display-mode: standalone)').matches
            || window.navigator.standalone === true;
    }

    function isIOS() {
        return /iP(hone|ad|od)/.test(navigator.userAgent)
            // iPadOS 13+ 桌面 UA，用平台指纹兜底识别
            || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    }

    function urlBase64ToUint8Array(base64) {
        var padding = '='.repeat((4 - base64.length % 4) % 4);
        var b64 = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/');
        var raw = atob(b64);
        var out = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
        return out;
    }

    function post(url, data) {
        return fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfMeta.content,
            },
            body: JSON.stringify(data || {}),
        }).then(function (res) {
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return res.json();
        });
    }

    function getRegistration() {
        if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
            return Promise.reject(new Error('unsupported'));
        }
        return navigator.serviceWorker.getRegistration('/sw.js')
            .then(function (reg) {
                if (!reg) throw new Error('no-sw');
                return reg;
            });
    }

    function refresh() {
        getRegistration()
            .then(function (reg) { return reg.pushManager.getSubscription(); })
            .then(function (sub) { mark(sub !== null); })
            .catch(function () { mark(false); });
    }

    function mark(on) {
        btn.classList.toggle('push-on', !!on);
        btn.title = on ? '已开启每日推送，点按关闭' : '开启每日推送';
    }

    function subscribe(reg) {
        return Notification.requestPermission().then(function (perm) {
            if (perm !== 'granted') throw new Error('denied');
            return reg.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(VAPID_KEY),
            });
        }).then(function (sub) {
            return post('/api/push/subscribe/', sub.toJSON()).then(function () { return sub; });
        }).then(function () {
            // 立刻发一条测试推送，让用户当场确认链路通
            return post('/api/push/test/').catch(function () { /* 测试失败不影响订阅 */ });
        }).then(function () {
            mark(true);
            alert('已开启每日推送！刚才应该收到一条测试通知，明早 8 点会准时收到早报。');
        });
    }

    function unsubscribe(reg) {
        return reg.pushManager.getSubscription().then(function (sub) {
            var endpoint = sub ? sub.endpoint : null;
            var p = sub ? sub.unsubscribe() : Promise.resolve();
            return p.then(function () {
                return post('/api/push/unsubscribe/', {endpoint: endpoint});
            });
        }).then(function () { mark(false); });
    }

    btn.addEventListener('click', function () {
        getRegistration().then(function (reg) {
            return reg.pushManager.getSubscription().then(function (sub) {
                if (sub) {
                    // 已订阅：先确认再退订，避免误点一次就静默关掉早报
                    if (!window.confirm('已开启每日推送，确定要关闭吗？\n关闭后每天 8 点的早报将不再推送。')) return null;
                    return unsubscribe(reg);
                }
                if (isIOS() && !isStandalone()) {
                    alert('iPhone 上接收推送需要先把本站添加到主屏幕：\n\nSafari 底部分享按钮 → 「添加到主屏幕」，然后从主屏幕打开再点铃铛。');
                    return null;
                }
                return subscribe(reg);
            });
        }).catch(function (err) {
            var msg = {
                'denied': '推送权限被拒绝。请在系统设置 → Safari/浏览器 → 通知 里重新允许。',
                'unsupported': '当前浏览器不支持 Web Push。',
                'no-sw': 'Service Worker 尚未就绪，请刷新页面后重试。',
            }[err && err.message] || '开启失败：' + (err && err.message ? err.message : '未知错误');
            alert(msg);
        });
    });

    refresh();
})();
