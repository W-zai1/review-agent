# holdout · 真实文档验收集

本目录用于**验收**，不用于调参。

`tests/golden/` 是作者自己出的题：适合回归和调参，但在它上面 100% 召回只能证明
「能命中我们设计过的错」。真实召回必须用**没参与过设计、没看过的文档**测。
Holdout 就是这份考卷，一旦冻结，在验收完成前不许改，也不许拿它的结果回头改
prompt / 规则 / 阈值——否则它会在几轮之内退化成第二个 golden。

物理隔离：

| 目录 | 用途 | 能否拿来调参 |
|---|---|---|
| `tests/golden/` | 回归、定位、日常调参 | 可以 |
| `holdout/` | 版本验收、对外报真实召回 | **不可以** |

`tools/holdout_eval.py` 只读 `holdout/`，不引用 `tests/golden/` 的任何数据或基线。

## 一、目录约定

```text
holdout/
├── cases/
│   ├── .gitkeep
│   └── <name>.txt                  # 待审文本（UTF-8）
├── labels/
│   ├── .gitkeep
│   └── <name>.labels.json          # 与 cases/<name>.txt 同名
└── MANIFEST.json                   # 冻结清单，由 --save-manifest 生成
```

- `holdout/cases/*.txt`：每份文档一个文件，文件名用 `contract_001`、`report_007`
  这类**不含客户名**的编号。
- `holdout/labels/<name>.labels.json`：格式与 `tests/golden/labels/` 完全一致：
  `errors[].id / desc / match / exclude_dimensions`。
- **没有 label 文件的 case = 干净对照**，期望 0 条 issue。它的所有 issue 都算误报。
- 有 label 文件但 `errors: []` 也按干净对照处理。
- 不要放 `.docx`：本验收器按纯文本读，docx 请先经 `tools/docx_extract.py`
  转成 txt，并保留原文以便人工复核。
- 真实合同/报告可能含隐私，**不要提交到公开仓库**。`MANIFEST.json` 只含文件名、
  sha256、计数和冻结时间，不含正文，但仍可能暴露客户名——命名时先脱敏。

## 二、收 30–50 份真实文档

少于 30 份不是不能跑，而是 Wilson 95% 区间会很宽（10 份、50% 召回时大约
±30 个百分点），不足以支撑「验收通过」的结论。建议第一版按 **30–50 份**收，
并且满足：

- **多样性**：合同/协议、会议纪要/周报、对外公告/宣传稿、数据报告各占一部分；
  不要全是同一业务、同一模板。
- **长度**：既有 1–2 页短文档，也有 10 页以上的长文档（长文档测分块与覆盖）。
- **干净对照占 20%–40%**：没有标注的 case 用来测误报。全是阳性样本时，
  precision 会被系统性高估。
- **来源独立**：不要从 `tests/golden/cases`、`targets/`、`demo/` 里复制；
  不要用自己生成/改写过的文档冒充真实文档。
- **先脱敏，再入库**：替换真实公司名、人名、金额、联系方式；替换要一致，
  否则会制造原文本来没有的矛盾（也会让标注失真）。

建议在收文档时同步记录一张私下的来源表（不进仓库）：编号、类型、页数、
来源、脱敏人、收集日期。以后复核「这份是不是参与过调参」要靠它。

## 三、盲标：先标，后看模型输出（关键）

顺序不能反：

1. 通读原文，独立写下你认为的错误（条款缺失、数字矛盾、法条错误、事实错误等）；
2. **在没看过任何模型报告的前提下**，把每条错误编码成 label；
3. 标完、互相复核后，才允许运行评测器看结果。

可以直接手写 label 文件，也可以借用现有脚本生成空白模板再填：

```powershell
# 生成模板（不会跑审查）；脚本会把模板写在 case 旁边
python tools/eval_review.py --sample holdout/cases/contract_001.txt --init-labels 8

# 把模板移到 labels/ 并改名为同名 .labels.json
Move-Item holdout/cases/contract_001.labels.json holdout/labels/
```

单条 label 的结构：

```json
{
  "id": "B01",
  "desc": "第三条违约金比例 30%，超过合同总额 20% 的常见上限",
  "match": [["违约金"], ["30%|三成|上限|过高"]],
  "exclude_dimensions": ["style"]
}
```

`match` 是 **AND-of-OR**：每个子数组里任一正则命中即可，所有子数组都要命中；
匹配文本 = `quote + evidence + suggestion + rule_text`。写 label 时注意：

- 只用**判定依据**里的词，不要用你希望模型说的措辞；
- 同一错误的不同说法用 `|` 并列；
- `exclude_dimensions: ["style"]` 建议保留：风格规则会整句复读问题句，
  不排除会让召回虚高；
- 一条 label 对应一个**独立错误**；同一错误的两种说法不要拆成两条；
- 拿不准的标注先和第二个标注人讨论口径，再定稿，不要边跑边补。

标注完成后先自查：每条 `match` 至少能匹配一种合理的 `evidence` 表述；
如果一个 label 永远不可能命中，它只是在让分母变大。

## 四、冻结：生成不可悄悄改动的清单

标注定稿后运行：

```powershell
python tools/holdout_eval.py --save-manifest
```

它会为 `cases/*.txt` 和 `labels/*.labels.json` 逐个计算 sha256，写入
`holdout/MANIFEST.json`：

```json
{
  "schema": "review-agent/holdout-manifest/1",
  "frozen_at": "2026-10-29T22:00:00+08:00",
  "count": 36,
  "label_count": 31,
  "total_files": 67,
  "files": {
    "cases/contract_001.txt": "…64 位 sha256…",
    "labels/contract_001.labels.json": "…"
  }
}
```

冻结后：

- **不要改任何 case/label**，包括改一个标点、换行、编码；
- 把 `MANIFEST.json` 和文档一起备份，最好放在评测者不能随手改的地方
  （只读共享盘、密码管理器附件、或单独提交到受保护的仓库）；
- 如果 manifest 已存在，`--save-manifest` 会拒绝覆盖；确需重新冻结必须显式
  `--save-manifest --force`。**重冻结之后，旧成绩全部作废，不能和新成绩比较**。

`MANIFEST.json` 是防误改的，不是密码学签名：它能拦住「边测边改」和手滑，
但拦不住有意重算清单的人。真正的可信度靠流程：冻结时间早于看结果、
manifest 有独立备份、验收报告里记录 `frozen_at`。

## 五、跑评测

```powershell
# 只测、不设门槛（默认门槛 0）
python tools/holdout_eval.py

# 正式验收：低于门槛退出 1
python tools/holdout_eval.py --min-recall 0.80 --min-precision 0.90

# 指定结果路径（默认 outputs/holdout/holdout-<timestamp>.json）
python tools/holdout_eval.py --json outputs/holdout/release-0.9.1.json
```

- 纯规则审查（`use_llm=False`），不联网、不调用 LLM，可复现。
- 每次 case 都走 `tools/eval_review.py` 的 `run_once`，其临时
  OUTPUTS/LOGS/LEDGER 隔离保证不会在真实项目里生成正式报告或台账。
- 评测前后各校验一次 manifest；评测期间文件被改也会退出 2。
- 门槛参数取值 `0..1`：`0.8` 表示 80%。默认 `0` = 不拦。

退出码：

| 码 | 含义 |
|---|---|
| 0 | 评测完成，达到门槛 |
| 1 | 评测完成，但低于 `--min-recall` 或 `--min-precision` |
| 2 | 无法评测：cases 为空、manifest 缺失/损坏、文件增删或 hash 变化、标注非法 |

## 六、怎么解读结果

结果 JSON 里每例都有：

```json
{"case": "contract_001.txt", "kind": "positive", "labels_hit": 7,
 "labels_total": 9, "fp": 2, "issue_count": 11}
```

总体口径：

- **recall** = `labels_hit / labels_total`（标注级召回）；
- **precision** = `labels_hit / (labels_hit + fp)`。`fp` 是「没有被任何 label
  覆盖的 issue」：干净对照上的 issue 全部计入；阳性用例里标注外的 issue 也计入。
  同一 label 命中多条 issue 只算一次命中，因此这是**标注级**精确率，不是
  issue 级精确率。
- 两者都给 **Wilson 95% 置信区间**：分母为 0 时 `pct` 为 `null`，区间按
  `[0, 0]` 记录并在 `note` 里说明。不要把它读成「0%」，它表示「没有样本」。
- 门槛判断时，分母为 0 的比例按 0 处理：没有证据不算达标。因此只有干净对照、
  没有阳性标注时，`--min-recall 0.8` 会失败，这是有意的。
- 读区间时看**下限**：例如 60 条标注命中 48，recall=80.0%，95% CI
  `[68.2%, 88.2%]`；虽然点估计达到 80%，但下限只有 68.2%，不能仅凭这 30 份
  文档断言真实召回一定 ≥80%。要么扩大样本，要么在验收记录里如实写不确定性。

报告里的 `fp_samples` 是误报样例（`规则|片段`），用于人工裁定：
有些可能是真问题但漏标，不一定是模型噪声。**验收前要把这些样例过一遍**，
该补标的补标——但补标必须在看结果之前完成；冻结后看到误报再补标，就是调参。

怎么下结论：

| 观察 | 结论 |
|---|---|
| 召回和精确率都达到门槛，且干净对照 fp 接近 0 | 可以作为该版本的验收结论 |
| 召回达标，但置信区间下限低于业务底线 | 样本还不够，先扩到 50 份，不要先庆祝 |
| 召回高、precision 低 | 模型在「多报」，需要按 `fp_samples` 看是漏标还是噪声 |
| 召回低、precision 高 | 模型偏保守，先看每例 `labels_hit/labels_total` 找系统性缺口 |
| 某类文档全灭 | 这是能力缺口，不是阈值问题；修复后必须换新 holdout 再验 |
| 只有 5–10 份文档 | 只能当探索性结果，不能当验收结论 |

置信区间是给「这份 holdout 上的估计」用的。换一批文档，区间会变；
不要用同一批文档反复验收不同版本，否则区间不再覆盖真实泛化误差。

## 七、生命周期与红线

1. **一个版本周期内冻结一次**。验收前看结果、验收后改规则，都算调参。
2. **holdout 结果不回流到规则/prompt**。看到漏检先记录，下一版用**新文档**
   验证；不要改完再跑同一份 holdout 直到过。
3. **发布后轮换**。如果某个版本已经拿这份 holdout 做过多次决策，它的
   「没被设计过」属性已经消耗掉了；下一轮验收应新建 holdout。旧目录归档，
   `frozen_at` 和旧结果一起保留，方便审计。
4. **不要用 `--force` 重冻结来「修复」失败**。重冻结只能用于「文档本身错了，
   且错误与模型表现无关」；必须在验收记录里写明原因，并视为一次新的 holdout。
5. **不要把 holdout 文档放进 `tests/golden/`**，也不要把 golden 用例复制进来。

## 八、还缺什么

仓库里现在只有占位文件和本说明，没有真实文档——这是故意的。真实的 30–50 份
合同/报告需要由**没有参与规则和 prompt 设计的人**提供，并完成盲标。没有这批
文档之前，`--save-manifest` 不应该被运行，评测器会以退出码 2 拒绝出分。
