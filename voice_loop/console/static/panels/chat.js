/* 文字对话面板：打字 → 服务走完整链路 → 文字回来 + 声音由服务放出。
   ★可以选角色★：选了谁，这句话就用谁的设定与声线回答。角色是**跟着这条消息一起**
   发过去的（不是「先切角色再问」），免得中间被语音唤醒或另一个页面换走。
   历史只存在控制台内存里（刷新就没），真正的对话记忆在服务的 llm 里。 */
(function () {
  let handleEvent = null;
  const LS_KEY = "console.chat.character";   // 记住上次选的，刷新后还在

  window.Console.register("chat", {
    render(root, ctx) {
      const { h, card, secs } = UI;
      let busy = false;
      let characters = [];
      let serviceWho = "";        // 服务当前是谁（ping 回来）

      const box = h("div", { class: "chat" });
      const input = h("input", { placeholder: "打一句话，按回车发送（她会念出来）" });
      const hint = h("span", { class: "muted", text: "" });
      const picker = h("select", { title: "用哪个角色回答" });
      const pickerWrap = h("label", { class: "row", style: "gap:6px;width:auto" }, [
        h("span", { class: "muted", text: "角色" }), picker,
      ]);
      const sendBtn = h("button", { class: "btn primary", text: "发送" });
      const sayBtn = h("button", { class: "btn", text: "只念不答" });

      function remembered() {
        try { return localStorage.getItem(LS_KEY) || ""; } catch (_e) { return ""; }
      }
      function chosen() {
        const v = picker.value || "";
        try { v ? localStorage.setItem(LS_KEY, v) : localStorage.removeItem(LS_KEY); } catch (_e) {}
        return v;
      }

      function fillPicker() {
        const keep = picker.value || remembered();
        picker.innerHTML = "";
        picker.appendChild(h("option", { value: "", text: "跟随服务当前角色" }));
        characters.forEach((c) => {
          picker.appendChild(h("option", {
            value: c.id,
            text: c.name + (c.enabled === false ? "（未启用）" : "")
              + (serviceWho && c.name === serviceWho ? " · 现在是她" : ""),
          }));
        });
        // 上次选的还在就恢复；不在（角色被删/改名）就回到「跟随」
        picker.value = (keep && characters.some((c) => c.id === keep)) ? keep : "";
      }

      function updateHint() {
        const v = picker.value;
        if (v) {
          const hit = characters.find((c) => c.id === v) || {};
          hint.textContent = "这句话会用「" + (hit.name || v) + "」的设定与声线";
        } else {
          hint.textContent = serviceWho ? "跟随服务当前角色（现在是 " + serviceWho + "）" : "";
        }
      }

      root.appendChild(card("文字对话", [
        box,
        h("div", { class: "row", style: "margin-top:10px" }, [
          h("div", { class: "grow" }, input),
          sendBtn,
          sayBtn,
          pickerWrap,
          h("button", {
            class: "btn ghost", text: "清屏",
            onclick: async () => {
              await ctx.api.safe(() => ctx.api.post("/api/chat/clear", {}));
              paint([]);
            },
          }),
        ]),
        h("p", { class: "muted", text: "「发送」= 技能/工具 → LLM → 说出声（最慢）；「只念不答」= 直接念这句（最快，验证声线用）。选角色只影响这句话，不会改别的面板。" }),
        hint,
      ]));

      function msg(m) {
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

      function paint(items) {
        box.innerHTML = "";
        if (!items.length) {
          box.appendChild(h("p", { class: "muted", text: "还没有对话。注意：这里打字不经过麦克风，但回答会从扬声器说出来。" }));
        }
        items.forEach((m) => box.appendChild(msg(m)));
        box.scrollTop = box.scrollHeight;
      }

      async function reload() {
        const items = await ctx.api.get("/api/chat/history").then((d) => d.items || []).catch(() => []);
        paint(items);
      }

      async function send(kind) {
        const text = input.value.trim();
        if (!text || busy) return;
        busy = true;
        sendBtn.disabled = sayBtn.disabled = true;
        input.value = "";
        hint.textContent = kind === "ask" ? "服务正在处理（可能要十几秒）…" : "正在念…";
        const r = await ctx.api.safe(() => ctx.api.post(
          kind === "ask" ? "/api/chat/ask" : "/api/chat/say",
          { text, character: chosen() },
        ));
        busy = false;
        sendBtn.disabled = sayBtn.disabled = false;
        if (r && !r.ok) ctx.toast("没成：" + (r.error || "未知错误"), "err", 12000);
        await reload();
        updateHint();
      }

      sendBtn.onclick = () => send("ask");
      sayBtn.onclick = () => send("say");
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") send("ask"); });

      function refreshWho() {
        return ctx.api.post("/api/overview/ping", {})
          .then((r) => { if (r && r.ok && r.data) serviceWho = r.data.character_name || ""; })
          .catch(() => {})
          .then(() => { fillPicker(); updateHint(); });
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

      picker.onchange = () => { chosen(); updateHint(); };

      // 服务被关掉 / 语音里换了角色 → 刷新一下「现在是谁」
      handleEvent = (ev) => {
        if (ev.kind === "status" && ev.data && !ev.data.running) {
          serviceWho = "";
          hint.textContent = "服务没在跑——先点右上角「启动服务」";
        }
        if (ev.kind === "log" && ev.data && /\[角色\]/.test(ev.data.line || "")) {
          clearTimeout(refreshWho._t);
          refreshWho._t = setTimeout(refreshWho, 1200);
        }
      };
    },

    onEvent(ev, ctx) { if (handleEvent) handleEvent(ev, ctx); },
    onLeave() { handleEvent = null; },
  });
})();
