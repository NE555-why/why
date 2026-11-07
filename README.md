# pdf转（why_harness）

把扫描 PDF 转成单文件 `.why` 电子书的本地流水线。八个阶段：切页 → 版面 → 识别 → 表格 → 切章 → 摘要 → 打包 → 交付。

- 安装、依赖与命令行：[说明书/使用说明.md](说明书/使用说明.md)
- 配置项逐条解释：[说明书/设置说明.md](说明书/设置说明.md)

## 现在能做什么

- **图形界面**：左栏「转换 / 提示词 / 设置 / 关于」；支持把 PDF 或文件夹拖进窗口、批量队列、实时进度和日志。
- **设置窗口**：可存多套 OpenAI 兼容接口（获取可用模型、测试连接）、识别/摘要模型分开、思考强度、并发、自动补跑、超时、工作区与扫描目录；地址/key 有防呆纠正，缺项不让保存。
- **快速档 / 精确档**：版面模型 `M`（默认，约 1 秒/页，24 MB）或 `plus-L`（更准，慢 5–8 倍，130 MB）；界面运行栏可切，CLI 用 `--档位`。
- **补跑**：失败后只重试失败页和后续阶段，已完成/已成功的不重做；③ 识别内部也会自动补跑失败页（可设置轮数）。
- **守恒审计**：逐页对账页稿与章节正文；丢字会点名页码并给出缺失片段。
- **提示词可改**：`提示词` 页直接编辑，程序只做变量替换，不会覆盖用户文件。

## 目录

- `why_harness/`：程序包（Python 3.12）
- `requirements.txt`、`requirements-layout.txt`：基础依赖 / 版面依赖
- `说明书/`：使用说明、设置说明
- `第三方许可证文件/`、`LICENSE`：许可证与第三方声明
- `启动.bat`：双击启动图形界面

## 快速开始

1. 装 Python 3.12，在仓库根目录执行 `pip install -r requirements.txt`；要插图/表格再装 `requirements-layout.txt`（体积大，paddle 系）。
2. 版面模型放到 `模型/M/` 或 `模型/plus-L/`；缺模型时启动会先问，也可运行 `python -m why_harness 模型 download M`。
3. 在仓库根目录运行 `python -m why_harness gui` 打开窗口（或双击 `启动.bat`，先把里面的 `PY` 改成你自己的 `pythonw.exe` 路径）。
4. 打开「设置」填接口地址、key、识别/摘要模型并保存；回到「转换」拖入 PDF，点「开始」。

## 常用命令（在仓库根目录执行）

```powershell
python -m why_harness run "书.pdf"                 # 跑完整八阶段
python -m why_harness 补跑 "书.pdf"                # 只补失败页和后续阶段
python -m why_harness 阶段 5 书名                  # 只跑第 5 阶段（①③ 传 PDF 路径）
python -m why_harness 模型 list|download|verify|remove <档位>
python -m why_harness 提示词 识别|摘要
python -m why_harness 自检 --连通
```

- 工作区默认 `运行时/<书名>`；同一本书重跑复用同一目录。
- `run` 支持 `--工作区 / --配置 / --force / --并发 / --档位 / --不下载` 等参数。
- 退出码：0 成功 / 1 输入错误 / 2 阶段失败 / 3 依赖或模型缺失 / 4 有缺口 / 130 中断。

## 文档

- [使用说明](说明书/使用说明.md)
- [设置说明](说明书/设置说明.md)

> `.why` 2.0 格式规范、why2.0 说明书与接口约定尚未随源码整理发布。

## 许可证

本项目按 GNU Affero General Public License v3.0（AGPL-3.0）发布，全文见 [LICENSE](LICENSE)。

本项目导入并使用 AGPL-3.0 许可的 PyMuPDF（底层 MuPDF 版权归 Artifex Software, Inc.）。因此，分发本项目（包括打包后的程序）或通过网络向用户提供服务时，须遵守 AGPL-3.0 的相应义务，包括向接收者或网络用户提供对应源代码。若需在 AGPL-3.0 之外使用，商业授权请联系 Artifex：<https://artifex.com/licensing/>。

第三方组件、许可证与分发义务见 [第三方许可证文件/THIRD-PARTY-NOTICES.md](第三方许可证文件/THIRD-PARTY-NOTICES.md)。

### 模型权重

- 版面模型权重（PP-DocLayout_plus-L、PP-DocLayout-M）不在本项目发行物内。
- 用户从官方来源下载后，其使用受该模型原始许可证约束。
- 本项目不对模型权重做任何转授权。
