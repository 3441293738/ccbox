# 第三方软件与许可

仓库的 MIT 许可只覆盖本项目原创代码与文档，不重新许可第三方软件或商标。构建时会将已安装依赖的原始许可文件收集到发行包的 `third-party-licenses`，同时记录包版本与许可元数据；请保留这些文件。

| 组件 | 使用方式 / 许可来源 |
|---|---|
| Python / Tcl / Tk | 桌面包内的解释器与界面运行时；各自许可见随包文件与上游源码 |
| CustomTkinter / darkdetect / packaging | 图形界面及依赖，实际版本和许可来自构建环境的包元数据 |
| keyring / jaraco 等传递依赖 | Mac 钥匙串接口，原许可随包收集 |
| certifi | Mac 根证书包，保留其 MPL-2.0 等原始许可说明 |
| PyInstaller | 打包工具，GPL 与 bootloader 分发例外以其 COPYING 为准 |
| Docker / Debian / Node / Firefox / noVNC / Openbox 等 | 用户本地下载或构建的运行环境；按各自许可分发和使用，不以本项目 MIT 替代 |
| Microsoft Visual Studio Code | 运行时从官方来源下载的发行版，适用 Microsoft 产品许可与商标政策 |
| Claude Code / Claude 扩展 | 运行时由用户安装并登录，适用 Anthropic 条款，不随本项目开源其专有实现 |

CCBOX 桌面发行包不预置用户账号、Docker 镜像、VS Code 或 Claude 可执行文件。Dockerfile 中的依赖会在首次构建时安装；基础镜像和未固定的上游版本可能变化，构建并非位级可复现。

### 思路参考

[CC Desk Tunnel](https://github.com/sun168567/cc-desk-tunnel)（Apache-2.0）提供了模型与执行位置分离的设计参考。当前 CCBOX 本机开发使用自行实现的 Docker stdio、临时 Unix socket 与 MCP 桥，未复制该项目的 SSH 隧道代码。未来引入其实现时须同时保留对应版权、许可与 NOTICE。
