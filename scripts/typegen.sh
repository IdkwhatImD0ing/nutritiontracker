#!/usr/bin/env bash
# Pydantic contracts -> OpenAPI -> TypeScript (DESIGN.md §2 "Shared types, defined once").
# Run from anywhere; CI runs `pnpm run typegen:check` in frontend/ to make drift unmergeable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT/backend"
uv run python -c "import json, main; print(json.dumps(main.app.openapi()))" > ../frontend/openapi.json

cd "$ROOT"
mkdir -p frontend/lib
pnpm dlx openapi-typescript frontend/openapi.json -o frontend/lib/api-types.ts
