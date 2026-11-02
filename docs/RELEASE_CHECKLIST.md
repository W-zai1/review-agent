# 发布检查清单

这份清单只处理交付安全与可复现性，不替代 `checks/` 和 `tests/`。

## 1. 凭据

- 交付前运行 `python tools/secret_scan.py`，必须退出码为 0。
- 不要提交或分发 `config/llm.local.json`、`.env`、`*.pem`、`*.key`。
- 如果密钥曾经落盘、进过压缩包或被复制到其它目录，**先到服务商后台轮换**；
  删除本地文件只能止损，不能使已泄露的 Key 失效。
- 优先使用环境变量或系统凭据存储，不要把 Key 写进 JSON。

## 2. 提交完整性

- 新文件先 `git add`，再运行：

```bash
python tools/release_check.py --require-private-tests
```

- 该命令要求工作区没有未暂存/未跟踪文件，并从 Git 暂存区导出临时快照，
  在快照内检查关键文件、密钥、编译和 `checks/`；`tests/` 私有回归在工作区单独跑。
- 公开仓库不包含 `tests/` 时，不要强行加 `--require-private-tests`；
  在私有镜像或发布机上运行。

## 3. 出包

不要直接压缩整个工作目录。提交后使用：

```bash
git archive --format=zip --output=review-agent.zip HEAD
```

该方式只包含 Git 跟踪的文件，不会带上 `outputs/`、`logs/`、`state/`、
`targets/`、`cache/`、`config/llm.local.json` 或 `.git/`。

出包后再解压到空目录，至少运行一次：

```bash
python -m unittest discover -s checks -q
python review.py --list-agents
```
