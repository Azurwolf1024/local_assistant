/* 控制台核心：路由（标签页）、接口调用、实时通道、面板挂载。
   ★没有构建步骤、没有外部依赖★：加面板只要加两个文件（py + panels/xxx.js）。
   面板脚本的契约：
       Console.register("xxx", {
         render(root, ctx) {},        // 必填：把内容画进 root
         onEvent(ev, ctx) {},         // 可选：收到实时事件（仅当前标签页）
         onLeave() {},                // 可选：切走时清理
       });
*/
window.Console = (function () {
  const panels = new Map();     // id -> 面板定义
  const meta = new Map();       // id -> 后端给的元数据（标题/顺序/hint）
  let current = null;           // 当前面板 id
  let currentCtx = null;
  let eventSource = null;
  let lastStatus = null;

  // ------------------------------------------------------------------ DOM 小工具
  const $ = (sel) => document.querySelector(sel);

  function toast(message, kind = "ok", ms = 4200) {
    const box = $("#banner");
    box.className = "banner " + kind;
    box.textContent = message;
    box.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => box.classList.add("hidden"), ms);
  }

  // ------------------------------------------------------------------ 接口调用
  async function request(method, path, body) {
    const init = { method, headers: {} };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch(path, init);
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_e) { data = { raw: text }; }
    if (!res.ok) {
      const err = (data && (data.error || data.detail)) || res.status + " " + res.statusText;
      throw new Error(err);
    }
    return data;
  }

  const api = {
    get: (p) => request("GET", p),
    post: (p, b) => request("POST", p, b === undefined ? {} : b),
    put: (p, b) => request("PUT", p, b),
    patch: (p, b) => request("PATCH", p, b),
    del: (p) => request("DELETE", p),
    /** 包一层：出错就弹横幅并返回 null，省得每个面板都写 try/catch */
    async safe(fn, okMessage) {
      try {
        const out = await fn();
        if (okMessage) toast(okMessage, "ok");
        return out;
      } catch (err) {
        toast("操作失败：" + err.message, "err");
        return null;
      }
    },
  };

  // ------------------------------------------------------------------ 实时通道
  function connectStream() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource("/api/stream");
    const dot = $("#link-dot");
    eventSource.onopen = () => dot.classList.add("on");
    eventSource.onerror = () => dot.classList.remove("on");
    eventSource.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch (_e) { return; }
      if (ev.kind === "status") applyStatus(ev.data);
      if (current && panels.has(current)) {
        const def = panels.get(current);
        if (typeof def.onEvent === "function") {
          try { def.onEvent(ev, currentCtx); } catch (e) { console.error(e); }
        }
      }
    };
  }

  // ------------------------------------------------------------------ 服务状态条
  function applyStatus(status) {
    lastStatus = status;
    const pill = $("#svc-pill");
    if (!status || status.error) {
      pill.className = "pill warn";
      pill.textContent = "服务状态读不到";
      return;
    }
    if (status.running) {
      pill.className = "pill on";
      const age = status.log_age_seconds;
      const quiet = age !== null && age > 60 ? "（日志已静默 " + Math.round(age) + "s）" : "";
      pill.textContent = "服务运行中 · PID " + status.pid + quiet;
    } else if (status.pid_stale) {
      pill.className = "pill warn";
      pill.textContent = "服务没在跑（pid 文件是旧进程 " + status.pid + "）";
    } else if (status.log_pending_stop) {
      pill.className = "pill warn";
      pill.textContent = "服务没在跑（有未处理的停止信号）";
    } else {
      pill.className = "pill off";
      pill.textContent = "服务未启动";
    }
    $("#svc-start").disabled = !!status.running;
    $("#svc-stop").disabled = !status.running && !status.pid_stale;
  }

  function status() { return lastStatus; }

  // ------------------------------------------------------------------ 标签页
  async function loadPanelScript(id) {
    if (panels.has(id)) return true;
    await new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/panels/" + id + ".js";
      s.onload = resolve;
      s.onerror = () => reject(new Error("面板脚本加载失败：" + id + ".js"));
      document.head.appendChild(s);
    });
    return panels.has(id);
  }

  async function show(id) {
    const view = $("#view");
    if (current && panels.has(current)) {
      const def = panels.get(current);
      if (typeof def.onLeave === "function") { try { def.onLeave(); } catch (_e) {} }
    }
    current = id;
    location.hash = id;
    document.querySelectorAll(".tab").forEach((el) => {
      el.classList.toggle("active", el.dataset.id === id);
    });
    view.innerHTML = '<p class="muted">正在加载…</p>';
    try {
      const ok = await loadPanelScript(id);
      if (!ok) throw new Error("这个面板没有前端脚本（后端有，前端还没写）");
      currentCtx = { api, status, toast, onEvent: null };
      view.innerHTML = "";
      panels.get(id).render(view, currentCtx);
    } catch (err) {
      view.innerHTML = "";
      const box = document.createElement("div");
      box.className = "card";
      box.innerHTML = "<h2>面板加载失败</h2><p class='mono'></p>";
      box.querySelector("p").textContent = err.message;
      view.appendChild(box);
    }
  }

  function buildTabs(list) {
    const nav = $("#tabs");
    nav.innerHTML = "";
    list.forEach((p) => {
      meta.set(p.id, p);
      const b = document.createElement("button");
      b.className = "tab";
      b.dataset.id = p.id;
      b.textContent = p.title;
      if (p.hint) b.title = p.hint;
      b.onclick = () => show(p.id);
      nav.appendChild(b);
    });
  }

  // ------------------------------------------------------------------ 启动
  async function boot() {
    try {
      const info = await api.get("/api/meta");
      $("#meta-root").textContent = info.root;
      $("#foot-left").textContent = "配置 " + (info.config || "config.toml");
      $("#foot-right").textContent = "信箱 " + info.console_dir + " · Python " + info.python;
      buildTabs(info.panels || []);
      if (info.panels_failed && Object.keys(info.panels_failed).length) {
        toast("有面板装载失败：" + JSON.stringify(info.panels_failed), "warn", 12000);
      }
      const want = (location.hash || "").replace("#", "") || (info.panels[0] || {}).id;
      await show(want);
    } catch (err) {
      toast("控制台启动失败：" + err.message, "err", 15000);
    }
    connectStream();
  }

  $("#svc-start").onclick = () => api.safe(async () => {
    const r = await api.post("/api/service/start");
    toast(r.message || "已发出启动命令", r.started ? "ok" : "warn");
    return r;
  });
  $("#svc-stop").onclick = () => api.safe(async () => {
    const r = await api.post("/api/service/stop");
    toast(r.message || "已发出停止命令", r.stopped ? "ok" : "warn");
    return r;
  });

  return {
    /** 面板脚本用它注册自己 */
    register(id, def) { panels.set(id, def); },
    show, api, toast, status, $,
    get meta() { return meta; },
    /** 当前标签 id（hashchange 处理要用） */
    current: () => current,
    boot,
  };
})();

document.addEventListener("DOMContentLoaded", () => window.Console.boot());

// ★地址栏片段也要能切标签★：不然手打 #logs、或者按浏览器的前进/后退，
// 只有 URL 变了、画面纹丝不动（踩过：goto 一个带 #xxx 的地址看起来像没反应）。
// ★只能走 window.Console 暴露出来的东西★：外面这个作用域里没有 current/meta
// （第一版就写错了，直接 ReferenceError: current is not defined）。
window.addEventListener("hashchange", () => {
  const want = (location.hash || "").replace("#", "");
  if (!want) return;
  if (window.Console.meta.size === 0) return;         // 还没 boot 完，boot 会自己挑
  if (want === window.Console.current()) return;      // 已经是这个面板了，别重画
  window.Console.show(want);
});