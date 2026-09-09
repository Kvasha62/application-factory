$ErrorActionPreference = "Stop"

Write-Host "========================================"
Write-Host "APPLICATION-FACTORY QUALITY GATE v1"
Write-Host "========================================"
Write-Host ""

function Invoke-GateStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action
    )

    Write-Host "[RUN ] $Name"
    & $Action

    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $Name"
        Write-Host ""
        Write-Host "QUALITY GATE: FAILED"
        exit $LASTEXITCODE
    }

    Write-Host "[PASS] $Name"
    Write-Host ""
}

$pythonVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] Python 3.13 (python executable not available)"
    exit 1
}

if ($pythonVersion -ne "3.13") {
    Write-Host "[FAIL] Python 3.13 (detected $pythonVersion)"
    exit 1
}

Write-Host "[PASS] Python 3.13"
Write-Host ""

Invoke-GateStep -Name "Compile source" -Action {
    python -m compileall -q src
}

Invoke-GateStep -Name "Ruff" -Action {
    ruff check .
}

Invoke-GateStep -Name "Black" -Action {
    black --check .
}

Invoke-GateStep -Name "Test suite" -Action {
    python -m pytest tests/
}

Write-Host "========================================"
Write-Host "QUALITY GATE: PASS"
Write-Host "========================================"
