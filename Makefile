.PHONY: dev build down logs logs-backend logs-frontend logs-redis test clean help

help:
	@echo "智旅云图 - 部署命令"
	@echo ""
	@echo "  make dev        启动开发环境 (docker compose up)"
	@echo "  make build      构建所有 Docker 镜像"
	@echo "  make down       停止并移除容器"
	@echo "  make clean      停止容器并删除数据卷"
	@echo "  make logs       查看所有服务日志"
	@echo "  make logs-backend  查看后端日志"
	@echo "  make logs-frontend 查看前端日志"
	@echo "  make logs-redis  查看 Redis 日志"
	@echo "  make test       在后端容器中运行测试"

dev:
	docker compose up -d --build

build:
	docker compose build

down:
	docker compose down

clean:
	docker compose down -v --remove-orphans

logs:
	docker compose logs -f --tail=100

logs-backend:
	docker compose logs -f --tail=100 backend

logs-frontend:
	docker compose logs -f --tail=100 frontend

logs-redis:
	docker compose logs -f --tail=50 redis

test:
	docker compose exec backend python -m pytest backend/tests -q
