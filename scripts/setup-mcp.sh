#!/usr/bin/env bash
# Скрипт проверки и установки зависимостей для MCP серверов
# Запуск: bash scripts/setup-mcp.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "=== Fluxio MCP Setup ==="
echo ""

# 1. Проверка Node.js
if command -v node &> /dev/null; then
    NODE_VERSION=$(node --version)
    echo -e "${GREEN}✓${NC} Node.js установлен: $NODE_VERSION"
else
    echo -e "${RED}✗${NC} Node.js не найден!"
    echo "  Установите Node.js 18+: https://nodejs.org/"
    echo "  Или: sudo apt install nodejs npm"
    exit 1
fi

# 2. Проверка npx
if command -v npx &> /dev/null; then
    echo -e "${GREEN}✓${NC} npx доступен"
else
    echo -e "${RED}✗${NC} npx не найден! Установите npm: sudo apt install npm"
    exit 1
fi

# 3. Предварительная загрузка MCP пакетов (кэширование)
echo ""
echo "Загрузка MCP пакетов..."

echo -n "  @playwright/mcp... "
npx -y @playwright/mcp@latest --help &> /dev/null && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}будет загружен при первом запуске${NC}"

echo -n "  @modelcontextprotocol/server-postgres... "
npx -y @modelcontextprotocol/server-postgres --help &> /dev/null 2>&1 && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}будет загружен при первом запуске${NC}"

echo -n "  mcp-server-redis... "
npx -y mcp-server-redis --help &> /dev/null 2>&1 && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}будет загружен при первом запуске${NC}"

# 4. Проверка .mcp.json
echo ""
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

if [ -f "$PROJECT_ROOT/.mcp.json" ]; then
    echo -e "${GREEN}✓${NC} .mcp.json найден в проекте"
else
    echo -e "${RED}✗${NC} .mcp.json не найден в корне проекта!"
    echo "  Файл должен быть в: $PROJECT_ROOT/.mcp.json"
    exit 1
fi

# 5. Проверка Docker сервисов (postgres, redis)
echo ""
echo "Проверка Docker сервисов..."

if command -v docker &> /dev/null; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q postgres; then
        echo -e "${GREEN}✓${NC} PostgreSQL контейнер запущен"
    else
        echo -e "${YELLOW}!${NC} PostgreSQL контейнер не найден — запустите: docker compose up -d"
    fi

    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q redis; then
        echo -e "${GREEN}✓${NC} Redis контейнер запущен"
    else
        echo -e "${YELLOW}!${NC} Redis контейнер не найден — запустите: docker compose up -d"
    fi
else
    echo -e "${YELLOW}!${NC} Docker не установлен — MCP postgres/redis не будут работать"
fi

echo ""
echo -e "${GREEN}=== Setup завершён ===${NC}"
echo "MCP серверы будут доступны при следующем запуске Claude Code."
