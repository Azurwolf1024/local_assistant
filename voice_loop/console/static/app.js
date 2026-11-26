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
  let engineSeq = 0;          // 防旧回执覆盖新状态
  let lastSvcRunning = null;  // 只在服务「运行状态变了」时才去 ping
  let engineState = { ok: false, running: false, asr: null, tts: null };

  // ------------------------------------------------------------------ DOM 小工具
  const $ = (sel) => document.querySelector(sel);

  // 提示条：右下角浮起来的卡片，点一下就关（原来是顶部横幅，太容易被忽略）
  function toast(message, kind = "ok", ms = 4200) {
    let wrap = document.getElementById("toasts");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.id = "toasts";
      document.body.appendChild(wrap);
    }
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = message;
    wrap.appendChild(el);
    requestAnimationFrame(() => el.classList.add("in"));
    while (wrap.children.length > 4) wrap.removeChild(wrap.firstChild);
    const close = () => {
      clearTimeout(timer);
      el.classList.remove("in");
      setTimeout(() => el.remove(), 220);
    };
    const timer = setTimeout(close, ms);
    el.addEventListener("click", close);
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
      // ★控制台自己退出了★（终端里按了 Ctrl+C）：主动断开、不再重连，
      // 并告诉用户「页面还在但后端没了」——否则 EventSource 会一直重连失败。
      if (ev.kind === "bye") {
        dot.classList.remove("on");
        eventSource.close();
        eventSource = null;
        toast("控制台已在终端退出（页面可以关了）——语音服务不受影响", "warn", 60000);
        return;
      }
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
    if (!status) return;
    lastStatus = status;
    const pill = $("#svc-pill");
    if (status.error) {
      pill.className = "pill warn";
      pill.textContent = "服务状态读不到";
      return;
    }
    if (status.running) {
      pill.className = "pill on";
      const age = status.log_age_seconds;
      const quiet = age !== null && age > 60 ? "（日志已静默 " + Math.round(age) + "s）" : "";
      pill.textContent = "服务运行中 · PID " + status.pid + quiet;
      pill.title = status.pid_source === "log" ? "PID 是从日志里恢复的（pid 文件被删了）" : "";
    } else if (status.leftovers || status.pid_stale || status.log_pending_stop) {
      pill.className = "pill warn";
      pill.textContent = "服务没在跑（有上次的残留文件）";
      pill.title = "点「启动服务」会先把残留文件清掉再启动";
    } else {
      pill.className = "pill off";
      pill.textContent = "服务未启动";
      pill.title = "";
    }
    // 停止按钮：只要还有残留可清（或真在跑）就能点
    $("#svc-start").disabled = !!status.running;
    $("#svc-stop").disabled = !status.running && !status.pid_stale && !status.log_pending_stop;
    // ★只有服务起/停这一刻才重新拉一次开关状态★：SSE status 每次心跳都 ping
    // 的话，服务正忙时 ping 超时会把开关闪成「未知」，下一轮又亮，看着像抽风。
    const running = !!status.running;
    if (running !== lastSvcRunning) {
      lastSvcRunning = running;
      refreshEngine();
    }
  }

  // 顶栏两个滑动开关：状态以最后一次成功回执为准，ping 失败不把界面闪回关闭。
  function applyEngine(state) {
    if (!state) return;
    const running = state.running === true;
    if (state.ok) {
      // 成功回执才更新已知状态；失败回执只更新运行状态。
      if (typeof state.asr === "boolean") engineState.asr = state.asr;
      if (typeof state.tts === "boolean") engineState.tts = state.tts;
      engineState.ok = true;
    } else if (!running) {
      engineState = { ok: false, running: false, asr: null, tts: null };
    }
    engineState.running = running;
    [["asr", "识别"], ["tts", "播报"]].forEach(([kind, label]) => {
      const btn = $("#" + kind + "-toggle");
      if (!btn) return;
      const on = running && engineState[kind] === true;
      btn.classList.toggle("on", on);
      btn.classList.toggle("off", !on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.disabled = !running;
      btn.title = !running ? "服务没在跑，先点「启动服务」"
        : (!state.ok ? "服务正忙，状态暂未刷新（仍可点）"
          : (on ? "点击关闭" + label : "点击开启" + label));
    });
  }

  async function refreshEngine() {
    const seq = ++engineSeq;
    try {
      const state = await api.get("/api/engine");
      if (seq !== engineSeq) return;   // 有更新的请求在飞，丢掉这份旧结果
      applyEngine(state);
    } catch (_e) {
      if (seq === engineSeq) applyEngine({ ok: false, running: !!lastSvcRunning });
    }
  }

  async function refreshStatus() {
    try {
      applyStatus(await api.get("/api/service"));
      refreshEngine();
    } catch (_e) { /* 状态读不到就保持原样，别扰动界面 */ }
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
      // ★半新半旧的进程★：静态脚本是现取的（新），路由/接口是启动时装载的（旧）。
      // 两个指纹不一样就说明改过代码没重启——不提醒的话，人只会看到一个空页面。
      if (info.panels_stamp && info.panels_stamp_now
          && info.panels_stamp !== info.panels_stamp_now) {
        toast("面板代码改过了，但这个控制台进程还是旧的（后端路由没跟着变）"
          + "—— Ctrl+C 停掉它、重新 python main.py ui，再刷新本页", "warn", 30000);
      }
      const want = (location.hash || "").replace("#", "") || (info.panels[0] || {}).id;
      await show(want);
      refreshStatus();          // 先把状态条填上，别等 SSE 那一轮
    } catch (err) {
      toast("控制台启动失败：" + err.message, "err", 15000);
    }
    connectStream();
  }

  [["asr", "语音识别"], ["tts", "语音播报"]].forEach(([kind, name]) => {
    const btn = $("#" + kind + "-toggle");
    if (!btn) return;
    btn.onclick = () => api.safe(async () => {
      const seq = ++engineSeq;        // 点下去这一刻起，旧 ping 回执都不作数
      btn.disabled = true;
      const r = await api.post("/api/engine", { kind });
      if (seq !== engineSeq) return r;
      applyEngine(r);
      btn.disabled = false;
      toast(r.ok ? (r.text || name + "已切换") : (r.error || name + "切换失败"),
            r.ok ? "ok" : "err");
      return r;
    });
  });

  $("#svc-start").onclick = () => api.safe(async () => {
    const btn = $("#svc-start");
    btn.disabled = true;
    toast("正在启动服务（最长等 25 秒）…", "warn", 25000);
    const r = await api.post("/api/service/start");
    applyStatus(r.status);
    refreshEngine();
    btn.disabled = false;
    toast(r.message || (r.started ? "已启动" : "没起来"), r.started ? "ok" : "err", r.started ? 6000 : 20000);
    return r;
  });
  $("#svc-stop").onclick = () => api.safe(async () => {
    const btn = $("#svc-stop");
    btn.disabled = true;
    toast("正在停止服务…", "warn", 20000);
    const r = await api.post("/api/service/stop");
    applyStatus(r.status);
    refreshEngine();
    btn.disabled = false;
    toast(r.message || (r.stopped ? "已停止" : "没停成"), r.stopped ? "ok" : "err", r.stopped ? 6000 : 20000);
    return r;
  });

  return {
    /** 面板脚本用它注册自己 */
    register(id, def) { panels.set(id, def); },
    show, api, toast, status, refreshStatus, $,
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