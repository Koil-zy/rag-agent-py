# 最低成本部署方案

面向「企业内部制度知识助理（RAG）」：FastAPI + PostgreSQL/pgvector + Ollama 本地模型。
模型与数据库全部通过 `config.yaml` / 环境变量切换，代码零改动。

---

## 一、组件与资源

| 组件 | 说明 | 内存占用（参考） |
| --- | --- | --- |
| app（FastAPI + uvicorn） | 单进程即可，1 worker | 150–300 MB |
| PostgreSQL 16 + pgvector | 也可用内嵌 pgserver（演示用） | 100–200 MB |
| Ollama + bge-m3 | 向量化，约 1.2 GB 模型 | 1.5–2 GB |
| Ollama + deepseek-r1:1.5b | 问答，约 1.1 GB 模型 | 1.5–2.5 GB |
| Nginx（可选） | 反代 + HTTPS + SSE 不缓冲 | 20 MB |

结论：**4 核 8 GB 是最低舒适配置**；2 核 4 GB 能跑，但 CPU 推理首字延迟明显变长（可用更小模型，见第四节）。

---

## 二、方案对比（按成本从低到高）

| 方案 | 组成 | 参考成本 | 适用 |
| --- | --- | --- | --- |
| A. 单机 + 内嵌 PG（最省） | 1 台云服务器，`uvicorn` 直接跑，PG 用 pgserver 内嵌 | 约 ¥60–120/月（2C4G 轻量服务器） | 内部 10–30 人、演示/POC |
| B. 单机 + Docker Compose（推荐） | 同机 3 容器：app / pgvector / ollama | 约 ¥120–300/月（4C8G） | 内部 30–100 人 |
| C. 托管 PG + Ollama 单机 | 云数据库 PG（含 pgvector）+ 单机 Ollama + app | 云 PG 约 ¥100–200/月 + 2C4G 约 ¥60–120/月 | 要求数据库托管、有备份 SLA |

说明：以上为国内云厂商按量/包月的大致区间，仅作预算参考；GPU 不必要，1.5B 级模型 CPU 即可，日均几百次问答足够。

---

## 三、方案 A：单机直接跑（成本最低）

```bash
# 1) 运行环境：Python 3.12 + 依赖
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# .venv/bin/pip install -r requirements.txt     # Linux

# 2) 拉取本地模型（只需一次）
ollama pull bge-m3
ollama pull deepseek-r1:1.5b

# 3) 首次入库（读 docs/manifest.yaml，按 ACL 打标）
.venv/Scripts/python -m app.ingest --force

# 4) 启动服务
.venv/Scripts/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- 数据库不填 `database.url` 时，自动用内嵌 PostgreSQL（pgserver 自带，无需安装）。
- 数据目录：`.data/pgdata`；密钥：`.data/secret.key`（首次自动生成）。
- 内嵌 PostgreSQL 随服务自动拉起且常驻：Web 服务或评测进程退出不会停库；连接失败会自动重启数据库。需要手动停库时执行 `python -m app.db`。

生产建议用 `nssm`/`systemd` 守护进程，示例（Linux）：

```ini
# /etc/systemd/system/rag-assistant.service
[Unit]
Description=RAG Policy Assistant
After=network.target ollama.service

[Service]
WorkingDirectory=/opt/ai-rag-agent
Environment=SECRET_KEY=<32+位随机串>
ExecStart=/opt/ai-rag-agent/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
User=app

[Install]
WantedBy=multi-user.target
```

---

## 四、方案 B：Docker Compose（推荐，隔离性好）

```yaml
# docker-compose.yml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: rag
    volumes: ["./data/pg:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d rag"]
      interval: 5s

  ollama:
    image: ollama/ollama:latest
    volumes: ["./data/ollama:/root/.ollama"]
    # 有 GPU 时取消注释
    # deploy: { resources: { reservations: { devices: [{ driver: nvidia, count: 1, capabilities: [gpu] }] } } }

  app:
    build: .
    environment:
      DATABASE_URL: postgresql://postgres:${POSTGRES_PASSWORD}@db:5432/rag
      SECRET_KEY: ${SECRET_KEY}
      OLLAMA_BASE_URL: http://ollama:11434
    depends_on: [db, ollama]
    ports: ["8000:8000"]
```

要点：
1. `OLLAMA_BASE_URL` 需在 `config.yaml` 的 `embedding.base_url` / `llm.base_url` 中改为容器名 `http://ollama:11434`。
2. 首次启动后执行一次初始化与入库：
   ```bash
   docker compose exec ollama ollama pull bge-m3
   docker compose exec ollama ollama pull deepseek-r1:1.5b
   docker compose exec app python -m app.ingest --force
   ```
3. 前端用 Nginx 反代时，SSE 必须关闭缓冲：
   ```nginx
   location /api/chat/stream {
       proxy_pass http://127.0.0.1:8000;
       proxy_buffering off;
       proxy_read_timeout 300s;
       proxy_set_header Connection "";
   }
   ```
4. Dockerfile（放在项目根目录）：
   ```dockerfile
   FROM python:3.12-slim
   WORKDIR /app
   COPY requirements.txt .
   RUN pip install --no-cache-dir -r requirements.txt
   COPY . .
   EXPOSE 8000
   CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
   ```

---

## 五、上线换模型（不改代码）

只改 `config.yaml`，然后重新入库：

```yaml
embedding:
  provider: openai          # ollama | openai（OpenAI 兼容协议） | local_hash
  model: bge-large-zh-v1.5
  dimension: 1024           # 必须与模型输出维度一致
  base_url: https://your-embedding-gateway/v1
  api_key: ${EMBEDDING_API_KEY}

llm:
  provider: openai          # 或 ollama
  model: deepseek-chat
  base_url: https://api.deepseek.com/v1
  api_key: ${LLM_API_KEY}
  max_tokens: 512
```

> 换向量模型后必须 `python -m app.ingest --force`，否则维度/语义空间不一致，检索会失效。

云端 LLM 方案成本估算：按 1 万次问答、每次约 1.5k 输入 + 200 输出 tokens 计，约 ¥10–40/月（视厂商定价），比自建 GPU 便宜得多。

---

## 六、安全与运维检查清单

- [ ] `SECRET_KEY` 用环境变量注入（≥32 位随机），不要提交到仓库。
- [ ] 生产库设置强密码；`pg_hba.conf` 仅允许内网网段。
- [ ] Nginx 上 HTTPS；登录接口加限流（当前应用内已限制连续失败 5 次锁定 5 分钟）。
- [ ] 演示账号 `13800000001/13800000002` 上线前删除或改密。
- [ ] 修改 `config.yaml` 的 `registration.leader_invite_codes`，换成一次性/定期轮换的邀请码；邀请码泄露后立即替换。
- [ ] 每日备份：`pg_dump -Fc rag > backup/rag-$(date +%F).dump`；保留 7–30 天。
- [ ] 日志与审计：`guard_events` 表记录拦截/校验事件，管理页 `/admin` 可查。
- [ ] 健康检查：`GET /api/health`（返回模型、文档数、切片数、阈值）。
- [ ] 发版前跑评测（见下），确认指标没有回退。

## 七、评测与回归

```bash
# dev 集：调参用
python -m eval.run_eval --set dev

# frozen 集：正式指标，内容已锁定，禁止修改（哈希校验不通过会直接退出）
python -m eval.run_eval --set frozen

# 单元测试（不依赖 Ollama，使用本地哈希向量）
python -m pytest -q
```

指标口径：检索命中率 hit@k、MRR、正确拒答率、ACL 隔离安全率、答案关键词覆盖、引用有效率、Guard 拦截率、平均耗时。

## 八、容量估算

- 单条切片约 500 字、1024 维向量，35 条切片仅占数百 KB；1 万条切片约 40 MB 向量数据，HNSW 索引内存约 2–3 倍，2C4G 足够。
- 并发：uvicorn 单 worker + CPU 推理，建议并发 ≤ 2–3；如需更高并发，加 `--workers 2` 并把 Ollama 换成 GPU 或云端 API。
