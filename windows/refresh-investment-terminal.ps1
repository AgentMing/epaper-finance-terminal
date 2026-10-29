param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$configPath = Join-Path $ProjectRoot 'data\quota-service.json'
$outDir = Join-Path $ProjectRoot 'data\out'
$logPath = Join-Path $outDir 'investment-terminal-refresh.log'
$previewPath = Join-Path $ProjectRoot 'design\investment-intelligence-preview.png'
$framePath = Join-Path $ProjectRoot 'data\investment-terminal-frame.bin'

New-Item -ItemType Directory -Path $outDir -Force | Out-Null

function Invoke-LoggedNative {
    param([string]$Executable, [string[]]$Arguments, [string]$Step)

    Add-Content -LiteralPath $logPath -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Step"
    $saved = $ErrorActionPreference
    try {
        # Native stderr is ordinary progress for Python/WSL and should go to the log.
        $ErrorActionPreference = 'Continue'
        & $Executable @Arguments *>> $logPath
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $saved
    }
    if ($code -ne 0) {
        throw "$Step failed with exit code $code"
    }
}

try {
    if (-not (Test-Path -LiteralPath $configPath)) {
        throw "Missing local device config: $configPath"
    }
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if (-not $config.deviceAddress) {
        throw 'deviceAddress is missing from data/quota-service.json'
    }

    $fullRoot = [IO.Path]::GetFullPath($ProjectRoot)
    $drive = $fullRoot.Substring(0, 1).ToLowerInvariant()
    $relative = $fullRoot.Substring(3).Replace('\', '/')
    $wslScript = "/mnt/$drive/$relative/tools/investment_terminal.py"
    $python = (Get-Command python.exe -ErrorAction Stop).Source

    Invoke-LoggedNative 'wsl.exe' @('-e', 'python3', $wslScript) 'Fetch EastMoney/FRED/MOVE data and render PNG'
    Invoke-LoggedNative $python @(
        (Join-Path $ProjectRoot 'tools\make_image.py'),
        $previewPath,
        '--out', $framePath
    ) 'Convert PNG to ZKC42V frame'
    Invoke-LoggedNative $python @(
        (Join-Path $ProjectRoot 'bleprobe.py'),
        'send',
        [string]$config.deviceAddress,
        $framePath
    ) 'Push frame to investment terminal'

    Add-Content -LiteralPath $logPath -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Refresh completed successfully."
}
catch {
    Add-Content -LiteralPath $logPath -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ERROR: $($_.Exception.Message)"
    exit 1
}
