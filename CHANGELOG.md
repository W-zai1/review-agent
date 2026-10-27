# 变更记录（CHANGELOG）

本文件是**逐条登记**的改动账本：每一次新增 / 修改 / 删除都记一条，
不看 `git diff` 也能知道「什么时候、动了哪个文件、改了什么、为什么、怎么验的」。

- **倒序**：最新在最上面。
- **粒度**：一次改动一条，不按版本合并。
- **与 README 的分工**：README 讲「为什么这样设计」（可读的长文，按主题成章）；
  本文件讲「谁在什么时候动了哪一处」（可查的流水）。
- **与 `.workbuddy/memory/` 的分工**：memory 是给下一次开工的备忘（坑、口径、教训）；
  本文件是给人看的账本。

---

## 登记约定

每条记录固定四行，字段不许省：

```md
### 2026-10-27 · 修改 · 一句话标题

- **文件**：`path/a.py`（+ `tests/x.py`、`README.md` §n）
- **改了什么**：删了/加了/换了什么，写到能照着复现的程度
- **为什么**：触发它的是哪个现象、哪个用户反馈、哪个缺陷
- **验证**：跑了什么、看到什么数（测试项数 / 实测数值 / 截图核对）
```

类型只用四种：

| 类型 | 含义 |
|---|---|
| **新增** | 新文件、新接口、新功能、新规则 |
| **修改** | 改了已有行为（含重构、调参、文案） |
| **删除** | 删文件、删接口、删功能（**删了要写清为什么不留着共存**） |
| **修复** | 修缺陷（要写清根因，不只是"修好了"） |

三条硬要求：

1. **删改必须写"为什么"**——半年后看"删了 X"最想问的就是这句。
2. **验证要带数**——"测试通过"不算，"405 项 OK / 偏移 0px"才算。
3. **改了必须登记，登记了必须是真的**——账本失真比没有账本更糟。

---

# 2026-10-27

### 2026-10-27 · 修改 · 公开推送清理：.gitignore 追加排除测试/报告/运行态并推送 GitHub

- **文件**：`.gitignore`、`CHANGELOG.md`（+ git 索引 `git rm --cached`）
- **改了什么**：① `.gitignore` 追加「公开推送补充排除」段：`tests/`、`targets/`、`config/llm_history.json` / `learning.json` / `report_order.json`、`docs/修复报告-*.md`、`docs/本地模型实测报告.md`、`docs/盲测协议.md`、根目录 `回归测试·*.md` / `对抗测试·*.md` / `暴力测试·*.md` / `格式合规测试·*.md` / `调教报告·*.md`、`测试结果面板/`、`裁定结果样本/`；② `git rm -r --cached tests config/llm_history.json` 解除 14 项历史跟踪（文件保留本地）；③ 新提交 `cc05de4`（feat: docx 格式审查 + 公开推送清理）推送至 `https://github.com/W-zai1/review-agent.git`（main：524c01a..cc05de4）。
- **为什么**：用户要求把当前版本上传到自己的 GitHub 账号（W-zai1/review-agent），并明确「不上传测试文件和相关报告」；此前 `tests/` 13 项与 `config/llm_history.json` 已在版本库中，根目录/`docs/` 的报告类 md 会在 `git add -A` 时进入推送——需要在公开仓库中彻底排除。
- **验证**：`git status` 复核暂存清单——无 tests/、targets/、报告类 md、运行态数据，仅源码+规则+前端+核心文档；`git push origin main` 返回 `524c01a..cc05de4 main -> main`（exit 0）；`git ls-tree HEAD` 复核无排除项。

### 2026-10-27 · 新增 · 真实问题测试交付报告《格式合规测试·真实Word文档·20261027.md》

- **文件**：`格式合规测试·真实Word文档·20261027.md`（新增）、`CHANGELOG.md`
- **改了什么**：新增真实问题测试的交付文档：以用户上传的真实课程报告 docx（网安1班_第十二组_N6_智能招聘平台…）对照教师课程规范（正文宋体/Times New Roman、小四、首行缩进 2 字符、固定 22 磅行距、两级或三级标题、图表编号与引用一致、不出现他人真实账号/密钥/完整联系方式）逐项核验，产出 8 行核对表 + 7 条异议明细 + 验证证据 + 已知边界。
- **为什么**：用户要求"根据实际问题进行测试，比如给出一个 word 文档，里面的格式是否按照要求来进行"——需要一份可直接阅读的格式合规结论，而不只是引擎报告。
- **验证**：结论与 `outputs/R-20261027-005.json` 逐条对齐（7 条异议：format 4 / fact 3）；配套证据：docx 格式单测 14 项 OK、全量回归 437 项 OK、integrity ledger 172 条链完整。

### 2026-10-27 · 删除 · 误报版报告 R-20261027-004（含"重复题注 2-1"假阳性）

- **文件**：`outputs/R-20261027-004.md/.json`、`logs/R-20261027-004/` → `outputs/.trash/`（+ `ledger/chain.log` 墓碑）
- **改了什么**：经 API `DELETE /api/report?id=R-20261027-004`（body 带 confirm 二次确认）删除；文件移入 `outputs/.trash/`，ledger 对应行原位打 `op=delete` 墓碑（seq/report_hash/prev_hash 不动），收藏表摘除该 id。
- **为什么**：R-004 是题注判定修复前的产物，把正文引用句"表 2-1 是本情境下…"误判为题注、误报"重复题注 2-1"（major）；留着会让浏览历史的人看到一份含假阳性的正式报告。
- **验证**：`outputs/` 已无 R-004 文件（仅 R-005）；`outputs/.trash/` 含 R-20261027-004.json/.md/_logs；`tools/integrity.py verify` 通过（ledger 172 条链完整，删除痕迹在链上）。

### 2026-10-27 · 修复 · 真实 docx 审查暴露的 5 处缺陷（None 排序 / 题注误判 / 标题判定 / 视觉字号 0 / declared_rules 口径）

- **文件**：`tools/docx_format_checks.py`、`review.py`、`tests/test_docx_format.py`
- **改了什么**：① `check_spacing` 对 `spacing_line=None`（继承默认）段落排序崩溃——排序键改为 `(kv[0][0] or 0, kv[0][1] or "")`；② 题注判定收紧：编号后剩余须为短行（≤60 字）、无句号分号、不以 是/为/指/表示/作/说明/用于/在/与/及/和/，/, 开头（真实题注用全角空格「表 1-1　标题」，正文引用"表 2-1 是本情境下…"不再误判）；③ 标题判定扩展：pStyle Heading/outlineLvl 直接算标题（原先漏判导致合规文档误报 FORMAT-005）；黑体短文本阈值 60→40（避免吞掉 40+ 字正文强调句）；④ FORMAT-005 视觉字号集合剔除无字号段（sz_set 为空 → 0 的脏值）；⑤ `coverage.declared_rules` 由"全库规则数"改为"当前审查维度集合内的规则数"——format 维只对 docx 激活，txt 审查不再误报"31 条中仅启用 24 条"。
- **为什么**：用户真实报告审查（首轮 R-20261027-004）暴露：继承行距段排序崩溃（TypeError）、"表 2-1 是…"正文引用被当题注（假阳性 major）、合规合成文档因无样式标题误报、全量回归 test_disabled_rule_is_excluded 因 format 规则加入打破"active = declared − disabled"不变量。
- **验证**：`tests/test_docx_format.py` 14 项 OK（含新增 pstyle 标题、长黑体正文、表格单元格豁免、题注误判回归用例）；`RuleStateTests + test_scenario + test_docx_format` 31 项 OK；全量回归 437 项 OK / 152.367s；重审真实 docx 产出 R-20261027-005（7 条异议，无假阳性）。

### 2026-10-27 · 新增 · docx 格式审查能力（结构提取 + FORMAT-001~006 版式规则 + format 维度集成）

- **文件**：`tools/docx_extract.py`（新增）、`tools/docx_format_checks.py`（新增）、`rules/format.yaml`（新增）、`tests/test_docx_format.py`（新增 14 项）、`review.py`、`agents/doc-review/manifest.json`（v1.3.0）、`tools/rule_exec.py`、`web/app.js`、`web/style.css`、`config/rules_version.txt`（v1-2026-10-27-a1）
- **改了什么**：① `docx_extract.py` 用 stdlib zipfile+ElementTree 按段落提取文本与版式属性（字体 eastAsia/ascii、半磅字号 sz、firstLineChars/firstLine、行距 line/lineRule、pStyle/outlineLvl、表格内外标记），并解析 theme1.xml minorFont(Hans) 与 styles docDefaults 作回退基准（第一版 `_walk` 只遍历 body 一层致段落全空，改为对一切容器元素递归）；② `docx_format_checks.py` 实现 FORMAT-001~006 确定性检查，正文段口径=非表格/非题注/非标题/文本≥10 字；③ `review.py` 读 `.docx/.docm` 改走 docx_extract、`detect_type_detail` 识别 docx、`DIM_FOR_TYPE["docx"]` 加 format 维度、`builtin:format` 处理器（结构解析失败只跳过格式维并记账）；④ manifest v1.3.0 新增 format 维度与 `.docx/.docm` 路由；⑤ 前端 web/app.js + style.css 加 format 维（cyan）；⑥ rules_version 升至 a1（改 rules 必须同步升版本，否则历史报告转 STALE 红）。
- **为什么**：用户要求用真实 Word 文档做格式合规测试——此前系统只把目标当纯文本/二进制读，`.docx` 进不了审查；规则库只有文字风格规则（STYLE-*），没有版式规则。
- **验证**：`tests/test_docx_format.py` 14 项全过（合成合规/越规 docx，含 theme 回退、继承行距、题注误判、表格豁免、Heading 样式、run_one 集成断言 docx 类型+format 维+FORMAT 确定性）；真实报告首轮审查 R-20261027-004（8 条，其中 1 条题注假阳性，见下条修复记录）；规则执行登记 FORMAT-001~006 为 deterministic。

### 2026-10-27 · 新增 · 第十二轮迭代回归测试报告（423 项全过 + 完整性 + API 冒烟）

- **文件**：`回归测试·20261027·第十二轮迭代.md`（新增）、`CHANGELOG.md`
- **改了什么**：新增测试报告，登记对当前迭代系统（scenario / CHANGELOG 守门 / 双向定位 / 落点居中 / NO_CONFLICT 修复 / 界面改版）的三层验证结果：全量 unittest **423 项 OK / 0 失败 / 145.352s**（test_api_contract 147、test_review 88、test_purpose 53、test_learning 36、test_blindspots_v6 35、test_match_matrix 33、test_scenario 16、test_changelog_guard 15）；`tools/integrity.py verify` 退出 0（ledger 170 条链完整，最新报告自洽，旧报告按设计 STALE）；`tools/changelog_guard.py` 退出 0；临时实例 `server.py --port 8901` API 冒烟全部 200（含新端点 `/api/scenarios`，`POST /api/llm/test` ok=true 911ms）。
- **为什么**：用户要求"根据迭代的系统进行测试"——第十二轮改动涉及审查尺度（关规则）、工程门禁（守门钩子）与前端交互（双向滚动/居中/改版），需要系统性回归确认无回归与断链。
- **验证**：回归日志 `tests/run_full_regression_20261027.log`（`Ran 423 tests in 145.352s` / OK，0 FAIL/ERROR）；integrity 与 guard 退出码均 0；冒烟探测结果逐条记录于报告；全程只读未污染 outputs/ledger。

### 2026-10-27 · 新增 · CHANGELOG 守门脚本 + pre-commit 钩子（把"每次增删改要登记"从约定变成拦截）

- **文件**：`tools/changelog_guard.py`（新增）、`.githooks/pre-commit`（新增）、`tests/test_changelog_guard.py`（新增 15 项）、`README.md`（§2 目录结构 + §32）、`.workbuddy/memory/MEMORY.md`
- **改了什么**：
  1. 新增 `tools/changelog_guard.py`：读 `git status --porcelain` / `git diff --cached`，若改动里有"值得登记"的文件（源码/规则/文档/配置/`.gitignore` 等）而 `CHANGELOG.md` 不在改动集合里 → 打印待登记清单并以退出码 **1** 拦截；取不到 git 时退出码 2 宽容放行（避免在非 git 环境里变成噪音）。忽略产物与缓存：`outputs/ logs/ ledger/ state/ .workbuddy/` 与 `__pycache__/`、`config/{favorites,report_order,llm.local,llm_history,learning}.json`、`_probe*/_dom*/_shot*`、`*.pyc/.log/.tmp`。
  2. 新增 `.githooks/pre-commit`：调用守门脚本的 `--staged` 模式；用 `core.hooksPath` 挂载，钩子随仓库走（`.git/hooks/` 不进版本控制）。
  3. 新增 15 项测试：纯函数口径（该登记的 12 个路径、不该登记的 20 个路径、rename 解析、`decide()` 三种结局）+ 4 项在**真临时 git 仓库**里跑子进程，确认真会拦（退出码 1）、登记后放行（0）、只动产物放行（0）、`--staged` 只看暂存区。
- **为什么**：上一轮已把"每次增删改必须写进 `CHANGELOG.md`"写进 `MEMORY.md`，但**约定靠记性执行，第三次改动就会被忘掉**。用户这轮再次强调"后续所有增删改都需要写入 md"，说明只靠自我提醒不够。守门脚本把约定变成退出码：漏登记时提交直接被拦，看到待登记清单，不存在"忘了"。
- **验证**：
  - `py -3 -m unittest tests.test_changelog_guard` → **15 项 OK**；
  - 真实仓库实测拦截：`git add web/app.js` 后跑钩子 → 退出码 **1**，输出"有代码/文档改动，但 CHANGELOG.md 没有对应条目"（已 `git reset` 撤销暂存）；
  - 空暂存区 → 退出码 **0**（只检查真正要提交的内容）；
  - 修掉一个真问题：`git status --porcelain` 默认把中文文件名转成 `\346\216\245` 八进制，加 `-c core.quotepath=false` 后才显示为 `docs/API接口对照表.md`，否则 `IGNORE_EXACT` 精确匹配会失效。

### 2026-10-27 · 新增 · 建立本变更记录（CHANGELOG.md）+ 格式契约测试

- **文件**：`CHANGELOG.md`（新增）、`tests/test_api_contract.py`（+`ChangelogContractTests`）、`README.md`（顶部指引 + §2 目录结构）、`.workbuddy/memory/MEMORY.md`
- **改了什么**：新建本文件并回填 2026-10-26 起的逐条记录（更早版本只留索引表）；新增 3 项契约测试锁格式：必须存在且有「登记约定」、每条必须有 `文件/改了什么/为什么/验证` 四字段、条目必须倒序。
- **为什么**：用户要求"每次增删改的内容写入到 md 文件，便于读取"——此前改动散在 README 主题章节（讲设计）、`.workbuddy/memory`（讲坑）和 `docs/修复报告-*`（讲某一轮），没有一个"按时间可查的流水"。
- **验证**：`ChangelogContractTests` 3 项 OK；全量回归 408 项 OK。

### 2026-10-27 · 修复 · 报告里出现「此处表述一致」的 issue

- **文件**：`tools/llm_review.py`（prompt 第 8 条 + `NO_CONFLICT_RE`/`CONFLICT_DENY_RE`/`_tail_sentence` + `validate()`）、`tests/test_api_contract.py`、`tests/test_review.py`、`README.md` §31
- **改了什么**：
  - prompt 补一句：比完两端结论是「表述一致 / 不构成矛盾 / 只是依据不足」的**不要输出**；
  - `validate()` 增加「自证无矛盾」判定：`NO_CONFLICT_RE` 命中 evidence **末句**、且 `CONFLICT_DENY_RE` 不命中 → 进 `retracted`（报告「已排除」区），不静默丢；
  - 反义护栏：`不一致|不相符|前后矛盾|与…矛盾` 一命中就保留原条目（反义表只加不删）。
- **为什么**：`R-20261027-001` 里一条 `LOGIC-002 / minor`，evidence 最后写着"此处两处表述一致…属时间线依据不足而非直接矛盾"——一条写着"没问题"的问题，用户直接质问"这个逻辑存在的必要在哪"。规则要留（`arith_check:weekday/duration` 是硬判据），错在模型把"核完能对上"当成命中。
- **验证**：拿该报告真实 evidence 回放 → 0 issue / 进已排除；4 个反例（`不一致`、`一致+但矛盾`、`前后矛盾`、`未标注入学`) 全部保留；全量 **405 项 OK**。

### 2026-10-27 · 修改 · 跳转落点从「贴模块顶部」改为「屏幕正中间」

- **文件**：`web/app.js`（+`tests/test_api_contract.py`、`README.md` §30）
- **改了什么**：
  - 新增 `viewportBounds(host)` / `centerScrollTop(rect, host)`：目标中线对齐**可视区中线**，`rect.height ≥ viewH*0.7` 的长片段才退化为顶部对齐；
  - `revealInViewport()` / `ensureMarkInViewport()` 判定从「是否被裁切」改为「**中心偏离中线**」（舒适带视口 12%~15%）；
  - `jumpToIssue()`：`revealInViewport($("#annotatedBlock"))` → `revealInViewport(mark, { skip: box })`；
  - 新增 `settleInBoxToViewportCenter()`：落位稳定**之后**把「标注中心 vs 屏幕中线」残余差补到框内滚动，两条护栏（中线必须落在框内、不得越过可滚最大值）。
- **为什么**：点「定位 →」后句子贴在卡片最上沿，长句首行还被卡片标题压掉；`#annotatedText` 在卡片里本身不居中（框中线比屏幕中线高 50px+），"居中在框内"≠"居中在屏幕上"。
- **验证**：Edge headless 探针，`R-20261026-011` 6 条问题 × 2 种宽度：1440 全 −13~−14px、1000 全 +28px（刻意对齐导航条以下正文区中线），目标全部在框内；修复前是 300px 量级偏上。全量 403 项 OK。

### 2026-10-27 · 修改 · 裁定结果界面改版（对齐「简洁版」样板）

- **文件**：`web/index.html`、`web/app.js`、`web/style.css`
- **改了什么**：中栏重构为「状态条 → 审查目的 → 审查裁定（三卡）→ 要求对照矩阵 → 原文证据 → 原文标注 → 问题汇总 → 问题清单 → 边界/已排除 → 技术参数」四主模块 + 四明细；`renderReport`/`renderPurpose` 重写；证据默认折叠，点裁定卡的 E 编号 chip 自动展开并高亮；`.result-card` 局部 `--rs-*` 变量 + `.rs-sec` 统一外壳，窄屏表格 `overflow-x:auto` 收在模块内。原有 id（resultView/verdictBanner/purposeBlock/issueList…）全部保留。
- **为什么**：需求是"每次审查完成后直接出现样板那样的简洁裁定界面，并按模块美化"。
- **验证**：`node --check` OK；契约测试 140 项全过；headless Edge 截图（桌面/长图/420px 窄屏）逐块核验无溢出错位，截图存 `裁定结果样本/_shots/`。

### 2026-10-27 · 修复 · 宽屏双向定位断链（点原文标注跳不到汇总表）

- **文件**：`web/app.js`（`scrollHost` / `scrollHostTo` / `revealInViewport`）、`tests/test_api_contract.py`、`README.md` §29
- **改了什么**：新增 `scrollHost(el, skipEl)` 找最近**真的能纵向滚**的祖先（`overflowY` ∈ auto/scroll/overlay **且** `scrollHeight > clientHeight + 1`），`scrollHostTo()` 滚它并在 200ms 后校验落点（差 >2px 直接 `auto` 写死）；`ensureMarkInViewport` 传 `skip=#annotatedText`。
- **为什么**：宽屏 ≥1101px 下 `.card` 是 `max-height + overflow-y:auto` 的独立滚动容器，整页 `scrollHeight === innerHeight`，`window.scrollTo()` 一点都滚不动——行确实高亮了，页面纹丝不动。
- **验证**：探针实测 cardTop 0→1961、rowTop 93（卡内）；反向仍正常（1366→890，markInBox true）。新增 `test_reverse_jump_scrolls_inner_container`。

### 2026-10-27 · 新增 · 审查情境（scenario）：不同用途关不同规则

- **文件**：`config/scenarios.yaml`（新增）、`tools/scenario.py`（新增）、`review.py`、`server.py`、`web/index.html`、`web/app.js`、`tests/test_scenario.py`（新增）、`docs/API接口对照表.md`、`README.md` §28
- **改了什么**：情境配置版本化 + 载入/校验/应用；`resume-screening` 关 `FACT-001`（具体标识可复现）、`FACT-002`（引用可定位），其余维度不动；过滤点放在 `merge_agent_rules()` 之后、`set_rule_index()` **之前**；报告新增 `scenario{id,name,version,disabled_rules,disabled_detail}` 且 limitations 首位写"关了哪几条 / 未报出 ≠ 已核实"；新增 `GET /api/scenarios`、`POST /api/review/start {scenario}`、CLI `--scenario` / `--list-scenarios`、左栏下拉 + 状态条蓝色 chip；未知情境 / 全关光 → 报错不起任务。
- **为什么**：用户指出简历初筛场景下"是否真的考过四六级"这类事实问题无足轻重——HR 不会一份份去核。全场景共用一套规则等于逼人读噪声。
- **验证**：同文件同目的对比：通用 6 条 warn（含 CET-4 / 项目地址）vs 初筛 3 条「未发现—不等于通过」，资质类全消失，参与规则 25→23。新增 16 项情境测试，全量 402 项 OK。

---

# 2026-10-26

### 2026-10-26 · 修改 · 历史列表改「纯时间倒序」+ 逐条收藏 + 标题 ⭐ 筛选（v0.6.3）

- **文件**：`server.py`、`web/index.html`、`web/app.js`、`web/style.css`、`config/favorites.json`（新增）、`.gitignore`、`tests/test_api_contract.py`、`README.md` §17、`docs/API接口对照表.md`
- **改了什么**：排序改 `sort(key=(timestamp, review_id), reverse=True)`；**拖拽排序整套删除**（`draggable` / `⋮⋮` / `moveReportInList` / `persistReportOrder` / `POST /api/reports/order` / `_save_report_order` / `_report_order`），`config/report_order.json` 文件保留但不读；新增 `POST /api/reports/favorite`；每行 `☆/★`，标题 `★ 收藏（N）` ↔ `← 全部（总数）`，纯前端筛选 `favoritesOnly`。
- **为什么**：只要时间先后（最新在最上）+ 可单独收藏。拖拽与时间倒序不可能同时成立，两套排序必然打架，所以删而不是共存；`report_order.json` 保留是因为删文件等于丢用户数据。
- **验证**：`/api/reports` 顶部 `011 → 010 → 007 → 047…`；截图核对收藏态标题「收藏（2）」、取消后行消失计数变 1；`favorite` 必须真布尔（拒绝 `"true"`/`1`）、幂等、文件损坏当空、删除报告时摘除。全量 383 项 OK。

### 2026-10-26 · 修改 · 问题汇总摘要改为「默认全文直出」

- **文件**：`web/app.js`、`web/style.css`、`tests/test_api_contract.py`、`README.md` §23.4
- **改了什么**：摘要单元格直接 `esc(brief)` 全文渲染；删净 `SUM_BRIEF_CHARS` / `sumBriefOpen` / `toggleSumBrief` / `.sum-more` / `.clamped` 及事件里的两个分支；CSS 只剩 `.sum-brief-cell{min-width:240px}` + `.sum-brief{line-height:1.65}`。
- **为什么**：用户要求"不要任何按钮，一眼看完"。演进三版（`slice(0,110)` 硬截断 → 折 3 行 + 展开按钮 → 全文直出），每版都是上一版的失败：截断丢结论；折叠栏窄时 3 行只有二三十字，还是要点。**"展示不全"优先级最高的解法通常不是加交互而是别裁。**
- **验证**：`test_summary_brief_is_always_fully_visible` 断言禁止残留上述标识、必须存在全文渲染；Edge headless 截图 7 条全部读到句末、无任何按钮。全量 383 项 OK。

### 2026-10-26 · 修复 · 「历史报告不更新」= 排序把新报告压到末尾 + 页面不刷新

- **文件**：`server.py`、`web/app.js`
- **改了什么**：`_reports()` 排序去掉 `rank.get(rid, len(rank))`（未列出的报告被当成最大 rank → 全部沉底），改为时间倒序；前端新增 `refreshReports({force})` + 标题「⟳ 刷新」+ `focus`/`visibilitychange` 自动刷新（8s 节流，服务端每次要读全部报告 JSON，不能轮询）。
- **为什么**：跑完审查，右栏顶部还是旧报告。两层原因：排序把新报告压到 140 条列表末尾；前端只在启动/完成/删除时 `loadReports()`，CLI 或另一个窗口跑完本页不知道。另有一条隐性问题：8000 端口跑的是旧进程。
- **验证**：修后顶部 `011 → 010 → 007 → 047`；新增 `test_new_report_not_in_order_sorts_first` + `test_history_list_can_be_refreshed`。注意改 `server.py` 必须重启控制台，`app.js`/`style.css` 需 Ctrl+F5。

### 2026-10-26 · 修复 · 问题汇总摘要被截断（现象修复，后被"全文直出"取代）

- **文件**：`web/app.js`、`web/style.css`
- **改了什么**：先做「全文进 DOM + CSS 折 3 行 + 展开按钮」。
- **为什么**：截图里三块文字都断在句子中间且**连省略号都没有**——根因是 `renderIssueSummary()` 的 `slice(0, 110)`，不是 CSS clamp。
- **验证**：用 R-20261026-011 复算 `slice(0,110)`，与截图三段逐字对齐（ISS-0005/0006/0004）。此方案次日被「全文直出」取代（见上）。

### 2026-10-26 · 修复 · 第七轮：审错对象 / OCR 标准失真 / 隐私规则脱离语境

- **文件**：`server.py`、`review.py`、`web/app.js`、`tools/regex_check.py`、`tools/ocr.py`、`tests/*`、`docs/修复报告-第七轮.md`（新增）
- **改了什么**：
  - `#targetSelect` 补 `change` 监听（原来是死控件：界面显示 A，`state.selected` 还是 B，后端审的是 B）；
  - `/api/targets` 加 `role`/`reviewable`，`*.labels.json` 标注文件标「标注·不可审」并默认不选，`_resolve_target` 硬拦；
  - 目的文件是图片时 OCR 风险进 `limitations`（新增 `_assemble_purpose` 第三返回值 `quality[]`），不进正文、不做成句尾括注；
  - `SAFE-003` 加语境豁免：同句含 `联系方式|电话：|手机：|邮箱：` 不报（**只豁免手机号**），模型命中一并让位并进 `excluded`。
- **为什么**：用户贴截图问"目的文件读取有乱码""为何有不相干的规则"，核出四个缺陷。`labels.json` 是 ground truth，逐条写着"某处有错"，被当正文审 → 9 条异议全来自它。
- **验证**：R-007(fail, 审标注) → R-010(warn, 审简历)；limitations[0]=OCR；目标 3/3 命中。全量 380 项 OK。

### 2026-10-26 · 修复 · 要求对照矩阵收口（F4 + 块标记）

- **文件**：`tools/match_matrix.py`、`tests/test_match_matrix.py`、`.workbuddy/memory/2026-10-26.md`
- **改了什么**：沿用 v1 53 条 / v2 26 条压测用例逐条修；空名归一「未命名」、畸形标记处理。
- **为什么**：矩阵暴力测试暴露"块标记格式"是最脆的一环（一行两标记 / 手写段伪标记 / 正文末尾恰为固定措辞）。
- **验证**：v1 53/0/5；v2 **26/0/3**；`test_match_matrix` 33 项；全量 367 OK；integrity 全绿。剩 3 条 OBS 需改块标记格式才能根治。

### 2026-10-26 · 新增 · 要求对照矩阵（双文档对比）

- **文件**：`tools/match_matrix.py`（新增）、`config/match_matrix.yaml`（新增）、`tests/test_match_matrix.py`（新增）、`docs/要求对照矩阵.md`（新增）、`web/*`
- **改了什么**：把"要求侧 / 响应侧"两份文档按块标记对齐，产出「命中 / 部分 / 缺失 / 超范围」四态矩阵，进报告与控制台。
- **为什么**：审核本质是"对照"，只给单侧问题清单答不了"要求有没有被覆盖"。
- **验证**：v1-2026-10-26-a1 基线 53 条用例，见 `docs/要求对照矩阵.md`。

### 2026-10-26 · 修复 · 「目标命中意识」缺陷（R-20261026-003 简历 + JD 截图）

- **文件**：`review.py`、`tools/purpose.py`、`web/app.js`
- **改了什么**：审查目的从"一句话"扩到"目标 → 证据 → 缺口"，缺目标时不允许给 `pass`；命中目标的问题单独标记。
- **为什么**：审出来的问题与招聘方真正关心的目标无关，读了等于没读。
- **验证**：见 `.workbuddy/memory/2026-10-26.md` 对应小节（R-003 复跑对比）。

### 2026-10-26 · 新增 · 审查台「审查目的」窗口支持多文件 / 多格式上传

- **文件**：`web/index.html`、`web/app.js`、`server.py`
- **改了什么**：目的文件上传与待审上传对齐，支持多文件、多格式（含图片 OCR）。
- **为什么**：判定标准往往不止一句话，可能是一份 JD、一张截图。
- **验证**：见 `.workbuddy/memory/2026-10-26.md` 对应小节。

---

## 历史一览（v0.2.x – v0.7.1，此前逐轮记录在 README 与 `docs/修复报告-*.md`）

| 版本 | 主题 | 详情 |
|---|---|---|
| v0.7.2 | 审查情境 + 宽屏双向定位 | README §28 / §29（本文件 2026-10-27 有逐条） |
| v0.7.1 | 向「目的」功能投暴力测试 | README §27 末 / `暴力测试·审查目的功能·系统漏洞报告.md` |
| v0.7.0 | 审查目的：一句话 → 目标 → 证据 | README §27 / `docs/审查目的.md` |
| v0.6.3 | 历史报告管理、收藏、盲区门禁 | README §17 / §26 |
| v0.6.2 | 摘要展示、规则学习面板 | README §25 / `docs/自学习闭环.md` |
| v0.6.0 | 自学习闭环：提案 → 门禁 → 受控晋升 | README §25 / `docs/自学习闭环.md` |
| v0.5.0 | 多 Agent 框架：判定标准成为数据 | README §24 / `docs/多Agent框架.md` |
| v0.4.3 | 原文标注视图（角标、双向定位） | README §23 |
| v0.4.2 | 规则集版本：区分「升级」与「篡改」 | README §22 |
| v0.4.1 | LLM 配置记忆与超时 | README §21 |
| v0.4.0 | 方向感：从「规则执行器」到「独立审查者」 | README §20 |
| v0.3.0 | PDF / Word / 图片 / 压缩包上传 | README §18 |
| v0.2.8 | 本地大模型真实测评 | README §16 / `docs/本地模型实测报告.md` |
| v0.2.6 | 常见服务商预设与本地模型 | README §15 |
| v0.2.5 | 四页控制台与配套后端 | README §14 |
| v0.2.4 | 控制台上传待审文件 | README §13 |
| v0.2.3 | 前端 LLM 配置面板 | README §12 |
| v0.2.2 | 大模型 API 接入 | README §10 / §11 |
| v0.2.1 | 首轮修复记录 | README §9 |

> 说明：v0.7.2 之前未逐条登记（当时只写 README 章节与 `docs/修复报告-*.md`）。
> 上表是索引入口，不是完整流水；从 2026-10-26 起改为逐条登记。
