# 相机录制工具包

独立公开仓库：[woshica/miicamera-kit](https://github.com/woshica/miicamera-kit)。Git 保存源码、配置、启动入口和说明；`runtime/`、`sdk/`、`installers/` 的完整依赖放在同一仓库的 [Releases](https://github.com/woshica/miicamera-kit/releases) 附件中。

**客户电脑请下载 Release 中的 `miicamera-kit-版本号.zip` 完整包**，并使用旁边的 SHA-256 文件核对。GitHub 自动生成的 `Source code (zip)` 和单独 `git clone` 只含源码，不能直接运行。解压后先运行 `test/校验软件包.cmd`。仓库已于 2026-10-28 按负责人要求改为公开，浏览器下载无需登录；客户网络仍须能够访问 GitHub 及附件下载地址。

**本版：v2026.10.28。** 完整交付包 `miicamera-kit-v2026.10.28.zip` 合并新版工作台、运行依赖和累计升级入口。客户按[下载与安装指南](docs/客户下载与安装指南.md)选择首次使用或更新已有安装；无需逐个安装旧更新包。

日常双击根目录 [启动录制.cmd](启动录制.cmd)，在本地浏览器页面选择保存位置、时长、分段和方案，打开预览后调整曝光与增益，保存应用、试录，再正式录制。支持自定义多个命名方案，一次录制及全部分段使用同一方案；修改设置须先停止并等待验证结束。

工作台采用左侧大预览、右侧拍摄与录制设置、底部运行状态的布局。曝光和增益直接可见，另存方案收在「方案管理」；窄窗口自动上下排列。

- [操作员上手指南](docs/操作员上手指南.md)：日常五步流程、开始后的核对及故障处理。
- [客户下载与安装指南](docs/customer-download-guide.html)：GitHub 下载完整附件、新电脑使用、已有安装保留配置更新，支持离线阅读。
- [离线阅读与打印版](docs/operator-guide.html)：与上手指南相同内容，无外部资源。
- [维护人员配置说明](docs/维护人员配置说明.md)：配置分层、首次导入、方案覆盖与客户更新。
- [一键更新可行性](docs/automatic-update-feasibility.md)：早期私有访问评估和待实现的在线更新流程，含公开后的状态补充。

优先使用已有 config/operator/。尚无该目录时，先兼容导入客户本机保留的根目录「录制参数.toml」；若没有该旧文件，则从 config/operator-template.json 创建配置。deprecated/录制参数.toml仅供旧工作流追溯，不是工作台的初始化来源。已有配置不会因模板变化而覆盖。初始化后再修改旧 TOML 不会同步界面。新界面只读显示帧率、分辨率和编码器，日常不需编辑编码参数。预览约 2 fps、宽度不超过 960；保存视频仍按所选方案实际参数录制。关闭浏览器不会停止录制，停止须按页面「停止并保存」并等待收尾验证；不要关闭后台 CMD。

其他入口保留：

| 入口 | 用途 |
| --- | --- |
| 页面「打开官方录制软件」 | 自动停止工作台预览后打开 ImageView；返回工作台前手动退出 ImageView |
| [客户电脑快速诊断.cmd](客户电脑快速诊断.cmd) | 检查环境与工作台当前所选方案，保存诊断参数快照；仍需实际画面试录 |
| [回看录像.cmd](回看录像.cmd) | 将视频拖到入口，用随包播放器回看 |
| [校验软件包](test/校验软件包.cmd) | 检查固定文件完整性 |

旧菜单、配置、CMD 和历史试录说明已归档至 [deprecated/](deprecated/README.md)，原位置与新位置见 [迁移清单](deprecated/migration-map.json)。旧菜单仅使用归档配置。日常在工作台点「详细画面配置」调整当前方案的白平衡、伽马、对比度等；点「保存画面配置」保存后，预览会重新打开并应用读回检查，正式录制使用同一组画面参数。录制期间不能修改设置或打开官方软件。

首次迁移复制完整工具包并保留目录层级；runtime/ 和 sdk/ 是运行必需项。已有安装使用完整交付包中的「升级现有安装/安装更新.cmd」，目标选择原工具包，保留现场 config/operator/、客户原有配置和匹配的运行依赖；归档旧入口时保留其原内容，不用包内示例覆盖客户设置。视频保存在软件包外，更新后先短录、回看和验证。

技术信息：[来源记录](docs/package/sources.json)、[固定文件清单](docs/package/manifest.json)。

## 仓库维护与发布

日常在本目录提交代码和配置。运行依赖被 `.gitignore` 排除，录像和诊断结果继续保存在软件包之外。本仓库不包含上级研究仓库的历史或实验录像；新版本请保留第三方程序的原有许可文件，仓库公开不改变第三方程序的授权。实际现场方案、订阅或代理凭据不得提交到本仓库或 Release。

开发时从对应 Release 完整包恢复 `runtime/`、`sdk/`、`installers/`，不要用不匹配的依赖覆盖当前版本。`.gitattributes` 保留文件原始字节，避免 Git 换行转换导致中文 CMD 或交付校验失效。

在软件包目录的 PowerShell 中，发布前执行：

```powershell
& ./runtime/python/python.exe -B -X utf8 ./scripts/build-manifest.py
& ./runtime/python/python.exe -B -X utf8 ./scripts/verify-kit.py
& ./runtime/python/python.exe -B -X utf8 ./scripts/package-delivery.py 'D:/CameraReleases/miicamera-kit-v版本号.zip'
```

先确认校验通过，再打包；输出必须位于软件包外，已有文件不会覆盖。package-delivery.py 在完整包外附上累计升级入口和离线下载指南，校验 ZIP 及升级文件摘要。基础完整包仅包含清单列出的文件、清单本身及可编辑配置，不包含 `.git`。更新后的清单与源码一起提交，将 ZIP 和 `.sha256` 上传到对应 Git 标签的 Release。完整包包含固定的首次安装模板及归档配置，不包含本机 config/operator/ 或客户根目录 TOML。正式录制前仍须核对保存路径、时长、曝光和增益。
