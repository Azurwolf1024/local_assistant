/* 概览面板：一眼看清「现在什么情况」。
   数据全部来自 /api/overview（只读）；三个按钮是「让服务做点事」（走信箱）。 */
(function () {
  let handleEvent = null;   // 当前面板的事件处理（切走时清掉）

  window.Console.register("overview", {
    render(root, ctx) {
      const { h, card, table, secs } = UI;
      let data = null;      // 最近一次 /api/overview
      let live = null;      // 最近一次 ping 回来「服务自报」
      // ★动作的回执要活在闭包外面★：服务状态一变就 `renderAll()` 重建整块卡片，
      // 只把文字写进那个 `<p>` 的话，几秒后就被新节点替换掉了（看起来"点了没反应"）。
      let syncMsg = "";
      let dreamMsg = "";

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
              if (!r) return;
              // 第一次念要先加载声线（实测 11 秒量级，机器忙时更久）——超时时别只说「失败」
              const hint = !r.ok && /没有回执/.test(r.error || "")
                ? "（服务还在跑；第一次念要先加载声线，可能要十几秒，等它念完再点一次通常立刻就有）"
                : "";
              ctx.toast(r.ok ? "服务已念出（服务侧耗时 " + secs(r.seconds) + "）" : "没念成：" + r.error + hint,
                r.ok ? "ok" : "err", 15000);
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
                ? h("span", { class: "tag ok", text: "运行中 · PID " + data.service.pid
                    + (data.service.pid_source === "log" ? "（PID 从日志恢复）" : "") })
                : h("span", { class: "tag warn", text: data.service.leftovers
                    ? "没在跑（有上次的残留文件，点「启动服务」会自动清掉）" : "未启动" }) },
            { k: "服务自报", v: live
                ? (live.character_name || "—") + " · 声线 " + (live.tts || "—") + " · TTS " + (live.tts_loaded ? "已加载" : "未加载")
                : h("span", { class: "muted", text: "点右边「问服务状态」问它一句" }) },
            { k: "日志文件", v: h("span", { class: "mono", text: data.service.log_file }) },
            { k: "信箱", v: [data.mailbox.pending + " 条待处理 · ", h("span", { class: "mono", text: data.mailbox.root })] },
            { k: "控制台启动于", v: data.started_at ? new Date(data.started_at * 1000).toLocaleTimeString() : "—" },
          ]
        )));

        // ★记忆上云★：放在概览，是因为「同步」是一次动作，不该每轮对话都去碰网络
        const ms = data.memory_sync || {};
        const syncBox = h("p", { class: "muted", text: syncMsg });
        const syncBtn = h("button", {
          class: "btn small", text: "同步到云端",
          onclick: async () => {
            syncBtn.disabled = true;
            syncBox.textContent = syncMsg = "正在导出 → 提交 → 推送…（第一次可能要几秒）";
            const r = await ctx.api.safe(() => ctx.api.post("/api/overview/memory-sync", {}));
            syncBtn.disabled = false;
            if (!r) { syncBox.textContent = syncMsg = "请求失败（看日志页）"; return; }
            syncBox.textContent = syncMsg = (r.ok ? "√ " : "× ") + (r.summary || r.error || "");
            if (r.ok) ctx.toast("记忆已同步", "ok");
            else ctx.toast("同步没成：" + (r.error || ""), "err");
          },
        });
        const pullBtn = h("button", {
          class: "btn small", text: "从云端合并回来",
          onclick: async () => {
            pullBtn.disabled = true;
            syncBox.textContent = syncMsg = "正在拉取并合并…";
            const r = await ctx.api.safe(() => ctx.api.post("/api/overview/memory-sync", { pull: true }));
            pullBtn.disabled = false;
            syncBox.textContent = syncMsg = r ? ((r.ok ? "√ " : "× ") + (r.summary || r.error || ""))
              : "请求失败（看日志页）";
          },
        });
        body.appendChild(card("记忆（同步到你的私有仓库）", [
          table([{ key: "k", label: "项目", cls: "num" }, { key: "v", label: "值" }], [
            { k: "同步开关", v: ms.enabled ? "退出时自动推一次（[memory] sync_on_close）" : "关（只手动同步）" },
            { k: "仓库", v: ms.remote
              ? h("span", { class: "mono", text: ms.remote })
              : h("span", { class: "muted", text: "没配 [memory] sync_remote（只在本地克隆里留着）" }) },
            { k: "上次同步", v: ms.updated_at
              ? ms.updated_at + " · " + (ms.characters || 0) + " 个角色 · episodes "
                + ((ms.counts || {}).episodes || 0) + " / facts " + ((ms.counts || {}).facts || 0)
              : h("span", { class: "muted", text: "还没同步过" }) },
          ]),
          h("div", { class: "row" }, [syncBtn, pullBtn]),
          syncBox,
          h("p", { class: "muted", text: "只同步「每角色一份的记忆」（episodes / facts / sessions 索引 / knowledge / 上下文），"
            + "原始对话（L1，sessions/*.jsonl）不上云。推之前会确认仓库不是公开的。" }),
        ]));

        // ★日上下文 + 做梦★：今天的对话记得多连、缓存省了多少、上次整理是什么时候
        const md = data.memory_dream || {};
        const dreamBox = h("p", { class: "muted", text: dreamMsg });
        const dreamBtn = h("button", {
          class: "btn small", text: "现在整理一次（规则版）",
          onclick: async () => {
            dreamBtn.disabled = true;
            dreamBox.textContent = dreamMsg = "正在整理上下文 → 记忆…";
            const r = await ctx.api.safe(() => ctx.api.post("/api/overview/dream", {}));
            dreamBtn.disabled = false;
            if (!r) { dreamBox.textContent = dreamMsg = "请求失败（看日志页）"; return; }
            dreamBox.textContent = dreamMsg = (r.ok ? "√ " : "× ") + (r.summary || r.error || "");
            if (r.ok) ctx.toast("整理完了", "ok");
          },
        });
        const charRows = (md.characters || []).length
          ? (md.characters || []).map((c) => ({
            k: c.id,
            v: c.segments + " 段 · 打开中 " + c.open_turns + " 轮"
              + (c.last_dream ? " · 上次整理 " + c.last_dream : " · 还没整理过")
              + (c.prompt ? " · 今天缓存命中 " + Math.round((c.hit / c.prompt) * 100) + "%" : ""),
          }))
          : [{ k: "（还没有）", v: h("span", { class: "muted", text: "聊几句就有了" }) }];
        body.appendChild(card("上下文与整理（让每天的对话接着长）", [
          table([{ key: "k", label: "角色", cls: "num" }, { key: "v", label: "上下文" }], charRows),
          h("p", { class: "muted", text: md.enabled
            ? "夜里 " + (md.window || "02:00-05:00") + " 随机整理一次，攒到 "
              + (md.at_tokens || 0) + " token 也会自动压一次。段封存后不再改写，"
              + "所以白天的提示词前缀是稳定的（云端缓存能命中）。"
              : "做梦关着（[memory.dream] enabled = false）：上下文只追加、不压缩。" }),
          h("div", { class: "row" }, [dreamBtn]),
          dreamBox,
        ]));

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

        const chars = data.persona.characters || [];        const defaultName = (chars.find((c) => c.is_default) || {}).name || data.persona.resolved_default || "—";
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
                  h("p", { class: "muted", text: "引擎：" + (c.engine === "api" ? "云端 API" : "本地 Ollama") }),
                  // ★说清楚她到底用哪个嗓子★（后端 + 音色 + 参考，见 persona_card.describe_voice）
                  h("p", { class: "muted mono", text: c.tts_line || "TTS：—" }),
                ]),
          ])))));

        body.appendChild(card("技术配置一览（要改去 config.toml）", table(
          [{ key: "k", label: "项", cls: "num" }, { key: "v", label: "值" }],
          [
            { k: "ASR", v: (data.asr.summary || "—")
                + (data.asr.missing && data.asr.missing.length
                  ? "  × 缺模型：" + data.asr.missing.join("、") : "") },
            { k: "识别策略", v: (data.asr.spec || "—")
                + " · 词库 " + ((data.asr.lexicon || {}).total || 0) + " 条"
                + (data.asr.kws ? " · KWS 唤醒已开" : "")
                + (data.asr.cloud ? " · ★允许云端识别（音频会上传）★" : "") },
            { k: "LLM", v: data.llm.model + " @ " + data.llm.host },
            { k: "LLM 路由", v: (data.llm.router || data.llm.route || "—") + " · temperature " + data.llm.temperature },
            { k: "API 引擎", v: (data.api.enabled ? (data.api.engine === "api" ? "全局默认 API" : "全局默认本地") : "已关闭")
                + " · " + data.api.model + " @ " + data.api.base_url
                + (data.api.has_key ? " · 配置里有 key" : " · key 走环境变量 " + data.api.key_env) },
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

      // 服务状态变了就刷新那几行；★停了就把「服务自报」清掉★（那是上一次 ping 的结果，
      // 留着会让人以为服务还活着）
      handleEvent = (ev) => {
        if (ev.kind === "status" && data) {
          data.service = ev.data;
          if (!ev.data.running) live = null;
          renderAll();
        }
      };
    },

    onEvent(ev, ctx) { if (handleEvent) handleEvent(ev, ctx); },
    onLeave() { handleEvent = null; },
  });
})();
