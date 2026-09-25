/* 日志面板：历史（一次性拉）+ 实时（SSE 追加）。
   ★只读★：服务独占写这个文件，控制台只读不写。 */
(function () {
  let handleEvent = null;

  window.Console.register("logs", {
    render(root, ctx) {
      const { h, card, table, secs } = UI;
      let paused = false;      // 暂停 = 不再自动追加（看报错时别被刷走）
      let onlyProblems = false;
      let buffer = [];         // 界面上的所有行（暂停/过滤时重绘用）

      const box = h("div", { class: "log" });
      const status = h("span", { class: "muted", text: "" });

      const toolbar = h("div", { class: "row" }, [
        h("button", {
          class: "btn small", text: "暂停跟随",
          onclick: (e) => { paused = !paused; e.target.textContent = paused ? "继续跟随" : "暂停跟随"; },
        }),
        h("button", {
          class: "btn small", text: "只看报错",
          onclick: (e) => { onlyProblems = !onlyProblems; e.target.textContent = onlyProblems ? "看全部" : "只看报错"; repaint(); },
        }),
        h("button", { class: "btn small", text: "重读文件", onclick: () => loadTail(600) }),
        h("button", { class: "btn small", text: "清屏（只清界面）",
          onclick: () => { buffer = []; repaint(); } }),
        h("span", { class: "grow" }, status),
      ]);

      root.appendChild(card("服务日志（实时跟随）", [
        toolbar,
        h("p", { class: "muted", text: "★这里只是看★：文件由服务独占写。要清理请先停服务再删 sessions/listen.log。" }),
        box,
      ]));

      function visible() {
        return onlyProblems ? buffer.filter((r) => r.level !== "info") : buffer;
      }

      function repaint() {
        box.innerHTML = "";
        for (const row of visible()) box.appendChild(line(row));
        if (!buffer.length) box.appendChild(h("div", { class: "l-dim", text: "（还没有日志。服务没启动时是空的）" }));
        if (!paused) box.scrollTop = box.scrollHeight;
      }

      function line(row) {
        return h("div", { class: "l-" + (row.level === "info" ? "info" : row.level), text: row.line });
      }

      function push(row) {
        buffer.push(row);
        if (buffer.length > 4000) buffer.splice(0, buffer.length - 4000);
        if (paused) return;
        if (onlyProblems && row.level === "info") return;
        // 贴近底部才自动滚（用户往回翻的时候别抢滚动条）
        const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
        box.appendChild(line(row));
        if (nearBottom) box.scrollTop = box.scrollHeight;
      }

      function loadTail(n) {
        ctx.api.get("/api/logs/tail?lines=" + n)
          .then((d) => {
            buffer = (d.lines || []).slice();
            status.textContent = (d.exists ? d.file : "（日志文件还不存在：" + d.file + "）") + " · " + buffer.length + " 行";
            repaint();
          })
          .catch((err) => ctx.toast("读日志失败：" + err.message, "err"));
      }

      loadTail(400);

      // 实时追加（SSE 已经在总线上，这里只挑 log 事件用）
      handleEvent = (ev) => {
        if (ev.kind === "log" && ev.data) push(ev.data);
        if (ev.kind === "status" && ev.data && !ev.data.error) {
          const s = ev.data;
          status.textContent = "服务 " + (s.running ? "运行中 PID " + s.pid : "未启动")
            + " · 日志 " + Math.round(s.log_size / 1024) + " KB"
            + (s.log_age_seconds !== null ? " · 最后写入 " + secs(s.log_age_seconds) + "前" : "");
        }
      };
    },

    onEvent(ev, ctx) { if (handleEvent) handleEvent(ev, ctx); },
    onLeave() { handleEvent = null; },
  });
})();
