param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutputDir = Join-Path $ProjectRoot "build\nuitka"
$OutputExe = Join-Path $OutputDir "PMIC_AUX_GUI.exe"

if ($Clean -and (Test-Path $OutputDir)) {
    Remove-Item -Recurse -Force $OutputDir
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$Arguments = @(
    "-m", "nuitka",
    "--standalone",
    "--onefile",
    "--assume-yes-for-downloads",
    "--enable-plugin=tk-inter",
    "--windows-console-mode=disable",
    "--output-dir=$OutputDir",
    "--output-filename=PMIC_AUX_GUI.exe",
    "--windows-icon-from-ico=$ProjectRoot\\app_icon.ico",
    "--include-package-data=customtkinter",
    "--include-data-files=$ProjectRoot\\app_icon.ico=app_icon.ico",
    "--include-data-files=$ProjectRoot\drivers\jtool\jtoollib.py=drivers/jtool/jtoollib.py",
    "--include-data-files=$ProjectRoot\drivers\jtool\jtool.dll=drivers/jtool/jtool.dll",
    "--company-name=Codex",
    "--product-name=PMIC AUX GUI",
    "--file-version=1.0.0.0",
    "--product-version=1.0.0.0",
    "$ProjectRoot\run_pmic_aux_gui.py"
)

Push-Location $ProjectRoot
try {
    & python @Arguments
}
finally {
    Pop-Location
}
