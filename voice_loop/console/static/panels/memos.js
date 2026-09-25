/* 备忘面板：一句话加、勾掉、删。 */
(function () {
  let handleEvent = null;

  window.Console.register("memos", {
    render(root, ctx) {
      const { h, card, table } = UI;
      const input = h("input", { placeholder: "比如：买牛奶 / 记一下周五交报告" });
      const add = async () => {
        const text = input.value.trim();
        if (!text) return;
        const r = await ctx.api.safe(() => ctx.api.post("/api/memos", { text }));
        if (r) { input.value = ""; ctx.toast("已记下：" + r.content, "ok"); refresh(); }
      };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });

      const box = h("div", {});
      root.appendChild(card("记一条", [
        h("div", { class: "row" }, [
          h("div", { class: "grow" }, input),
          h("button", { class: "btn primary", text: "记下", onclick: add }),
          h("button", {
            class: "btn", text: "清掉已完成",
            onclick: async () => {
              const r = await ctx.api.safe(() => ctx.api.post("/api/memos/clear-done", {}));
              if (r) { ctx.toast("清掉了 " + r.removed + " 条", "ok"); refresh(); }
            },
          }),
        ]),
        h("p", { class: "muted", text: "跟语音说的是同一条清洗规则：「记一下买牛奶」存下来就是「买牛奶」。" }),
      ]));
      root.appendChild(box);

      function render(d) {
        box.innerHTML = "";
        const rows = d.items || [];
        box.appendChild(card("备忘（待办 " + d.open + " · 已完成 " + d.done + "）", table([
          { key: "done", label: "", cls: "num", render: (r) => h("input", {
              type: "checkbox", checked: r.done,
              onchange: async (e) => {
                await ctx.api.safe(() => ctx.api.patch("/api/memos/" + r.index, { done: e.target.checked }));
                refresh();
              },
            }) },
          { key: "content", label: "内容", render: (r) => h("span", {
              text: r.content,
              style: r.done ? "text-decoration:line-through;opacity:.55" : "",
            }) },
          { key: "created_at", label: "记录于", cls: "num" },
          { key: "act", label: "", cls: "act", render: (r) => h("div", { class: "row" }, [
              h("button", { class: "btn small", text: r.done ? "恢复" : "完成",
                onclick: async () => {
                  await ctx.api.safe(() => ctx.api.patch("/api/memos/" + r.index, { done: !r.done }));
                  refresh();
                } }),
              h("button", { class: "btn small danger", text: "删除",
                onclick: async () => {
                  if (!confirm("删除「" + r.content + "」？")) return;
                  const ok = await ctx.api.safe(() => ctx.api.del("/api/memos/" + r.index), "已删除");
                  if (ok) refresh();
                } }),
            ]) },
        ], rows, { emptyText: "还没有备忘。" })));
      }

      function refresh() {
        ctx.api.get("/api/memos")
          .then(render)
          .catch((err) => ctx.toast("读取备忘失败：" + err.message, "err"));
      }
      refresh();

      // 语音里记了备忘 → 界面跟着更新（日志里会出现「备忘」）
      handleEvent = (ev) => {
        if (ev.kind === "log" && ev.data && /备忘/.test(ev.data.line || "")) {
          clearTimeout(refresh._t);
          refresh._t = setTimeout(refresh, 1200);
        }
      };
    },

    onEvent(ev, ctx) { if (handleEvent) handleEvent(ev, ctx); },
    onLeave() { handleEvent = null; },
  });
})();
