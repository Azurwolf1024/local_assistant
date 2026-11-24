/* 文字对话面板：打字 → 服务走完整链路 → 文字回来 + 声音由服务放出。
   ★可以选角色★：选了谁，这句话就用谁的设定与声线回答。角色是**跟着这条消息一起**
   发过去的（不是「先切角色再问」），免得中间被语音唤醒或另一个页面换走。

   ★聊天记录（2026-11-24）★
   - 一个角色一份文件：`sessions/console/chat/<角色id>.jsonl`。切角色就切记录。
   - 「跟随服务当前角色」= 服务此刻那个人；它换人（语音唤醒/日志里的 [角色]）就跟着换。
   - 不一次性加载：先给最近一页（PAGE 条），往上滚到底再要更早的一页，且**不跳位置**。
   - ★隔离靠「显式 character」★：每次请求都带上「我要看/我要问的是谁」这个 id，
     绝不靠「后端自己猜」。切换时用序号丢弃过期响应（晚回来的旧请求不会覆盖新记录）。 */
(function () {
  let handleEvent = null;
  let refreshTimer = null;      // 「每 30 秒轻刷一次」的定时器（离开面板要清掉）
  const LS_KEY = "console.chat.character";   // 记住上次选的，刷新后还在
  const PAGE = 40;

  window.Console.register("chat", {
    render(root, ctx) {
      const { h, card, secs } = UI;
      let busy = false;
      let loading = false;
      let seq = 0;                // 每次 reload +1；晚到的旧响应直接丢掉
      let characters = [];
      let serviceWho = "";        // 服务当前是谁（名字）
      let serviceCid = "";        // 服务当前角色 id
      // 当前**正在展示**的那一份记录
      let viewCid = "";
      let items = [];             // 已加载的条目（旧 → 新）
      let total = 0;
      let nextBefore = 0;         // 已加载的最早那条在整个文件里的下标
      let hasMore = false;

      const box = h("div", { class: "chat" });
      const input = h("input", { placeholder: "打一句话，按回车发送（她会念出来）" });
      const hint = h("span", { class: "muted", text: "" });
      const viewLabel = h("span", { class: "muted", text: "" });
      const counter = h("span", { class: "muted", text: "" });
      const picker = h("select", { title: "用哪个角色回答（也决定看谁的聊天记录）" });
      const pickerWrap = h("label", { class: "row", style: "gap:6px;width:auto" }, [
        h("span", { class: "muted", text: "角色" }), picker,
      ]);
      const sendBtn = h("button", { class: "btn primary", text: "发送" });
      const sayBtn = h("button", { class: "btn", text: "只念不答" });
      const syncBtn = h("button", { class: "btn", text: "同步" });
      const who_label = h("span", { class: "muted", text: "" });

      function remembered() {
        try { return localStorage.getItem(LS_KEY) || ""; } catch (_e) { return ""; }
      }
      function remember(v) {
        try { v ? localStorage.setItem(LS_KEY, v) : localStorage.removeItem(LS_KEY); } catch (_e) {}
      }
      function labelOf(id) {
        const hit = characters.find((c) => c.id === id);
        return hit ? hit.name : (id || "");
      }

      /** 现在要「看/问」的是谁：显式选了就选他，否则跟随服务当前角色。 */
      function targetCid() {
        return picker.value || serviceCid || "";
      }

      function fillPicker() {
        const keep = picker.value || remembered();
        picker.innerHTML = "";
        picker.appendChild(h("option", { value: "", text: "跟随服务当前角色" }));
        characters.forEach((c) => {
          picker.appendChild(h("option", {
            value: c.id,
            text: c.name + (c.enabled === false ? "（未启用）" : "")
              + (serviceCid && c.id === serviceCid ? " · 现在是她" : ""),
          }));
        });
        // ★别把用户选的覆盖掉★：还在就恢复，不在（角色被删/改名）才回到「跟随」
        picker.value = (keep && characters.some((c) => c.id === keep)) ? keep : "";
      }

      function updateHint() {
        const v = picker.value;
        if (v) {
          hint.textContent = "这句话会用「" + labelOf(v) + "」的设定与声线；显示的是她的聊天记录";
        } else {
          hint.textContent = serviceWho
            ? "跟随服务当前角色（现在是 " + serviceWho + "）—— 显示她的聊天记录"
            : "";
        }
      }

      function updateLabels() {
        const name = labelOf(viewCid) || viewCid || "（未定）";
        viewLabel.textContent = "正在看：" + name;
        who_label.textContent = serviceWho ? "服务现在：" + serviceWho : "服务没在跑";
        if (!total) { counter.textContent = ""; return; }
        const base = "已加载 " + items.length + " / 共 " + total + " 条";
        counter.textContent = hasMore ? base + " · 往上滚动加载更早的" : base + " · 已经到最早";
      }

      root.appendChild(card("文字对话", [
        h("div", { class: "row", style: "align-items:center;gap:10px;margin-bottom:6px" }, [
          viewLabel, who_label, counter,
        ]),
        box,
        h("div", { class: "row", style: "margin-top:10px" }, [
          h("div", { class: "grow" }, input),
          sendBtn,
          sayBtn,
          pickerWrap,
          syncBtn,
          h("button", {
            class: "btn ghost", text: "清屏",
            onclick: async () => {
              const who = targetCid();
              await ctx.api.safe(() => ctx.api.post("/api/chat/clear", { character: who }));
              await reload();
            },
          }),
        ]),
        h("p", { class: "muted", text: "「发送」= 技能/工具 → LLM → 说出声（最慢）；「只念不答」= 直接念这句（最快，验证声线用）。选角色只影响这句话，不会改别的面板。" }),
        hint,
      ]));

      function msgNode(m) {
        const meta = [];
        if (m.at) meta.push(m.at);
        if (m.character) meta.push(m.character);
        if (m.seconds !== null && m.seconds !== undefined) meta.push(secs(m.seconds));
        if (m.detail && m.detail.total_seconds) meta.push("LLM+出声共 " + secs(m.detail.total_seconds));
        return h("div", { class: "msg " + (m.role === "me" ? "me" : "bot") }, [
          h("div", { text: m.text }),
          meta.length ? h("span", { class: "meta", text: meta.join(" · ") }) : null,
        ]);
      }

      function emptyNote() {
        return h("p", { class: "muted", text: "还没有对话。注意：这里打字不经过麦克风，但回答会从扬声器说出来。" });
      }

      function paintAll() {
        box.innerHTML = "";
        if (!items.length) box.appendChild(emptyNote());
        const frag = document.createDocumentFragment();
        items.forEach((m) => frag.appendChild(msgNode(m)));
        box.appendChild(frag);
        box.scrollTop = box.scrollHeight;
        updateLabels();
      }

      function pushPair(entries) {
        const note = box.querySelector("p.muted");
        if (note) note.remove();
        entries.forEach((m) => {
          if (!m) return;
          items.push(m);
          total += 1;
          nextBefore = total - items.length;   // 最早那条在整个文件里的下标
          box.appendChild(msgNode(m));
        });
        box.scrollTop = box.scrollHeight;
        updateLabels();
      }

      async function loadOlder() {
        if (loading || !hasMore) return;
        loading = true;
        const mySeq = seq;
        const prevH = box.scrollHeight;
        const prevTop = box.scrollTop;
        const note = h("p", { class: "muted", text: "正在加载更早的…" });
        box.insertBefore(note, box.firstChild);
        try {
          const d = await ctx.api.get(
            "/api/chat/history?character=" + encodeURIComponent(viewCid)
            + "&limit=" + PAGE + "&before=" + nextBefore
          );
          if (mySeq !== seq) return;           // 期间换过角色：这一页作废
          const older = (d && d.items) || [];
          if (older.length) {
            const frag = document.createDocumentFragment();
            older.forEach((m) => frag.appendChild(msgNode(m)));
            box.insertBefore(frag, note);
            items = older.concat(items);
            nextBefore = d.next_before;
            hasMore = !!d.has_more;
            total = d.total || total;
          } else {
            hasMore = false;
          }
        } catch (_e) {
          /* 加载失败就保持原样，下次滚动再试 */
        } finally {
          if (mySeq === seq) {
            note.remove();
            // 把视口钉在原来那条上，别让内容“跳”一下
            box.scrollTop = box.scrollHeight - prevH + prevTop;
            updateLabels();
          }
          loading = false;
        }
      }

      async function reload() {
        const mySeq = ++seq;
        const want = targetCid();
        items = [];
        total = 0;
        nextBefore = 0;
        hasMore = false;
        viewCid = want;                 // 先按目标显示，避免闪一下旧角色
        loading = true;
        box.innerHTML = "";
        try {
          const d = await ctx.api.get(
            "/api/chat/history?character=" + encodeURIComponent(want) + "&limit=" + PAGE
          );
          if (mySeq !== seq) return;    // 晚回来的旧请求：丢掉，别覆盖新记录
          viewCid = d.character || want;
          items = d.items || [];
          total = d.total || items.length;
          nextBefore = d.next_before;
          hasMore = !!d.has_more;
        } catch (err) {
          if (mySeq !== seq) return;
          ctx.toast("读聊天记录失败：" + err.message, "err");
        } finally {
          if (mySeq === seq) {
            loading = false;
            paintAll();
          }
        }
      }

      async function send(kind) {
        const text = input.value.trim();
        if (!text || busy) return;
        busy = true;
        sendBtn.disabled = sayBtn.disabled = true;
        input.value = "";
        hint.textContent = kind === "ask" ? "服务正在处理（可能要十几秒）…" : "正在念…";
        let r = null;
        try {
          r = await ctx.api.safe(() => ctx.api.post(
            kind === "ask" ? "/api/chat/ask" : "/api/chat/say",
            { text, character: targetCid() },
          ));
        } finally {
          busy = false;
          sendBtn.disabled = sayBtn.disabled = false;
        }
        if (r && !r.ok) ctx.toast("没成：" + (r.error || "未知错误"), "err", 12000);
        // 回来的那份记录就是当前看的这份才追加；换人了 / 不是这份 → 重载
        if (r && r.entry && r.character && r.character === viewCid) {
          pushPair([r.entry]);
        } else {
          await reload();
        }
        updateHint();
      }

      sendBtn.onclick = () => send("ask");
      sayBtn.onclick = () => send("say");
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") send("ask"); });

      function atBottom() {
        return box.scrollTop + box.clientHeight >= box.scrollHeight - 60;
      }

      async function syncNow() {
        if (busy) return;
        syncBtn.disabled = true;
        const oldText = syncBtn.textContent;
        syncBtn.textContent = "同步中…";
        const r = await ctx.api.safe(() => ctx.api.post("/api/chat/sync", { timeout: 120 }));
        syncBtn.disabled = false;
        syncBtn.textContent = oldText;
        if (r && r.ok) {
          ctx.toast("已同步：" + (r.message || "ok"), "ok", 4000);
          await reload();
        } else {
          ctx.toast("同步失败：" + ((r && (r.message || r.error)) || "服务没在跑？"), "err", 9000);
        }
      }
      syncBtn.onclick = syncNow;

      // 每 30 秒轻刷一次：服务那边定时拉云端（[memory] sync_pull_seconds），
      // 拉下来的新记录只要刷新就能看见。★只在「在底部」时重载★，免得把你正在
      // 往上翻的旧记录冲掉；想看最新的手动点「同步」。
      clearInterval(refreshTimer);
      refreshTimer = setInterval(() => {
        if (busy || loading) return;
        if (atBottom()) reload();
      }, 30000);

      // 往上滚到顶 → 再要一页
      box.addEventListener("scroll", () => {
        if (box.scrollTop <= 24) loadOlder();
      });

      function refreshWho() {
        return ctx.api.post("/api/overview/ping", {})
          .then((r) => {
            if (r && r.ok && r.data) {
              serviceWho = r.data.character_name || "";
              serviceCid = r.data.character_id || "";
            }
          })
          .catch(() => {})
          .then(() => {
            fillPicker();
            updateHint();
            updateLabels();
          });
      }

      ctx.api.get("/api/chat/characters")
        .then((d) => {
          characters = d.items || [];
          if (d.error) ctx.toast("角色文件有问题：" + d.error, "warn", 12000);
          fillPicker();
          updateHint();
        })
        .catch((err) => ctx.toast("读角色列表失败：" + err.message, "err"))
        .finally(() => { reload(); refreshWho(); });

      picker.onchange = () => {
        remember(picker.value || "");
        updateHint();
        reload();                      // ★切角色 = 切记录★（序号会作废在途的旧请求）
      };

      // 服务被关掉 / 语音里换了角色 → 刷新「现在是谁」；跟随模式下记录也跟着换
      handleEvent = (ev) => {
        if (ev.kind === "status" && ev.data && !ev.data.running) {
          serviceWho = "";
          serviceCid = "";
          updateLabels();
          hint.textContent = "服务没在跑——先点右上角「启动服务」";
        }
        if (ev.kind === "log" && ev.data && /\[角色\]/.test(ev.data.line || "")) {
          clearTimeout(refreshWho._t);
          refreshWho._t = setTimeout(() => {
            const before = serviceCid;
            refreshWho().then(() => {
              if (!picker.value && serviceCid && serviceCid !== before) reload();
            });
          }, 1200);
        }
      };
    },

    onEvent(ev, ctx) { if (handleEvent) handleEvent(ev, ctx); },
    onLeave() { handleEvent = null; clearInterval(refreshTimer); refreshTimer = null; },
  });
})();
