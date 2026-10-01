/* 角色资料卡面板：按格式填一个人格文件，或者直接选文件导进来。
   ★表单结构不是写死在前端的★：字段/说明/类型全部来自 /api/persona/spec ——
   后端那份 SPEC 是唯一真相源，加了字段这里不用改。 */
(function () {
  let handleEvent = null;

  const KIND_HINT = {
    list: "一行一条",
    json: "JSON（对象或数组）",
  };

  window.Console.register("persona", {
    render(root, ctx) {
      const { h, card } = UI;
      let example = {};                       // 样板（点「填模板」用）
      const inputs = {};                      // key -> {kind, el}
      let spec = null;

      const status = h("p", { class: "muted", text: "" });
      const problemsBox = h("div", {});
      const jsonBox = h("pre", { class: "mono", style: "font-size:11px;max-height:320px;overflow:auto" });
      const filePick = h("input", { type: "file", accept: ".json,.txt,application/json,text/plain" });
      const overwrite = h("input", { type: "checkbox" });
      const exportPick = h("select", {});
      const formBox = h("div", {});
      const meta = h("p", { class: "muted", text: "" });

      // ---- 表单 ----
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
          status.textContent = r.problems && r.problems.length
            ? "有 " + r.problems.length + " 处要先改"
            : "会写到 " + (r.target || "?") + (r.exists ? "（★已存在，要勾「覆盖已存在」★）" : "（新文件）");
        }
        return r;
      }

      async function doCreate() {
        const r = await doPreview(true);
        if (!r || (r.problems || []).length) { ctx.toast("先看红色那几条", "err"); return; }
        if (r.exists && !overwrite.checked) {
          ctx.toast("这个 id 已经有了：想覆盖就勾「覆盖已存在」", "err");
          return;
        }
        status.textContent = "正在写文件…";
        const got = await ctx.api.safe(() => ctx.api.post("/api/persona/create",
          { fields: r.fields, apply: true, overwrite: overwrite.checked }));
        if (!got) { status.textContent = "写失败（看日志页）"; return; }
        if (!got.ok) { status.textContent = "没写成：" + (got.error || ""); return; }
        status.textContent = "已写入 " + got.file_rel
          + (got.added_index_row ? "，并在索引里加了一行" : "（索引里本来就有她）")
          + (got.backup ? "，旧文件备份在 " + got.backup.split(/[\\/]/).pop() : "");
        ctx.toast("角色已创建：" + got.name, "ok");
        load();
      }

      async function doImport() {
        const f = (filePick.files || [])[0];
        if (!f) { ctx.toast("先选一个 .json 或 .txt 文件", "err"); return; }
        status.textContent = "正在读 " + f.name + "…";
        let data = "";
        try {
          data = await new Promise((resolve, reject) => {
            const fr = new FileReader();
            fr.onload = () => resolve(String(fr.result || "").split(",").pop());
            fr.onerror = () => reject(new Error("读文件失败"));
            fr.readAsDataURL(f);
          });
        } catch (err) { ctx.toast(err.message, "err"); return; }
        const r = await ctx.api.safe(() => ctx.api.post("/api/persona/import",
          { filename: f.name, data }));
        if (!r) { status.textContent = "导入失败（看日志页）"; return; }
        showProblems(r.problems, r.note ? [r.note] : []);
        if ((r.problems || []).length) { status.textContent = "这个文件读不了"; return; }
        fill(r.fields);
        status.textContent = "已从 " + r.filename + " 填入表单（还没落盘）——检查一下再点「创建」";
        await doPreview(true);
      }

      function downloadExport() {
        const cid = exportPick.value;
        if (!cid) { ctx.toast("先选一个要导出的角色", "err"); return; }
        ctx.api.get("/api/persona/export?cid=" + encodeURIComponent(cid)).then((r) => {
          const blob = new Blob([JSON.stringify(r.json, null, 2) + "\n"],
            { type: "application/json" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = r.filename;
          a.click();
          URL.revokeObjectURL(a.href);
          ctx.toast("已导出 " + r.filename, "ok");
        }).catch((err) => ctx.toast("导出失败：" + err.message, "err"));
      }

      function load() {
        ctx.api.get("/api/persona/spec").then((d) => {
          spec = d;
          example = d.example || {};
          meta.textContent = "资料卡写到 " + d.personas_dir + "/<id>.json，并往 " + d.index
            + " 追加一行 —— 只有索引里有的角色才会被唤醒。改完保存即生效（几秒内热加载）。";
          if (!formBox.childElementCount) {
            buildForm();
            fill({ id: "", name: "" });       // 默认留空：先让人自己决定她是谁
          }
          exportPick.innerHTML = "";
          (d.ids || []).forEach((id) => exportPick.appendChild(
            h("option", { value: id, text: id })));
          status.textContent = "现有 " + (d.ids || []).length + " 个角色。填完点「预览」再「创建」。";
        }).catch((err) => { status.textContent = "读不到表单规范：" + err.message; });
      }

      root.appendChild(card("新建角色（按格式填）", [
        meta,
        h("div", { class: "row" }, [
          h("button", { class: "btn small", text: "填一份模板", onclick: () => { fill(example); doPreview(true); } }),
          h("button", { class: "btn small", text: "清空", onclick: () => {
            const blank = {}; Object.keys(inputs).forEach((k) => { blank[k] = ""; });
            fill(blank); problemsBox.innerHTML = ""; jsonBox.textContent = ""; status.textContent = "";
          } }),
          h("label", { class: "muted", style: "display:flex;align-items:center;gap:4px" },
            [overwrite, "覆盖已存在"]),
        ]),
        h("div", { class: "row" }, [
          h("div", { class: "grow" }, filePick),
          h("button", { class: "btn small", text: "从文件导入", onclick: doImport }),
        ]),
        h("p", { class: "muted", text: "导入认两种：① .json —— 一张资料卡（本面板导出的就是）；② .txt —— 素材清单（名字一行 + 正文一行），只填「示例台词」这一栏。" }),
      ]));

      root.appendChild(card("检查与落盘", [
        h("div", { class: "row" }, [
          h("button", { class: "btn small", text: "预览（不写文件）", onclick: () => doPreview(false) }),
          h("button", { class: "btn small primary", text: "创建角色", onclick: doCreate }),
        ]),
        problemsBox,
        status,
        jsonBox,
      ]));

      root.appendChild(card("导出已有角色", [
        h("div", { class: "row" }, [
          h("div", { class: "grow" }, exportPick),
          h("button", { class: "btn small", text: "下载 JSON", onclick: downloadExport }),
        ]),
        h("p", { class: "muted", text: "导出的是她当前的人格文件内容，可以直接分享或存进 git（音频素材另算）。" }),
      ]));

      root.appendChild(formBox);
      load();

      handleEvent = (ev) => {
        // 角色被切走 / 语音里喊了名字 → 刷新导出列表
        if (ev.kind === "log" && ev.data && /\[角色\]/.test(ev.data.line || "")) {
          clearTimeout(load._t);
          load._t = setTimeout(load, 1200);
        }
      };
    },

    onEvent(ev, ctx) { if (handleEvent) handleEvent(ev, ctx); },
    onLeave() { handleEvent = null; },
  });
})();
