/* 角色 / 声线面板：看人设与参考音、切声线、试听（切过去再念一句）。 */
(function () {
  let handleEvent = null;

  window.Console.register("voices", {
    render(root, ctx) {
      const { h, card, table, secs } = UI;
      const text = h("input", { value: "你好呀，我是本地语音助手。", placeholder: "要念的话（可留空用默认）" });
      const listBox = h("div", {});

      root.appendChild(card("试听（声音由服务放出来）", [
        h("div", { class: "row" }, [
          h("div", { class: "grow" }, text),
          h("button", {
            class: "btn", text: "用当前声线念这句",
            onclick: async (e) => {
              e.target.disabled = true;
              const r = await ctx.api.safe(() => ctx.api.post("/api/voices/audition", { text: text.value, switch: false }));
              e.target.disabled = false;
              if (r) ctx.toast(r.ok ? "已念出（" + secs(r.seconds) + "）" : "没念成：" + r.error, r.ok ? "ok" : "err");
            },
          }),
        ]),
        h("p", { class: "muted", text: "点下面某个卡片的「切过去并试听」= 先切声线再念这句（两步都走信箱，服务最多 5 秒内响应）。服务没启动会失败。" }),
      ]));
      root.appendChild(listBox);

      async function audition(id) {
        const r = await ctx.api.safe(() => ctx.api.post("/api/voices/audition", { id, text: text.value }));
        if (r) ctx.toast(r.ok ? "已切到该角色并念出（" + secs(r.seconds) + "）" : "没成：" + r.error, r.ok ? "ok" : "err");
        refresh();
      }

      async function switchTo(id) {
        const r = await ctx.api.safe(() => ctx.api.post("/api/voices/switch", { id }));
        if (r) ctx.toast(r.ok ? "已切到 " + r.message : "没切成：" + r.message, r.ok ? "ok" : "err");
        refresh();
      }

      function render(d) {
        listBox.innerHTML = "";
        if (d.error) {
          listBox.appendChild(card("角色文件有问题", UI.line(d.error, "mono")));
        }
        const chars = d.characters || [];
        listBox.appendChild(card("角色（默认 " + (d.resolved_default || d.default || "—") + " · 人格文件 " + d.persona_file + "）",
          h("div", { class: "grid" }, chars.map((c) => card(c.name + (c.is_default ? " ·默认" : ""), [
            h("p", { class: "muted", text: c.title || "" }),
            h("p", { class: "muted", text: "唤醒词：" + ((c.wake_words || []).join("、") || "—") }),
            h("p", { class: "muted", text: "称呼你：「" + (c.user_title || "你") + "」" + (c.ack ? " · 应答语：" + c.ack : "") }),
            c.style && c.style.length ? h("p", { class: "muted", text: "风格：" + c.style.join(" / ") }) : null,
            h("p", { class: "mono", text: "参考音：" + (c.voice_ref || "（用全局）") + (c.voice_ref ? (c.voice_ref_exists ? " ✓" : " ✗ 文件不在") : "") }),
            c.voice_model ? h("p", { class: "mono", text: "专属模型：" + c.voice_model + (c.voice_model_exists ? " ✓" : " ✗ 目录不在") }) : null,
            c.ref_candidates && c.ref_candidates.length
              ? h("details", {}, [
                  h("summary", { class: "muted", text: "素材库里还有 " + c.ref_candidates.length + " 个参考候选" }),
                  h("div", { class: "mono", style: "font-size:11px" }, c.ref_candidates.map((p) => h("div", { text: p }))),
                ])
              : null,
            c.lines && c.lines.length
              ? h("details", {}, [
                  h("summary", { class: "muted", text: "示例台词 " + c.lines.length + " 条" }),
                  h("div", {}, c.lines.map((ln) => h("div", { class: "muted", text: "· " + (ln.text || "") }))),
                ])
              : null,
            h("div", { class: "row" }, [
              h("button", { class: "btn small primary", text: "切过去并试听", onclick: () => audition(c.id) }),
              h("button", { class: "btn small", text: "只切过去", onclick: () => switchTo(c.id) }),
            ]),
          ])))));

        listBox.appendChild(card("全局声线配置（只读；要改去 config.toml 或角色 json）", table(
          [{ key: "k", label: "项", cls: "num" }, { key: "v", label: "值" }],
          [
            { k: "TTS 后端", v: d.tts.backend },
            { k: "克隆模型目录", v: d.tts.clone_dir },
            { k: "克隆参考音（全局）", v: d.tts.clone_audio },
            { k: "采样步数", v: d.tts.clone_steps },
            { k: "Piper 模型", v: d.tts.model },
            { k: "信箱待处理", v: d.mailbox.pending + " 条" },
          ]
        )));
      }

      function refresh() {
        ctx.api.get("/api/voices")
          .then(render)
          .catch((err) => ctx.toast("读取角色失败：" + err.message, "err"));
      }
      refresh();

      // 语音里喊了角色名切换 → 界面跟着更新
      handleEvent = (ev) => {
        if (ev.kind === "log" && ev.data && /\[角色\]/.test(ev.data.line || "")) {
          clearTimeout(refresh._t);
          refresh._t = setTimeout(refresh, 1200);
        }
      };
    },

    onEvent(ev, ctx) { if (handleEvent) handleEvent(ev, ctx); },
    onLeave() { handleEvent = null; },
  });
})();
