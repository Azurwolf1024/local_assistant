/* 更新面板：版本、检查新版、一键更新、还原备份。
   设计取向：★每一步都先说清会发生什么★（更新会覆盖程序文件），
   并且把「日志」原样摆在界面上 —— 出事时这页就是唯一的证据。 */
(function () {
  let pollTimer = null;
  let lastLines = 0;

  window.Console.register("update", {
    render(root, ctx) {
      const { h, card } = UI;
      const info = h("div", {});
      const actions = h("div", { class: "row" });
      const logBox = h("pre", { class: "mono", style: "max-height:340px;overflow:auto;white-space:pre-wrap" });
      const resultBox = h("div", {});

      root.appendChild(card("版本", [info, actions]));
      root.appendChild(card("进度", [logBox, resultBox]));

      function line(text, cls) {
        logBox.appendChild(h("div", { class: cls || "", text }));
        logBox.scrollTop = logBox.scrollHeight;
      }

      async function loadCheck(refresh) {
        info.innerHTML = "";
        info.appendChild(h("p", { class: "muted", text: "正在检查…" }));
        try {
          const r = await ctx.api.get("/api/update/check" + (refresh ? "?refresh=1" : ""));
          info.innerHTML = "";
          const c = r.check || {};
          const rows = [
            ["当前版本", c.current || "?", c.ok ? "" : "warn"],
            ["远端版本", c.latest || (c.mode === "git" ? "（这份是 git 检出，按提交数判断）" : "?"),
              c.behind ? "warn" : ""],
            ["安装方式", c.mode === "git" ? "git 检出（升级 = git pull）" : "压缩包 / exe（升级 = 下载覆盖）", ""],
            ["结论", c.reason || c.error || "?", c.behind ? "warn" : (c.ok ? "ok" : "warn")],
          ];
          if (c.release && c.release.notes) rows.push(["更新说明", c.release.notes, ""]);
          if (c.release && c.release.date) rows.push(["发布时间", c.release.date, ""]);
          rows.forEach(([k, v, cls]) => {
            info.appendChild(h("div", { class: "row" }, [
              h("span", { class: "muted", style: "min-width:6em", text: k + "：" }),
              h("span", { class: cls, text: String(v) }),
            ]));
          });
          info.appendChild(h("p", { class: "muted", text: (r.hint || "") }));
          buildActions();
        } catch (err) {
          info.innerHTML = "";
          info.appendChild(h("p", { class: "err", text: "检查失败：" + err.message }));
        }
      }

      function buildActions() {
        actions.innerHTML = "";
        actions.appendChild(h("button", {
          class: "btn", text: "检查更新",
          onclick: () => ctx.api.safe(() => loadCheck(true)),
        }));
        actions.appendChild(h("button", {
          class: "btn", text: "试运行（只列步骤）",
          onclick: () => ctx.api.safe(async () => {
            const r = await ctx.api.post("/api/update/start", { dry_run: true });
            resultBox.innerHTML = "";
            logBox.innerHTML = "";
            (r.plan || []).forEach((s) => line("· " + s));
            ctx.toast("这是试运行：一个文件都没动", "ok");
            return r;
          }),
        }));
        actions.appendChild(h("button", {
          class: "btn primary", text: "现在更新",
          onclick: () => ctx.api.safe(async () => {
            const c = (await ctx.api.get("/api/update/check")).check || {};
            const ask = "现在更新？\n\n"
              + "· 当前 " + (c.current || "?") + " → " + (c.latest || "最新")
              + "\n· 会先把你 data/ 与 config.toml 备份到 data/backup/"
              + "\n· 只覆盖程序文件，data/ models/ sessions/ .venv/ config.toml 一律不动"
              + "\n· 更新完要重启这个控制台窗口才生效";
            if (!confirm(ask)) return null;
            logBox.innerHTML = "";
            lastLines = 0;
            const r = await ctx.api.post("/api/update/start", { confirm: true });
            ctx.toast(r.message || "开始更新", r.started ? "warn" : "err", 12000);
            poll(true);
            return r;
          }),
        }));
      }

      // ------------------------------------------------------------ 进度轮询
      async function poll(force) {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        let state;
        try {
          state = await ctx.api.get("/api/update/status?since=" + lastLines);
        } catch (_e) {
          pollTimer = setTimeout(poll, 3000);
          return;
        }
        (state.lines || []).forEach((text) => line(text));
        lastLines = state.total_lines || lastLines;
        if (state.running) {
          pollTimer = setTimeout(poll, 1200);
        } else if (state.ok !== null && state.ok !== undefined) {
          const res = state.result || {};
          resultBox.innerHTML = "";
          resultBox.appendChild(h("p", {
            class: state.ok ? "ok" : "err",
            text: state.ok ? "更新完成 —— 关掉这个窗口重开就是新版" : ("没成功：" + (state.error || "看上面的日志")),
          }));
          if (res.backup) {
            resultBox.appendChild(h("p", { class: "muted",
              text: "备份在 " + res.backup + "（要还原：Post /api/update/restore 或跑 python main.py upgrade --restore）" }));
          }
          if ((res.added || []).length || (res.updated || []).length) {
            resultBox.appendChild(h("details", {}, [
              h("summary", { text: "动了哪些文件（新增 " + (res.added || []).length + " · 更新 " + (res.updated || []).length + "）" }),
              h("pre", { class: "mono", text: (res.added || []).concat(res.updated || []).join("\n") }),
            ]));
          }
          if ((res.removed_upstream || []).length) {
            resultBox.appendChild(h("details", {}, [
              h("summary", { text: "上游删掉、但你这里还在的文件（没动）" }),
              h("pre", { class: "mono", text: res.removed_upstream.join("\n") }),
            ]));
          }
          if (state.ok) {
            resultBox.appendChild(h("button", {
              class: "btn primary", text: "重新检查",
              onclick: () => { lastLines = 0; logBox.innerHTML = ""; loadCheck(true); },
            }));
          }
          ctx.refreshStatus();
        } else if (force) {
          pollTimer = setTimeout(poll, 1500);
        }
      }

      loadCheck(false).then(() => poll(false));
    },

    onLeave() {
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    },
  });
})();
