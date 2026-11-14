/* 通用：token 管理、请求封装、模板工具 */
const API = {
  token() { return localStorage.getItem("kb_token") || ""; },
  user() {
    try { return JSON.parse(localStorage.getItem("kb_user") || "null"); } catch { return null; }
  },
  save(token, user) {
    localStorage.setItem("kb_token", token);
    localStorage.setItem("kb_user", JSON.stringify(user));
  },
  clear() {
    localStorage.removeItem("kb_token");
    localStorage.removeItem("kb_user");
  },
  async request(path, options = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    if (this.token()) headers.Authorization = "Bearer " + this.token();
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    if (resp.status === 401) {
      this.clear();
      if (!location.pathname.startsWith("/login")) location.href = "/login";
      throw new Error("登录已失效");
    }
    let data = null;
    try { data = await resp.json(); } catch { /* empty body */ }
    if (!resp.ok) throw new Error((data && (data.detail || data.message)) || "请求失败");
    return data;
  },
  get(path) { return this.request(path); },
  post(path, body) {
    return this.request(path, { method: "POST", body: JSON.stringify(body) });
  },
  toast(message) {
    let el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.add("show");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.remove("show"), 2200);
  },
  esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  },
  requireAuth(role) {
    const user = this.user();
    if (!this.token() || !user) { location.href = "/login"; return null; }
    if (role && user.role !== role) { location.href = "/"; return null; }
    return user;
  },
  logout() { this.clear(); location.href = "/login"; },
  initials(name, phone) {
    const n = (name || phone || "员").trim();
    return n.slice(0, 1).toUpperCase();
  },
};
