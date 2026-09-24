# 智旅云图项目约定

## 项目结构

- `backend/`：FastAPI、Pydantic、SQLAlchemy、LangChain、ChromaDB、Redis、行程、地图、天气和导出服务。
- `frontend/`：Vue 3、Vite、Axios、Ant Design Vue 和高德地图前端。
- `travel/`：项目 Python 3.11 虚拟环境。
- `.kilo/`：Kilo 项目级智能体、Agent Manager 初始化和运行配置。

## 核心链路

1. 前端收集目的地、日期、预算、人数和偏好。
2. 后端通过 RAG 或动态城市候选生成结构化 itinerary。
3. 行程可补充高德 POI、坐标、路线、图片和天气。
4. 行程保存到 SQLite，并支持历史管理、编辑和文档导出。
5. PDF 和 Markdown 导出前同步当前页面数据。

## 安全与兼容性

- 不读取、输出、提交或记录 `.env`、环境变量和密钥。
- 不破坏现有 API 路径、请求/响应字段、Pydantic 模型和前端类型。
- 外部服务失败时返回脱敏错误并降级，不暴露密钥或完整上游响应。
- 不提交、推送或创建 Pull Request，除非用户明确要求。
- Agent Manager worktree 中禁止使用 `git stash`。

## 验证

修改后端后运行：

```powershell
travel\Scripts\python.exe -m pytest backend\tests -q
travel\Scripts\python.exe -m compileall -q backend\app
```

修改前端后运行：

```powershell
cd frontend
npm run build
```

修改导出、地图、天气或 RAG 时，优先运行对应测试和真实接口冒烟测试。

## 部署

快速启动（跨平台，Docker）：

```bash
make dev    # 启动所有服务
make build  # 构建镜像
make logs   # 查看日志
make test   # 运行测试
make down   # 停止服务
make clean  # 停止并清理数据
```

## Kilo 使用

- 项目级智能体：`.kilo/agent/travel-agent.md`
- 本地运行：`.kilo/run-script.ps1`
- worktree 初始化：`.kilo/setup-script.ps1`
- Agent Manager worktree 模式需要 Git 仓库；未初始化 Git 时使用 Local 模式。
