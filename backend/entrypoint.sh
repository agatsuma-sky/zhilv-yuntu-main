#!/bin/sh
set -e

# 在容器启动前初始化 SQLite 数据库表结构
python -c "from app.services.storage_service import init_db; init_db()"

# 将指南数据嵌入 ChromaDB（跳过已存在的分块）
python -c "
from app.rag.vector_db import ingest_guide_chunks_to_chroma
count = ingest_guide_chunks_to_chroma()
print(f'ChromaDB 初始化完成，写入 {count} 个分块')
"

exec "$@"
