param(
    [Parameter(Mandatory = $true)]
    [string]$Pdf,

    [string]$Zh = "",

    [int]$Port = 8788
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ViewerPy = Join-Path $ScriptDir "paper_viewer.py"

$ArgsList = @($ViewerPy, "--pdf", $Pdf, "--port", "$Port")
if ($Zh -ne "") {
    $ArgsList += @("--zh", $Zh)
}

Write-Host "Starting Bilingual Vector PDF Viewer on http://127.0.0.1:$Port ..." -ForegroundColor Cyan
& python @ArgsList
