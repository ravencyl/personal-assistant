/* 全站侧边 AI 助手（docs/sidebar-agent-plan.md D2/D3，P1）
 *
 * 为什么单独一份文件而不是塞进 chat-turn.js：这里是「壳」（开关/装载/新话题），
 * chat-turn.js 是「收发流」。侧栏是 PaChatTurn 的第四个挂载点（分栏页 / 详情页 /
 * 问 AI 抽屉之后），与页面内其它实例同开一个会话时 window._paActiveChatTurn
 * 会被后建实例顶掉——可接受：visibilitychange 只需要唤醒「用户看得见的那个」，
 * 双写冲突由服务端 claim_turn / 409 兜住。
 *
 * 装载策略：懒装载——首次展开或开局恢复（localStorage）时才 GET /chat/sidebar/。
 * 每次整页导航都是新的侧栏，实例不需要跨页 halt。「新话题」走既有 POST
 * /chat/create/，建完重挂（侧栏端点按 updated_at 取最新非归档，天然落到新会话）。
 * 所有 fetch 走 chat-turn.js 的 paJsonFetch（Accept 补头 + 401 整页跳转唯一出口）。
 */
(function () {
    'use strict';

    // 登录页不装载侧栏：会话过期后浏览器可能复用「登录态时缓存的登录页 HTML」
    // （带着侧栏壳），若照常恢复展开态就会 fetch /chat/sidebar/ → 401 → 跳登录页，
    // 与 reauthTarget 的 next 嵌套叠加成死循环（2026-11-11 线上 400 事故）。
    // 登录页本就不该有侧栏，这里直接早退，连事件都不绑。
    if (location.pathname.indexOf('/accounts/login') === 0) return;

    var OPEN_KEY = 'pa-side-agent-open';
    var CONV_KEY = 'pa-side-agent-conv';
    var chatTurn = null;
    var mounting = false;
    var currentConvId = null;
    var historyOpen = false;

    function handle() { return document.getElementById('side-agent-handle'); }
    function messagesEl() { return document.querySelector('[data-side-agent-messages]'); }
    function formEl() { return document.querySelector('[data-side-agent-form]'); }
    function inputEl() { return document.querySelector('[data-side-agent-input]'); }
    function historyEl() { return document.querySelector('[data-side-agent-history-panel]'); }

    function mountUrl(convId) {
        // 指定会话重挂（历史列表点选 / 跨页恢复）；不带参数 = 服务端取最新非归档
        return convId ? '/chat/sidebar/?conversation_id=' + encodeURIComponent(convId) : '/chat/sidebar/';
    }

    function rememberConv(id) {
        currentConvId = id;
        window._paSidebarChatId = id;
        try { localStorage.setItem(CONV_KEY, String(id)); } catch (err) { /* 同 OPEN_KEY，静默降级 */ }
        markActive();
    }

    function markActive() {
        var hp = historyEl();
        if (!hp) return;
        var items = hp.querySelectorAll('[data-sb-conv-id]');
        for (var i = 0; i < items.length; i++) {
            items[i].classList.toggle('active',
                items[i].getAttribute('data-sb-conv-id') === String(currentConvId));
        }
    }

    function showHistory(open) {
        historyOpen = open;
        var hp = historyEl();
        if (hp) hp.hidden = !open;
        // 聊天视图三件套用 hidden 属性切换（都不是带 display 类的同层元素，无 hidden 失效坑）
        var bits = [messagesEl(), document.querySelector('[data-side-agent-status]'), formEl()];
        for (var i = 0; i < bits.length; i++) { if (bits[i]) bits[i].hidden = open; }
        var btn = document.querySelector('[data-side-agent-history]');
        if (btn) btn.setAttribute('aria-pressed', open ? 'true' : 'false');
    }

    function renderHistory(open) {
        showHistory(open);
        if (!open) { fit(); return; }   // 回到聊天视图：textarea 刚从 hidden 恢复，补一次拟合
        var hp = historyEl();
        if (!hp) return;
        hp.innerHTML = '<p class="side-hist-note">加载中…</p>';
        window.paJsonFetch('/chat/sidebar/conversations/', { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (!historyOpen) return;   // 等待期间用户已点「历史」切回去：别把列表写进隐藏面板
                if (hp) hp.innerHTML = d.list_html || '<p class="side-hist-note">还没有历史对话</p>';
                markActive();
            })
            .catch(function () {
                if (hp && historyOpen) hp.innerHTML = '<p class="side-hist-note">历史列表加载失败，请重试</p>';
            });
    }

    function switchTo(convId) {
        if (!convId || String(convId) === String(currentConvId)) { renderHistory(false); return; }
        if (chatTurn) chatTurn.halt();   // 同「新话题」：只停轮询，不打断服务端正在跑的本轮
        chatTurn = null;
        mount(convId);
        renderHistory(false);
    }

    function setOpen(open) {
        document.body.classList.toggle('side-agent-open', open);
        var h = handle();
        if (h) h.setAttribute('aria-expanded', open ? 'true' : 'false');
        try { localStorage.setItem(OPEN_KEY, open ? '1' : ''); } catch (err) { /* 隐私模式存不了就用默认收起 */ }
        if (open) { mount(); fit(); }
    }

    function fit() {
        // 展开瞬间 textarea 从 display:none 变可见：paFitTextarea 的守卫此前会跳过它，
        // 这里补一次拟合（隐藏容器 scrollHeight 恒 0、placeholder 裁字的老坑）
        var ta = inputEl();
        if (ta && window.paFitTextarea) window.paFitTextarea(ta);
    }

    function mount(convId) {
        if (chatTurn || mounting) return;
        mounting = true;
        var msgs = messagesEl();
        if (msgs) msgs.innerHTML = '<p class="text-center text-xs text-[var(--text-muted)] py-6">正在接续对话…</p>';
        window.paJsonFetch(mountUrl(convId), { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                mounting = false;
                if (!d.conversation_id) throw new Error(d.error || '装载失败');
                if (msgs) msgs.innerHTML = d.messages_html;
                // 历史片段是 JS 插入的：htmx 1.x 没有 MutationObserver，片段里的
                // 确认卡/候选卡表单必须手动接管（innerHTML 整体替换 → 全部是新节点）
                if (msgs && window.paProcessHtmx) window.paProcessHtmx(msgs, 0);
                // 非 /chat 页面上，快捷卡片（每日简报等）的 chatId 以侧栏装载的会话为准
                // （chat-turn.js::currentChatId 在 pathname 命不中时读这个钩子）
                rememberConv(d.conversation_id);
                var base = '/chat/' + d.conversation_id + '/';
                chatTurn = window.PaChatTurn({
                    messagesEl: msgs,
                    statusEl: document.querySelector('#side-agent-panel [data-turn-status]'),
                    form: formEl(),
                    input: inputEl(),
                    urls: function () {
                        return { send: base + 'send/', poll: base + 'turn/',
                                 wait: base + 'turn-wait/', cancel: base + 'turn/cancel/' };
                    },
                    extraParams: function () {
                        return (window.paPageContext && window.paPageContext()) || {};
                    },
                    onActivityChanged: function () {
                        document.dispatchEvent(new CustomEvent('activities:changed'));
                    }
                });
                // 片段自带 turn_resume（本轮还在跑）：读一次即撤地接上轮询
                var resumeEl = msgs && msgs.querySelector('[data-turn-resume]');
                if (resumeEl) {
                    chatTurn.resume(resumeEl.getAttribute('data-state'),
                                    parseInt(resumeEl.getAttribute('data-ttl') || '0', 10));
                }
            })
            .catch(function () {
                mounting = false;
                // 带着历史点选/跨页恢复的 convId 装载失败（多半已被归档或删除）：
                // 静默回落「最新非归档」，重试一次不带参数的装载
                if (convId) { mount(); return; }
                if (msgs) msgs.innerHTML = '<p class="text-center text-sm text-[var(--text)] font-semibold py-6">侧栏装载失败，请重试</p>';
            });
    }

    function newTopic() {
        if (chatTurn) chatTurn.halt();   // 只停轮询：切会话不是取消服务端正在跑的本轮
        chatTurn = null;
        var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
        window.paJsonFetch('/chat/create/', {
            method: 'POST',
            headers: { 'X-CSRFToken': csrf }
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (!d.conversation_id) throw new Error(d.error || '创建失败');
                renderHistory(false);   // 从历史视图点「新话题」：先回聊天视图再挂载
                mount();   // 最新非归档对话就是刚建的这条，复用同一份装载链路
            })
            .catch(function () {
                // 失败不悄悄恢复旧实例：旧 urls 已指向换掉的会话，宁可让用户再点一次
                var msgs = messagesEl();
                if (msgs) msgs.innerHTML = '<p class="text-center text-sm text-[var(--text)] font-semibold py-6">新话题创建失败，请重试</p>';
            });
    }

    // ── 事件绑定（把手/面板随 base.html 常驻 DOM，直接绑不必委托） ──
    var h = handle();
    if (h) h.addEventListener('click', function () { setOpen(true); });
    var closeBtn = document.querySelector('[data-side-agent-close]');
    if (closeBtn) closeBtn.addEventListener('click', function () { setOpen(false); });
    var newBtn = document.querySelector('[data-side-agent-new]');
    if (newBtn) newBtn.addEventListener('click', newTopic);
    var histBtn = document.querySelector('[data-side-agent-history]');
    if (histBtn) histBtn.addEventListener('click', function () { renderHistory(!historyOpen); });
    // 历史列表是 innerHTML 换进来的动态节点：委托绑在常驻面板容器上，条目随便换
    var hp = historyEl();
    if (hp) hp.addEventListener('click', function (e) {
        var item = e.target.closest('[data-sb-conv-id]');
        if (item) switchTo(item.getAttribute('data-sb-conv-id'));
    });

    // 开局恢复（D3 常驻）：上次展开过就直接还原并装载。base.html 里更早的内联脚本
    // 已先把 body.side-agent-open 加上（防面板滑入闪动），这里只补 aria 与装载。
    // 会话选择也一并恢复（P2）：侧栏跨页保持用户挑的那条对话；它若已被归档/删除，
    // mount 的 404 分支自动回落「最新非归档」
    try {
        if (localStorage.getItem(OPEN_KEY) === '1') {
            if (h) h.setAttribute('aria-expanded', 'true');
            var stored = localStorage.getItem(CONV_KEY);
            mount(stored && /^\d+$/.test(stored) ? stored : null);
        }
    } catch (err) { /* 同上，静默降级 */ }
})();
