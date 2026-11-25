/* 工具面板：模型能 call 什么、这些工具从哪来、谁在拦着。
   ★真实清单要问服务★（POST /api/tools/live）——只有服务知道当前角色是不是 API 引擎、
   api_tools 展开了什么、MCP 宿主里哪个服务器真的连上了。静态那几张卡只读配置。 */
(function () {
  window.Console.register("tools", {
    render(root, ctx) {
      const { h, card, table, secs } = UI;
      let data = null;        // /api/tools 的静态部分
      let live = null;        // 服务自报的真实清单（/api/tools/live）
      let liveMsg = "还没问服务。静态部分（预设 / 服务器 / 角色多开）不用服务也能看。";

      // ---------------- 实时清单 ----------------
      const liveBox = h("p", { class: "muted", text: liveMsg });
      const liveBtn = h("button", {
        class: "btn primary", text: "问服务要真实清单",
        onclick: async () => {
          liveBtn.disabled = true;
          liveBox.textContent = liveMsg = "正在问服务（它最多 5 秒内响应）…";
          const r = await ctx.api.safe(() => ctx.api.post("/api/tools/live", {}));
          liveBtn.disabled = false;
          if (!r) { liveBox.textContent = liveMsg = "请求失败（看日志页）"; return; }
          if (!r.ok) {
            live = null;
            liveBox.textContent = liveMsg = "没问到：" + (r.error || "服务没回执");
            renderAll();
            return;
          }
          live = r.data || {};
          liveBox.textContent = liveMsg = "√ 服务自报 " + (live.total || 0) + " 个工具（"
            + secs(r.seconds) + "）";
          renderAll();
        },
      });

      const body = h("div", {});
      root.appendChild(body);

      function renderAll() {
        body.innerHTML = "";

        // ---- 1. 现在模型能 call 什么 ----
        const blocks = [h("div", { class: "row" }, [liveBtn]), liveBox];
        if (live && live.error) {
          blocks.push(h("p", { class: "mono wrap-path", text: String(live.error) }));
        } else if (live) {
          const who = (live.character_name || "—") + "（" + (live.character_id || "—") + "）";
          blocks.push(table([{ key: "k", label: "项", cls: "num" }, { key: "v", label: "值" }], [
            { k: "服务当前角色", v: who },
            { k: "引擎 / 路由", v: (live.engine === "api" ? "云端 API" : "本地 Ollama")
                + " · router=" + (live.router || "—") },
            { k: "harness 多轮循环", v: live.agent_mode
                ? h("span", { class: "tag ok", text: "开着（模型→工具→模型）" })
                : h("span", { class: "tag", text: "关（单轮：选一个工具就执行）" }) },
            { k: "模型能看到的工具", v: h("strong", { text: String(live.total || 0) + " 个" }) },
            { k: "MCP 宿主现状", v: h("span", { class: "mono wrap-path", text: live.mcp_status || "—" }) },
            { k: "角色卡多开的", v: (live.extra || []).length
                ? h("span", { class: "mono wrap-path", text: (live.extra || []).join("、") })
                : h("span", { class: "muted", text: "没有（本地角色永远没有）" }) },
          ]));
          if (live.missing && live.missing.length) {
            blocks.push(h("p", { class: "muted", text: "★这些名字点名了但没有服务器提供★：" + live.missing.join("、") }));
          }
          if (live.warn) blocks.push(h("p", {}, h("span", { class: "tag warn", text: live.warn })));

          // 按服务器分组列出（这一份就是真的工具清单）
          const groups = new Map();
          (live.tools || []).forEach((t) => {
            const key = t.server || "（未知）";
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(t);
          });
          if (groups.size === 0) {
            blocks.push(h("p", { class: "muted", text: "服务里现在一个工具都没有（检查 [[mcp.servers]] 与 [llm] router）" }));
          }
          groups.forEach((items, server) => {
            blocks.push(h("h3", { text: server + "（" + items.length + "）" }));
            blocks.push(table([
              { key: "name", label: "模型看到的名字", render: (r) => h("span", { class: "mono wrap-path", text: r.name }) },
              { key: "tool", label: "服务器内的名字", render: (r) => h("span", { class: "mono", text: r.tool }) },
              { key: "description", label: "说明", render: (r) => h("span", { class: "muted", text: (r.description || "").split("\n")[0].slice(0, 90) }) },
            ], items));
          });
        }
        body.appendChild(card("模型现在能 call 什么（服务自报）", blocks));

        if (!data) return;

        // ---- 2. 预设：角色卡里写这些名字 ----
        const presets = data.presets || [];
        body.appendChild(card("预设（角色卡 api_tools 里写这些名字，见 voice_loop/tool_presets.py）", [
          table([
            { key: "name", label: "预设", render: (r) => h("span", { class: "mono", text: r.name }) },
            { key: "title", label: "是什么" },
            { key: "mode", label: "暴露方式", render: (r) => (r.mode === "progressive"
                ? h("span", { class: "tag ok", text: "渐进：核心 + 按需发现" })
                : h("span", { class: "tag", text: "全量" })) },
            { key: "count", label: "几个", cls: "num" },
            { key: "tools", label: "展开成", render: (r) => h("span", { class: "mono wrap-path", text: r.tools.join("、") }) },
          ], presets, { emptyText: "还没定义预设。" }),
          h("p", { class: "muted", text: "也可以直接写具体工具名（不是预设的名字就当成一个工具去 MCP 宿主里找）。"
            + "找不到的名字只打警告，不会让一轮对话失败。" }),
          h("p", { class: "muted", text: "渐进式预设的 tools 数组固定较小，长工具靠 "
            + (data.discovery?.tools || []).join(" / ") + " 按需发现。"
            + (data.discovery?.note || "") }),
        ]));

        // ---- 3. 每个角色会多开什么 ----
        const chars = data.characters || [];
        body.appendChild(card("每个角色会多开什么（engine=api 才生效）", [
          table([
            { key: "name", label: "角色" },
            { key: "engine", label: "引擎", render: (r) => (r.engine === "api" ? "云端 API" : "本地 Ollama") },
            { key: "entries", label: "卡片里写的", render: (r) => (r.entries || []).length
                ? h("span", { class: "mono wrap-path", text: r.entries.map((e) => e.kind === "preset" ? e.name + "（预设）" : e.name).join("、") })
                : h("span", { class: "muted", text: r.from_global ? "（跟 [api] extra_tools）" : "没写" }) },
            { key: "expanded", label: "展开成", render: (r) => r.active
                ? h("span", { class: "mono wrap-path", text: (r.expanded || []).join("、") || "（空）" })
                : h("span", { class: "muted", text: r.why || "不生效" }) },
            { key: "count", label: "多开", cls: "num", render: (r) => (r.active ? String((r.expanded || []).length) : "0") },
            { key: "discoverable", label: "按需可发现", render: (r) => (r.mode === "progressive" && r.active
                ? h("span", { class: "mono wrap-path", text: "另 " + (r.discoverable || []).length + " 项：" + (r.discoverable || []).join("、") })
                : h("span", { class: "muted", text: "—" })) },
          ], chars, { emptyText: "还没有角色文件。" }),
          h("p", { class: "muted", text: "「多开」是在默认白名单之上的增量；真实固定数量看上面服务自报那一行。"
            + "渐进式角色另有一批「按需可发现」工具，不进每轮 tools 数组——发现不等于授权，"
            + "调用仍受白名单 / guard 约束。" }),
        ]));

        // ---- 4. MCP 服务器 ----
        const servers = (data.mcp || {}).servers || [];
        body.appendChild(card("[mcp.servers]（config.toml）· " + (data.mcp && data.mcp.enabled ? "已启用" : "已关闭"), [
          table([
            { key: "name", label: "服务器", render: (r) => h("span", { class: "mono", text: r.name }) },
            { key: "transport", label: "传输", render: (r) => h("span", { class: "mono", text: r.transport }) },
            { key: "allowlist", label: "白名单", render: (r) => r.allowlist.length
                ? h("span", { class: "mono wrap-path", text: r.allowlist.join("、") })
                : h("span", { class: "tag warn", text: "全部（不推荐）" }) },
            { key: "hidden", label: "露出", render: (r) => (r.hidden
                ? h("span", { class: "tag warn", text: "hidden：等角色点名" })
                : h("span", { class: "tag ok", text: "进白名单就露出" })) },
            { key: "namespace", label: "名字前缀", render: (r) => (r.namespace ? "mcp__" + r.name + "__" : "不加（原样）") },
            { key: "timeout", label: "超时", cls: "num", render: (r) => r.timeout + "s" },
          ], servers, { emptyText: "没有配置任何服务器（工具一个都不会有）。" }),
          h("p", { class: "muted", text: "hidden 的服务器**默认不露给任何角色**：API 角色在人格文件里写 "
            + "api_tools 点名才会看到；本地 4B 看不到，也就不会误调。白名单之外的工具根本不出现在模型面前。" }),
        ]));

        // ---- 5. 安全边界 ----
        const ag = data.agent || {};
        body.appendChild(card("[agent]（harness 的安全边界）", [
          table([{ key: "k", label: "项", cls: "num" }, { key: "v", label: "值" }], [
            { k: "harness 开关", v: ag.enabled ? "开（API 角色走多轮工具循环）" : "关（只做单轮选工具）" },
            { k: "最大轮数", v: (ag.max_rounds || 0) + " 轮（到顶会不给工具、强制收口）" },
            { k: "单个工具结果上限", v: (ag.max_tool_chars || 0) + " 字（超了截断再喂回模型）" },
            { k: "工作区", v: h("span", { class: "mono wrap-path", text: ag.workspace || "." }) },
            { k: "写文件", v: ag.allow_write
                ? h("span", { class: "tag warn", text: "允许 write_file / edit_file" })
                : h("span", { class: "tag", text: "关（只读）" }) },
            { k: "执行命令", v: ag.allow_shell
                ? h("span", { class: "tag err", text: "允许 run_command（等于把本机 shell 交给模型）" })
                : h("span", { class: "tag", text: "关" }) },
          ]),
          h("p", { class: "muted", text: "harness 运行时还自带两个工具（不在 MCP 里）："
            + (data.runtime_tools || []).map((t) => t.name + "（" + t.desc + "）").join("；") + "。" }),
        ]));
      }

      ctx.api.get("/api/tools")
        .then((d) => { data = d; renderAll(); })
        .catch((err) => ctx.toast("工具清单读取失败：" + err.message, "err"));
    },

    onLeave() {},
  });
})();
