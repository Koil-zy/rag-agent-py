/* 管理页：文档台账、重新入库、Guard 事件、运行配置 */
(function () {
  const user = API.requireAuth("leader");
  if (!user) return;

  document.getElementById("operator").textContent = `${user.display_name || user.phone}（${user.role}）`;
  document.getElementById("logoutBtn").addEventListener("click", () => API.logout());

  const docsBody = document.getElementById("docsBody");
  const eventsBox = document.getElementById("events");
  const healthBox = document.getElementById("health");
  const reindexBtn = document.getElementById("reindexBtn");
  const statsBox = document.getElementById("stats");

  async function loadDocs() {
    try {
      const data = await API.get("/api/documents");
      docsBody.innerHTML = data.documents
        .map((d) => `
          <tr>
            <td>${API.esc(d.code)}</td>
            <td>${API.esc(d.title)}</td>
            <td>${API.esc(d.category)}</td>
            <td><span class="tag ${d.acl_level}">${d.acl_level}</span></td>
            <td>${d.chunk_count}</td>
            <td>${d.effective_date || "-"}</td>
          </tr>`)
        .join("");
    } catch (err) {
      docsBody.innerHTML = `<tr><td colspan="6" style="color:#c0392b">${API.esc(err.message)}</td></tr>`;
    }
  }

  async function loadEvents() {
    try {
      const data = await API.get("/api/admin/guard-events?limit=20");
      if (!data.events.length) {
        eventsBox.innerHTML = '<div class="side-empty">暂无拦截/校验事件</div>';
        return;
      }
      eventsBox.innerHTML = data.events
        .map((e) => `
          <div class="event">
            <span class="kind">${API.esc(e.kind)}</span>
            <span class="time">${API.esc(e.created_at)}</span>
            <div class="q">${API.esc(e.question || "")}</div>
          </div>`)
        .join("");
    } catch (err) {
      eventsBox.innerHTML = `<div style="color:#c0392b">${API.esc(err.message)}</div>`;
    }
  }

  async function loadHealth() {
    try {
      const data = await API.get("/api/health");
      statsBox.innerHTML = `
        <div class="kv">
          <dt>文档数</dt><dd>${data.stats.documents}</dd>
          <dt>切片数</dt><dd>${data.stats.chunks}</dd>
          <dt>用户数</dt><dd>${data.stats.users}</dd>
        </div>`;
      healthBox.innerHTML = `
        <dl class="kv">
          <dt>问答模型</dt><dd>${API.esc(data.models.provider)} / ${API.esc(data.models.model)}</dd>
          <dt>向量模型</dt><dd>${API.esc(data.models.embedding_provider)} / ${API.esc(data.models.embedding_model)}</dd>
          <dt>检索阈值</dt><dd>相似度 ≥ ${data.retrieval.min_score}，top_k=${data.retrieval.top_k}</dd>
          <dt>Guard 窗口</dt><dd>window=${data.guard.window_size} / stride=${data.guard.stride}</dd>
        </dl>`;
    } catch (err) {
      healthBox.innerHTML = `<div style="color:#c0392b">${API.esc(err.message)}</div>`;
    }
  }

  reindexBtn.addEventListener("click", async () => {
    if (!confirm("将对 docs/ 下全部文档重新切片并重建向量，确定继续？")) return;
    reindexBtn.disabled = true;
    reindexBtn.textContent = "入库中…";
    try {
      const data = await API.post("/api/admin/reindex", {});
      const detail = data.results.map((r) => `${r.code}:${r.status}(${r.chunks})`).join("  ");
      API.toast("入库完成：" + detail);
      await Promise.all([loadDocs(), loadHealth()]);
    } catch (err) {
      API.toast(err.message);
    } finally {
      reindexBtn.disabled = false;
      reindexBtn.textContent = "重新入库";
    }
  });

  loadDocs();
  loadEvents();
  loadHealth();
})();
