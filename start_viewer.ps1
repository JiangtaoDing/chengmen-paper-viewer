[CmdletBinding(DefaultParameterSetName = "SinglePdf")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "SinglePdf")]
    [string]$Pdf,

    [Parameter(Mandatory = $true, ParameterSetName = "PaperDir")]
    [string]$PaperDir,

    [Parameter(ParameterSetName = "PaperDir")]
    [string]$Default = "",

    [string]$Zh = "",

    [ValidateRange(1, 65535)]
    [int]$Port = 8788
)

function Write-Stderr([string]$Message) {
    [Console]::Error.WriteLine("[start_viewer] $Message")
}

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$ViewerPy = Join-Path $ScriptDir "paper_viewer.py"

if (-not (Test-Path -LiteralPath $ViewerPy -PathType Leaf)) {
    Write-Stderr "Error: paper_viewer.py not found at: $ViewerPy"
    exit 1
}

if ($PSCmdlet.ParameterSetName -eq "SinglePdf") {
    if (-not (Test-Path -LiteralPath $Pdf -PathType Leaf)) {
        Write-Stderr "Error: PDF file not found: $Pdf"
        exit 1
    }
} else {
    if (-not (Test-Path -LiteralPath $PaperDir -PathType Container)) {
        Write-Stderr "Error: Paper directory not found: $PaperDir"
        exit 1
    }
}

if ($Zh -ne "" -and -not (Test-Path -LiteralPath $Zh -PathType Leaf)) {
    Write-Stderr "Error: Companion JSON file not found: $Zh"
    exit 1
}

# Pre-flight TCP port occupancy check on 127.0.0.1
$listener = $null
try {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
    $listener.Start()
} catch {
    Write-Stderr "Error: Port $Port on 127.0.0.1 is already in use or unavailable. Try another -Port value."
    exit 1
} finally {
    if ($null -ne $listener) {
        try { $listener.Stop() } catch {}
    }
}

# Resolve Python interpreter (prefer python, fall back to py -3)
$PythonCmd = $null
$PythonPrefix = @()
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd) {
    $PythonCmd = $pyCmd.Source
} else {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $PythonCmd = $pyLauncher.Source
        $PythonPrefix = @("-3")
    } else {
        Write-Stderr "Error: Python 3 not found in PATH. Please install Python 3 and ensure 'python' or 'py' is available."
        exit 1
    }
}

$ArgsList = @()
$ArgsList += $PythonPrefix
$ArgsList += @($ViewerPy)
if ($PSCmdlet.ParameterSetName -eq "SinglePdf") {
    $ArgsList += @("--pdf", $Pdf)
} else {
    $ArgsList += @("--paper-dir", $PaperDir)
    if ($Default -ne "") {
        $ArgsList += @("--default", $Default)
    }
}
$ArgsList += @("--port", "$Port")
if ($Zh -ne "") {
    $ArgsList += @("--zh", $Zh)
}

$env:PYTHONUTF8 = "1"
Write-Host "Starting Chengmen Paper Viewer on http://127.0.0.1:$Port ..." -ForegroundColor Cyan
& $PythonCmd @ArgsList
exit $LASTEXITCODE
