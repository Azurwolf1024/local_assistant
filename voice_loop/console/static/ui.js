/* 面板共用的小工具：建 DOM、做表格、做表单。
   为什么不用框架：这套界面只需要「表格 + 表单 + 一点交互」，
   为了几十个控件引一个框架（还要打包步骤）不划算；但这个文件要挡住重复代码，
   不然每个面板都会自己发明一套拼字符串的办法。 */

window.UI = (function () {
  /** h("div", {class:"x"}, [子元素或字符串]) —— 极简 DOM 构造 */
  function h(tag, attrs, children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "html") el.innerHTML = v;
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k in el) el[k] = v;
        else el.setAttribute(k, v);
      }
    }
    if (children !== undefined && children !== null) {
      const list = Array.isArray(children) ? children : [children];
      for (const c of list) {
        if (c === null || c === undefined || c === false) continue;
        el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
      }
    }
    return el;
  }

  /** 卡片：h2 标题 + 内容 */
  function card(title, children, actions) {
    const head = h("div", { class: "row" }, [h("h2", { text: title, class: "grow" })]);
    if (actions) head.appendChild(h("div", { class: "row" }, actions));
    const body = h("div", {}, children);
    return h("div", { class: "card" }, [head, body]);
  }

  /** 表格：columns = [{key, label, cls, render(row)->node|string}] */
  function table(columns, rows, opts = {}) {
    const thead = h("thead", {}, h("tr", {}, columns.map((c) =>
      h("th", { class: c.cls || "", text: c.label }))));
    const tbody = h("tbody", {}, rows.map((row) => h("tr", {}, columns.map((c) => {
      const v = c.render ? c.render(row) : row[c.key];
      return h("td", { class: c.cls || "" }, v === undefined || v === null ? "" : (typeof v === "object" ? v : String(v)));
    }))));
    if (opts.emptyText && rows.length === 0) {
      return h("p", { class: "muted", text: opts.emptyText });
    }
    return h("table", {}, [thead, tbody]);
  }

  /** 带标签的输入框；config = {label, value, type, options, placeholder, min, step} */
  function field(label, config, onChange) {
    const { type = "text", value = "", options = null, placeholder = "", ...rest } = config || {};
    let input;
    if (type === "select") {
      input = h("select", {}, (options || []).map((o) =>
        h("option", { value: o.value, text: o.label, selected: String(o.value) === String(value) })));
    } else if (type === "textarea") {
      input = h("textarea", { placeholder }, value);
    } else if (type === "checkbox") {
      input = h("input", { type: "checkbox", checked: !!value }, null);
    } else {
      input = h("input", { type, value: value === null || value === undefined ? "" : value, placeholder, ...rest });
    }
    if (onChange) {
      const evt = (type === "checkbox" || type === "select") ? "change" : "input";
      input.addEventListener(evt, () => onChange(type === "checkbox" ? input.checked : input.value));
    }
    input.dataset.role = "input";
    const wrap = h("label", { class: "field" }, [h("span", { text: label }), input]);
    wrap.input = input;
    return wrap;
  }

  /** 把 form 里所有 [data-role=input] 收成一个对象（key 用 wrap.dataset.key） */
  function collect(form) {
    const out = {};
    form.querySelectorAll("[data-key]").forEach((wrap) => {
      const input = wrap.querySelector("[data-role=input]");
      if (!input) return;
      out[wrap.dataset.key] = input.type === "checkbox" ? input.checked : input.value;
    });
    return out;
  }

  /** 键盘：输入框里按回车就提交（表单里第一个按钮） */
  function onSubmit(form, fn) {
    form.addEventListener("submit", (e) => { e.preventDefault(); fn(); });
    return form;
  }

  function line(text, cls) { return h("div", { class: cls || "muted", text }); }

  function tags(list) {
    return h("div", { class: "row" }, (list || []).map((t) =>
      h("span", { class: t.cls ? "tag " + t.cls : "tag", text: t.text })));
  }

  /** 秒 → 人话 */
  function secs(v) {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    if (!isFinite(n)) return "—";
    if (n < 1) return Math.round(n * 1000) + " ms";
    return n.toFixed(n < 10 ? 2 : 1) + " s";
  }

  return { h, card, table, field, collect, onSubmit, line, tags, secs };
})();
