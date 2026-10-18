# Windows PowerShell 5.1 loses embedded quotes with native @args invocation.
# Encode argv using the Windows CRT rules and launch without cmd.exe/a shell.
function Resolve-KVMemWorkerArgs([System.Collections.IDictionary]$BoundParameters, [string[]]$Tail) {
    $arguments = @()
    if ($BoundParameters.ContainsKey('WorkerArgsFile')) {
        if ($BoundParameters.ContainsKey('WorkerArgs') -or $Tail.Count) {
            throw 'Use either -WorkerArgs or -WorkerArgsFile, not both'
        }
        $path = $BoundParameters['WorkerArgsFile']
        if (!$path -or !(Test-Path -LiteralPath $path -PathType Leaf)) {
            throw 'WorkerArgsFile must be an existing UTF-8 JSON file'
        }
        try {
            # A property preserves empty/singleton arrays in Windows PowerShell 5.1.
            $payload = ConvertFrom-Json -InputObject ('{"argv":' + [IO.File]::ReadAllText((Resolve-Path -LiteralPath $path).Path) + '}')
        } catch { throw 'WorkerArgsFile must contain a JSON array of strings' }
        if (@($payload.PSObject.Properties).Count -ne 1 -or $payload.argv -isnot [array]) {
            throw 'WorkerArgsFile must contain a JSON array of strings'
        }
        foreach ($value in $payload.argv) {
            if ($value -isnot [string] -or $value.Contains([string][char]0)) {
                throw 'WorkerArgsFile must contain strings without NUL characters'
            }
        }
        $arguments = @($payload.argv)
    } else {
        if ($Tail.Count -and !$BoundParameters.ContainsKey('WorkerArgs')) {
            throw 'Unexpected arguments: use named launcher settings, or put native options last after -WorkerArgs'
        }
        if ($BoundParameters.ContainsKey('WorkerArgs')) { $arguments += @($BoundParameters['WorkerArgs']) }
        # -File binds only the first value of a named string[]; collect its tail
        # explicitly instead of letting it bind to Model/StageLayers/other fields.
        $arguments += $Tail
    }
    foreach ($value in $arguments) {
        if ($null -eq $value -or $value.Contains([string][char]0)) {
            throw 'WorkerArgs must contain strings without NUL characters'
        }
    }
    return ,$arguments
}

function ConvertTo-KVMemArgument([AllowEmptyString()][string]$Value) {
    '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

function New-KVMemProcessInfo([string]$Binary, [string[]]$Arguments) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Binary
    $info.UseShellExecute = $false
    $info.Arguments = (($Arguments | ForEach-Object { ConvertTo-KVMemArgument $_ }) -join ' ')
    return $info
}
