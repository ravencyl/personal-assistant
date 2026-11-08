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

    var OPEN_KEY = 'pa-side-agent-open';
    var chatTurn = null;
    var mounting = false;

    function handle() { return document.getElementById('side-agent-handle'); }
    function messagesEl() { return document.querySelector('[data-side-agent-messages]'); }
    function formEl() { return document.querySelector('[data-side-agent-form]'); }
    function inputEl() { return document.querySelector('[data-side-agent-input]'); }

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

    function mount() {
        if (chatTurn || mounting) return;
        mounting = true;
        var msgs = messagesEl();
        if (msgs) msgs.innerHTML = '<p class="text-center text-xs text-[var(--text-muted)] py-6">正在接续对话…</p>';
        window.paJsonFetch('/chat/sidebar/', { cache: 'no-store' })
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
                window._paSidebarChatId = d.conversation_id;
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

    // 开局恢复（D3 常驻）：上次展开过就直接还原并装载。base.html 里更早的内联脚本
    // 已先把 body.side-agent-open 加上（防面板滑入闪动），这里只补 aria 与装载
    try {
        if (localStorage.getItem(OPEN_KEY) === '1') {
            if (h) h.setAttribute('aria-expanded', 'true');
            mount();
        }
    } catch (err) { /* 同上，静默降级 */ }
})();
