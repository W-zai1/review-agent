# 发布检查清单

这份清单只处理交付安全与可复现性，不替代 `checks/` 和 `tests/`。

## 0. 2026-11-10 凭据与历史核查结论

- 范围：`HEAD`（47 个提交、966 个唯一 blob）的全部历史 blob 与当前工作区。
- `config/llm.local.json` **从未进入 Git 历史**（`git log --all -- config/llm.local.json` 为空），
  它始终被 `.gitignore` 排除；当前文件只有 `base_url/timeout/model/max_tokens/enabled`，
  **没有 `api_key` 字段**，无需轮换。
- 历史 blob 用 `regex_check` 规则族扫描得到 193 处命中，逐条核对全部是
  `API_KEY=sk-xxxx` 占位符、`token = CancellationToken` 之类的标识符，以及测试夹具
  `password = "hunter2xyz"`；**没有真实密钥**。用带允许清单的 `tools/secret_scan.py`
  扫当前工作区为 0 命中，两者差异来自规则族的允许清单，不是漏报。
- 结论：历史凭据风险已核查，处置动作为“无密钥可轮换”；后续新密钥只走环境变量或
  本地 `config/llm.local.json`，不提交、不打包。
- 仍待决策：`Agent_逐步优化提示词.md` 目前是被 Git 跟踪的顶层文件，会随
  `git archive` 进入发布包；它是个人提示词，不是产品内容，建议后续从仓库移除
  （本轮不改跟踪状态，只把它显式列入白名单并在此记录）。

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

### 2.1 发布包边界（自动检查覆盖什么）

`tools/release_check.py` 在导出暂存区快照后，先做两件事，再做密钥扫描与 `checks/`：

1. 打印**快照清单**：文件数 / 总字节 / 顶层条目数，人工核对“打进去了什么”。
2. 执行**边界断言**（`snapshot_violations`），命中即失败：
   - 顶层条目必须在白名单 `RELEASE_TOP_LEVEL` 里（`outputs/`、`logs/`、`state/`、
     `ledger/`、`targets/`、`cache/`、`.git/`、`__pycache__/` 不在名单里，天然被挡）；
   - 任何位置的 `.env*`、`*.local.json`、`rules.state.json`、`favorites.json`、
     `*.pem`、`*.key`、`*.pyc` 都不许进包。

**不覆盖**：Git 历史（历史扫描是一次性核查，见第 0 节，不是 CI 步骤）；
`.gitignore` 的语义（忽略只影响是否被跟踪，本检查影响是否被打包，两者不能互替）；
发布后解压到空目录的运行验证（仍需按第 3 节手工做一次）。

**新增顶层目录时**：必须同时改 `RELEASE_TOP_LEVEL`，否则发布检查会失败——
这是有意为之的摩擦，避免“暂存区里有就打包”。

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
