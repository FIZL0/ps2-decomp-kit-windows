<#
    Entry point for PowerShell: `.\kit.ps1 <command>`, the same commands as `./kit` on Linux.

    PowerShell will not run `kit` either (no extension, and a shebang is just a comment), so this picks an
    interpreter and passes the arguments through unchanged. `py` first because it is the only name that is
    reliably Python 3, `python3` last because without Python installed that name is the Store stub.
    KIT_PYTHON overrides the search. The exit code is the kit's own, so `if (kit test) {...}` works.

    If PowerShell refuses to run the file at all, it is the execution policy, not the kit:
        Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
    or use kit.cmd, which has no policy to satisfy.
#>
$kit = Join-Path $PSScriptRoot 'kit'

function Find-Python {
    if ($env:KIT_PYTHON) { return @{ exe = $env:KIT_PYTHON; pre = @() } }
    foreach ($c in @(@{ exe = 'py'; pre = @('-3') },
                     @{ exe = 'python'; pre = @() },
                     @{ exe = 'python3'; pre = @() })) {
        if (-not (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
        try { & $c.exe @($c.pre) -c '' 2>$null | Out-Null } catch { continue }
        if ($LASTEXITCODE -eq 0) { return $c }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Error ('kit: no Python 3 found on PATH. Install Python 3.11 or newer (python.org, or: ' +
                 'winget install Python.Python.3.12), or set KIT_PYTHON to the interpreter to use.')
    exit 2
}

& $py.exe @($py.pre) $kit @args
exit $LASTEXITCODE
