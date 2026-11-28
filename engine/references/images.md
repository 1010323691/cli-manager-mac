# 通过 Codex CLI 生图

用于 CLI 工作流中用户明确要求生图的任务。命令中的 `$R` 为 `scripts/delegate.py` 的绝对路径。图像任务使用支持内置生图的受管 Codex 实例，和代码任务共用 [队列与账户额度](scheduling.md)。有可用槽位时，可在同一实例并行跑代码与生图。

## 单张与参考图

```bash
python3 "$R" task submit --kind image --id cover --instance codex1 \
  --prompt '生成一张 1536×1024 的封面，沿用参考图的配色，但采用新的构图' \
  --reference "$HOME/reference.png"
python3 "$R" queue work --parallel 4
python3 "$R" task status cover
```

- 文本可用 `--prompt` 或 `--brief`。尺寸、比例、质量和参考图用法写入提示词。可重复传 `--reference`。参考图支持 PNG、JPEG 和 WebP，单个文件最多 64 MiB。
- 可用 `--instance` 指定已授权实例，或省略选路参数使用已登记的 Codex 实例。派发时检查账户、模型、额度和内置生图能力。
- 生图使用独立工作目录。App Server 在同一连接中核对工作区后才发送提示词和本地参考图，并在接受产物前再次核对。核对失败时任务记为 `auth`。任务参数中禁止依赖、Git 快照文件和验证命令。`task resume` 不适用于图像任务，重试使用 `task retry`。当前实现要求 Codex 正常结束，并在该实例对应线程的生成目录中找到唯一符合路径、文件大小和格式签名检查的图像，再登记产物路径和哈希。

## 批量提交与补跑

```bash
python3 "$R" task submit --kind image --id covers --batch-dir "$HOME/image-prompts" --parallel 3
```

每个 `.txt` 对应一个任务。脚本按路径排序分配 `covers-1`、`covers-2` 等 ID，提交结果带 `source`，可用于将提示词文件对应到任务和导出名。批量选项中的参考图会传给各个任务，逐图差异写进各自的提示词。整个批次校验通过后一起入队。以同一 ID 重提时，完整成员清单、提示词路径和内容、参考图内容及任务选项须一致。更改输入或重新提交早期未保存批次清单的任务时，使用新的批次 ID。如果 `covers-2` 已失败，可单独补跑。

```bash
python3 "$R" task retry covers-2
```

## 审核、导出与总览

```bash
python3 "$R" task review approve cover --revision "$REPORT_REVISION"
python3 "$R" task export cover --revision "$APPROVED_REVISION" --output "$HOME/cover.png"
python3 "$R" image-sheet covers-1 covers-2 --output "$HOME/covers.png" --cols 2
```

- 先从报告的 `artifacts` 查看实际图像，再取报告的 `revision` 提交审核。导出使用返回的 `approved`。示例按实际产物为 PNG 编写。若产物是 JPEG 或 WebP，使用报告中的扩展名。导出中断后，用同一审核版本和路径重试。控制器核对文件身份和哈希，再恢复记账。目标被其他文件占用时拒绝覆盖。
- 总览图从已批准或已导出的任务读取核验过的产物，要求 Pillow。`--cols` 默认 2，目标路径须为空闲路径。
- 原生 CLI 的真实生图与配额验收见 [原生协议验证记录](v2-validation.md)。

源码对应 [批量与总览入口](../scripts/delegate.py)、[原生生图与格式探测](../scripts/cli_manager/backend.py)、[产物导出](../scripts/cli_manager/controller.py) 和 [总览图脚本](../backends/codex/image/contact_sheet.py)。
