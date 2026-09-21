/* 聊天收发流：发送秒返回 + 轮询取结果 + 可停止 + 刷新能续上
 *
 * 为什么要有这个文件：旧实现是一个请求里 sleep 轮询到 AI 回完（最长 90s），
 * 于是用户只能干等、不能取消、不能接着打字，刷新还会丢掉这一轮；同时一个提问
 * 占死一个 gunicorn worker（线上只有 3 个）。后端改成 turn 状态机 + 轮询端点后，
 * 前端必须配套改造，且**分栏页与详情页共用这一份**，否则两处行为必然漂移。
 * （曾与右下角浮窗三方共用；浮窗已于 2026-09 整体下线）
 *
 * 协议约定（AGENTS.md 双协议条）：send/turn/cancel 都是 JSON 端点，一律原生 fetch，
 * 严禁在元素上挂 hx-*。消息内容由服务端渲染成 HTML 片段回传（消息卡片模板只有一份），
 * 前端只负责 append。
 */
(function () {
    'use strict';

    // 后端 phase → 给用户看的进度文案。后端只报事实阶段，措辞留在前端一处，
    // 这样改文案不用动 Python（也便于测试断言 phase 而不是断言中文）
    var PHASE_TEXT = {
        queued: '排队中…',
        session_busy: '上一条还在收尾，稍等…',
        sent: '已发送，正在等 AI…',
        idle_grace: '正在整理回复…',
        finalizing: '正在落地结果…',
        poll_error: '网络抖动，重试中…',
        auto_retry: '回应慢了，自动重试中…'
    };

    var ACTIVE_STATES = ['queued', 'awaiting', 'finalizing'];

    // 写剪贴板：站点是 HTTPS（本地是 localhost），navigator.clipboard 通常可用；
    // 但 http 访问局域网 IP 时它是 undefined，所以必须留 execCommand 降级，
    // 不能失败就默默什么都不发（用户按了复制就是期待剪贴板里有东西）
    function legacyCopy(text) {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '-1000px';
        document.body.appendChild(ta);
        ta.select();
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
        document.body.removeChild(ta);
        return ok;
    }

    function copyToClipboard(text, btn) {
        var flash = function (ok) {
            if (!btn) return;
            btn.textContent = ok ? '已复制' : '复制失败';
            setTimeout(function () { btn.textContent = '复制'; }, 1500);
        };
        if (!text) { flash(false); return; }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(function () { flash(true); },
                                                      function () { flash(legacyCopy(text)); });
        } else {
            flash(legacyCopy(text));
        }
    }

    // 聊天 JSON 请求的唯一出口（send / poll / cancel / 钉选搜索与设置 / 新建会话）
    //
    // 为何要包一层：
    //  1. 统一补 Accept: application/json。视图拿它区分「fetch」与「无 JS 表单提交」，
    //     漏了这个头会被当成无 JS 提交而返回 302（本地实测：消息已落库、轮次已发起，
    //     但前端拿到 HTML → 报「发送失败」，用户再发一次就是重复提问）。五个调用点各写
    //     一遍就必然有漏的那个。
    //  2. 集中处理 401「登录已过期」。未登录的 fetch 如果也拿 302，fetch 会自动跟随重定向，
    //     最终拿到 200 的登录页 HTML，只能当「操作失败」提示；core.utils.json_login_required
    //     改成 401 + login_url 后，这里做一次整页跳转，用户看到的直接就是登录页（带 next）。
    //     redirecting 闸门是因为轮询循环可能连着好几拍都 401，只允许触发一次跳转。
    var redirecting = false;

    function apiFetch(url, init) {
        init = init || {};
        var headers = init.headers || {};
        headers['Accept'] = 'application/json';
        init.headers = headers;
        return fetch(url, init).then(function (r) {
            if (r.status !== 401 || redirecting) return r;
            redirecting = true;
            // login_url 由服务端拼好（含 ?next=），不自己拼 LOGIN_URL：改了登录地址不会说谎。
            // 但 next 必须换掉：服务端的 next 来自 get_full_path()，对 XHR 来说就是接口地址，
            // 登录后会看到一坨 JSON（真机实测），而用户要去的是「当前这一页」。
            return r.json().then(function (d) {
                location.href = reauthTarget(d && d.login_url);
                // 绝不把原响应交回调用链：跳转已在路上，而调用链会把 401 当「发送失败/
                // 创建失败」再提示一次（真机实测：新建对话会闪一个 alert），用户看上去
                // 像两个错，且提示马上会被翻页掉
                return new Promise(function () {});
            }, function () {
                location.href = reauthTarget('');
                return new Promise(function () {});
            });
        });
    }

    function reauthTarget(loginUrl) {
        var here = location.pathname + location.search;
        if (!loginUrl) return '/accounts/login/?next=' + encodeURIComponent(here);
        try {
            var u = new URL(loginUrl, location.href);
            u.searchParams.set('next', here);
            return u.pathname + u.search;
        } catch (err) {
            return loginUrl;                    // 老浏览器没有 URL：用服务端给的地址总比卡死强
        }
    }

    // 宿主页（base.html 的新建对话）也用同一个出口，否则它会靠假的 HX-Request 头骗视图返回 JSON
    window.paJsonFetch = apiFetch;

    /* 「下一步」chips 收敛：把空间还给消息流。
     * 两条规则（都是视觉收纳，不碰发送委托 [data-followup] 的行为）：
     *  ① 历史消息的 chips 整组加 .follow-ups-folded 藏起，只留最新一条 ——
     *     回复滚过一屏后旧建议没有再点的意义，却一路占着屏；
     *  ② 单条超过 2 个的：第 3 个起先 hidden，加一颗「还有 N 条建议…」
     *     展开钮（不带 data-followup，不会被委托误发）。
     * append() 每次新增消息后重跑一遍，页面加载完也跑一遍（详情页共用本文件，
     * 无需各模板自己接线）。无 JS 时 chips 全部可见，渐进增强。 */
    window.paTidyFollowUps = function (root) {
        var scope = root && root.querySelectorAll ? root : document;
        var groups = scope.querySelectorAll('[data-follow-ups]');
        Array.prototype.forEach.call(groups, function (group, i) {
            var chips = group.querySelectorAll('[data-followup]');
            Array.prototype.forEach.call(chips, function (chip, j) {
                if (j >= 2) chip.classList.add('hidden');
            });
            if (chips.length > 2 && !group.querySelector('[data-followups-more]')) {
                var more = document.createElement('button');
                more.type = 'button';
                more.className = 'chat-chip tap-target follow-up';
                more.setAttribute('data-followups-more', '');
                more.textContent = '还有 ' + (chips.length - 2) + ' 条建议…';
                more.addEventListener('click', function () {
                    Array.prototype.forEach.call(
                        group.querySelectorAll('[data-followup].hidden'),
                        function (c) { c.classList.remove('hidden'); });
                    more.remove();
                });
                group.appendChild(more);
            }
            // 文档序即消息序：最后一组是最新的回复，只有它保持展开
            group.classList.toggle('follow-ups-folded', i !== groups.length - 1);
        });
    };

    window.PaChatTurn = function (opts) {
        var messagesEl = opts.messagesEl;
        var statusEl = opts.statusEl;
        var form = opts.form;
        var input = opts.input;
        var urls = opts.urls;                       // function() -> {send, poll, cancel}
        var intervalMs = opts.intervalMs || 1500;
        var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
        var sendBtn = form ? form.querySelector('button[type="submit"]') : null;
        var timer = null;
        var ticking = false;                        // 上一拍没回来就不叠加发请求
        var ceiling = 0;                            // 兜底轮次上限（服务端 TTL 才是权威裁决）
        var running = false;

        function api() { return urls() || {}; }

        function statusTextEl() {
            return statusEl ? statusEl.querySelector('[data-turn-text]') : null;
        }

        function showStatus(text) {
            if (!statusEl) return;
            var el = statusTextEl();
            if (el && text) el.textContent = text;
            statusEl.classList.toggle('hidden', !text);
        }

        function setPhase(phase) {
            showStatus(PHASE_TEXT[phase] || PHASE_TEXT.sent);
        }

        function setBusy(on) {
            running = !!on;
            // 只锁「发送」按钮，输入框保持可编辑：能边等边打下一条是这次改造的目的之一
            if (sendBtn) sendBtn.disabled = !!on;
            if (!on) showStatus('');
        }

        function append(html) {
            if (!html) return;
            messagesEl.insertAdjacentHTML('beforeend', html);
            // 新增节点里的 hx-* 由 htmx 自带的 MutationObserver 处理，
            // 这里绝不能再调 htmx.process()（会双重绑定、旧节点引用残留）
            if (window.paTidyFollowUps) window.paTidyFollowUps(messagesEl);
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }

        function stopLoop() {
            if (timer) { clearTimeout(timer); timer = null; }
            ticking = false;
        }

        function schedule(ttl) {
            if (timer) clearTimeout(timer);
            timer = setTimeout(tick, intervalMs);
        }

        function tick() {
            var u = api();
            if (!u.poll) return;
            if (ticking) { schedule(); return; }
            ticking = true;
            apiFetch(u.poll, { cache: 'no-store' })
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    ticking = false;
                    if (d.state === 'processing') {
                        setPhase(d.phase);
                        // 自动重试（服务端把本轮拉回 queued 重发）：预算重算 ——
                        // 重试是服务端显式裁决的续期，享有完整 TTL；与服务端一直
                        // 不裁决时 ceiling 慢慢减到 0 的兕底不冲突
                        if (d.phase === 'auto_retry') {
                            ceiling = Math.ceil(((d.ttl || 180) + 60) / (intervalMs / 1000));
                        }
                        // ceiling 只在 start() 里算一次：它是「服务端一直不裁决」的兕底，
                        // 每拍重算就永远达不到（服务端只要还在应答，它总会在 TTL 到时给出 error）
                        if (--ceiling > 0) { schedule(); return; }
                        // 兜底：服务端一直没裁决（理论上不会）就停止轮询，交给下次刷新恢复
                        setBusy(false);
                        showStatus('这一轮还没有结束，稍后回到本页可继续查看');
                        return;
                    }
                    stopLoop();
                    append(d.html);                 // done → 回复片段；error → 服务端渲染的中断气泡
                    setBusy(false);
                    if (d.changed && opts.onActivityChanged) opts.onActivityChanged();
                })
                .catch(function () {
                    ticking = false;
                    // 轮询失败不算本轮失败（平台抽风/网络抖动），继续轮到服务端定论为止
                    setPhase('poll_error');
                    if (ceiling > 0) { ceiling -= 1; schedule(); }
                });
        }

        function start(ttl) {
            ceiling = Math.ceil(((ttl || 180) + 60) / (intervalMs / 1000));
            stopLoop();
            schedule(ttl);
        }

        function clearDraft(content) {
            // 只在输入框仍是刚发出去那段话时清空：用户等回复期间又改了内容就不能覆盖
            if (input && input.value.trim() === content) {
                input.value = '';
                if (window.paFitTextarea) window.paFitTextarea(input);
            }
        }

        function restoreDraft(content) {
            if (input && !input.value.trim()) {
                input.value = content;
                if (window.paFitTextarea) window.paFitTextarea(input);
            }
        }

        function send(content) {
            content = (content || '').trim();
            if (!content) return Promise.resolve(false);
            var u = api();
            if (!u.send) {
                // 没选对话时给一句提示：旧实现是默默 return，用户点了发送什么也没发生
                showStatus(opts.noConversationHint || '先选一个对话');
                return Promise.resolve(false);
            }
            if (running) { showStatus('上一条还在处理中，等它回完或先点「停止」'); return Promise.resolve(false); }

            var body = new URLSearchParams();
            body.set('content', content);
            var extra = opts.extraParams ? (opts.extraParams() || {}) : {};
            Object.keys(extra).forEach(function (k) { body.set(k, extra[k]); });

            setBusy(true);
            setPhase('sent');
            return apiFetch(u.send, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': csrf,
                    'Content-Type': 'application/x-www-form-urlencoded'
                    // Accept 由 apiFetch 统一补（漏了会被当成无 JS 提交返 302）
                },
                body: body.toString()
            })
                .then(function (r) {
                    return r.json().then(function (d) { return { status: r.status, ok: r.ok, d: d }; })
                        .catch(function () { return { status: r.status, ok: false, d: {} }; });
                })
                .then(function (res) {
                    if (res.status === 409) {
                        // 服务端说上一条还在跑：草稿原样留在输入框，绝不静默丢字
                        setBusy(false);
                        showStatus(res.d.error || '上一条还在处理中');
                        return false;
                    }
                    if (!res.ok) {
                        setBusy(false);
                        showStatus(res.d.error || '发送失败，请重试');
                        restoreDraft(content);
                        return false;
                    }
                    clearDraft(content);
                    append(res.d.html);             // 用户消息立刻上屏（旧实现是等 AI 回完才一起出现）
                    start(res.d.ttl);
                    return true;
                })
                .catch(function () {
                    setBusy(false);
                    showStatus('网络异常，消息没发出去');
                    restoreDraft(content);
                    return false;
                });
        }

        // 停止本轮：请服务端取消并落一条「已停止」的消息（气泡由服务端渲染，前端不复制一份）
        function stop() {
            var u = api();
            if (!u.cancel) return Promise.resolve();
            stopLoop();
            showStatus('正在停止…');
            return apiFetch(u.cancel, { method: 'POST', headers: { 'X-CSRFToken': csrf } })
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    append(d.html);
                    setBusy(false);
                    // 只在真写过数据时通知宿主页面：无条件广播会让点「停止」后弹出
                    // 「活动数据已更新」（实测就是这个），用户无从判断到底改了什么
                    if (d.changed && opts.onActivityChanged) opts.onActivityChanged();
                })
                .catch(function () { setBusy(false); });
        }

        // 刷新后续上：服务端说这一轮还在跑就重新起循环，气泡不重复渲染
        function resume(state, ttl) {
            if (ACTIVE_STATES.indexOf(state) === -1) { setBusy(false); return false; }
            setBusy(true);
            setPhase('queued');
            start(ttl || 180);
            return true;
        }

        if (form) {
            form.addEventListener('submit', function (e) {
                e.preventDefault();
                send(input ? input.value : '');
            });
        }
        if (statusEl) {
            statusEl.addEventListener('click', function (e) {
                if (e.target && e.target.closest && e.target.closest('[data-turn-cancel]')) stop();
            });
        }
        // 消息区的委托交互：「重试」（中断气泡）与「复制」（AI 回复）都在这一层
        messagesEl.addEventListener('click', function (e) {
            if (!e.target || !e.target.closest) return;
            var btn = e.target.closest('[data-retry-text]');
            if (btn) {
                var text = btn.getAttribute('data-retry-text');
                var holder = btn.closest('.chat-message');
                if (holder) holder.remove();        // 旧的「已中断」气泡撤掉，避免和新一轮并排
                send(text);
                return;
            }
            var copy = e.target.closest('[data-copy-msg]');
            if (copy) {
                // 复制渲染后的可读文本而不是 Markdown 源文：用户复制是为了直接粘到
                // 微信/备忘录里，带一堆星号和竖线等于没复制
                var box = copy.closest('.chat-message');
                var body = box && box.querySelector('.md-body');
                copyToClipboard(body ? (body.innerText || body.textContent) : '', copy);
                return;
            }
            // 「下一步」chips：把这一句直接发出去（与「重试」同一条 send，不叉第二套）
            var follow = e.target.closest('[data-followup]');
            if (follow) {
                send(follow.getAttribute('data-followup'));
            }
        });

        // 只停轮询、不取消服务端的本轮：分栏页切到另一个对话时用。
        // 不单独供这个口子就会误用 stop()：切个对话把 AI 正在跑的那轮真停了。
        function halt() {
            stopLoop();
            setBusy(false);
        }

        return { send: send, stop: stop, resume: resume, halt: halt,
                 isBusy: function () { return running; } };
    };


    /* @ 钉选：把某个活动钉在本对话上（会话级），之后每一轮自动带上它的结构化现状
     *
     * 为什么状态存服务端而不是前端：钉选要扛刷新、要在详情页与浮窗之间一致，
     * 还要能进 conversation.turn_prompt（重试发送时必须拿到同一份文本）。
     * 前端只负责「选哪个」，不拼 DOM 结构也不拼字符串：候选列表用 createElement +
     * textContent 构建（活动名是用户数据，走字符串拼接就给自己埋了 XSS）。
     */
    window.PaChatPin = function (opts) {
        var host = opts.host;
        var input = opts.input;
        var urls = opts.urls;              // function() -> {set, search}
        var noop = { paint: function () {}, close: function () {} };
        if (!host || !input) return noop;
        var slot = host.querySelector('[data-pin-slot]');
        var list = host.querySelector('[data-pin-list]');
        var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
        var timer = null;
        var seq = 0;                       // 只认最后一次请求的结果，慢响应不得盖掉新结果

        function api() { return urls() || {}; }

        function paint(html) { if (slot) slot.innerHTML = html || ''; }

        function close() {
            if (!list) return;
            list.hidden = true;
            list.innerHTML = '';
        }

        // 光标前是不是「@关键词」：@ 必须在行首或空白/括号之后，邮箱里的 @ 不算
        function atToken() {
            var before = input.value.slice(0, input.selectionStart || 0);
            var m = /@([^\s@]{0,20})$/.exec(before);
            if (!m) return null;
            var at = before.length - m[0].length;
            if (at > 0 && !/[\s(（]/.test(before.charAt(at - 1))) return null;
            return m[1];
        }

        function stripAtToken() {
            var pos = input.selectionStart || 0;
            var before = input.value.slice(0, pos);
            var m = /@([^\s@]{0,20})$/.exec(before);
            if (!m) return;
            // 只删 @ 与它后面的关键词，前面那个空格留着（它就是词间分隔）
            input.value = input.value.slice(0, pos - m[0].length) + input.value.slice(pos);
            var caret = pos - m[0].length;
            if (input.setSelectionRange) input.setSelectionRange(caret, caret);
            if (window.paFitTextarea) window.paFitTextarea(input);
        }

        function renderCandidates(items) {
            list.innerHTML = '';
            if (!items.length) {
                var empty = document.createElement('p');
                empty.className = 'pin-empty';
                empty.textContent = '没有匹配的活动';
                list.appendChild(empty);
            }
            items.forEach(function (it) {
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'pin-item';
                btn.setAttribute('data-pin-id', it.id);
                var name = document.createElement('span');
                name.className = 'pin-item-name';
                name.textContent = it.name;
                var meta = document.createElement('span');
                meta.className = 'pin-item-meta';
                meta.textContent = it.meta || '';
                btn.appendChild(name);
                btn.appendChild(meta);
                list.appendChild(btn);
            });
            list.hidden = false;
        }

        function load(q) {
            var u = api();
            if (!u.search || !list) return;
            var mine = ++seq;
            apiFetch(u.search + '?q=' + encodeURIComponent(q), { cache: 'no-store' })
                .then(function (r) { return r.ok ? r.json() : { candidates: [] }; })
                .then(function (d) { if (mine === seq) renderCandidates(d.candidates || []); })
                .catch(function () { if (mine === seq) close(); });
        }

        // 失败不弹框（全站约定）：在槽里插一条自消提示，只撤自己插的那条，
        // 不整体回滚 innerHTML（期间可能已被 paint）
        function flash(text) {
            if (!slot) return;
            var tip = document.createElement('span');
            // pin-error 只是测试钩子，配色沿用全站已有的 text-red-600（模板里已在用）
            tip.className = 'pin-error text-xs text-red-600';
            tip.textContent = text;
            slot.appendChild(tip);
            setTimeout(function () { tip.remove(); }, 2500);
        }

        function setActivity(id) {
            var u = api();
            if (!u.set) { flash('先选一个对话'); return; }
            var body = new URLSearchParams();
            body.set('activity_id', id || '');
            apiFetch(u.set, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': csrf,
                    'Content-Type': 'application/x-www-form-urlencoded'
                    // Accept 同样交给 apiFetch：与 send 同口径，漏了会被当成无 JS 提交返 302
                },
                body: body.toString()
            })
                .then(function (r) {
                    return r.json().then(function (d) { return { ok: r.ok, d: d }; },
                                         function () { return { ok: false, d: {} }; });
                })
                .then(function (res) {
                    if (!res.ok) { flash((res.d && res.d.error) || '钉选失败'); return; }
                    close();
                    paint(res.d.html);
                })
                .catch(function () { flash('钉选失败，请重试'); });
        }

        input.addEventListener('input', function () {
            if (timer) clearTimeout(timer);
            var token = atToken();
            if (token === null) { close(); return; }
            timer = setTimeout(function () { load(token); }, 250);
        });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') close();
        });
        if (list) {
            // 候选项上抢在 blur 前拦住默认行为：否则输入框先 blur、点选来不及
            list.addEventListener('mousedown', function (e) { e.preventDefault(); });
            list.addEventListener('click', function (e) {
                var item = e.target.closest && e.target.closest('[data-pin-id]');
                if (!item) return;
                stripAtToken();                 // @xxx 已经变成钉选，留在文本里就是重复
                setActivity(item.getAttribute('data-pin-id'));
            });
        }
        if (slot) {
            slot.addEventListener('click', function (e) {
                if (e.target.closest && e.target.closest('[data-pin-clear]')) setActivity('');
            });
        }

        return { paint: paint, close: close };
    };

    /* 非 AI 快捷卡片与卡片内操作的统一委托（分栏页 / 详情页共用这一份，document 级）。
       [data-local-card] → POST /chat/<id>/local-card/ 直出卡片（零 token、不碰 turn 锁），
         chatId 从 location.pathname 解析：分栏页切对话会 replaceState 成 /chat/<id>/，
         详情页是 /chat/<id>/detail/，两个形态都被同一条正则覆盖。
       [data-quick-open] → 调 base.html 暴露的 paQuickOpen(tab) 打开快记面板
         （daily 卡片内的「新建活动 / 记一笔」走这条，对话式创建不跳页）。 */
    function currentChatId() {
        var m = /\/chat\/(\d+)/.exec(location.pathname);
        return m ? m[1] : null;
    }

    function flashLocalError(btn, text) {
        var tip = document.createElement('span');
        tip.className = 'text-xs text-red-600';
        tip.textContent = text;
        btn.insertAdjacentElement('afterend', tip);
        setTimeout(function () { if (tip.parentNode) tip.parentNode.removeChild(tip); }, 2500);
    }

    function insertLocalHtml(html) {
        var box = document.getElementById('split-messages') || document.getElementById('messages');
        if (!box) return;
        box.insertAdjacentHTML('beforeend', html);
        box.scrollTop = box.scrollHeight;
        if (window.paTidyFollowUps) window.paTidyFollowUps(box);
    }

    document.addEventListener('click', function (e) {
        var quick = e.target.closest && e.target.closest('[data-quick-open]');
        if (quick) {
            if (typeof window.paQuickOpen === 'function') window.paQuickOpen(quick.getAttribute('data-quick-open'));
            return;
        }
        var btn = e.target.closest && e.target.closest('[data-local-card]');
        if (!btn) return;
        var chatId = currentChatId();
        if (!chatId) { flashLocalError(btn, '先选择一个对话'); return; }
        var meta = document.querySelector('meta[name="csrf-token"]');
        // 端点读 request.POST（表单体），发 urlencoded 而不是 JSON
        apiFetch('/chat/' + chatId + '/local-card/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': meta ? meta.getAttribute('content') : '' },
            body: 'card=' + encodeURIComponent(btn.getAttribute('data-local-card'))
        }).then(function (r) {
            if (!r.ok) throw new Error('http ' + r.status);
            return r.json();
        }).then(function (d) {
            if (!d || !d.html) throw new Error('empty');
            insertLocalHtml(d.html);
        }).catch(function () {
            flashLocalError(btn, '插入失败，请重试');
        });
    });

    // 页面加载完：① 收一遍历史里已有的 chips（详情页 / 分栏页首屏都靠这条覆盖）；
    // ② 撤掉快捷按钮行的初始 hidden —— 分栏页有自己的 updateLocalCards，但详情页
    // 没有那段 JS，不在这里统一撤的话详情页的「daily」按钮永远点不了（线上实测）
    function onReady() {
        window.paTidyFollowUps(document);
        if (currentChatId()) {
            var row = document.getElementById('local-cards');
            if (row) row.classList.remove('hidden');
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', onReady);
    } else {
        onReady();
    }
})();
