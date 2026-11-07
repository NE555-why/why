# 第三方组件与许可证声明

本文件列出 why-harness 发行物涉及的第三方组件、其许可证、是否随发行物分发，以及分发时必须保留的声明与履行的义务。

**总说明：本项目按 AGPL-3.0 发布，许可证正文见根目录 [LICENSE](LICENSE)；第三方组件的许可证正文见 `licenses\` 目录。**

- 项目自身许可证：GNU Affero General Public License v3.0（AGPL-3.0），全文见根目录 [LICENSE](LICENSE)。
- 核查依据：本地 `.venv` 中各包的 `dist-info`（`METADATA`、`LICENSE`/`COPYING` 正文）与项目代码；核查日期：2026-10-25。
- 本文件不是法律意见；发布前建议由法务复核。

## 1. 总览

| 组件 | 版本 | 许可证 | 随发行物分发 | 需要保留 / 提供的声明 |
|---|---|---|---|---|
| PyMuPDF（含 MuPDF） | 1.28.2 | AGPL-3.0 或 Artifex 商业授权（本项目按 AGPL-3.0 使用） | 是 | AGPL-3.0 全文、版权声明、对应源代码（含网络服务场景） |
| PaddleOCR | 3.7.0 | Apache-2.0 | 是 | Apache-2.0 副本、版权与归属声明、NOTICE（如有） |
| PaddlePaddle | 3.0.0 | Apache-2.0 | 是 | 同上 |
| PaddleX | 3.7.2 | Apache-2.0（已核对本地 LICENSE 正文） | 是 | 同上 |
| Pillow | 13.0.0 | MIT-CMU | 是 | 版权与许可声明（含捆绑组件许可） |
| NumPy | 2.3.5 | BSD-3-Clause（含捆绑组件许可） | 是 | 版权声明、条件列表、免责声明 |
| PP-DocLayout 版面模型权重（PP-DocLayout_plus-L、PP-DocLayout-M） | — | 本地未见许可声明，以官方发布页为准 | **否**（用户自行下载） | 本项目不转授权；用户须遵守上游许可 |

## 2. 逐项说明

### 2.1 PyMuPDF 1.28.2

- 权利方：Artifex Software, Inc.；其底层 MuPDF 亦版权归 Artifex Software, Inc.
- 许可证依据：
  - `.venv/Lib/site-packages/pymupdf-1.28.2.dist-info/COPYING` 原文：`Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License`
  - `METADATA` 的 `License` 字段同文，`Author` 为 Artifex。
- 使用方式：本项目按 **AGPL-3.0** 分支使用。代码实际导入 PyMuPDF（`why_harness/pipeline/s1_pages.py`、`why_harness/pipeline/s3_ocr.py`），它属于实际依赖。
- 许可证正文：根目录 `LICENSE`（AGPL-3.0 全文，同一份正文覆盖本项目与 PyMuPDF/MuPDF）。
- 随发行物分发：**是**。作为运行依赖，打包后的程序会包含 PyMuPDF 及其内置的 MuPDF。
- 需要保留 / 履行的义务：
  1. 向接收者提供 AGPL-3.0 完整正文（根目录 `LICENSE`）。
  2. 随二进制分发时，按 AGPL-3.0 第 6 条提供完整对应源代码；本项目源代码整体以 AGPL-3.0 发布。
  3. 保留版权声明与许可证声明，不得附加额外限制。
  4. 若通过网络向用户提供服务，按 AGPL-3.0 第 13 条向网络用户提供获取对应源代码的途径。
  5. 如不愿承担 AGPL-3.0 义务，须另行向 Artifex 取得商业授权：<https://artifex.com/licensing/>。

### 2.2 PaddleOCR 3.7.0

- 权利方：PaddlePaddle Authors（百度）。
- 许可证：Apache-2.0；依据 `paddleocr-3.7.0.dist-info/LICENSE`（11,376 字节，Apache License 2.0 正文）。
- 许可证正文：`licenses/Apache-2.0.txt`
- 随发行物分发：是（运行依赖，打包后随程序分发）。
- 需要保留 / 履行的义务：附 Apache-2.0 副本；保留版权、专利、商标与归属声明；若原件带 `NOTICE` 文件则转达其内容；修改过的文件须标注“已修改”；不得暗示 PaddleOCR 或百度为本项目背书。

### 2.3 PaddlePaddle 3.0.0

- 权利方：Copyright (c) 2016 PaddlePaddle Authors。
- 许可证：Apache-2.0；依据 `paddlepaddle-3.0.0.dist-info/LICENSE`（11,641 字节，正文开头为上述版权行，其后为 Apache License 2.0）。
- 许可证正文：`licenses/PaddlePaddle-Apache-2.0.txt`（11,641 字节，取自 `paddlepaddle-3.0.0.dist-info/LICENSE` 原文副本；前置 "Copyright (c) 2016 PaddlePaddle Authors" 版权行，其后为 Apache License 2.0 正文）
- 随发行物分发：是（运行依赖，打包后随程序分发）。
- 需要保留 / 履行的义务：同 Apache-2.0 各项（副本、版权与归属、NOTICE、修改标注、无背书）。
- 待确认：该 wheel 内含第三方组件，其完整声明尚未逐项核对（见第 4 节）。

### 2.4 PaddleX 3.7.2

- 权利方：PaddlePaddle Authors。
- 许可证：**Apache-2.0**。依据：`paddlex-3.7.2.dist-info/licenses/LICENSE`（11,325 字节），正文为 Apache License 2.0；`METADATA` 中 `License: Apache-2.0`、`License-File: LICENSE`。
- 许可证正文：`licenses/Apache-2.0.txt`（11,325 字节，即本包 `licenses/LICENSE` 原文副本）
- 随发行物分发：是（运行依赖，打包后随程序分发）。
- 需要保留 / 履行的义务：同 Apache-2.0 各项（副本、版权与归属、NOTICE、修改标注、无背书）。

### 2.5 Pillow 13.0.0

- 权利方：Copyright © 1997–2011 Secret Labs AB；Copyright © 1995–2011 Fredrik Lundh 及贡献者；Pillow Copyright © 2010 Jeffrey A. Clark 及贡献者。
- 许可证：MIT-CMU。依据：`METADATA` 中 `License-Expression: MIT-CMU`；`pillow-13.0.0.dist-info/licenses/LICENSE`（78,481 字节）正文为 PIL 的 MIT-CMU 式许可，并附捆绑组件的许可。
- 许可证正文：`licenses/Pillow-MIT-CMU.txt`（78,481 字节，即上述文件原文副本，含 Secret Labs AB / Fredrik Lundh 版权行与“MIT-CMU License”条款，以及 Brotli、FreeType、zlib、HarfBuzz 等捆绑组件许可）
- 随发行物分发：是（运行依赖，打包后随程序分发）。
- 需要保留 / 履行的义务：在随附文档或材料中复现上述版权声明与许可声明；`LICENSE` 文件还包含 Brotli、FreeType、zlib、HarfBuzz 等捆绑组件的许可条款，须随二进制分发一并提供。

### 2.6 NumPy 2.3.5

- 权利方：Copyright (c) 2005–2025, NumPy Developers。
- 许可证：BSD-3-Clause。依据：`numpy-2.3.5.dist-info/LICENSE.txt`（46,841 字节），开头为三条款 BSD 正文；该文件同时收录捆绑组件（zlib、MIT、Apache-2.0、GCC Runtime Library Exception 等）的许可。
- 许可证正文：`licenses/NumPy-BSD-3-Clause.txt`（46,841 字节，即上述 `LICENSE.txt` 原文副本，含三条款 BSD 全文与“不得背书”条款）
- 随发行物分发：是（运行依赖，打包后随程序分发）。
- 需要保留 / 履行的义务：保留版权声明、条件列表与免责声明（源码与二进制分发均适用）；不得使用 NumPy 或其贡献者的名称为衍生产品背书；完整 `LICENSE.txt` 随发行物提供。

### 2.7 PP-DocLayout 版面模型权重

- 涉及模型：`PP-DocLayout_plus-L`、`PP-DocLayout-M`（PaddleX 官方推理模型，Paddle 3.0.0 静态图）。
- 随发行物分发：**否**。代码只记录官方下载地址与 SHA-256 校验值，权重由用户自行下载到 `模型/plus-L`、`模型/M`（见 `why_harness/media/models.py`）。
  - 官方来源：<https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-DocLayout_plus-L_infer.tar>
  - 官方来源：<https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-DocLayout-M_infer.tar>
- 许可证正文：**未随仓库提供，待补**（本地权重包与模型目录内无 LICENSE，须以官方发布页为准）。
- 许可证：本地权重包与模型目录内**未找到** model card、README 或 LICENSE，因此**以官方发布页为准**。此前文档中“Apache-2.0、可商用、无需申请”的说法未能在本地核实，不得作为依据。
- 本项目立场：不转授权、不重新分发、不暗示官方背书；用户下载后须自行遵守上游许可（包括是否允许商用、署名与 NOTICE 要求）。
- 待确认：官方模型卡/发布页的许可证与商用限制；权重包内是否含第三方文件或 NOTICE。

## 3. 发行物应附带的许可证正文

| 文件 | 对应组件 | 状态 |
|---|---|---|
| `LICENSE` | AGPL-3.0（why-harness、PyMuPDF/MuPDF） | 已提供（官方正文 + 项目版权声明） |
| `THIRD-PARTY-NOTICES.md` | 本文件 | 已提供 |
| `licenses/Apache-2.0.txt` | PaddleOCR、PaddleX | 已提供（11,325 字节；取自 `paddlex-3.7.2.dist-info/licenses/LICENSE`，含 "Apache License" 与 "Version 2.0"） |
| `licenses/PaddlePaddle-Apache-2.0.txt` | PaddlePaddle | 已提供（11,641 字节；取自 `paddlepaddle-3.0.0.dist-info/LICENSE`，含 "Copyright (c) 2016 PaddlePaddle Authors" 与 Apache License 2.0 正文） |
| `licenses/Pillow-MIT-CMU.txt` | Pillow 及其捆绑组件 | 已提供（78,481 字节；取自 `pillow-13.0.0.dist-info/licenses/LICENSE`） |
| `licenses/NumPy-BSD-3-Clause.txt` | NumPy 及其捆绑组件 | 已提供（46,841 字节；取自 `numpy-2.3.5.dist-info/LICENSE.txt`） |

> 说明：AGPL-3.0 正文收录在根目录 `LICENSE`；Apache-2.0、Pillow MIT-CMU、NumPy BSD-3-Clause 的正文已于 2026-10-25 从本地安装包原文收进 `licenses\` 目录。PP-DocLayout 权重许可正文仍未随仓库提供，待补。

## 4. 待确认

1. PP-DocLayout 两档权重的官方许可证、商用限制、署名与 NOTICE 要求（本地无声明，须查官方发布页）。
2. PaddlePaddle / PaddleOCR / PaddleX / Pillow / NumPy 的 wheel 内第三方组件与 NOTICE 清单（本轮只核对了各自的顶层 LICENSE）。
3. ~~Apache-2.0、MIT-CMU、BSD-3-Clause 正文尚未收入仓库~~ 已办：2026-10-25 收入 `licenses/`（见第 3 节）；PP-DocLayout 权重许可正文仍待补。
4. 最终打包产物（完整版/精简版 exe）实际包含的二进制清单，应以成品扫描结果为准；现有发行说明称完整版内置版面模型，与“权重不进发行物”的政策是否一致，需在发布前核实。
5. 本文件不是法律意见。

## 5. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-25 | 将 Apache-2.0（PaddleX 原文）、Pillow MIT-CMU、NumPy BSD-3-Clause 的许可证正文从本地安装包复制进 `licenses/` 目录，并在各组件条目补“许可证正文”行；PaddlePaddle 的 LICENSE（含其版权前置行）另存为 `licenses/PaddlePaddle-Apache-2.0.txt`；PP-DocLayout 权重许可正文标注为待补。 |
| 2026-10-25 | 按当前实际依赖重写：PyMuPDF 1.28.2 明确为实际导入的 AGPL-3.0 依赖；核对 PaddleX 3.7.2 本地 LICENSE 正文为 Apache-2.0；模型权重改为“不随发行物分发、以官方发布页为准”。 |
