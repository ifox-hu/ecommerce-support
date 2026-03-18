param([int]$Port = 8765)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$demoPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'

if (-not (Test-Path -LiteralPath $demoPython)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -m venv .venv }
    else { python -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw '无法创建 Python 虚拟环境。请安装 Python 3.11+。' }
}

& $demoPython -c 'import fastapi, langgraph, mcp, jieba, sqlglot, redis' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $demoPython -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。' }
}

Push-Location -LiteralPath 'frontend'
try {
    if (-not (Test-Path -LiteralPath 'node_modules/.bin/vite.cmd')) {
        npm.cmd ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
    }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
}
finally { Pop-Location }

& $demoPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
