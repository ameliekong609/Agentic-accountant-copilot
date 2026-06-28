param(
    [string]$HermesBin = "hermes"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not (Get-Command $HermesBin -ErrorAction SilentlyContinue)) {
    Write-Host "Hermes CLI not found. Install Hermes Desktop/CLI first, then rerun this script."
    exit 1
}

function Test-HermesProfile {
    param([string]$Name)

    & $HermesBin profile show $Name *> $null
    return $LASTEXITCODE -eq 0
}

function Ensure-HermesProfile {
    param(
        [string]$Name,
        [string]$Description,
        [string]$SoulTemplate
    )

    if (Test-HermesProfile -Name $Name) {
        Write-Host "Updating existing Hermes profile: $Name"
    }
    else {
        Write-Host "Creating Hermes profile: $Name"
        & $HermesBin profile create $Name --clone --description $Description
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create Hermes profile: $Name"
        }
    }

    $ProfileDir = Join-Path $env:USERPROFILE ".hermes\profiles\$Name"
    New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
    Copy-Item -Force $SoulTemplate (Join-Path $ProfileDir "SOUL.md")

    & $HermesBin profile describe $Name --text $Description | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to describe Hermes profile: $Name"
    }

    & $HermesBin -p $Name config set terminal.cwd $RepoRoot | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to set terminal.cwd for Hermes profile: $Name"
    }

    $CodexSource = Join-Path $env:USERPROFILE ".codex"
    $CodexTarget = Join-Path $ProfileDir "home\.codex"
    New-Item -ItemType Directory -Force -Path $CodexTarget | Out-Null

    $AuthSource = Join-Path $CodexSource "auth.json"
    if (Test-Path $AuthSource) {
        Copy-Item -Force $AuthSource (Join-Path $CodexTarget "auth.json")
    }
    else {
        Write-Host "Warning: $AuthSource not found. Run 'codex login' before using nested Codex from Hermes profile '$Name'."
    }

    $ConfigSource = Join-Path $CodexSource "config.toml"
    if (Test-Path $ConfigSource) {
        Copy-Item -Force $ConfigSource (Join-Path $CodexTarget "config.toml")
    }
}

Ensure-HermesProfile `
    -Name "workpaper" `
    -Description "Accountant-facing financial statement workpaper assistant. Takes a local client folder path, coordinates Codex CLI to prepare a TB Bridge workbook, and returns accountant-friendly Excel output plus short review summary." `
    -SoulTemplate (Join-Path $RepoRoot "docs\hermes_profiles\workpaper\SOUL.md")

Ensure-HermesProfile `
    -Name "turing" `
    -Description "Senior accountant supervisor for financial statement automation. Reviews Codex-generated TB Bridge workpapers, challenges accounting logic and evidence, and writes correction briefs for Codex." `
    -SoulTemplate (Join-Path $RepoRoot "docs\hermes_profiles\turing\SOUL.md")

Write-Host ""
Write-Host "Installed Hermes accounting profiles:"
Write-Host "  - workpaper: accountant-facing front door"
Write-Host "  - turing: senior accountant supervisor"
Write-Host ""
Write-Host "Open Hermes Desktop and start a new session under the workpaper profile."
