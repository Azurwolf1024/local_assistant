/* 角色面板（★资料卡 + 声线合并★，2026-10-01）：
   上面 —— 看角色 / 切声线 / 试听 / 只用一条语音克隆（这些要服务在跑）；
   底下 —— 资料卡表单：填表新建、或点卡片上的「编辑资料卡」改一个已有角色
           （只写人格文件，不需要服务）。
   ★表单结构不是写死在前端的★：字段/说明/类型全部来自 /api/persona/spec ——
   后端那份 SPEC 是唯一真相源，加了字段这里不用改。 */
(function () {
  let handleEvent = null;

  window.Console.register("persona", {
    render(root, ctx) {
      const { h, card, table, secs } = UI;

      // ---------------------------------------------------------------- 状态
      let spec = null;              // /api/persona/spec（字段规范）
      let template = {};            // 「填模板」用：新建时是样板，编辑时=她当前的值
      const inputs = {};            // key -> {kind, el}
      let editing = "";             // 正在编辑的 id（空串 = 新建）
      let editFile = "";            // 编辑中的人格文件（索引里写的那条，不一定是 personas/<id>.json）
      let lastRows = null;          // 上一次拿到的 /api/voices（重画列表时不再请求一遍）

      // ---------------------------------------------------------------- 上半页：声线
      const sayText = h("input", { value: "你好呀，我是本地语音助手。", placeholder: "要念的话（可留空用默认）" });
      const listBox = h("div", {});

      // ---------------------------------------------------------------- 下半页：资料卡
      const formTitle = h("h2", { class: "grow", text: "新建角色（按格式填）" });
      const modeNote = h("p", { class: "muted", text: "" });
      const formBox = h("div", {});
      const status = h("p", { class: "muted", text: "" });
      const problemsBox = h("div", {});
      const jsonBox = h("pre", { class: "mono", style: "font-size:11px;max-height:320px;overflow:auto" });
      const filePick = h("input", { type: "file", accept: ".json,.txt,application/json,text/plain" });
      const exportPick = h("select", {});
      const meta = h("p", { class: "muted", text: "" });
      const saveBtn = h("button", { class: "btn small primary", text: "创建角色", onclick: () => save() });
      const cancelBtn = h("button", {
        class: "btn small hidden", text: "取消编辑（改为新建）", onclick: () => clearEdit(),
      });

      // ---------------------------------------------------------------- 表单
      function field(f) {
        const id = "pc-" + f.key;
        let el;
        if (f.kind === "textarea") {
          el = h("textarea", { id, rows: f.rows || 4, placeholder: f.placeholder || "" });
        } else if (f.kind === "list") {
          el = h("textarea", { id, rows: 3, placeholder: f.placeholder || "" });
        } else if (f.kind === "json") {
          el = h("textarea", { id, rows: 3, placeholder: f.placeholder || "", class: "mono" });
        } else if (f.kind === "bool") {
          el = h("input", { id, type: "checkbox" });
        } else if (f.kind === "select") {
          el = h("select", { id });
          (f.options || []).forEach((o) => el.appendChild(
            h("option", { value: o, text: o === "" ? "（跟着全局）" : o })));
        } else if (f.kind === "number") {
          el = h("input", { id, type: "number", step: f.step || 0.1, placeholder: f.placeholder || "" });
        } else {
          el = h("input", { id, placeholder: f.placeholder || "" });
        }
        inputs[f.key] = { kind: f.kind, el };
        return h("div", { class: "row", style: "align-items:flex-start" }, [
          h("label", { class: "muted", style: "min-width:150px;padding-top:4px",
            text: f.label + (f.required ? " *" : "") }),
          h("div", { class: "grow" }, [
            el,
            f.hint ? h("p", { class: "muted", style: "margin:2px 0 6px", text: f.hint }) : null,
          ]),
        ]);
      }

      function buildForm() {
        formBox.innerHTML = "";
        (spec.groups || []).forEach((group) => {
          formBox.appendChild(card(group.title + (group.note ? " · " + group.note : ""),
            group.fields.map(field)));
        });
      }

      function fill(values) {
        Object.entries(inputs).forEach(([key, got]) => {
          const v = values[key];
          if (v === undefined || v === null) return;
          if (got.kind === "bool") got.el.checked = !!v;
          else if (got.kind === "list") got.el.value = Array.isArray(v) ? v.join("\n") : String(v);
          else if (got.kind === "json") got.el.value = (Array.isArray(v) && !v.length)
            || (v && typeof v === "object" && !Object.keys(v).length) ? "" : JSON.stringify(v, null, 1);
          else if (got.kind === "number") got.el.value = v === 0 ? "" : String(v);
          else got.el.value = String(v);
        });
      }

      function collect() {
        const out = {};
        const bad = [];
        Object.entries(inputs).forEach(([key, got]) => {
          if (got.kind === "bool") { out[key] = got.el.checked; return; }
          const raw = String(got.el.value || "").trim();
          if (got.kind === "list") { out[key] = raw ? raw.split("\n").map((s) => s.trim()).filter(Boolean) : []; return; }
          if (got.kind === "json") {
            if (!raw) { out[key] = {}; return; }
            try { out[key] = JSON.parse(raw); }
            catch (err) { bad.push(key); out[key] = {}; }
            return;
          }
          out[key] = raw;
        });
        return { fields: out, bad };
      }

      function showProblems(problems, warnings) {
        problemsBox.innerHTML = "";
        (problems || []).forEach((p) => problemsBox.appendChild(
          h("p", { style: "color:var(--err,#c00);margin:2px 0", text: "✗ " + p })));
        (warnings || []).forEach((w) => problemsBox.appendChild(
          h("p", { class: "muted", style: "margin:2px 0", text: "· " + w })));
      }

      async function doPreview(quiet) {
        const { fields, bad } = collect();
        if (bad.length) {
          showProblems(["这几个栏目的 JSON 写错了：" + bad.join("、")], []);
          return null;
        }
        const r = await ctx.api.safe(() => ctx.api.post("/api/persona/preview", { fields }));
        if (!r) { status.textContent = "预览失败（看日志页）"; return null; }
        showProblems(r.problems, r.warnings);
        jsonBox.textContent = r.problems && r.problems.length ? "" : JSON.stringify(r.json, null, 2);
        if (!quiet) {
          const has = (r.problems || []).length;
          if (has) status.textContent = "有 " + has + " 处要先改";
          else if (editing) status.textContent = "会写回 " + (editFile || r.target || "?")
            + "（★只动表单里这几栏★）";
          else status.textContent = "会写到 " + (r.target || "?")
            + (r.exists ? "（★已存在 → 请在上面的卡片上点「编辑资料卡」★）" : "（新文件）");
        }
        return r;
      }

      async function save() {
        const r = await doPreview(true);
        if (!r) return;
        if ((r.problems || []).length) { ctx.toast("先看红色那几条", "err"); return; }
        if (editing) {
          status.textContent = "正在写回 " + (editFile || r.target || "?") + "…";
          const got = await ctx.api.safe(() => ctx.api.post("/api/persona/update",
            { fields: r.fields, apply: true }));
          if (!got) { status.textContent = "写失败（看日志页）"; return; }
          if (!got.ok) { status.textContent = "没保存成：" + (got.error || ""); return; }
          const ch = (got.changed || []).length ? "改动 " + got.changed.join("、") : "内容没变";
          const rm = (got.removed || []).length ? "；清掉了 " + got.removed.join("、") : "";
          status.textContent = "已保存 " + got.file_rel + "：" + ch + rm
            + (got.backup ? "，旧文件备份在 " + got.backup.split(/[\\/]/).pop() : "");
          ctx.toast("已保存：" + got.name, "ok");
        } else {
          if (r.exists) {
            ctx.toast("这个 id 已经有了 —— 在上面她的卡片上点「编辑资料卡」改她", "err");
            return;
          }
          status.textContent = "正在写文件…";
          const got = await ctx.api.safe(() => ctx.api.post("/api/persona/create",
            { fields: r.fields, apply: true }));
          if (!got) { status.textContent = "写失败（看日志页）"; return; }
          if (!got.ok) { status.textContent = "没写成：" + (got.error || ""); return; }
          status.textContent = "已写入 " + got.file_rel
            + (got.added_index_row ? "，并在索引里加了一行" : "（索引里本来就有她）")
            + (got.backup ? "，旧文件备份在 " + got.backup.split(/[\\/]/).pop() : "");
          ctx.toast("角色已创建：" + got.name, "ok");
        }
        await refresh();
      }

      // ---------------------------------------------------------------- 模式切换
      function setMode() {
        const idEl = inputs.id && inputs.id.el;
        if (editing) {
          formTitle.textContent = "编辑角色资料：正在改 " + editing;
          modeNote.textContent = "改的是 " + (editFile || "她的人格文件")
            + " —— ★只动表单里这几栏★：enabled / default / voice_dir 这些「表单不暴露」的键、"
            + "以及本来就不认识的键都会原样保留（整份覆盖会把它们静默删掉，那样「只改了个称呼」也可能把她弄哑）。"
            + " id 是文件名，在这里锁住。";
          if (idEl) idEl.disabled = true;
          saveBtn.textContent = "保存修改";
          cancelBtn.classList.remove("hidden");
        } else {
          formTitle.textContent = "新建角色（按格式填）";
          modeNote.textContent = "";
          if (idEl) idEl.disabled = false;
          saveBtn.textContent = "创建角色";
          cancelBtn.classList.add("hidden");
        }
      }

      async function startEdit(id) {
        status.textContent = "正在读 " + id + " 的资料…";
        const d = await ctx.api.safe(() => ctx.api.get("/api/persona/get?cid=" + encodeURIComponent(id)));
        if (!d) { status.textContent = "读不到这个角色的资料（看日志页）"; return; }
        editing = d.id;
        editFile = d.file || "";
        template = d.fields || {};          // 编辑时「填模板」= 她当前的值
        fill(d.fields || {});
        setMode();
        if (lastRows) renderList(lastRows);  // 让那张卡片当场标上「★正在编辑★」（不再请求一遍）
        await doPreview(true);
        const quiet = Object.keys(d.quiet || {});
        status.textContent = "正在编辑「" + d.name + "」（" + (d.file || "?") + "）"
          + (quiet.length ? "；这些键不在这里改，但会原样保住：" + quiet.join("、") : "")
          + " —— 改完点「保存修改」。";
        formTitle.scrollIntoView({ behavior: "smooth", block: "start" });
      }

      function clearEdit() {
        // ★必须清空★：不清的话「取消编辑 → 创建」会把上一个角色的台词/背景/参考音
        // 原样带进新角色（真机截图里就是这么造出一个带着 probe 台词的「新人」的）。
        editing = "";
        editFile = "";
        template = (spec && spec.example) || {};
        blank();
        setMode();
        status.textContent = "已退出编辑（表单已清空 —— 现在填的表会建成一个新角色）";
        formTitle.scrollIntoView({ behavior: "smooth", block: "start" });
      }

      function blank() {
        const empty = {};
        Object.keys(inputs).forEach((k) => {
          const got = inputs[k];
          empty[k] = got.kind === "bool" ? false : (got.kind === "list" || got.kind === "json") ? [] : "";
        });
        fill(empty);
        problemsBox.innerHTML = "";
        jsonBox.textContent = "";
      }

      // ---------------------------------------------------------------- 导入 / 导出
      async function doImport() {
        const f = (filePick.files || [])[0];
        if (!f) { ctx.toast("先选一个 .json 或 .txt 文件", "err"); return; }
        status.textContent = "正在读 " + f.name + "…";
        let data = "";
        try {
          data = await readBase64(f);
        } catch (err) { ctx.toast(err.message, "err"); return; }
        const r = await ctx.api.safe(() => ctx.api.post("/api/persona/import",
          { filename: f.name, data }));
        if (!r) { status.textContent = "导入失败（看日志页）"; return; }
        showProblems(r.problems, r.note ? [r.note] : []);
        if ((r.problems || []).length) { status.textContent = "这个文件读不了"; return; }
        fill(r.fields);
        let extra = "";
        if (editing && String((r.fields || {}).id || "") !== editing) {
          // 导入的是另一个人 → 别拿她的字段去覆盖正在编辑的那个角色
          editing = "";
          editFile = "";
          template = (spec && spec.example) || {};
          setMode();
          extra = "（导入的是另一个 id，已切回「新建」模式）";
        }
        status.textContent = "已从 " + r.filename + " 填入表单（还没落盘）" + extra + " —— 检查一下再点「"
          + saveBtn.textContent + "」";
        await doPreview(true);
      }

      function downloadExport(cid) {
        const target = cid || exportPick.value;
        if (!target) { ctx.toast("先选一个要导出的角色", "err"); return; }
        ctx.api.get("/api/persona/export?cid=" + encodeURIComponent(target)).then((r) => {
          const blob = new Blob([JSON.stringify(r.json, null, 2) + "\n"],
            { type: "application/json" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = r.filename || target + ".json";
          a.click();
          URL.revokeObjectURL(a.href);
          ctx.toast("已导出 " + a.download, "ok");
        }).catch((err) => ctx.toast("导出失败：" + err.message, "err"));
      }

      // ---------------------------------------------------------------- 声线那半边
      // 读一个文件成 base64（不带上 data: 前缀也没关系，服务端两头都认）
      function readBase64(file) {
        return new Promise((resolve, reject) => {
          const fr = new FileReader();
          fr.onload = () => resolve(String(fr.result || "").split(",").pop());
          fr.onerror = () => reject(new Error("读文件失败"));
          fr.readAsDataURL(file);
        });
      }

      async function audition(id) {
        const r = await ctx.api.safe(() => ctx.api.post("/api/voices/audition",
          { id, text: sayText.value }));
        if (r) ctx.toast(r.ok ? "已切到该角色并念出（" + secs(r.seconds) + "）" : "没成：" + r.error,
          r.ok ? "ok" : "err");
        refresh();
      }

      async function switchTo(id) {
        const r = await ctx.api.safe(() => ctx.api.post("/api/voices/switch", { id }));
        if (r) ctx.toast(r.ok ? "已切到 " + r.message : "没切成：" + r.message, r.ok ? "ok" : "err");
        refresh();
      }

      // ★只用一条语音就能克隆★：挑一条素材（或传一条）→ 写进人格文件当参考 → 可选立刻试听
      // 文本能按文件名对上素材清单（<id>.txt）/她的台词时**自动填入** ——
      // 零样本克隆吃的是「这条音频 + 它逐字对应的文本」，文本对不上就会声不对词。
      function cloneBlock(c) {
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
        const box = h("p", { class: "muted", text: "" });

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

        async function apply(auditionIt) {
          const f = (file.files || [])[0];
          const body = { id: c.id, apply: true, audition: !!auditionIt, ref_text: refText.value };
          try {
            if (f) { body.data = await readBase64(f); body.filename = f.name; }
            else if (pick.value) { body.path = pick.value; }
            else { ctx.toast("先挑一条素材，或者选一个音频文件", "err"); return; }
          } catch (err) { ctx.toast("读文件失败：" + err.message, "err"); return; }
          box.textContent = auditionIt
            ? "正在写人格文件并让服务重建声线（首次可能要十几秒）…" : "正在写人格文件…";
          const r = await ctx.api.safe(() => ctx.api.post("/api/voices/clone", body));
          if (!r) { box.textContent = "请求失败（看日志页）"; return; }
          if (!r.ok) { box.textContent = "没成：" + (r.error || ""); ctx.toast("克隆参考没设上", "err"); return; }
          const changed = Object.keys(r.changed || {}).join("、") || "（无变化）";
          let msg = "参考已设为 " + r.reference + "（写入 " + changed
            + (r.backup ? "，旧文件备份在 " + r.backup.split("\\").pop() : "") + "）";
          if (r.ref_text) msg += "；参考文本：" + (r.ref_text.length > 24 ? r.ref_text.slice(0, 24) + "…" : r.ref_text)
            + "（来源 " + (r.text_source || "手填") + "）";
          else if (r.cleared_text) msg += "；旧参考文本已清掉（换了音频又没新文本，留着会声不对词）";
          if (r.audition) msg += r.audition.ok ? "；已念出（" + secs(r.audition.seconds) + "）"
            : "；但试听没成：" + (r.audition.message || "");
          box.textContent = msg;
          ctx.toast(r.audition ? (r.audition.ok ? "已应用并试听" : "已应用，试听失败") : "已应用克隆参考",
            r.audition && !r.audition.ok ? "err" : "ok");
          if (editing === c.id) startEdit(c.id);       // 表单里那条参考音也跟着更新
          refresh();
        }

        return h("details", { "data-clone": "1" }, [
          h("summary", { class: "muted", text: "★只用一条语音就能克隆★（现在参考：" + (c.voice_ref || "用全局") + "）" }),
          h("div", { style: "padding:6px 0" }, [
            h("p", { class: "muted", text: "素材目录 " + (c.materials_dir || "—") + "：" + (c.materials_count || 0)
              + " 条音频，其中 " + (c.materials_with_text || 0)
              + " 条能按文件名对上文本。挑一条（或传一段你自己的录音）→ 写进人格文件当参考 → 可选立刻试听。只换参考音是当场生效的，不用重启服务。" }),
            h("div", { class: "row" }, [h("div", { class: "grow" }, pick)]),
            h("div", { class: "row" }, [h("div", { class: "grow" }, file)]),
            h("div", { class: "row" }, [h("div", { class: "grow" }, refText)]),
            hint,
            h("div", { class: "row" }, [
              h("button", { class: "btn small primary", text: "设为克隆参考并试听", onclick: () => apply(true) }),
              h("button", { class: "btn small", text: "只设为参考", onclick: () => apply(false) }),
            ]),
            box,
          ]),
        ]);
      }

      function charCard(c) {
        const isEditing = editing === c.id;
        const node = card(c.name + (c.is_default ? " ·默认" : "") + (isEditing ? " ·★正在编辑★" : ""), [
          h("p", { class: "mono wrap-path", text: "id " + c.id + (c.title ? " · " + c.title : "") }),
          h("p", { class: "muted", text: "唤醒词：" + ((c.wake_words || []).join("、") || "—") }),
          h("p", { class: "muted", text: "称呼你：「" + (c.user_title || "你") + "」"
            + (c.ack ? " · 应答语：" + c.ack : "") }),
          c.style && c.style.length ? h("p", { class: "muted", text: "风格：" + c.style.join(" / ") }) : null,
          h("p", { class: "mono wrap-path", text: "参考音：" + (c.voice_ref || "（用全局）")
            + (c.voice_ref ? (c.voice_ref_exists ? " ✓" : " ✗ 文件不在") : "") }),
          c.voice_model ? h("p", { class: "mono wrap-path", text: "专属模型：" + c.voice_model
            + (c.voice_model_exists ? " ✓" : " ✗ 目录不在") }) : null,
          c.ref_candidates && c.ref_candidates.length
            ? h("details", {}, [
                h("summary", { class: "muted", text: "素材库 " + (c.materials_dir || "（没写 voice_dir）")
                  + "：" + c.ref_candidates.length + " 条音频，" + (c.materials_with_text || 0) + " 条有文本" }),
                h("div", { class: "mono", style: "font-size:11px" }, c.ref_candidates.map((it) => h("div", {
                  text: "· " + it.name + (it.seconds ? "  " + it.seconds.toFixed(1) + "s" : "")
                    + (it.text ? "  ✓有文本" : "（没文本）")
                    + (it.ok ? "" : "  ✗ " + (it.problem || "不能用")),
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
            h("button", { class: "btn small", text: isEditing ? "正在编辑…" : "★编辑资料卡★",
              disabled: isEditing, onclick: () => startEdit(c.id) }),
            h("button", { class: "btn small", text: "下载 JSON", onclick: () => downloadExport(c.id) }),
          ]),
          cloneBlock(c),
        ]);
        // ★整张卡片可点 = 进编辑★（用户要的：想改哪个就直接点它，不用去底下找表单）
        node.classList.add("persona-card");
        if (isEditing) node.classList.add("editing");
        node.tabIndex = 0;
        // 手型光标写两份：样式表里一份（.persona-card），这里再 inline 一份 ——
        // 浏览器的 CSS 缓存有可能还是旧的，而「能点」这件事最好别依赖缓存。
        node.style.cursor = "pointer";
        node.title = isEditing ? "正在编辑她" : "点这张卡片就能改她的资料（也可以按回车）";
        const open = () => { if (!isEditing) startEdit(c.id); };
        node.addEventListener("click", (e) => {
          // 卡里的按钮/下拉/输入框/details（克隆那块）自己有行为，别抢
          if (e.target.closest("button, input, select, textarea, summary, label, a, [data-clone]")) return;
          open();
        });
        node.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
        });
        return node;
      }

      function renderList(d) {
        lastRows = d;
        listBox.innerHTML = "";
        if (d.error) listBox.appendChild(card("角色文件有问题", UI.line(d.error, "mono")));
        const chars = d.characters || [];
        listBox.appendChild(card(
          "角色（默认 " + (d.resolved_default || d.default || "—") + " · 人格文件 " + d.persona_file + "）",
          [
            h("p", { class: "muted", text: "★想改哪个就直接点她那张卡片★（表单会带出她现在的资料并滚到下面；"
              + "卡里的按钮、下拉、克隆那块仍然是各自的功能）" }),
            h("div", { class: "grid" }, chars.map(charCard)),
          ]));
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
        exportPick.innerHTML = "";
        chars.forEach((c) => exportPick.appendChild(h("option", { value: c.id, text: c.id })));
      }

      function refresh(attempt) {
        const n = attempt || 0;
        if (!listBox.childElementCount) listBox.appendChild(emptyHint("正在读角色与声线…"));
        return ctx.api.get("/api/voices")
          .then((d) => { dropHint(); renderList(d); return d; })
          .catch((err) => {
            dropHint();
            // ★失败必须看得见★：只弹一个几秒就消失的提示，会让「列表空白」变成一个说不清的谜
            //（真实案例：控制台进程还是旧的 → /api/voices 不存在 → 404，页面就永远空着）。
            if (n < 1) { setTimeout(() => refresh(n + 1), 1200); return null; }   // 自己再试一次
            showListError(err);
            return null;
          });
      }

      function emptyHint(text) {
        return h("p", { class: "muted", id: "persona-loading", text });
      }

      function dropHint() {
        const got = listBox.querySelector("#persona-loading");
        if (got) got.remove();
      }

      function showListError(err) {
        if (listBox.childElementCount) {      // 已经有上一次的好数据：留着，只弹个提示
          ctx.toast("刷新角色失败：" + err.message, "err");
          return;
        }
        // ★404 有专属解辞★：这个项目的控制台是「静态脚本从磁盘现取、路由却在进程里」，
        // 改了面板代码但没重启控制台时，就会正好撞上这种情况。
        const stale = /not found|404/i.test(String(err.message || ""));
        listBox.appendChild(card("读不到角色列表", [
          h("p", { style: "color:var(--err,#c00);margin:2px 0", text: "✗ " + err.message }),
          h("p", { class: "muted", text: stale
            ? "接口不在这台控制台上 —— 十有八九是这个控制台进程在改代码之前就启动了"
              + "（页面脚本是现取的新的，路由却还在旧进程里）。重启控制台（Ctrl+C 再 python main.py ui）"
              + "后点下面的「重试」就行，不必刷新页面。"
            : "去「日志」标签页看背面的报错，再点「重试」。" }),
          h("div", { class: "row" }, [
            h("button", { class: "btn small primary", text: "重试", onclick: () => refresh() }),
          ]),
        ]));
      }

      function loadSpec() {
        ctx.api.get("/api/persona/spec").then((d) => {
          spec = d;
          template = d.example || {};
          meta.textContent = "资料卡写到 " + d.personas_dir + "/<id>.json，并往 " + d.index
            + " 追加一行 —— 只有索引里有的角色才会被唤醒。改完保存即生效（几秒内热加载）。";
          if (!formBox.childElementCount) {
            buildForm();
            blank();
          }
          setMode();
        }).catch((err) => {
          status.textContent = "读不到表单规范：" + err.message;
          showProblems(["读不到表单规范：" + err.message
            + "（若是「Not Found」，说明这个控制台进程还是旧的 —— 重启它后刷新本页）"], []);
        });
      }

      // ---------------------------------------------------------------- 组装
      root.appendChild(card("试听（声音由服务放出来）", [
        h("div", { class: "row" }, [
          h("div", { class: "grow" }, sayText),
          h("button", {
            class: "btn", text: "用当前声线念这句",
            onclick: async (e) => {
              e.target.disabled = true;
              const r = await ctx.api.safe(() => ctx.api.post("/api/voices/audition",
                { text: sayText.value, switch: false }));
              e.target.disabled = false;
              if (r) ctx.toast(r.ok ? "已念出（" + secs(r.seconds) + "）" : "没念成：" + r.error,
                r.ok ? "ok" : "err");
            },
          }),
          h("button", { class: "btn small", text: "重新读一遍",
            onclick: () => { listBox.innerHTML = ""; refresh(); } }),
        ]),
        h("p", { class: "muted", text: "点某个角色的「切过去并试听」= 先切声线再念这句（两步都走信箱，服务最多 5 秒内响应）。服务没启动会失败。" }),
      ]));

      root.appendChild(listBox);

      root.appendChild(h("div", { class: "card", id: "persona-form" }, [
        h("div", { class: "row" }, [formTitle, cancelBtn]),
        modeNote,
        h("div", { class: "row" }, [
          h("button", { class: "btn small", text: "填一份模板", onclick: () => { fill(template); doPreview(true); } }),
          h("button", { class: "btn small", text: "清空", onclick: () => { blank(); status.textContent = ""; } }),
          h("button", { class: "btn small", text: "看 JSON（不写文件）", onclick: () => doPreview(false) }),
          saveBtn,
        ]),
        h("div", { class: "row" }, [
          h("div", { class: "grow" }, filePick),
          h("button", { class: "btn small", text: "从文件导入", onclick: doImport }),
        ]),
        h("p", { class: "muted", text: "导入认两种：① .json —— 一张资料卡（本面板导出的就是，也认数组/索引，取第一个）；② .txt —— 素材清单（名字一行 + 正文一行），只填「示例台词」这一栏。" }),
        meta,
        formBox,
        problemsBox,
        status,
        jsonBox,
        h("hr", { style: "border:none;border-top:1px solid var(--line,#333);margin:12px 0" }),
        h("div", { class: "row" }, [
          h("div", { class: "grow" }, exportPick),
          h("button", { class: "btn small", text: "下载她的人格文件 JSON", onclick: () => downloadExport() }),
        ]),
      ]));

      loadSpec();
      refresh();

      handleEvent = (ev) => {
        // 角色被切走 / 语音里喊了名字 → 列表跟着刷新（编辑中的表单不动，别把用户填的一半冲掉）
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
