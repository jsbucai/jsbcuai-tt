param([string[]]$Tasks = @('assembleDebug', 'testDebugUnitTest', 'lintDebug'))
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskJavaCandidates = @()
if ($env:JAVA_HOME) { $taskJavaCandidates += $env:JAVA_HOME }
if ($env:UL_ANDROID_TOOLS) { $taskJavaCandidates += (Join-Path $env:UL_ANDROID_TOOLS 'jdk17\jdk-17.0.19+10') }
$taskJavaCandidates += (Join-Path $taskRoot 'runtime\toolchain\jdk17')
$taskJavaCandidates += 'C:\Program Files\Android\Android Studio\jbr'
# Keep the original workstation path as a compatibility fallback.
$taskJavaCandidates += 'C:\Users\pc-2022\Documents\Codex\2026-05-15\android-build-tools\jdk17\jdk-17.0.19+10'
$taskJavaHome = $taskJavaCandidates | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $_ 'bin\java.exe')) } | Select-Object -First 1
if (-not $taskJavaHome) {
    $taskJava = Get-Command java -ErrorAction SilentlyContinue
    if ($taskJava) { $taskJavaHome = Split-Path -Parent (Split-Path -Parent $taskJava.Source) }
}
if (-not $taskJavaHome) {
    throw 'JDK 17 not found. Install Android Studio/JDK 17 or set JAVA_HOME before running this script.'
}
$env:JAVA_HOME = $taskJavaHome
$env:UL_MAVEN_BRIDGE = '1'
$taskBridge = $null
try { $taskBridge = (Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:5192/health' -TimeoutSec 2).Content } catch { }
if ($taskBridge -is [byte[]]) { $taskBridge = [Text.Encoding]::UTF8.GetString($taskBridge) }
if ($taskBridge -ne 'ul-build-maven-cache') {
    $taskBridgeScript = Join-Path $PSScriptRoot 'maven_bridge.py'
    Start-Process -FilePath 'python' -ArgumentList @(('"' + $taskBridgeScript + '"')) -WindowStyle Hidden
    Start-Sleep -Seconds 2
}
$taskGradle = Join-Path $taskRoot 'runtime\toolchain\gradle-8.9\bin\gradle.bat'
$taskSigning = Join-Path $taskRoot 'runtime\signing\debug.keystore'
if (-not (Test-Path -LiteralPath $taskSigning)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $taskSigning) | Out-Null
    $taskDefaultSigning = Join-Path $env:USERPROFILE '.android\debug.keystore'
    if (Test-Path -LiteralPath $taskDefaultSigning) { Copy-Item -LiteralPath $taskDefaultSigning -Destination $taskSigning }
    else {
        & (Join-Path $env:JAVA_HOME 'bin\keytool.exe') -genkeypair -keystore $taskSigning -alias androiddebugkey -storepass android -keypass android -keyalg RSA -keysize 2048 -validity 10000 -dname 'CN=UL Workbench Development,O=UL,C=CN'
        if ($LASTEXITCODE -ne 0) { throw 'Development signing initialization failed' }
    }
}
if (-not (Test-Path -LiteralPath $taskGradle)) {
    python (Join-Path $PSScriptRoot 'bootstrap_gradle.py')
    if ($LASTEXITCODE -ne 0) { throw 'Gradle bootstrap failed' }
}
Push-Location $taskRoot
try {
    & $taskGradle @Tasks --no-daemon --console=plain
    if ($LASTEXITCODE -ne 0) { throw 'Android build failed' }
} finally { Pop-Location }
