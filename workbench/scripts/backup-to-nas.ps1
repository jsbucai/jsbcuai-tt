param(
    [string]$NasRoot = '\\192.168.100.1\NAS',
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "Project directory not found: $ProjectRoot"
}
if (-not (Test-Path -LiteralPath $NasRoot -PathType Container)) {
    throw "NAS share is unavailable: $NasRoot"
}

$versionFile = Join-Path $ProjectRoot 'version.properties'
$versionName = 'unknown'
if (Test-Path -LiteralPath $versionFile) {
    $versionLine = Get-Content -LiteralPath $versionFile | Where-Object { $_ -match '^versionName=' } | Select-Object -First 1
    if ($versionLine) { $versionName = ($versionLine -split '=', 2)[1].Trim() }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupName = "UL随行-workbench-android-$versionName-$stamp"
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$tempRoot = Join-Path $tempBase "ul-workbench-nas-$stamp-$PID"
$stageRoot = Join-Path $tempRoot 'source'
$stageProject = Join-Path $stageRoot 'workbench-android'
$nasParent = Join-Path $NasRoot '开发项目\UL随行'
$uploadPath = Join-Path $nasParent ($backupName + '.uploading')
$finalPath = Join-Path $nasParent $backupName

if ((Test-Path -LiteralPath $uploadPath) -or (Test-Path -LiteralPath $finalPath)) {
    throw "Backup destination already exists: $finalPath"
}

$excludedDirectories = @('.git', '.gradle', '.kotlin', '.pytest_cache', '.venv', '__pycache__', 'build', 'runtime')
$excludedNames = @('.env', 'local.properties')
$excludedExtensions = @('.apk', '.jks', '.keystore', '.log', '.pyc')

try {
    New-Item -ItemType Directory -Path $stageProject -Force | Out-Null
    $sourceFiles = foreach ($file in Get-ChildItem -LiteralPath $ProjectRoot -Recurse -Force -File) {
        $relative = [IO.Path]::GetRelativePath($ProjectRoot, $file.FullName)
        $parts = $relative -split '[\\/]'
        if ($parts | Where-Object { $_ -in $excludedDirectories }) { continue }
        if ($file.Name -in $excludedNames) { continue }
        if ($file.Extension.ToLowerInvariant() -in $excludedExtensions) { continue }
        [pscustomobject]@{ File = $file; Relative = $relative }
    }

    foreach ($entry in $sourceFiles) {
        $target = Join-Path $stageProject $entry.Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $entry.File.FullName -Destination $target
    }

    $sourceManifest = foreach ($entry in $sourceFiles) {
        [ordered]@{
            path = ($entry.Relative -replace '\\', '/')
            bytes = $entry.File.Length
            sha256 = (Get-FileHash -LiteralPath $entry.File.FullName -Algorithm SHA256).Hash
        }
    }

    New-Item -ItemType Directory -Path $nasParent -Force | Out-Null
    New-Item -ItemType Directory -Path $uploadPath -Force | Out-Null

    $zipPath = Join-Path $uploadPath "$backupName-source.zip"
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($stageRoot, $zipPath, [IO.Compression.CompressionLevel]::Optimal, $false)

    $signingSource = Join-Path $ProjectRoot 'runtime\signing\debug.keystore'
    if (Test-Path -LiteralPath $signingSource -PathType Leaf) {
        $signingDirectory = Join-Path $uploadPath 'private-signing'
        New-Item -ItemType Directory -Path $signingDirectory -Force | Out-Null
        Copy-Item -LiteralPath $signingSource -Destination (Join-Path $signingDirectory 'debug.keystore')
        @'
This is the development-only signing key used by UL Workbench debug APKs.
Keep this directory private. On a new PC, copy debug.keystore to:
  workbench-android\runtime\signing\debug.keystore
It is required only when a new development APK must update an already-installed build.
'@ | Set-Content -LiteralPath (Join-Path $signingDirectory 'README.txt') -Encoding utf8
    }

    $latestApk = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'releases') -File -Filter '*.apk' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latestApk) {
        Copy-Item -LiteralPath $latestApk.FullName -Destination (Join-Path $uploadPath $latestApk.Name)
    }

    $createdAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')
    $restoreGuide = @"
# UL随行 App 换电脑继续开发

- 备份时间：$createdAt
- 源电脑项目：$ProjectRoot
- App 版本：$versionName
- NAS 目录：$finalPath

## 备份内容

- $($backupName)-source.zip：Android App、Python 服务、接口工具、测试、文档和设计资源。
- `private-signing/debug.keystore`：仅用于开发版覆盖安装的调试签名，请勿公开。
- 最新开发 APK：用于安装或对照当前版本。
- `source-manifest.json`、`SHA256SUMS.txt`：文件清单和完整性校验。

## 未备份内容

构建缓存、Gradle/JDK/Android SDK、Python 虚拟环境、APK 中间产物、运行日志、ERP 原始响应、本机数据库、`local.properties`、AppKey、App Secret、Sign Key、Token 和 Windows 凭据均未写入迁移包。

## 新电脑恢复

1. 从 NAS 复制源码 ZIP 到新电脑并解压。
2. 安装 Android Studio（含 Android SDK 35）和 JDK 17；安装 Python 3.10 或更高版本。
3. 在项目根目录创建 local.properties，内容示例：sdk.dir=C:/Users/你的用户名/AppData/Local/Android/Sdk。
4. 将 private-signing/debug.keystore 复制到项目的 workbench-android\runtime\signing\debug.keystore。
5. 在项目根目录运行：powershell -ExecutionPolicy Bypass -File .\scripts\build.ps1。
6. Python 服务首次安装：python -m venv service\.venv，激活后运行 pip install -r service\requirements.txt。
7. ERP 接口凭据没有迁移；请在新电脑重新保存连接信息并重新授权。不要把密钥写进源码。

当前服务仍默认读取 `E:\UL\ERP自动化` 下的数据文件。新电脑若盘符或目录不同，需要恢复同样目录，或在继续开发时将这些路径改为可配置项。

## 校验

在本目录执行：Get-FileHash .\*-source.zip -Algorithm SHA256，结果应与 SHA256SUMS.txt 一致。

外地访问 NAS 应先连接家庭 VPN/WireGuard。不要把 SMB 445 端口直接开放到公网。
"@
    $guidePath = Join-Path $uploadPath 'README-换电脑继续开发.md'
    $restoreGuide | Set-Content -LiteralPath $guidePath -Encoding utf8

    $manifestPath = Join-Path $uploadPath 'source-manifest.json'
    [ordered]@{
        project = 'UL随行 workbench-android'
        version = $versionName
        created_at = $createdAt
        source_file_count = @($sourceManifest).Count
        source_bytes = ($sourceFiles.File | Measure-Object Length -Sum).Sum
        excluded = [ordered]@{
            directories = $excludedDirectories
            files = $excludedNames
            extensions = $excludedExtensions
            private_data = @('ERP credentials and tokens', 'runtime databases', 'raw ERP responses')
        }
        files = $sourceManifest
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding utf8

    $checksumTargets = Get-ChildItem -LiteralPath $uploadPath -Recurse -File | Where-Object { $_.Name -ne 'SHA256SUMS.txt' }
    $checksumLines = foreach ($file in $checksumTargets) {
        $relative = [IO.Path]::GetRelativePath($uploadPath, $file.FullName) -replace '\\', '/'
        '{0}  {1}' -f (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash, $relative
    }
    $checksumLines | Set-Content -LiteralPath (Join-Path $uploadPath 'SHA256SUMS.txt') -Encoding utf8

    $zip = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $zipEntries = @($zip.Entries | Where-Object { $_.Name })
        $forbidden = @($zipEntries | Where-Object {
            $_.FullName -match '(^|/)(runtime|build|\.gradle|\.kotlin|\.venv|__pycache__|\.git)(/|$)' -or
            $_.FullName -match '(^|/)(local\.properties|\.env)$' -or
            $_.FullName -match '\.(apk|jks|keystore|log|pyc)$'
        })
        if ($forbidden.Count -gt 0) {
            throw "Forbidden local/runtime files found in source ZIP: $($forbidden[0].FullName)"
        }
        if ($zipEntries.Count -ne @($sourceManifest).Count) {
            throw "Source ZIP entry count mismatch: expected $(@($sourceManifest).Count), got $($zipEntries.Count)"
        }
    } finally {
        $zip.Dispose()
    }

    Rename-Item -LiteralPath $uploadPath -NewName $backupName

    [pscustomobject]@{
        destination = $finalPath
        version = $versionName
        source_files = @($sourceManifest).Count
        source_bytes = ($sourceFiles.File | Measure-Object Length -Sum).Sum
        package_files = @(Get-ChildItem -LiteralPath $finalPath -Recurse -File).Count
        source_zip_sha256 = (Get-FileHash -LiteralPath (Join-Path $finalPath "$backupName-source.zip") -Algorithm SHA256).Hash
    } | ConvertTo-Json -Depth 3
} finally {
    $resolvedTemp = [IO.Path]::GetFullPath($tempRoot)
    if ($resolvedTemp.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolvedTemp).StartsWith('ul-workbench-nas-', [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedTemp)) {
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}
