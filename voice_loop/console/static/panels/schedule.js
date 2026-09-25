/* 日程 / 闹钟面板：列表 + 一句话添加 + 编辑表单 + 周视图。 */
(function () {
  let handleEvent = null;

  window.Console.register("schedule", {
    render(root, ctx) {
      const { h, card, table, field, collect } = UI;
      let data = { items: [] };
      let schema = { repeats: [], categories: [], weekdays: [] };
      let weekStart = null;      // 周视图基准日期（null = 今天）
      let editing = null;        // 正在编辑的事件 id

      // ---------------- 一句话添加 ----------------
      const quick = h("input", { placeholder: "比如：明天下午三点组会，提前半小时 / 每周三 9 点上课" });
      const quickAdd = async () => {
        const text = quick.value.trim();
        if (!text) return;
        const r = await ctx.api.safe(() => ctx.api.post("/api/events", { text }));
        if (!r) return;
        quick.value = "";
        ctx.toast(r.sentence || "已添加", "ok");
        refresh();
      };
      quick.addEventListener("keydown", (e) => { if (e.key === "Enter") quickAdd(); });

      // ---------------- 结构化表单（新增/编辑共用） ----------------
      const formBox = h("div", {});

      function openForm(item) {
        editing = item ? item.id : null;
        const it = (item && item.item) || {};
        const inputs = {};
        const f = h("form", { class: "grid" });
        const add = (key, label, config) => {
          const wrap = field(label, config, null);
          wrap.dataset.key = key;
          inputs[key] = wrap;
          f.appendChild(wrap);
        };
        add("title", "内容（留空=纯闹钟）", { value: it.title || "" });
        add("start", "时间（一次性事件：日期+时刻）", { type: "datetime-local", value: _toLocalInput(it.start) });
        add("repeat", "重复", {
          type: "select", value: it.repeat || "once",
          options: schema.repeats.map((r) => ({ value: r.value, label: r.label })),
        });
        add("time", "钟点（★重复事件用这个★ HH:MM）", { type: "time", value: it.time || "" });
        add("weekday", "星期（每周/每两周用）", {
          type: "select", value: it.weekday === undefined || it.weekday === null ? "0" : String(it.weekday),
          options: schema.weekdays.map((w) => ({ value: String(w.value), label: w.label })),
        });
        add("remind_before", "提前量（分钟，逗号分隔；0=准时）", {
          value: (it.remind_before || [0]).join(","),
        });
        add("duration_minutes", "时长（分钟，0=不占时间）", { type: "number", value: it.duration_minutes || 0, min: 0 });
        add("category", "标签", {
          type: "select", value: it.category || "",
          options: schema.categories.map((c) => ({ value: c.value, label: c.label })),
        });
        add("location", "地点", { value: it.location || "" });
        add("note", "备注（提醒时会念出来）", { value: it.note || "" });
        add("until", "截止日期（YYYY-MM-DD）", { value: it.until || "" });

        const submit = h("button", {
          class: "btn primary", type: "submit", text: editing ? "保存改动" : "添加",
        });
        const cancel = h("button", {
          class: "btn ghost", type: "button", text: "取消",
          onclick: () => { formBox.innerHTML = ""; editing = null; },
        });

        const doSubmit = async () => {
          const payload = collect(f);
          if (editing) {
            const ok = await ctx.api.safe(
              () => ctx.api.patch("/api/events/" + editing, { fields: payload, confirm: true }),
              "已保存");
            if (ok) { formBox.innerHTML = ""; editing = null; refresh(); }
          } else {
            const r = await ctx.api.safe(() => ctx.api.post("/api/events", { fields: payload }));
            if (r) { ctx.toast(r.sentence || "已添加", "ok"); formBox.innerHTML = ""; refresh(); }
          }
        };
        UI.onSubmit(f, doSubmit);
        f.appendChild(h("div", { class: "row" }, [submit, cancel]));
        formBox.innerHTML = "";
        formBox.appendChild(card(editing ? "编辑事件 #" + editing : "手动添加一条", f));
      }

      // ---------------- 列表 ----------------
      const listBox = h("div", {});
      const weekBox = h("div", {});

      function renderList() {
        listBox.innerHTML = "";
        const items = data.items || [];
        if (data.missing_id) {
          listBox.appendChild(card("需要整理一下数据", [
            h("p", { text: "有 " + data.missing_id + " 条事件没有 id（多半是手工编辑过 data/events.json）。"
              + "没有 id 就没法改/删（那些按钮点了会报错）。点一下右边就能补上——不会动其他字段。" }),
            h("div", { class: "row" }, [
              h("button", {
                class: "btn primary", text: "补 id（整理数据）",
                onclick: async () => {
                  const r = await ctx.api.safe(() => ctx.api.post("/api/events/tidy", {}));
                  if (r) { ctx.toast("已补 " + r.fixed + " 条", "ok"); refresh(); }
                },
              }),
            ]),
          ]));
        }
        listBox.appendChild(card("全部事件（" + items.length + " 条）· 现在 " + (data.now || ""), [
          h("div", { class: "row" }, [
            h("button", {
              class: "btn", text: "只显示重复的",
              onclick: () => { filter.kind = filter.kind === "repeat" ? "all" : "repeat"; refresh(); },
            }),
            h("button", {
              class: "btn", text: "只显示一次性的",
              onclick: () => { filter.kind = filter.kind === "alarm" ? "all" : "alarm"; refresh(); },
            }),
            h("span", { class: "muted", text: "当前筛选：" + (filter.kind === "all" ? "全部" : filter.kind) }),
          ]),
          table([
            { key: "id", label: "#", cls: "num", render: (r) => (r.id === null || r.id === undefined ? "无" : r.id) },
            { key: "next_at", label: "下一次", cls: "num", render: (r) => r.next_at
                ? h("div", {}, [h("div", { text: r.next_at }),
                                r.in_minutes !== null ? h("span", { class: "muted", text: (r.in_minutes >= 0 ? "还有 " : "已过 ") + Math.abs(r.in_minutes) + " 分" }) : null])
                : h("span", { class: "muted", text: "—" }) },
            { key: "title", label: "内容", render: (r) => h("div", {}, [
                h("div", { text: r.title }),
                r.location ? h("span", { class: "muted", text: "📍" + r.location }) : null,
                r.note ? h("span", { class: "muted", text: " · " + r.note }) : null,
              ]) },
            { key: "repeat_text", label: "重复", render: (r) => r.repeat_text || "一次" },
            { key: "leads", label: "提前", render: (r) => r.leads.map(leadText).join(" / ") },
            { key: "category", label: "标签", render: (r) => (r.category ? h("span", { class: "tag", text: r.category }) : "") },
            { key: "state", label: "状态", render: (r) => {
                const done = (r.state.done || []).length, fired = (r.state.fired || []).length;
                const skipped = (r.state.skipped || []).length;
                return h("div", { class: "row" }, [
                  done ? h("span", { class: "tag ok", text: "完成 " + done }) : null,
                  fired ? h("span", { class: "tag", text: "已响 " + fired }) : null,
                  skipped ? h("span", { class: "tag warn", text: "跳过 " + skipped }) : null,
                ]);
              } },
            { key: "act", label: "", cls: "act", render: (r) => h("div", { class: "row" }, [
                r.id === null || r.id === undefined
                  ? h("span", { class: "muted", text: "先补 id 才能操作" })
                  : h("div", { class: "row" }, [
                      h("button", { class: "btn small", text: "跳过本次", title: "只跳过下一次，不删事件",
                        onclick: () => act(r, "skip") }),
                      h("button", { class: "btn small", text: "完成", onclick: () => act(r, "done") }),
                      h("button", { class: "btn small", text: "编辑", onclick: () => openForm(r) }),
                      h("button", { class: "btn small danger", text: "删除",
                        onclick: async () => {
                          const warn = r.repeating
                            ? "「" + r.title + "」是重复事件（" + (r.repeat_text || "") + "），删掉会影响往后每一次。确定？"
                            : "删除「" + r.title + "」？";
                          if (!confirm(warn)) return;
                          const ok = await ctx.api.safe(
                            () => ctx.api.del("/api/events/" + r.id + "?confirm=1"), "已删除");
                          if (ok) refresh();
                        } }),
                    ]),
              ]) },
          ], items, { emptyText: "还没有任何事件。用上面那个输入框一句话加一条试试。" }),
        ]));
      }

      async function act(row, action) {
        const r = await ctx.api.safe(() => ctx.api.post("/api/events/" + row.id + "/" + action, {}));
        if (r) { ctx.toast(r.message || "已处理", "ok"); refresh(); }
      }

      // ---------------- 周视图 ----------------
      function renderWeek() {
        weekBox.innerHTML = "";
        ctx.api.get("/api/events/week" + (weekStart ? "?start=" + weekStart : ""))
          .then((w) => {
            const nav = h("div", { class: "row" }, [
              h("button", { class: "btn small", text: "上一周", onclick: () => shiftWeek(-7) }),
              h("button", { class: "btn small", text: "本周", onclick: () => { weekStart = null; renderWeek(); } }),
              h("button", { class: "btn small", text: "下一周", onclick: () => shiftWeek(7) }),
              h("span", { class: "muted grow", text: w.base + " 起 7 天" }),
            ]);
            const grid = h("div", { class: "week" }, w.days.map((d) => h("div", {
              class: "day" + (d.is_today ? " today" : ""),
            }, [
              h("h4", { text: d.date.slice(5) + " " + d.label + (d.is_today ? " ·今天" : "") }),
              ...d.items.map((it) => h("div", { class: "slot", title: it.at }, [
                h("div", { text: it.title }),
                h("span", { class: "t", text: it.time + (it.duration_minutes ? " · " + it.duration_minutes + "分" : "") }),
                it.done ? h("span", { class: "tag ok", text: "完成" }) : null,
              ])),
              d.items.length === 0 ? h("p", { class: "muted", text: "—" }) : null,
            ])));
            weekBox.appendChild(card("周视图", [nav, grid]));
          })
          .catch((err) => { weekBox.appendChild(card("周视图", UI.line("读取失败：" + err.message))); });
      }

      function shiftWeek(days) {
        const base = weekStart ? new Date(weekStart + "T00:00:00") : new Date();
        base.setDate(base.getDate() + days);
        weekStart = base.toISOString().slice(0, 10);
        renderWeek();
      }

      // ---------------- 组装 ----------------
      const filter = { kind: "all" };
      root.appendChild(card("加一条（跟语音说的是同一套解析）", [
        h("div", { class: "row" }, [
          h("div", { class: "grow" }, quick),
          h("button", { class: "btn primary", text: "添加", onclick: quickAdd }),
          h("button", { class: "btn", text: "用表单填", onclick: () => openForm(null) }),
        ]),
        h("p", { class: "muted", text: "支持的说法和语音完全一致：日期、时间、每周几、提前量、时长、地点、备注。解析结果就是落地的事件。" }),
        formBox,
      ]));
      root.appendChild(listBox);
      root.appendChild(weekBox);

      function refresh() {
        ctx.api.get("/api/events?kind=" + filter.kind)
          .then((d) => { data = d; renderList(); })
          .catch((err) => ctx.toast("读取事件失败：" + err.message, "err"));
      }

      ctx.api.get("/api/events/schema").then((s) => { schema = s; });
      refresh();
      renderWeek();

      handleEvent = (ev) => {
        // 服务那边响了提醒会写 state，前端顺手刷新一下列表（不然「已响」看着是旧的）
        if (ev.kind === "log" && /提醒|reminder|fired/.test(ev.data && ev.data.line ? ev.data.line : "")) {
          clearTimeout(renderList._t);
          renderList._t = setTimeout(refresh, 1500);
        }
      };
    },

    onEvent(ev, ctx) { if (handleEvent) handleEvent(ev, ctx); },
    onLeave() { handleEvent = null; },
  });

  /** ISO 字符串 → datetime-local 输入框要的格式（本地时间，不要 Z） */
  function _toLocalInput(value) {
    if (!value) return "";
    const s = String(value).replace(" ", "T");
    return s.length >= 16 ? s.slice(0, 16) : s;
  }

  /** 提前量（分钟）→ 人话：1440 该说「1天」而不是「1440分」 */
  function leadText(minutes) {
    const m = Number(minutes) || 0;
    if (m === 0) return "准时";
    if (m % 1440 === 0) return m / 1440 + "天";
    if (m % 60 === 0) return m / 60 + "小时";
    return m + "分钟";
  }
})();
