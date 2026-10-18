#Requires -Version 5.1
# Qualified 16 GB IQ3_S + DFlash2 profile; requires a companion-enabled artifact.
[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$Model,
    [string]$Worker,
    [string]$Gpu,
    [ValidateRange(1, 65535)][int]$Port = 18200,
    [string]$ListenHost = '127.0.0.1',
    [string]$ApiKey,
    [string]$ModelAlias,
    [ValidateSet('off', 'error', 'warning', 'info', 'trace', 'debug')][string]$LogLevel,
    [ValidateRange(256, 2147483647)][int]$Context = 262144,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$global:LASTEXITCODE = 0
$forward = @{
    Context = $Context; Budget = 36864; Reserve = 10240; HostMiB = 12288
    Prefill = 256; MaxTokens = 16384; KvType = 'int8'
    MtpDrafts = 5; AdaptiveMtp = $false; NgramDrafts = 0
    Concurrency = 1; RetainedSessions = 1
    Vision = $true; VisionResidency = 'cpu'; VisionTokens = 1024
    DeviceProfile = 'off'; ThinkingBudget = 8192
    SinkTokens = 12288; RecentTokens = 1024
    LrfuLambda = 0.5; LrfuTopFraction = 0.9; ScoreAggregation = 'max'
    WorkerArgs = @('--spec', 'dflash2', '--gdn-state-fp16', '--cuda-graph-allowance-mib', '64')
}
# The shared launcher calls its draft-count parameter MtpDrafts. Its final native
# options select DFlash2; MTP adaptation must remain disabled for this backend.
foreach ($entry in $PSBoundParameters.GetEnumerator()) { $forward[$entry.Key] = $entry.Value }
& (Join-Path $PSScriptRoot 'start-ninfer.ps1') @forward
if ($LASTEXITCODE) { exit $LASTEXITCODE }
