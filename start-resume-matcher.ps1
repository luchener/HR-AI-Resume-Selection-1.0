$ErrorActionPreference = 'Stop'

function Get-NodeNpmInfo {
    # PATH 可能被其他工具注入损坏片段，只有在路径真实存在时才使用它。
    $candidates = @()
    foreach ($programRoot in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($programRoot) {
            $candidates += (Join-Path $programRoot 'nodejs')
        }
    }
    $candidates += 'C:\Program Files\nodejs'
    $candidates += 'C:\Program Files (x86)\nodejs'
    foreach ($commandName in @('node.exe', 'node')) {
        $command = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue
        if ($command -and $command.Path -and $command.Path -match '^[A-Za-z]:[\\/]' -and (Test-Path -LiteralPath $command.Path)) {
            $candidates += (Split-Path -Parent $command.Path)
        }
    }

    $nodeInstallPath = $null
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        try {
            $resolvedCandidate = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
        } catch {
            continue
        }
        if (
            (Test-Path -LiteralPath (Join-Path $resolvedCandidate 'node.exe')) -and
            (Test-Path -LiteralPath (Join-Path $resolvedCandidate 'npm.cmd'))
        ) {
            $nodeInstallPath = $resolvedCandidate
            break
        }
    }
    if (-not $nodeInstallPath) {
        throw "未找到可用的 Node.js/npm.cmd，请确认已安装 Node.js。"
    }

    $nodeExe = Join-Path $nodeInstallPath 'node.exe'
    $npmExe = Join-Path $nodeInstallPath 'npm.cmd'
    Write-Host "使用 Node.js: $nodeExe"
    Write-Host "使用 npm:     $npmExe"

    return @{
        NodeExe  = $nodeExe
        NpmExe   = $npmExe
        NodeDir  = $nodeInstallPath
    }
}

function Get-ProjectRoot {
    if ($env:RESUME_MATCHER_ROOT) {
        $envPath = $env:RESUME_MATCHER_ROOT.Trim()
        if (Test-Path -LiteralPath $envPath -PathType Container) {
            Write-Host "使用环境变量指定项目根: $envPath"
            return $envPath
        }
    }

    $scriptDir = $PSScriptRoot
    Write-Host "脚本所在目录: $scriptDir"

    if (
        (Test-Path -LiteralPath (Join-Path $scriptDir 'apps\backend') -PathType Container) -and
        (Test-Path -LiteralPath (Join-Path $scriptDir 'apps\frontend') -PathType Container)
    ) {
        Write-Host "检测：脚本位于项目根目录内"
        return $scriptDir
    }

    $localProject = Get-ChildItem -LiteralPath $scriptDir -Directory -Recurse -Filter 'HR-AI-Resume-Selection-1.0' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($localProject) {
        Write-Host "在脚本目录下找到项目目录: $($localProject.FullName)"
        return $localProject.FullName
    }

    $documentsDir = Join-Path $env:USERPROFILE 'Documents'
    $docProject = Get-ChildItem -LiteralPath $documentsDir -Directory -Recurse -Filter 'HR-AI-Resume-Selection-1.0' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($docProject) {
        Write-Host "在Documents文档目录找到项目目录: $($docProject.FullName)"
        return $docProject.FullName
    }

    throw 'Project root not found. 找不到 HR-AI-Resume-Selection-1.0 项目目录。'
}

function Start-ProjectWindow {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )

    if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
        throw "Directory not found: $WorkingDirectory"
    }
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        throw "Executable not found: $FilePath"
    }

    # 直接启动目标程序，避免嵌套 PowerShell 的 PATH 和引号解析问题。
    return Start-Process -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Normal `
        -PassThru
}

function Assert-PortBindable {
    param(
        [Parameter(Mandatory = $true)][int]$Port
    )

    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Parse('127.0.0.1'),
            $Port
        )
        $listener.Start()
    } catch {
        throw "端口 $Port 无法监听，可能已被占用或被 Windows 保留。可运行 `"netsh interface ipv4 show excludedportrange protocol=tcp`" 检查；当前脚本不会再等待到超时。原始错误: $($_.Exception.Message)"
    } finally {
        if ($listener) {
            $listener.Stop()
        }
    }
}

function Test-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Uri
    )

    try {
        $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400)
    } catch {
        return $false
    }
}

function Wait-ForPort {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSeconds = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $connect = $client.ConnectAsync('127.0.0.1', $Port)
            if ($connect.Wait(1000) -and $client.Connected) {
                return
            }
        } catch {
            # The service may still be compiling or starting.
        } finally {
            $client.Dispose()
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    throw "端口 $Port 在 $TimeoutSeconds 秒内未启动。请检查新打开的后端/前端窗口中的错误信息。"
}

# ==========主流程==========
$nodeInfo = Get-NodeNpmInfo
Write-Host "node路径: $($nodeInfo.NodeExe)"
Write-Host "npm路径: $($nodeInfo.NpmExe)"
Write-Host "node目录(注入PATH): $($nodeInfo.NodeDir)`n"

$projectRoot = Get-ProjectRoot
Write-Host "`n最终确认项目根目录：$projectRoot`n"

$backendDir = Join-Path $projectRoot 'apps\backend'
$frontendDir = Join-Path $projectRoot 'apps\frontend'
$backendPort = 9001
$frontendPort = 3008
$backendPython = Join-Path $backendDir '.venv\Scripts\python.exe'

# npm.cmd 仍会通过 PATH 查找 node，先把已验证的 Node 安装目录放到最前面。
$env:PATH = "$($nodeInfo.NodeDir);$env:PATH"

$backendReady = Test-HttpEndpoint -Uri "http://127.0.0.1:$backendPort/ping"
if ($backendReady) {
    Write-Host "后端已在 http://127.0.0.1:$backendPort 运行，复用现有服务。"
} else {
    Assert-PortBindable -Port $backendPort
    $backendWindow = Start-ProjectWindow `
        -WorkingDirectory $backendDir `
        -FilePath $backendPython `
        -ArgumentList @('run.py', '--host', '127.0.0.1', '--port', [string]$backendPort)
}

$frontendReady = Test-HttpEndpoint -Uri "http://127.0.0.1:$frontendPort"
if ($frontendReady) {
    Write-Host "前端已在 http://127.0.0.1:$frontendPort 运行，复用现有服务。"
} else {
    Assert-PortBindable -Port $frontendPort
    $frontendWindow = Start-ProjectWindow `
        -WorkingDirectory $frontendDir `
        -FilePath $nodeInfo.NpmExe `
        -ArgumentList @('run', 'dev', '--', '--hostname', '127.0.0.1', '-p', [string]$frontendPort)
}

# 端口检测容易误判，如果超时报错，换成下面Sleep
<#
Write-Host "等待前端服务启动8秒..."
Start-Sleep -Seconds 8
Start-Process "http://127.0.0.1:$frontendPort" | Out-Null
#>

Wait-ForPort -Port $backendPort -TimeoutSeconds 30
Wait-ForPort -Port $frontendPort -TimeoutSeconds 180
Start-Process "http://127.0.0.1:$frontendPort" | Out-Null
Write-Host "后端和前端服务已就绪。前端地址：http://127.0.0.1:$frontendPort"
