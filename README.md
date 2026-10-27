# 体检影像印象生成研究：Python 基础骨架

依据 `intro.png` 的目标与 Li 等人的两阶段报告评估框架，实现**影像所见文本 → 印象生成 → 人工评价 → 自动汇总**，并提供配对读者研究的基础工具。Python 3.11+；核心流程只依赖标准库。

当前输入是报告 Findings 文本和有限临床信息，与参考论文的 report-based 路线一致。模型不直接读取 DICOM；不能据此评价原始影像病灶检测能力。`picc/` 是独立项目。

## 目录

```text
configs/study.json                 模型参数、prompt 路径、随机种子、洗脱期
prompts/impression_v1.txt           待在开发集上完善并锁定的 prompt
data/examples/                     纯虚构病例、独立参考与读者名单
docs/evaluation.md                 人工评价定义、五维量表与统计边界
src/impression_study/
  schemas.py                       数据契约与明确的模型输入白名单
  generation.py                    Mock / OpenAI 单轮生成、原始输出和清单
  review.py                        匿名评价包、评分校验与描述性汇总
  reader_study.py                   平衡计划、交互计时、配对变化
  storage.py                       UTF-8 JSONL/CSV
  cli.py                           命令行
scripts/demo.py                    完全离线的端到端演示
tests/                             泄漏控制、人工评分和配对流程验证
outputs/                           运行产物（自动创建、Git 忽略）
```

## 1. 安装与离线演示

在本目录执行（PowerShell）：

```powershell
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m impression_study --help
python scripts/demo.py --out outputs/demo-new
python -m unittest discover -s tests -v
```

演示包含 1 个开发病例、3 个评估病例和 2 名模拟读者。Mock 仅复制所见。demo 中的读者回答、评价标签与时间全部人为构造，并带 `demo_only` 标记，**不能作为 GPT 的质量或临床结果**。输出目录必须不存在，重复运行换一个目录名，避免覆盖。

## 2. 数据准备

`cases.jsonl` 每行一个病例；参照 `data/examples/cases.jsonl`。`patient_id` 用去标识化研究编号，同一患者禁止同时出现在 development 和 evaluation。`clinical_info.history/prior_imaging=null` 表示缺失，不等于阴性。`input_qc_passed=true` 表示研究者已人工检查初诊时点、去标识化、删除原印象与检查后信息。

字段白名单会拒绝 `impression`/`pathology` 等额外字段，但不能自动识别 Findings 文本内隐藏的答案；文本清理仍需人工 QC。参考答案放在单独的 `references.jsonl`，`generate` 命令没有读取它的参数。

```powershell
python -m impression_study validate --cases data/examples/cases.jsonl --references data/examples/references.jsonl
```

`references.jsonl` 定义每例专家参考、参考依据、正常/异常与必需结论清单。人工评分规则见 [docs/evaluation.md](docs/evaluation.md)。示例参考同样是虚构占位内容，不是已建立的医学金标准。

## 3. Phase 1：模型独立生成

```powershell
python -m impression_study generate --cases data/examples/cases.jsonl --config configs/study.json --backend mock --out outputs/run01
```

只生成 evaluation 病例，开发集用于人工完善 prompt。每次生成保存：

- `predictions.jsonl`：印象条目、支持所见 ID、确定性、建议、信息局限；
- `raw/*.json`：先于解析保存后端原始响应，包含截断、拒绝或格式不合法的响应；
- `errors.jsonl`：失败时记录病例、响应编号及异常类型，便于定位对应 raw 文件；
- `manifest.json`：prompt 全文/哈希、输入哈希、实际模型 ID、参数、软件版本、完成数和状态。

真实 API：

```powershell
python -m pip install -e ".[api]"
$env:OPENAI_API_KEY = "你的 API key"
# 将下面 model-id 替换为账户实际可调用、支持 Chat Completions 和 JSON Schema 的模型 ID
python -m impression_study generate --cases data/private/cases.jsonl --config configs/study.json --backend openai --model model-id --out outputs/api01
```

配置默认 `model=null`，不假定 GPT-6 的公开接口或名称。`temperature=null` 表示不发送该参数，适配部分不接受 temperature 的推理模型；目标模型支持时可显式设为 0。低温也不保证完全确定性。当前适配器使用严格 JSON Schema；接口/参数不兼容时显式失败，不会自动换模型、换 prompt 或退化输出格式。SDK 自动重试关闭；失败保留已完成结果、原始响应（若已收到）和 failed 状态，换新目录重新实验。

数据不自动上传；仅显式选择 openai 后端时调用 API。不要把 API key 写入配置文件。正式数据按研究既定的数据处理方案准备。

## 4. 导出人工盲评并自动汇总

```powershell
python -m impression_study export-review --cases data/examples/cases.jsonl --references data/examples/references.jsonl --records outputs/run01/predictions.jsonl --out outputs/review01
```

只将 `outputs/review01/reviewer/` 交给评价者：

- `packets.jsonl`：随机排列的匿名样本、输入、独立参考、待评印象；
- `ratings.csv`：空评分表，填写 evaluator_id、0/1 判定、遗漏项 ID、无依据/矛盾项序号及 1–5 分质量评分。

`private/key.jsonl` 是研究协调者持有的解盲表，不提供给评价者。只是隐藏来源元数据，语言风格仍可能使评价者猜出来源；不宣称完全盲法。

多位评价者各自填写完整表，保留单独 evaluator_id；汇总前合并 CSV（只保留一次表头）。仲裁结果使用另一个 evaluator_id。没有遗漏/无依据结论时显式填 `[]`，不能留空。程序拒绝遗漏评分或重复 sample/evaluator；不把空值当成“正确”，不自动语义判分。

```powershell
python -m impression_study summarize --ratings outputs/review01/completed_ratings.csv --key outputs/review01/private/key.jsonl --out outputs/summary01.json
```

`completed_ratings.csv` 由评价者填写后另存，不是程序自动产生。输出逐评价者、模型/读者、辅助条件的诊断一致率、可接受率、重大错误率、清单召回、关键遗漏、无依据/矛盾条目比例、五维均值与时间。模型延迟不是医生阅片时间；汇总不计算 BLEU/ROUGE，也不把模型自己声称“有依据”当作正确。

## 5. Phase 2：配对读者研究

```powershell
python -m impression_study plan-readers --cases data/examples/cases.jsonl --readers data/examples/readers.jsonl --config configs/study.json --start-date 2026-10-26 --out outputs/assignments01.csv
python -m impression_study reader-session --cases data/examples/cases.jsonl --assignments outputs/assignments01.csv --predictions outputs/run01/predictions.jsonl --reader-id DEMO-JUNIOR --session 1 --out outputs/reader_records.jsonl
```

每位读者、每个病例两次阅读，辅助条件反转，病例/会话内顺序按种子平衡随机化；名单中的 seniority 由研究方案事先定义。例数为奇数时两种条件数量相差 1。计划日期至少间隔 28 天；运行 session 2 时还会检查文件中真实的首次完成时间和相反条件，不仅相信计划日期。

交互命令在展示病例时开始计时，输入每行一条最终印象、建议、局限后逐例保存。再次运行会跳过已完成病例。同一研究的所有读者/会话追加到同一个 `reader_records.jsonl`，单进程顺序使用，不支持多人并发写同一文件。取消当前未完成病例不会保存该病例。读者记录的 certainty 是占位值 indeterminate，evidence_ids 为空；骨架不把它们用于读者质量评分，正式 UI 可扩充手工选择。

对 `reader_records.jsonl` 使用相同的 export-review → 人工评分流程后：

```powershell
python -m impression_study paired-summary --ratings outputs/reader_review/completed_ratings.csv --key outputs/reader_review/private/key.jsonl --readers data/examples/readers.jsonl --out outputs/paired01.json
```

该命令只做每位读者的描述性配对变化与探索性 McNemar 检验，并显示未配对样本数。总体多读者多病例效应、年资差距的交互检验、置信区间、κ/ICC、多中心/病种分层以及样本量计算是后续扩展点，参见评价文档。它们没有被不恰当地替换为简单独立样本检验。

## 与参考论文的对应

沿用：报告文本输入、初诊信息限制、锁定 prompt、模型独立评估 + 辅助读者研究、人工质量量表与统计计算分离。

调整：体检人群多种/多项异常，不强制每例良恶性分类或 top-3；以印象可接受性、错误和必要内容覆盖为中心。五维名称来自原文 Figure 4，评分锚点及本项目判分流程为自行设计。原文正文没有充分披露自动/人工匹配实现细节，本骨架不声称是作者代码的复现。
