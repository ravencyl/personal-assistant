/* 全站移动端浮窗 AI 助手（2026-11-13）：手机专属弹窗式对话壳。
 *
 * 为什么单独一份文件：这里是「壳」（FAB 开关 / 装载 / 历史 / 新话题 / 页面标题），
 * chat-turn.js 是「收发流」。浮窗是 PaChatTurn 的第五个挂载点（分栏页 / 详情页 /
 * 问 AI 抽屉 / 桌面侧栏之后），与桌面侧栏（sidebar-agent.js）功能一致、只是 UI 不同：
 * 侧栏是左缘滑入栏，浮窗是右下角 FAB + 底部上滑弹窗。
 *
 * 与桌面侧栏互斥：两个壳都常驻 base.html，但同一视口只允许一个在跑——
 * 本文件在桌面视口（>=768px）早退，sidebar-agent.js 在移动视口（<768px）早退。
 * 否则移动视口里侧栏壳虽然 display:none，其 JS 仍会 mount 一个看不见的会话，
 * 与浮窗抢 window._paActiveChatTurn / _paSidebarChatId（活动页浮窗挂的是活动专属
 * 会话，侧栏挂的是最新通用会话，两者不同，快捷卡片会落错对话）。
 *
 * 会话口径（与侧栏/抽屉一致，全部复用既有端点，零后端改动）：
 *  - 活动详情/编辑页（/activities/<id>/ 或 /activities/<id>/edit/）→
 *    GET /chat/for-activity/<id>/（create-or-get 活动专属会话，服务端自动钉选），
 *    再 GET 其 widget-messages 片段。AI 默认以当前活动沟通与操作（用户要求）。
 *  - 其余页面 → GET /chat/sidebar/（页面感知单会话，filter(user) 个人上下文口径）。
 * 历史点选 / 新话题一律走 /chat/sidebar/?conversation_id= 显式挂载（绕过活动自动钉选）。
 *
 * 不做跨页持久化：每次整页导航都是新页面，浮窗默认收起、按当前页面重新装载——
 * 这正是「AI 默认以当前页面/当前活动沟通」的语义（侧栏的跨页记忆会话在这里不适用）。
 * 所有 fetch 走 chat-turn.js 的 paJsonFetch（Accept 补头 + 401 整页跳转唯一出口）。
 */
(function () {
    'use strict';

    // 登录页不装载：会话过期后浏览器可能复用「登录态时缓存的登录页 HTML」（带着浮窗壳），
    // 照常装载会 fetch 401 → 跳登录页，与 reauthTarget 的 next 嵌套叠加成死循环
    //（2026-11-11 线上 400 事故，侧栏同款守卫）。登录页本就不该有浮窗，直接早退。
    if (location.pathname.indexOf('/accounts/login') === 0) return;
    // 桌面视口由 sidebar-agent.js 的侧栏接管（浮窗壳在桌面 display:none），早退避免双壳
    if (window.matchMedia && window.matchMedia('(min-width: 768px)').matches) return;

    var chatTurn = null;
    var mounting = false;
    var currentConvId = null;
    var historyOpen = false;

    // 活动详情/编辑页 → 自动钉选当前活动。URL 结构由 activities/urls.py 锁定：
    // /activities/<id>/ 与 /activities/<id>/edit/。pathname 不含查询串，正则稳定命中。
    var activityMatch = /^\/activities\/(\d+)\/(?:edit\/)?$/.exec(location.pathname);
    var activityId = activityMatch ? activityMatch[1] : null;

    function panel() { return document.getElementById('mobile-agent-panel'); }
    function fab() { return document.getElementById('mobile-agent-fab'); }
    function backdrop() { return document.getElementById('mobile-agent-backdrop'); }
    function messagesEl() { return document.querySelector('[data-mobile-agent-messages]'); }
    function formEl() { return document.querySelector('[data-mobile-agent-form]'); }
    function inputEl() { return document.querySelector('[data-mobile-agent-input]'); }
    function historyEl() { return document.querySelector('[data-mobile-agent-history-panel]'); }
    function titleEl() { return document.getElementById('mobile-agent-title'); }

    /* 顶部标题 = 当前网页 title 去掉「 · 站名」后缀（用户要求「和当前网页的标题保持一致」）：
     * 今日 · 三磊 → 今日；活动记录 · 三磊 → 活动记录；<活动名> · 三磊 → <活动名>。
     * 只剥末尾的已知后缀（活动名里即便含「 · 」也不误伤）；剥不出就用兜底文案。 */
    function pageTitle() {
        var siteName = (panel() && panel().getAttribute('data-site-name')) || '';
        var t = (document.title || '').trim();
        var suffix = ' · ' + siteName;
        if (siteName && t.length > suffix.length && t.slice(-suffix.length) === suffix) {
            t = t.slice(0, -suffix.length).trim();
        }
        if (!t || t === siteName) t = '当前页面';
        return t;
    }

    function rememberConv(id) {
        currentConvId = id;
        // 非 /chat 页面上快捷卡片（每日简报等）的 chatId 以浮窗装载的会话为准
        //（chat-turn.js::currentChatId 在 pathname 命不中时读这个钩子；移动视口侧栏早退不写它）
        window._paSidebarChatId = id;
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
        var bits = [messagesEl(), document.querySelector('[data-mobile-agent-status]'), formEl()];
        for (var i = 0; i < bits.length; i++) { if (bits[i]) bits[i].hidden = open; }
        var btn = document.querySelector('[data-mobile-agent-history]');
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
                if (!historyOpen) return;   // 等待期间用户已切回聊天视图：别把列表写进隐藏面板
                if (hp) hp.innerHTML = d.list_html || '<p class="side-hist-note">还没有历史对话</p>';
                markActive();
            })
            .catch(function () {
                if (hp && historyOpen) hp.innerHTML = '<p class="side-hist-note">历史列表加载失败，请重试</p>';
            });
    }

    function switchTo(convId) {
        if (!convId || String(convId) === String(currentConvId)) { renderHistory(false); return; }
        if (chatTurn) chatTurn.halt();   // 只停轮询，不打断服务端正在跑的本轮
        chatTurn = null;
        mountSidebar(convId);
        renderHistory(false);
    }

    function fit() {
        // 展开瞬间 textarea 可能刚从 hidden 恢复：paFitTextarea 的守卫会跳过隐藏元素，
        // 这里补一次拟合（隐藏容器 scrollHeight 恒 0、placeholder 裁字的老坑）
        var ta = inputEl();
        if (ta && window.paFitTextarea) window.paFitTextarea(ta);
    }

    function setMsgsLoading() {
        var msgs = messagesEl();
        if (msgs) msgs.innerHTML = '<p class="text-center text-xs text-[var(--text-muted)] py-6">正在接续对话…</p>';
    }

    function showMountError() {
        var msgs = messagesEl();
        if (msgs) msgs.innerHTML = '<p class="text-center text-sm text-[var(--text)] font-semibold py-6">装载失败，请重试</p>';
    }

    function installMessages(html) {
        var msgs = messagesEl();
        if (msgs) msgs.innerHTML = html;
        // 历史片段是 JS 插入的：htmx 1.x 没有 MutationObserver，片段里的确认卡/候选卡
        // 表单必须手动接管（innerHTML 整体替换 → 全部是新节点，传 0）
        if (msgs && window.paProcessHtmx) window.paProcessHtmx(msgs, 0);
    }

    function installTurn(d) {
        mounting = false;
        rememberConv(d.conversation_id);
        var base = '/chat/' + d.conversation_id + '/';
        var msgs = messagesEl();
        chatTurn = window.PaChatTurn({
            messagesEl: msgs,
            statusEl: document.querySelector('#mobile-agent-panel [data-turn-status]'),
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
        fit();
    }

    /* 活动页：create-or-get 活动专属会话（服务端自动钉选）→ 再拉历史片段。
     * 与详情页「问 AI 抽屉」同一条链路（for-activity 只回 urls，历史另取 widget-messages）。
     * for-activity 是 json_login_required（401 由 paJsonFetch 统一跳转），它先验明登录态，
     * 随后的 widget-messages（login_required 的 HTML 片段）不会碰到未登录 302。 */
    function mountActivity() {
        mounting = true;
        setMsgsLoading();
        window.paJsonFetch('/chat/for-activity/' + activityId + '/', { cache: 'no-store' })
            .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
            .then(function (res) {
                if (!res.ok || !res.data.conversation_id) throw new Error(res.data.error || '装载失败');
                var d = res.data;
                // widget-messages 是 HTML 片段：paJsonFetch 补的 Accept:application/json 不影响
                // 该视图（它无条件 render HTML），用 .text() 取片段；chrome=drawer 隐藏 @ 钉选误导提示
                return window.paJsonFetch(d.urls.messages + '?chrome=drawer', { cache: 'no-store' })
                    .then(function (r) { return r.text(); })
                    .then(function (html) { installMessages(html); installTurn(d); });
            })
            .catch(function () { mounting = false; showMountError(); });
    }

    /* 非活动页 / 历史点选 / 新话题：页面感知单会话（filter(user) 个人上下文口径）。
     * convId 为空 = 服务端取最新非归档；带 id = 重挂指定会话（别人的/已归档 → JSON 404，
     * 静默回落「最新非归档」重试一次）。 */
    function mountSidebar(convId) {
        mounting = true;
        setMsgsLoading();
        var url = '/chat/sidebar/' + (convId ? '?conversation_id=' + encodeURIComponent(convId) : '');
        window.paJsonFetch(url, { cache: 'no-store' })
            .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
            .then(function (res) {
                if (!res.ok || !res.data.conversation_id) throw new Error(res.data.error || '装载失败');
                installMessages(res.data.messages_html);
                installTurn(res.data);
            })
            .catch(function () {
                mounting = false;
                if (convId) { mountSidebar(null); return; }
                showMountError();
            });
    }

    function mount() {
        if (chatTurn || mounting) return;
        if (activityId) mountActivity();
        else mountSidebar(null);
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
                // 显式按新建会话的 id 挂载（绕过活动页的自动钉选）：新话题是通用会话，
                // 不能走 mount()——那在活动页会又挂回活动专属会话，新建的这条被晾在一边
                mountSidebar(d.conversation_id);
            })
            .catch(function () {
                // 失败不悄悄恢复旧实例：旧 urls 已指向换掉的会话，宁可让用户再点一次
                var msgs = messagesEl();
                if (msgs) msgs.innerHTML = '<p class="text-center text-sm text-[var(--text)] font-semibold py-6">新话题创建失败，请重试</p>';
            });
    }

    function setOpen(open) {
        document.body.classList.toggle('mobile-agent-open', open);
        var f = fab();
        if (f) f.setAttribute('aria-expanded', open ? 'true' : 'false');
        var p = panel();
        if (p) p.setAttribute('aria-hidden', open ? 'false' : 'true');
        var bd = backdrop();
        if (bd) bd.setAttribute('aria-hidden', open ? 'false' : 'true');
        if (open) {
            mount();
            fit();
            var ta = inputEl();
            if (ta) ta.focus();
        }
        // 收起只隐藏不 halt（与侧栏一致）：本轮继续跑，回复落在后台，展开即是最新状态；
        // 写操作完成照样广播 activities:changed，活动页该刷就刷
    }

    // ── 事件绑定（FAB/弹窗随 base.html 常驻 DOM，直接绑不必委托） ──
    var f = fab();
    if (f) f.addEventListener('click', function () { setOpen(true); });
    var bd = backdrop();
    if (bd) bd.addEventListener('click', function () { setOpen(false); });
    var closeBtn = document.querySelector('[data-mobile-agent-close]');
    if (closeBtn) closeBtn.addEventListener('click', function () { setOpen(false); });
    var newBtn = document.querySelector('[data-mobile-agent-new]');
    if (newBtn) newBtn.addEventListener('click', newTopic);
    var histBtn = document.querySelector('[data-mobile-agent-history]');
    if (histBtn) histBtn.addEventListener('click', function () { renderHistory(!historyOpen); });
    // 历史列表是 innerHTML 换进来的动态节点：委托绑在常驻面板容器上，条目随便换
    var hp = historyEl();
    if (hp) hp.addEventListener('click', function (e) {
        var item = e.target.closest('[data-sb-conv-id]');
        if (item) switchTo(item.getAttribute('data-sb-conv-id'));
    });

    // 顶部标题随页面定（不随会话切换变——它表达的是「AI 此刻在哪个页面」）
    var t = titleEl();
    if (t) t.textContent = pageTitle();
})();