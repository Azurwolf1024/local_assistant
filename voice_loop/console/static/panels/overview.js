/* 概览面板：一眼看清「现在什么情况」。
   数据全部来自 /api/overview（只读）；三个按钮是「让服务做点事」（走信箱）。 */
(function () {
  let handleEvent = null;   // 当前面板的事件处理（切走时清掉）

  window.Console.register("overview", {
    render(root, ctx) {
      const { h, card, table, secs } = UI;
      let data = null;      // 最近一次 /api/overview
      let live = null;      // 最近一次 ping 回来「服务自报」

      // ---------------- 动作条 ----------------
      const audition = h("input", { value: "你好呀，我是本地语音助手。", placeholder: "要念的话" });
      root.appendChild(card("让服务做点事", [
        h("div", { class: "row" }, [
          h("div", { class: "grow" }, audition),
          h("button", {
            class: "btn primary", text: "念这句（试听）",
            onclick: async (e) => {
              e.target.disabled = true;
              const r = await ctx.api.safe(() => ctx.api.post("/api/overview/audition", { text: audition.value }));
              e.target.disabled = false;
              if (r) ctx.toast(r.ok ? "服务已念出（服务侧耗时 " + secs(r.seconds) + "）" : "没念成：" + r.error, r.ok ? "ok" : "err");
            },
          }),
          h("button", {
            class: "btn", text: "弹个提醒",
            onclick: async () => {
              const r = await ctx.api.safe(() => ctx.api.post("/api/overview/toast", { title: "控制台测试", text: "看到这条说明提醒小窗是通的" }));
              if (r) ctx.toast(r.ok ? r.message : r.error, r.ok ? "ok" : "err");
            },
          }),
          h("button", {
            class: "btn", text: "问服务状态",
            onclick: async () => {
              const r = await ctx.api.safe(() => ctx.api.post("/api/overview/ping"));
              if (!r) return;
              if (r.ok) { live = r.data; renderAll(); ctx.toast("服务响应 " + secs(r.seconds), "ok"); }
              else ctx.toast("没有响应：" + r.error, "err");
            },
          }),
        ]),
        h("p", { class: "muted", text: "这三个是往服务信箱投命令，服务最多 5 秒内响应（它每轮抓一次）。服务没启动会失败——先点右上角「启动服务」。" }),
      ]));

      const body = h("div", {});
      root.appendChild(body);

      // ---------------- 渲染 ----------------
      function renderAll() {
        if (!data) return;
        body.innerHTML = "";

        body.appendChild(card("现在什么情况", table(
          [{ key: "k", label: "项目", cls: "num" }, { key: "v", label: "值" }],
          [
            { k: "服务进程", v: data.service.running
                ? h("span", { class: "tag ok", text: "运行中 · PID " + data.service.pid })
                : h("span", { class: "tag warn", text: data.service.pid_stale
                    ? "没在跑（pid 文件里是旧进程 " + data.service.pid + "）" : "未启动" }) },
            { k: "服务自报", v: live
                ? (live.character_name || "—") + " · 声线 " + (live.tts || "—") + " · TTS " + (live.tts_loaded ? "已加载" : "未加载")
                : h("span", { class: "muted", text: "点右边「问服务状态」问它一句" }) },
            { k: "日志文件", v: h("span", { class: "mono", text: data.service.log_file }) },
            { k: "信箱", v: [data.mailbox.pending + " 条待处理 · ", h("span", { class: "mono", text: data.mailbox.root })] },
            { k: "控制台启动于", v: data.started_at ? new Date(data.started_at * 1000).toLocaleTimeString() : "—" },
          ]
        )));

        const today = data.events.today || [];
        const next = data.events.due_next;
        body.appendChild(card("今天的提醒（库里共 " + data.events.total + " 条）", [
          table([
            { key: "time", label: "时间", cls: "num" },
            { key: "title", label: "内容" },
            { key: "category", label: "标签", render: (r) => (r.category ? h("span", { class: "tag", text: r.category }) : "") },
            { key: "repeat", label: "重复", render: (r) => (r.repeat === "once" ? "一次" : r.repeat) },
            { key: "done", label: "状态", render: (r) => h("span", { class: r.done ? "tag ok" : "tag", text: r.done ? "已完成" : "待办" }) },
          ], today, { emptyText: "今天没有安排。去「日程 / 闹钟」面板加一条。" }),
          next ? h("p", { class: "muted", text: "下一条：" + next.at + " " + next.title + "（还有 " + next.in_minutes + " 分钟）" }) : null,
        ]));

        const chars = data.persona.characters || [];
        const defaultName = (chars.find((c) => c.is_default) || {}).name || data.persona.resolved_default || "—";
        body.appendChild(card("角色（" + chars.length + " 个，默认 " + defaultName + "）",
          h("div", { class: "grid" }, chars.map((c) => h("div", { class: "card", style: "margin:0" }, [
            h("div", { class: "row" }, [
              h("strong", { class: "grow", text: c.name || c.error || c.id }),
              c.is_default ? h("span", { class: "tag ok", text: "默认" }) : null,
              c.enabled === false ? h("span", { class: "tag warn", text: "未启用" }) : null,
            ]),
            c.error
              ? h("p", { class: "mono", text: c.error })
              : h("div", {}, [
                  h("p", { class: "muted", text: c.title || "" }),
                  h("p", { class: "muted", text: "唤醒词：" + ((c.wake_words || []).join("、") || "—") }),
                  h("p", { class: "muted mono", text: c.voice_ref || c.voice || "（用全局声线）" }),
                ]),
          ])))));

        body.appendChild(card("技术配置一览（要改去 config.toml）", table(
          [{ key: "k", label: "项", cls: "num" }, { key: "v", label: "值" }],
          [
            { k: "ASR", v: data.asr.strategy + "（SenseVoice: " + data.asr.sensevoice + "）" },
            { k: "Whisper", v: data.asr.whisper },
            { k: "LLM", v: data.llm.model + " @ " + data.llm.host },
            { k: "LLM 路由", v: (data.llm.router || data.llm.route || "—") + " · temperature " + data.llm.temperature },
            { k: "TTS 后端", v: data.tts.backend + "（" + (data.tts.clone_dir || data.tts.model) + "）" },
            { k: "输出高频压制", v: "≥" + Math.round(data.tts.out_tilt[0] / 1000) + " kHz：" + data.tts.out_tilt[1] + " dB" },
            { k: "音区守卫", v: data.tts.pitch_guard[0] + " 半音 · 最多 " + data.tts.pitch_guard[1] + " 遍" },
            { k: "文本保真守卫", v: data.tts.text_guard ? "相似度 < " + data.tts.text_guard + " 就重采" : "关" },
            { k: "词内最小间隔", v: data.tts.trim_min_gap_ms + " ms" + (data.tts.trim_min_gap_ms === 0 ? "（关，推荐）" : "") },
          ]
        )));
      }

      ctx.api.get("/api/overview")
        .then((d) => { data = d; renderAll(); })
        .catch((err) => ctx.toast("概览读取失败：" + err.message, "err"));

      // 服务状态变了就刷新那几行
      handleEvent = (ev) => {
        if (ev.kind === "status" && data) { data.service = ev.data; renderAll(); }
      };
    },

    onEvent(ev, ctx) { if (handleEvent) handleEvent(ev, ctx); },
    onLeave() { handleEvent = null; },
  });
})();
