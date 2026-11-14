/* 问答页逻辑：SSE 流式接收、来源渲染、引用跳转、Guard 提示 */
(function () {
  const user = API.requireAuth();
  if (!user) return;

  const body = document.getElementById("chatBody");
  const input = document.getElementById("questionInput");
  const sendBtn = document.getElementById("sendBtn");
  const sourceList = document.getElementById("sourceList");
  const sourceCount = document.getElementById("sourceCount");
  const roleText = document.getElementById("roleText");
  const avatar = document.getElementById("avatar");
  const adminLink = document.getElementById("adminLink");
  const logoutBtn = document.getElementById("logoutBtn");
  const chatMeta = document.getElementById("chatMeta");

  let busy = false;
  let sources = [];
  let conversationId = null;
  let activeSource = null;

  avatar.textContent = API.initials(user.display_name, user.phone);
  avatar.title = `${user.display_name || user.phone}（${user.role === "leader" ? "领导" : "员工"}）`;
  roleText.textContent = user.role === "leader" ? "领导视图 · 可检索 guide 文档" : "员工视图 · 仅检索 public 文档";
  roleText.className = "role-pill" + (user.role === "leader" ? " leader" : "");
  if (user.role === "leader") adminLink.classList.remove("hidden");

  logoutBtn.addEventListener("click", () => API.logout());

  function nowText() {
    const d = new Date();
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  function scrollBottom() { body.scrollTop = body.scrollHeight; }

  function addUserMessage(text) {
    const el = document.createElement("div");
    el.className = "msg user";
    el.innerHTML = `<div class="bubble-user">${API.esc(text)}</div>`;
    body.appendChild(el);
  }

  function addAnswerShell() {
    const el = document.createElement("div");
    el.className = "msg assistant";
    el.innerHTML = `
      <div class="avatar-ai">知</div>
      <div class="answer">
        <div class="answer-name">制度知识助理<span class="time">${nowText()}</span></div>
        <div class="answer-text streaming"></div>
        <div class="stream-status typing" data-status><i></i><i></i><i></i><span>正在检索制度文档…</span></div>
      </div>`;
    body.appendChild(el);
    return {
      root: el,
      text: el.querySelector(".answer-text"),
      status: el.querySelector("[data-status]"),
      answer: el.querySelector(".answer"),
    };
  }

  function setStatus(shell, text) {
    if (!text) { shell.status.remove(); return; }
    const span = shell.status.querySelector("span");
    if (span) span.textContent = text;
  }

  function renderWithCitations(shell, text) {
    const html = API.esc(text).replace(/\[(\d{1,2})\]/g, (m, n) => {
      return `<button class="cite" data-cite="${n}" title="查看来源 ${n}">${n}</button>`;
    });
    shell.text.innerHTML = html;
    shell.text.querySelectorAll("[data-cite]").forEach((btn) => {
      btn.addEventListener("click", () => focusSource(Number(btn.dataset.cite)));
    });
  }

  function addActions(shell, question) {
    if (shell.answer.querySelector(".answer-actions")) return;
    const bar = document.createElement("div");
    bar.className = "answer-actions";
    bar.innerHTML = `
      <button class="action-btn" data-act="copy">复制</button>
      <button class="action-btn" data-act="trace">溯源</button>
      <button class="action-btn" data-act="follow">追问</button>`;
    shell.answer.appendChild(bar);
    bar.addEventListener("click", async (e) => {
      const act = e.target.dataset.act;
      if (!act) return;
      if (act === "copy") {
        await navigator.clipboard.writeText(shell.text.innerText);
        API.toast("已复制回答");
      } else if (act === "trace") {
        if (sources.length) focusSource(sources[0].index);
        else API.toast("本次回答没有命中来源");
      } else if (act === "follow") {
        input.value = "关于上述回答，";
        input.focus();
        autoGrow();
      }
    });
  }

  function addNotice(shell, kind, text) {
    const el = document.createElement("div");
    el.className = "notice " + kind;
    el.textContent = text;
    shell.answer.appendChild(el);
  }

  const COVER_CN = ["c1", "c2", "c3", "c4"];
  function renderSources(list) {
    sources = list || [];
    sourceCount.textContent = sources.length;
    if (!sources.length) {
      sourceList.innerHTML =
        '<div class="side-empty">本次没有命中制度片段。<br>可尝试补充制度名称或具体场景。</div>';
      return;
    }
    sourceList.innerHTML = sources
      .map((s) => `
        <div class="source" data-index="${s.index}">
          <div class="rank">${s.index}</div>
          <div class="cover ${COVER_CN[s.index % COVER_CN.length]}">${API.esc(s.category.slice(0, 2))}</div>
          <div class="source-main">
            <div class="source-title">${API.esc(s.title)}</div>
            <div class="source-meta">
              <span>${API.esc(s.heading)}</span>
              <span class="acl-tag">${s.acl_level}</span>
              <span>${(s.score * 100).toFixed(0)}%</span>
            </div>
            <div class="score-bar"><i style="width:${Math.min(100, s.score * 100)}%"></i></div>
          </div>
        </div>`)
      .join("");
    sourceList.querySelectorAll(".source").forEach((el) => {
      el.addEventListener("click", () => focusSource(Number(el.dataset.index)));
    });
    activeSource = null;
  }

  function focusSource(index) {
    const src = sources.find((s) => s.index === index);
    if (!src) return;
    sourceList.querySelectorAll(".source").forEach((el) => {
      el.classList.toggle("active", Number(el.dataset.index) === index);
    });
    let detail = document.getElementById("sourceDetail");
    if (!detail) {
      detail = document.createElement("div");
      detail.id = "sourceDetail";
      detail.className = "side-detail";
      sourceList.appendChild(detail);
    }
    detail.innerHTML = `
      <div style="font-weight:600;color:var(--ink);margin-bottom:6px">[${src.index}] ${API.esc(src.title)} · ${API.esc(src.heading)}</div>
      <div>${API.esc(src.content)}</div>
      <div style="margin-top:8px;color:var(--muted)">文档编号 ${API.esc(src.doc_code)} · 相似度 ${(src.cosine * 100).toFixed(1)}% · 权限 ${src.acl_level}</div>`;
    detail.scrollIntoView({ block: "nearest", behavior: "smooth" });
    activeSource = index;
  }

  function parseSSE(buffer, onEvent) {
    const blocks = buffer.split("\n\n");
    const rest = blocks.pop();
    blocks.forEach((block) => {
      let event = "message";
      const dataLines = [];
      block.split("\n").forEach((line) => {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      });
      if (!dataLines.length) return;
      try { onEvent(event, JSON.parse(dataLines.join("\n"))); } catch (e) { console.warn("SSE parse", e); }
    });
    return rest;
  }

  async function ask(question) {
    const text = (question || "").trim();
    if (!text) return API.toast("请输入问题");
    if (busy) return API.toast("上一条回答还在生成中…");
    busy = true;
    sendBtn.disabled = true;
    const welcome = document.querySelector(".welcome");
    if (welcome) welcome.remove();

    addUserMessage(text);
    const shell = addAnswerShell();
    let answerText = "";
    scrollBottom();

    const payload = new Request("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + API.token() },
      body: JSON.stringify({ question: text, conversation_id: conversationId }),
    });

    try {
      const resp = await fetch(payload);
      if (resp.status === 401) { API.clear(); location.href = "/login"; return; }
      if (!resp.ok || !resp.body) {
        let msg = "服务暂不可用";
        try { msg = (await resp.json()).detail || msg; } catch {}
        throw new Error(msg);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        buffer = parseSSE(buffer, (event, data) => {
          if (event === "status") {
            setStatus(shell, data.text || "正在生成…");
          } else if (event === "sources") {
            renderSources(data.sources || []);
            if (data.abstained) {
              const tag = document.createElement("div");
              tag.className = "abstain-tag";
              tag.textContent = `未找到相关依据 · 最高相似度 ${(data.best_cosine * 100).toFixed(0)}%`;
              shell.answer.insertBefore(tag, shell.answer.querySelector(".answer-text"));
            }
          } else if (event === "delta") {
            answerText += data.text || "";
            shell.text.textContent = answerText;
            setStatus(shell, "正在输出…");
            scrollBottom();
          } else if (event === "validation_failed") {
            shell.text.classList.remove("streaming");
            shell.text.textContent = "";
            setStatus(shell, null);
            addNotice(shell, "warn", data.message);
          } else if (event === "guard") {
            answerText = data.message || "";
            shell.text.classList.remove("streaming");
            shell.text.textContent = answerText;
            setStatus(shell, null);
            addNotice(shell, "error", `内容检测已拦截：${data.kind}。已停止输出并替换为安全提示。`);
          } else if (event === "error") {
            shell.text.classList.remove("streaming");
            setStatus(shell, null);
            addNotice(shell, "error", data.message || "服务异常");
          } else if (event === "done") {
            conversationId = data.conversation_id || conversationId;
            shell.text.classList.remove("streaming");
            setStatus(shell, null);
            renderWithCitations(shell, answerText);
            addActions(shell, text);
            if (data.abstained) {
              addNotice(shell, "muted", "知识库中没有足够依据，系统未调用大模型，避免编造答案。");
            }
            chatMeta.textContent = `最近一次：${nowText()} · 引用覆盖率 ${Math.round((data.citation_ratio || 0) * 100)}%`;
            scrollBottom();
          }
        });
      }
    } catch (err) {
      shell.text.classList.remove("streaming");
      setStatus(shell, null);
      addNotice(shell, "error", err.message || "请求失败，请稍后重试");
    } finally {
      busy = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  function autoGrow() {
    input.style.height = "38px";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }

  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const q = input.value;
      input.value = "";
      autoGrow();
      ask(q);
    }
  });
  sendBtn.addEventListener("click", () => {
    const q = input.value;
    input.value = "";
    autoGrow();
    ask(q);
  });

  document.querySelectorAll("[data-ask]").forEach((el) => {
    el.addEventListener("click", () => ask(el.dataset.ask));
  });

  const chips = document.querySelectorAll(".chip[data-ask]");
  chips.forEach((chip) => {
    chip.addEventListener("click", () => {
      chips.forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      const q = chip.dataset.ask;
      if (q) ask(q);
    });
  });

  const search = document.getElementById("globalSearch");
  if (search) {
    search.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && search.value.trim()) ask(search.value.trim());
    });
    document.getElementById("searchBtn").addEventListener("click", () => {
      if (search.value.trim()) ask(search.value.trim());
    });
  }

  document.querySelectorAll("[data-hero-ask]").forEach((el) => {
    el.addEventListener("click", () => ask(el.dataset.heroAsk));
  });
})();
