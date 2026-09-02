/* 聊天收发流：发送秒返回 + 轮询取结果 + 可停止 + 刷新能续上
 *
 * 为什么要有这个文件：旧实现是一个请求里 sleep 轮询到 AI 回完（最长 90s），
 * 于是用户只能干等、不能取消、不能接着打字，刷新还会丢掉这一轮；同时一个提问
 * 占死一个 gunicorn worker（线上只有 3 个）。后端改成 turn 状态机 + 轮询端点后，
 * 前端必须配套改造，且**详情页与右下角浮窗共用这一份**，否则两处行为必然漂移。
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
        poll_error: '网络抖动，重试中…'
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

    window.PaChatTurn = function (opts) {
        var messagesEl = opts.messagesEl;
        var statusEl = opts.statusEl;
        var chipsEl = opts.chipsEl;                 // 常驻快捷指令容器（可缺省）
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
            fetch(u.poll, { headers: { 'Accept': 'application/json' }, cache: 'no-store' })
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    ticking = false;
                    if (d.state === 'processing') {
                        setPhase(d.phase);
                        // ceiling 只在 start() 里算一次：它是「服务端一直不裁决」的兜底，
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
            return fetch(u.send, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': csrf,
                    'Content-Type': 'application/x-www-form-urlencoded',
                    // 必须显式要 JSON：视图拿 Accept 区分「fetch」与「无 JS 表单提交」，
                    // 漏这个头会被当成无 JS 提交而返回 302（本地实测：消息已落库、轮次已发起，
                    // 但前端拿到的是 HTML → 报「发送失败」，用户再发一次就是重复提问）
                    'Accept': 'application/json'
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
            return fetch(u.cancel, { method: 'POST', headers: { 'X-CSRFToken': csrf, 'Accept': 'application/json' } })
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

        // 刷新/重开面板后续上：服务端说这一轮还在跑就重新起循环，气泡不重复渲染
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

        if (chipsEl) {
            chipsEl.addEventListener('click', function (e) {
                var chip = e.target.closest && e.target.closest('[data-chip]');
                if (!chip || !input) return;
                var text = chip.getAttribute('data-chip') || '';
                // 有草稿时追加而不是覆盖：快捷指令不能吃掉用户已经打好的字
                input.value = input.value.trim() ? input.value.replace(/\s+$/, '') + '\n' + text : text;
                input.focus();
                if (window.paFitTextarea) window.paFitTextarea(input);
            });
        }

        // 只停轮询、不取消服务端的本轮：浮窗切到另一个对话时用。
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
            fetch(u.search + '?q=' + encodeURIComponent(q),
                  { headers: { 'Accept': 'application/json' }, cache: 'no-store' })
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
            fetch(u.set, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': csrf,
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': 'application/json'      // 与 send 同口径：漏了会被当成无 JS 提交返 302
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
})();
