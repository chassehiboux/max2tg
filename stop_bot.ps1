$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $scriptRoot
$exitCode = 0

try {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -m app.launcher stop
        $exitCode = $LASTEXITCODE
    } else {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            & $py.Source -3 -m app.launcher stop
            $exitCode = $LASTEXITCODE
        } else {
            throw "Python не найден в PATH. Установите Python 3.12+ и повторите запуск."
        }
    }
}
finally {
    Pop-Location
}

exit $exitCode
