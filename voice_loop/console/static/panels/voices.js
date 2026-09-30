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

      // 读一个文件成 base64（不带上 data: 前缀也没关系，服务端两头都认）
      function readBase64(file) {
        return new Promise((resolve, reject) => {
          const fr = new FileReader();
          fr.onload = () => resolve(String(fr.result || "").split(",").pop());
          fr.onerror = () => reject(new Error("读文件失败"));
          fr.readAsDataURL(file);
        });
      }

      // ★只用一条语音就能克隆★：挑一条素材（或传一条）→ 写进人格文件当参考 → 可选立刻试听
      // 文本能按文件名对上素材清单（<id>.txt）/她的台词时**自动填入** ——
      // 零样本克隆吃的是「这条音频 + 它逐字对应的文本」，文本对不上就会声不对词。
      function cloneBlock(c) {
        const { h } = UI;
        const cands = c.ref_candidates || [];
        const index = c.ref_index || {};
        const loose = (s) => String(s).replace(/[\s.\-_()（）\[\]【】·、]+/g, "").toLowerCase();
        const pick = h("select", {});
        cands.forEach((it) => {
          const bits = [
            it.name,
            it.seconds ? it.seconds.toFixed(1) + " 秒" : "时长未知",
            it.text ? "有文本" : "没文本",
            it.ok ? "" : "✗ " + (it.problem || "不能用"),
          ];
          pick.appendChild(h("option", {
            value: it.path, title: it.text || "", disabled: !it.ok,
            text: bits.filter(Boolean).join(" · "),
          }));
        });
        if (!cands.length) {
          pick.appendChild(h("option", {
            value: "",
            text: "（素材目录里没找到音频：" + (c.materials_dir || "没写 voice_dir") + "，就传一条）",
          }));
        }
        if (c.voice_ref && cands.some((it) => it.path === c.voice_ref)) pick.value = c.voice_ref;
        const file = h("input", { type: "file", accept: "audio/*", style: "font-size:11px" });
        const refText = h("input", { placeholder: "这条音频的逐字文本（可留空）", value: c.voice_ref_text || "" });
        const hint = h("p", { class: "muted", text: "" });
        const status = h("p", { class: "muted", text: "" });

        function fillFromPick() {
          const it = cands.find((x) => x.path === pick.value);
          if (it && it.text) {
            refText.value = it.text;
            hint.textContent = "文本已自动填入（来源：" + (it.text_source || "素材清单") + "）";
          } else if (it) {
            hint.textContent = "这条没找到对应文本 —— 手填它的原文会让克隆更准（可留空）";
          }
        }
        pick.addEventListener("change", fillFromPick);
        file.addEventListener("change", () => {
          const f = (file.files || [])[0];
          if (!f) return;
          const stem = String(f.name).replace(/\.[^.]+$/, "");
          const hit = index[loose(stem)];
          if (hit) {
            refText.value = hit;
            hint.textContent = "文本已自动填入（文件名对上了素材清单里的「" + stem + "」）";
          } else {
            hint.textContent = "这个文件名没查到对应文本，手填它的原文更准（可留空）";
          }
        });
        if (!refText.value) fillFromPick();

        async function apply(audition) {
          const f = (file.files || [])[0];
          const body = { id: c.id, apply: true, audition: !!audition, ref_text: refText.value };
          try {
            if (f) { body.data = await readBase64(f); body.filename = f.name; }
            else if (pick.value) { body.path = pick.value; }
            else { ctx.toast("先挑一条素材，或者选一个音频文件", "err"); return; }
          } catch (err) { ctx.toast("读文件失败：" + err.message, "err"); return; }
          status.textContent = audition ? "正在写人格文件并让服务重建声线（首次可能要十几秒）…" : "正在写人格文件…";
          const r = await ctx.api.safe(() => ctx.api.post("/api/voices/clone", body));
          if (!r) { status.textContent = "请求失败（看日志页）"; return; }
          if (!r.ok) { status.textContent = "没成：" + (r.error || ""); ctx.toast("克隆参考没设上", "err"); return; }
          const changed = Object.keys(r.changed || {}).join("、") || "（无变化）";
          let msg = "参考已设为 " + r.reference + "（写入 " + changed + "" + (r.backup ? "，旧文件备份在 " + r.backup.split("\\").pop() : "") + "）";
          if (r.ref_text) msg += "；参考文本：" + (r.ref_text.length > 24 ? r.ref_text.slice(0, 24) + "…" : r.ref_text) + "（来源 " + (r.text_source || "手填") + "）";
          else if (r.cleared_text) msg += "；旧参考文本已清掉（换了音频又没新文本，留着会声不对词）";
          if (r.audition) msg += r.audition.ok ? "；已念出（" + secs(r.audition.seconds) + "）" : "；但试听没成：" + (r.audition.message || "");
          status.textContent = msg;
          ctx.toast(r.audition ? (r.audition.ok ? "已应用并试听" : "已应用，试听失败") : "已应用克隆参考", r.audition && !r.audition.ok ? "err" : "ok");
          refresh();
        }

        return h("details", {}, [
          h("summary", { class: "muted", text: "★只用一条语音就能克隆★（现在参考：" + (c.voice_ref || "用全局") + "）" }),
          h("div", { style: "padding:6px 0" }, [
            h("p", { class: "muted", text: "素材目录 " + (c.materials_dir || "—") + "：" + (c.materials_count || 0) + " 条音频，其中 " + (c.materials_with_text || 0) + " 条能按文件名对上文本。挑一条（或传一段你自己的录音）→ 写进人格文件当参考 → 可选立刻试听。只换参考音是当场生效的，不用重启服务。" }),
            h("div", { class: "row" }, [h("div", { class: "grow" }, pick)]),
            h("div", { class: "row" }, [h("div", { class: "grow" }, file)]),
            h("div", { class: "row" }, [h("div", { class: "grow" }, refText)]),
            hint,
            h("div", { class: "row" }, [
              h("button", { class: "btn small primary", text: "设为克隆参考并试听", onclick: () => apply(true) }),
              h("button", { class: "btn small", text: "只设为参考", onclick: () => apply(false) }),
            ]),
            status,
          ]),
        ]);
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
                  h("summary", { class: "muted", text: "素材库 " + (c.materials_dir || "（没写 voice_dir）") + "：" + c.ref_candidates.length + " 条音频，" + (c.materials_with_text || 0) + " 条有文本" }),
                  h("div", { class: "mono", style: "font-size:11px" }, c.ref_candidates.map((it) => h("div", {
                    text: "· " + it.name + (it.seconds ? "  " + it.seconds.toFixed(1) + "s" : "") + (it.text ? "  ✓有文本" : "（没文本）") + (it.ok ? "" : "  ✗ " + (it.problem || "不能用")),
                  }))),
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
            cloneBlock(c),
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
