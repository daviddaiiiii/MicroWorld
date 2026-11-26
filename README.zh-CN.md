# MicroWorld

**面向 OpenFlexure 显微镜的原生 macOS 工作台。**

在一个窗口里找设备、看实时画面、控制载物台、扫描画布，再圈出要复拍或分析的 ROI。随 App 打包的 PC Worker 在 Mac 上保存 Canvas、配准和融合图像。接入 MCP 后，可以在 Codex 对话里发起扫描，再接着分析你在 App 里标出的区域。

[下载 Mac 与 Pi 安装包](https://github.com/daviddaiiiii/MicroWorld/releases/tag/v0.1.0) · [安装和连接](#安装和连接) · [配合 Codex 使用](#配合-codex-使用) · [English](README.md)

![MicroWorld 的四图块 Slide，显示已标记的矩形 ROI 和图层面板](docs/images/canvas-roi.jpg)

*截图来自真实的原生 App，使用模拟设备及合成标定图。*

## 安装和连接

**准备条件。** Pi 已运行提供 v2 API 的 [OpenFlexure 显微镜服务](https://openflexure.org/projects/microscope/)，并且你有 Pi 的 sudo 权限。Mac 需要 Apple Silicon 和 macOS 14 或更新版本。两端可通过局域网或 Tailscale 等私有网络互连。

### 1. 在 Pi 上安装扩展

从[同一个公开 Release](https://github.com/daviddaiiiii/MicroWorld/releases/tag/v0.1.0)下载 **MicroWorld-0.1.0-Pi.tar.gz** 及对应的 **.tar.gz.sha256** 文件。可以在 Pi 上直接下载，也可从 Mac 把两个文件复制到 Pi。等显微镜空闲后，在 Pi 的下载目录执行：

```bash
sha256sum -c MicroWorld-0.1.0-Pi.tar.gz.sha256
tar -xzf MicroWorld-0.1.0-Pi.tar.gz
cd MicroWorld-0.1.0-Pi
sudo ./install-openmhs-pi.sh
```

脚本会备份原有扩展，安装 Canvas 扫描和视觉提示接口，重启 OpenFlexure 并检查接口。Pi 原来的网页界面和采集数据会保留。安装成功后记下输出的备份路径。[Pi 安装、核验与回滚说明](pi_setup/README.md)

### 2. 在 Mac 上安装 App

1. 下载 [MicroWorld-0.1.0-macOS-arm64.zip](https://github.com/daviddaiiiii/MicroWorld/releases/download/v0.1.0/MicroWorld-0.1.0-macOS-arm64.zip)。解压到有写入权限的位置，例如 `~/Documents/MicroWorld`。把启动脚本和 `script/` 留在 `MicroWorld.app` 同一目录。
2. 打开 **MicroWorld.app**，按提示允许访问本地网络。这个预览版使用临时签名。如果 macOS 拦截首次启动，按 [Apple 的操作说明](https://support.apple.com/zh-cn/102445)批准打开。
3. 在 **Microscopes** 里选中 Pi 并点 **Connect**。找不到时用 **Add by Address**，手动输入主机名或 IP 及端口，例如 `microscope.local:5000`。连接到已装扩展的 Pi 后，App 会启动自带的 PC Worker。Pi 需要能在局域网或 VPN 内回连 Mac 的 TCP `18766` 端口，才能同步 Canvas。

### 3. 试扫一张画布

先在 **Navigate** 确认物镜与样品间有足够间隙，再试一次小步移动。在 **Observe → New Scan** 选择 **2 × 2** 并启动。采集时观察四个源图块依次出现，Mac 完成配准后打开 **Slide** 查看结果。已保存的画布可在 **Gallery** 里重开。

## App 界面

### Observe · 实时相机和采集

实时查看视野，运行 Fast AF、拍快照、标记 ROI，或发起固定网格和 Full 扫描。支持连续采集的 Pi 可沿扫描行选帧，并停下来补拍需要重拍的位置。

![Observe 中的实时视野、自动对焦、快照、ROI 和扫描入口](docs/images/observe.jpg)

### Navigate · 载物台和对焦

保持相机画面可见，用按钮点动 XYZ，或用方向键移动 XY、用 Page Up / Page Down 移动 Z。**Go to** 支持直接输入目标坐标。

![Navigate 同时显示相机画面、XYZ 位置、点动和绝对坐标输入](docs/images/navigate.jpg)

### Canvas · 图块、拼接图和 ROI

在 **Tiles** 和 **Slide** 间切换，查看和调整图层，放大观察拼接图，并用 **Mark ROI** 圈出目标。**Gallery** 用于重开历史扫描和快照。采集和相机–载物台标定设置位于 **Capture** 和 **Settings**。

## 配合 Codex 使用

1. 在 MicroWorld 连接已安装 OpenMHS 的显微镜。如已安装 Codex CLI，从菜单选择 **Open Codex**，也可在会读取项目 MCP 配置的 Codex 客户端中打开 MicroWorld 目录。
2. 在对话里说 **“扫一张 2 × 2，打开拼接好的 Slide”**。对应 MCP 工具为 `microworld_scan_2x2` 和 `microworld_scan_status`。点击返回的 `microworld://slide/…` 链接，或从 HTTP 查看器选择 **Open in MicroWorld**。
3. 在 Slide 里点 **Mark ROI**，圈出目标区域并等待确认，再回原对话让 Codex 查看或分析。MCP 工具 `microworld_get_slide_roi` 会返回 ROI 裁图和坐标信息。切换显微镜后需要开启新的 MCP 会话。

App 同目录下的 `Workspace/<device-id>/` 存放 Canvas 和采集结果，`ConversationHistory/<device-id>/` 存放对应设备的 MCP 活动和通过启动器打开的 Codex CLI 数据。连接状态和日志位于 `State/` 与 `Logs/`。

## 从源码构建

在 Apple Silicon Mac 上安装 Xcode Command Line Tools、Node.js 与 npm、Python 3，然后执行：

```bash
git clone https://github.com/daviddaiiiii/MicroWorld.git
cd MicroWorld
./script/build_app.sh
open MicroWorld.app
```

构建包含 SwiftUI App、Python 运行时、PC Worker、MCP 服务及浏览器 Slide 查看器。在此仓库运行 `./script/build_pi_release.sh 0.1.0` 可以重新打包 Pi 扩展。来源和归属见 [NOTICE.md](NOTICE.md)。
