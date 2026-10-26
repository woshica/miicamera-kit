# 相机录制工具包

独立私有仓库：[woshica/miicamera-kit](https://github.com/woshica/miicamera-kit)。Git 保存源码、配置、启动入口和说明；`runtime/`、`sdk/`、`installers/` 的完整依赖放在同一仓库的 [Releases](https://github.com/woshica/miicamera-kit/releases) 附件中。

**客户电脑请下载 Release 中的 `miicamera-kit-版本号.zip` 完整包**，并使用旁边的 SHA-256 文件核对。GitHub 自动生成的 `Source code (zip)` 和单独 `git clone` 只含源码，不能直接运行。解压后先运行 `test/校验软件包.cmd`。私有附件需要有仓库访问权限的账号下载；不要为分发附件擅自改成公开仓库。

日常从根目录的四个入口开始：

| 入口 | 用途 |
| --- | --- |
| [启动录制.cmd](启动录制.cmd) | 按当前录制参数开始录制，可显示实时预览与进度 |
| [打开官方录制软件.cmd](打开官方录制软件.cmd) | 打开 ImageView，检查构图、对焦与照明 |
| [客户电脑快速诊断.cmd](客户电脑快速诊断.cmd) | 到新电脑后检查环境、编码能力与相机条件 |
| [回看录像.cmd](回看录像.cmd) | 将视频拖到此入口，使用随包播放器回看 |

录制前编辑根目录 [录制参数.toml](录制参数.toml)。实际参数以当前文件和启动摘要为准；开始脚本录制前退出 ImageView，释放相机。

首次使用或迁移，请看 [客户现场先看](docs/客户现场先看.md)。参数含义与预览、进度设置见 [录制参数说明](docs/录制参数说明.md)；历史方案与完整操作见 [完整使用说明](docs/完整使用说明.md)。

辅助工具集中在 `test/`：

- [检查录制配置](test/检查录制配置.cmd)：离线检查当前配置。
- [试录当前配置一分钟](test/试录当前配置一分钟.cmd)：临时按60秒、20秒分段验证当前配置。
- [校验软件包](test/校验软件包.cmd)：检查固定文件完整性。
- [启动相机工具](test/启动相机工具.cmd)：打开完整工具菜单。

旧的推荐配置、H.265和官方参数CRF入口保留在 `test/legacy/`，供历史对照；它们读取各自JSON，使用前先看完整说明。

迁移时请复制整个 `miicamera-kit` 文件夹并保留内部层级。`test/` 中的入口也依赖包内文件；`sdk/` 和 `runtime/` 是必需运行依赖，不能删除或只复制根目录CMD。视频请保存在软件包外。

技术信息：[来源记录](docs/package/sources.json)、[固定文件清单](docs/package/manifest.json)。

## 仓库维护与发布

日常在本目录提交代码和配置。运行依赖被 `.gitignore` 排除，录像和诊断结果继续保存在软件包之外。本仓库不包含上级研究仓库的历史或实验录像；新版本请保留第三方程序的原有许可文件，不因本仓库私有而改变其授权。

开发时从对应 Release 完整包恢复 `runtime/`、`sdk/`、`installers/`，不要用不匹配的依赖覆盖当前版本。`.gitattributes` 保留文件原始字节，避免 Git 换行转换导致中文 CMD 或交付校验失效。

在软件包目录的 PowerShell 中，发布前执行：

```powershell
& ./runtime/python/python.exe -B -X utf8 ./scripts/build-manifest.py
& ./runtime/python/python.exe -B -X utf8 ./scripts/verify-kit.py
& ./runtime/python/python.exe -B -X utf8 ./scripts/package-release.py 'D:/CameraReleases/miicamera-kit-版本号.zip'
```

先确认校验通过，再打包；输出必须位于软件包外，已有文件不会覆盖。打包仅包含清单列出的文件、清单本身及可编辑配置，不包含 `.git`。更新后的清单与源码一起提交，将 ZIP 和 `.sha256` 上传到对应 Git 标签的 Release。配置文件随包保留实际值，正式录制前仍须核对保存路径、时长、曝光和增益。
