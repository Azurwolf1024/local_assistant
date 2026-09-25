/* 文字对话面板：打字 → 服务走完整链路 → 文字回来 + 声音由服务放出。
   历史只存在控制台内存里（刷新就没），真正的对话记忆在服务的 llm 里。 */
(function () {
  let handleEvent = null;

  window.Console.register("chat", {
    render(root, ctx) {
      const { h, card, secs } = UI;
      let busy = false;

      const box = h("div", { class: "chat" });
      const input = h("input", { placeholder: "打一句话，按回车发送（她会念出来）" });
      const hint = h("span", { class: "muted", text: "" });
      const sendBtn = h("button", { class: "btn primary", text: "发送" });
      const sayBtn = h("button", { class: "btn", text: "只念不答" });

      root.appendChild(card("文字对话", [
        box,
        h("div", { class: "row", style: "margin-top:10px" }, [
          h("div", { class: "grow" }, input),
          sendBtn,
          sayBtn,
          h("button", {
            class: "btn ghost", text: "清屏",
            onclick: async () => {
              await ctx.api.safe(() => ctx.api.post("/api/chat/clear", {}));
              paint([]);
            },
          }),
        ]),
        h("p", { class: "muted", text: "「发送」= 技能/工具 → LLM → 说出声（最慢，本地 4B 首字几秒）；「只念不答」= 直接念这句（最快，验证声线用）。" }),
        hint,
      ]));

      function msg(m) {
        const cls = m.role === "me" ? "me" : (m.role === "error" ? "bot" : "bot");
        const meta = [];
        if (m.at) meta.push(m.at);
        if (m.seconds !== null && m.seconds !== undefined) meta.push(secs(m.seconds));
        if (m.detail && m.detail.total_seconds) meta.push("LLM+出声共 " + secs(m.detail.total_seconds));
        if (m.detail && m.detail.character) meta.push(m.detail.character);
        return h("div", { class: "msg " + cls }, [
          h("div", { text: m.text }),
          meta.length ? h("span", { class: "meta", text: meta.join(" · ") }) : null,
        ]);
      }

      function paint(items) {
        box.innerHTML = "";
        if (!items.length) box.appendChild(h("p", { class: "muted", text: "还没有对话。注意：这里打字不会经过麦克风，但回答会从扬声器说出来。" }));
        items.forEach((m) => box.appendChild(msg(m)));
        box.scrollTop = box.scrollHeight;
      }

      async function send(kind) {
        const text = input.value.trim();
        if (!text || busy) return;
        busy = true;
        sendBtn.disabled = sayBtn.disabled = true;
        input.value = "";
        hint.textContent = kind === "ask" ? "服务正在处理（可能要十几秒）…" : "正在念…";
        const url = kind === "ask" ? "/api/chat/ask" : "/api/chat/say";
        const r = await ctx.api.safe(() => ctx.api.post(url, { text }));
        busy = false;
        sendBtn.disabled = sayBtn.disabled = false;
        hint.textContent = "";
        if (!r) { paint(await ctx.api.get("/api/chat/history").then((d) => d.items).catch(() => [])); return; }
        if (!r.ok) ctx.toast("没成：" + (r.error || "未知错误"), "err");
        paint(await ctx.api.get("/api/chat/history").then((d) => d.items).catch(() => []));
      }

      sendBtn.onclick = () => send("ask");
      sayBtn.onclick = () => send("say");
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") send("ask"); });

      ctx.api.get("/api/chat/history").then((d) => paint(d.items || [])).catch(() => paint([]));

      // 服务被关掉时给个提示（对话会失败）
      handleEvent = (ev) => {
        if (ev.kind === "status" && ev.data && !ev.data.running) hint.textContent = "服务没在跑——先点右上角「启动服务」";
      };
    },

    onEvent(ev, ctx) { if (handleEvent) handleEvent(ev, ctx); },
    onLeave() { handleEvent = null; },
  });
})();
