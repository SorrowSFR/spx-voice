#!/usr/bin/env pwsh
# Run the local SPX Voice stack from locally built Docker images.

[CmdletBinding()]
param(
    [ValidateSet('up', 'rebuild', 'restart', 'down', 'logs', 'ps')]
    [string]$Command = 'up'
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BaseDir = Split-Path -Parent $ScriptDir
Set-Location $BaseDir

$ComposeArgs = @(
    'compose',
    '-f', 'docker-compose.yaml',
    '-f', 'docker-compose.dev.yaml'
)

function Ensure-EnvFile([string]$Example, [string]$Destination) {
    if ((Test-Path $Destination) -or -not (Test-Path $Example)) {
        return
    }
    Copy-Item $Example $Destination
    Write-Host "Created $Destination from $Example"
}

function Print-NextSteps {
    Write-Host ''
    Write-Host 'SPX Voice Docker dev stack is starting/running.'
    Write-Host '  UI:     http://localhost:3010'
    Write-Host '  API:    http://localhost:8000/api/v1/health'
    Write-Host '  MinIO:  http://localhost:9001'
    Write-Host ''
    Write-Host 'Useful commands:'
    Write-Host '  .\scripts\docker_dev.ps1 logs'
    Write-Host '  .\scripts\docker_dev.ps1 ps'
    Write-Host '  .\scripts\docker_dev.ps1 down'
    Write-Host ''
    Write-Host 'This dev stack bind-mounts local api/ and ui/ code; it does not build the heavy production images.'
}

switch ($Command) {
    'up' {
        Ensure-EnvFile '.env.example' '.env'
        Ensure-EnvFile 'api/.env.example' 'api/.env'
        Ensure-EnvFile 'ui/.env.example' 'ui/.env'
        # `up` pulls the public images and builds the API image locally from
        # api/Dockerfile on first run when no published image is available. The
        # first build can take a few minutes; later starts reuse the built image.
        docker @ComposeArgs up -d
        Print-NextSteps
    }
    'rebuild' {
        docker @ComposeArgs build api
        docker @ComposeArgs up -d --force-recreate api ui
        Print-NextSteps
    }
    'restart' {
        docker @ComposeArgs up -d --force-recreate api ui
        Print-NextSteps
    }
    'down' {
        docker @ComposeArgs down
    }
    'logs' {
        docker @ComposeArgs logs -f --tail=150
    }
    'ps' {
        docker @ComposeArgs ps
    }
}
