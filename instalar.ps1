# Instala a Lia em um ambiente virtual (.venv) no Windows.
# Uso:  powershell -ExecutionPolicy Bypass -File instalar.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python nao encontrado. Instale o Python 3.10-3.12 em python.org e tente de novo." -ForegroundColor Red
    exit 1
}

Write-Host "==> Criando ambiente virtual (.venv)..." -ForegroundColor Cyan
python -m venv .venv

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "==> Atualizando pip..." -ForegroundColor Cyan
& $python -m pip install --upgrade pip

Write-Host "==> Instalando PyTorch para CPU (~200 MB)..." -ForegroundColor Cyan
& $python -m pip install torch --index-url https://download.pytorch.org/whl/cpu

Write-Host "==> Instalando as demais dependencias..." -ForegroundColor Cyan
& $python -m pip install -r requirements.txt

if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    Copy-Item (Join-Path $PSScriptRoot ".env.example") (Join-Path $PSScriptRoot ".env")
    Write-Host "==> Arquivo .env criado a partir de .env.example." -ForegroundColor Cyan
}

Write-Host ""
Write-Host "Pronto!" -ForegroundColor Green
Write-Host "  Teste:  .\.venv\Scripts\python.exe verify.py"
Write-Host "  Rodar:  .\.venv\Scripts\python.exe run.py --fullscreen"
Write-Host ""
Write-Host "Na primeira execucao, o Kokoro baixa cerca de 350 MB." -ForegroundColor DarkGray
Write-Host "Depois disso o totem abre em segundos, usando o cache em cache\voz." -ForegroundColor DarkGray
