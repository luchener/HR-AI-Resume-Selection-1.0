$ErrorActionPreference = 'Stop'

function Get-NodeNpmInfo {
    # 优先当前会话PATH
    $nodeCmd = Get-Command node -ErrorAction SilentlyContinue
    $npmCmd = Get-Command npm -ErrorAction SilentlyContinue

    if ($nodeCmd -and $npmCmd) {
        $nodeExe = $nodeCmd.Source
        $npmExe = $npmCmd.Source
        $nodeDir = Split-Path $nodeExe -Parent
        Write-Host "从进程PATH获取Node信息"
        return @{
            NodeExe  = $nodeExe
            NpmExe   = $npmExe
            NodeDir  = $nodeDir
        }
    }

    # PATH找不到 → 读取注册表定位Node.js安装目录
    Write-Host "PATH未找到Node，尝试读取系统注册表..."
    $regPaths = @(
        "HKLM:\SOFTWARE\Node.js",
        "HKLM:\SOFTWARE\WOW6432Node\Node.js"
    )
    $nodeInstallPath = $null
    foreach ($rp in $regPaths) {
        if(Test-Path $rp){
            $prop = Get-ItemProperty -Path $rp -ErrorAction SilentlyContinue
            if ($prop.InstallPath -and (Test-Path $prop.InstallPath)) {
                $nodeInstallPath = $prop.InstallPath.TrimEnd('\')
                break
            }
        }
    }

    if(-not $nodeInstallPath){
        throw "未找到Node.js，请确认本机已安装Node.js"
    }

    $nodeExe = Join-Path $nodeInstallPath "node.exe"
    $npmExe  = Join-Path $nodeInstallPath "npm.cmd"

    if(-not (Test-Path $nodeExe) -or -not (Test-Path $npmExe)){
        throw "注册表找到Node目录，但缺少node.exe/npm.cmd"
    }

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
        [Parameter(Mandatory = $true)][string]$Command,
        [string]$ExtraPATH = ""
    )

    if (-not (Test-Path -LiteralPath $WorkingDirectory)) {
        throw "Directory not found: $WorkingDirectory"
    }

    if (-not [string]::IsNullOrEmpty($ExtraPATH)){
        $scriptBlock = "`$env:PATH = '$ExtraPATH;' + `$env:PATH; Set-Location -LiteralPath '$WorkingDirectory'; $Command"
    }else{
        $scriptBlock = "Set-Location -LiteralPath '$WorkingDirectory'; $Command"
    }

    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($scriptBlock))

    Start-Process powershell.exe -ArgumentList @(
        '-NoExit',
        '-ExecutionPolicy', 'Bypass',
        '-EncodedCommand', $encodedCommand
    ) -WindowStyle Normal | Out-Null
}

function Wait-ForPort {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSeconds = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-NetConnection -ComputerName '127.0.0.1' -Port $Port -InformationLevel Quiet) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    throw "Port $Port did not become ready within $TimeoutSeconds seconds."
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

Start-ProjectWindow `
    -WorkingDirectory $backendDir `
    -Command '.\.venv\Scripts\python.exe run.py --host 127.0.0.1 --port 9001'

Start-ProjectWindow `
    -WorkingDirectory $frontendDir `
    -Command "& '$($nodeInfo.NpmExe)' run dev -- -p 3003" `
    -ExtraPATH $nodeInfo.NodeDir

# 端口检测容易误判，如果超时报错，换成下面Sleep
<#
Write-Host "等待前端服务启动8秒..."
Start-Sleep -Seconds 8
Start-Process 'http://127.0.0.1:3003' | Out-Null
#>

Wait-ForPort -Port 3003
Start-Process 'http://127.0.0.1:3003' | Out-Null
Write-Host 'Started backend and frontend. Frontend URL: http://127.0.0.1:3003'
