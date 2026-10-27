# 评价规则草案 v1

这是本项目拟定的规则，不是 Li 等人补充材料的转录。本文 PDF 正文只说明了病理参照、top-k、五维五点量表及统计方法；并未明确披露自由文本诊断匹配是否自动化、评分人员及完整裁决过程。复现原研究仍需核对 Appendix S3。

## 1. 先锁定参考标准

- 在查看待评价模型/读者输出之前，为每个病例建立 `reference_impression` 和 `required_items`。
- `basis` 标注 expert_consensus / followup / pathology；体检病例通常首先建立基于当前检查的专家共识。
- 专家共识评价的是“当前信息所支持的印象”，不等同于病理确诊。仅有共识时，`diagnostic_correct` 的汇总应表述为**与专家参考的一致率**，不能声称真实疾病诊断准确率。不同参考依据应另作分层分析。
- 如使用随访/病理，参考材料可以供评价者使用，但不得回填进模型的初始输入。不能因模型未猜中当前证据无法支持的病理亚型而将其自动计为重大错误。
- required_items 是“必须保留的结论/不确定性”清单，不是所有正常所见的逐字复制清单。正常病例也建立至少一条阴性表达要求。
- `critical=true` 只标注遗漏会实质影响临床处理的项目；判定原则由研究者在正式评价前锁定。

## 2. 每个样本由人填写的字段

| 字段 | 含义与填写规则 |
|---|---|
| evaluator_id | 评价者匿名 ID；不同评价者分别保留，仲裁结果使用单独 ID |
| diagnostic_correct | 0/1；整体诊断是否达到预先约定的参考一致性标准，允许同义表达；不做字符串匹配 |
| clinically_acceptable | 0/1；是否无需实质性医学修改即可作为印象；存在重大错误必须为 0 |
| major_error | 0/1；是否包含会实质影响诊断/处置的错误，含重要遗漏、矛盾或不当建议 |
| omitted_item_ids | 未满足的参考清单 ID，JSON 数组，例如 `["R1"]`；没有遗漏填 `[]` |
| unsupported_item_indices | 缺乏输入支持的印象项序号，**从 1 开始**；例如 `[2]` |
| contradicted_item_indices | 与输入明确矛盾的印象项序号；与 unsupported 类别互斥 |
| quality_* | 下述五维评分，整数 1–5 |
| notes | 分歧、错误说明、不可判定问题；允许空白 |

unsupported / contradicted 以 `output.impression` 的项为计数单位，每项尽量只有一个可核查命题。它们不是逐词幻觉率，也不把 recommendations/limitations 纳入分母。建议错误通过 major_error、actionability 和 notes 记录。证据 ID 存在只能说明结构合法，**不能证明该证据支持诊断**。

## 3. 五维人工量表

五个维度名称对应原文 Figure 4（Logic / Completeness / Terminology / Actionability / Structure），以下锚点为本项目草案。

| 维度 | 1 分 | 3 分 | 5 分 |
|---|---|---|---|
| quality_logic | 结论与所见严重不符或推理自相矛盾 | 核心方向基本合理，但需实质性修订 | 结论有据、逻辑清楚，不夸大确定性 |
| quality_completeness | 缺少主要临床相关结论 | 有部分相关遗漏 | 覆盖预定必要内容，包含恰当不确定性 |
| quality_terminology | 术语错误造成误解 | 可理解但术语需修订 | 表达准确规范、同义术语使用合理 |
| quality_actionability | 错误建议/过度检查，或漏掉必须的处置提示 | 建议总体合理但需调整 | 有证据支持的适度建议；无须建议时正确保持为空 |
| quality_structure | 主次混乱、无法直接使用 | 组织基本清楚但冗余或顺序欠妥 | 简洁有序、重点突出、便于签发 |

2 和 4 分分别是相邻锚点的中间水平。五维分别报告，不把风格分数当作诊断正确性。至少两名评价者独立评分；按预设规则仲裁。骨架保留各评价者结果，不自动多数投票。评价者间 κ/ICC 尚未实现。

## 4. 自动汇总的含义

- diagnostic_accuracy / clinically_acceptable_rate / major_error_rate：人工 0/1 标签的比例。
- checklist_recall：已覆盖 required_items 数 / 全部 required_items 数（微平均）。
- critical_item_omission_rate：遗漏的 critical 项 / 全部 critical 项；没有 critical 项时输出 null。
- unsupported_item_rate / contradicted_item_rate：相应标记的印象项数 / 生成印象项总数。
- reports_with_unsupported_items：至少有一个 unsupported 项的报告比例。
- 正常、异常病例分别汇总诊断一致率；五维分数取描述性均值。
- 不同评价者、模型/读者、辅助条件分别汇总；不把多个评价者或重复读者样本混为独立病例。

## 5. 两阶段研究与统计扩展点

**Phase 1**：对同一组 evaluation 病例生成印象并人工评价。可以用相同记录格式导入常规报告或不同年资读者结果；先人工转换成 impression 条目，并记录来源。不要直接用原报告结论作为模型输入。

**Phase 2**：相同读者/病例进行两次文本解释，有/无模型辅助。骨架采用病例内交叉、会话条件顺序平衡，而原论文是先无辅助后有辅助；前者有助于减小期间效应，但不能彻底消除学习/记忆影响。`reader-session` 在开始展示病例时启动单调时钟，覆盖阅读、参考预生成辅助意见及最终输入时间；模型推理延迟另存，不能冒充医生阅片时间。

`paired-summary` 提供每位读者的错误→正确、正确→错误、准确率变化、时间变化，以及无患者重复时的探索性精确 McNemar 检验；不是总体 MRMC 效应。多个读者检验未校正多重比较。正式研究应另实现预先锁定的交叉随机效应模型，考虑病例、读者、期间/顺序、年资及 assistance×seniority；分析时间可用合适的连续结局模型。不能通过“P>0.05”证明与高年资等效，需要预设非劣/等效界值及相应样本量。

样本量、真实患病率、多中心抽样、病种/信息受限程度分层、置信区间及多重比较方案需在正式实验前确定。骨架不把原论文的 100 例或 7 名读者当作通用样本量答案。
