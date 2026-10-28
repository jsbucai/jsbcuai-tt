# Mac Android 构建环境

本机为 Apple Silicon、8 GB 内存。采用命令行工具链，不需要 Android Studio 或模拟器即可运行本项目的 JVM/Robolectric 测试及生成 APK。

## 本机路径

- JDK 17：`~/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home`
- Android SDK：`~/Library/Android/sdk`
- 项目 Gradle 8.9：`runtime/toolchain/gradle-8.9`
- 本机环境变量：`runtime/toolchain/local-env.sh`（不纳入版本控制）
- 本项目 Gradle 缓存：`runtime/gradle-home`，内存上限 2 GB、两个工作线程，Kotlin 在同一进程编译。
- 原项目开发签名：`runtime/signing/debug.keystore`（NAS 原文件副本，权限 600，不纳入源码包）

## 使用

在项目根目录运行：

```bash
bash scripts/build.sh
```

默认依次执行 `testDebugUnitTest lintDebug assembleDebug`。也可以指定任务：

```bash
bash scripts/build.sh assembleDebug
```

需要使用 ADB 时，在 Bash 中加载本机环境：

```bash
source runtime/toolchain/local-env.sh
adb devices
```

APK 输出在 `app/build/outputs/apk/debug/app-debug.apk`。开发包带 `.dev` 应用 ID 后缀；构建脚本不会自动安装、覆盖手机应用或发布到公网。

## 下载和隔离

官方下载端点在本机网络下存在 TLS 连接失败。因此本机使用清华 Adoptium 镜像、腾讯 Android/Gradle 镜像及阿里云 Maven 镜像。TLS 校验保持开启。

Gradle 8.9 SHA-256 固定为官方 [release-checksums](https://gradle.org/release-checksums/) 公布值 `d725d707bfabd4dfdc958c624003b3c80accc03f7037b5122c4b1d0ef15cecab`。SDK 归档与镜像同步的 Android 仓库 XML 中的长度和校验值核对。此处 SDK 校验来源是镜像元数据，并非已独立从 Google 直连验证。

Maven 缓存只监听 `127.0.0.1:5192`；`build.sh` 在需要时启动、结束后关闭自己启动的进程。缓存下载支持 HTTP Range 断点续传并检查响应长度；本机 Gradle 下载读取超时设为 10 分钟，以容纳首次下载的大型测试运行库。镜像选择只通过本地环境变量生效，源码默认仍使用官方端点，不修改系统代理或路由配置。

另一台机器应自行准备 JDK 17、SDK 35 和原签名文件，并设置 `JAVA_HOME`、`ANDROID_HOME`；不要照搬本机绝对路径。必要时可用 `UL_GRADLE_DOWNLOAD_URL` 指定 Gradle 镜像，完整文件仍必须通过固定 SHA-256 校验。

## 验证结果

已验证 Java 17.0.20.1（原生 arm64）、Gradle 8.9、sdkmanager 19.0、Android SDK Platform 35 rev 2、Build-Tools 34.0.0 和 Platform-Tools 37.0.1 可运行。JDK 通过 `codesign --verify --deep --strict`，签名发布者为 Eclipse Foundation（JCDTMS22B4）。

ADB 服务、`adb devices -l` 及 `aapt2 version` 已运行通过；当前没有连接的 Android 真机。

2026-09-25 本机执行 `bash scripts/build.sh`：

- `BUILD SUCCESSFUL`，最后完整复跑耗时 2 分 27 秒。
- 9 个测试套件、118 个测试，失败/错误/跳过均为 0。
- Lint 0 error、4 warning：两处同步 SharedPreferences 持久化建议、一处权限 API 属性提示、一处 Android 12 备份配置提示。没有为了消除警告更改凭据/任务持久化行为。
- APK：`releases/UL随行-0.2.0-dev-mac.apk`，9,831,611 字节；应用 ID `com.ul.workbench.dev`、versionCode 5、versionName 0.2.0-dev、minSdk 26、targetSdk 35。
- APK SHA-256：`749eb9dd4cd87c99671de680a23064a5ae4df91c593e8493d1012021a854e007`，与 NAS 原 Windows 独立构建产物逐字节散列一致。
- `apksigner verify` 通过，v2 签名；证书 SHA-256 `1061d4910cea45072a51600e921bf58d3314f2607ba2395c161ccb7bb27c30ac`，与原 NAS 签名一致。
- 构建结束后，脚本自行启动的 5192 端口缓存桥已停止。
- 唯一测试源码修正：保存相同服务地址的用例改用当前 `serverUrl`，对应原电脑交付补丁；跨服务器清空工作区用例保持通过。

完整日志位于 `runtime/toolchain/build-macos.log`，JUnit 与 Lint 报告位于 `app/build/reports`。这些结果证明本机可以编译和测试此开发版本；公网连接、扫码及 A300E 蓝牙打印仍需真机验收。
