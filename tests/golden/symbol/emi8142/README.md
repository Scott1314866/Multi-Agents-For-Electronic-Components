# EMI8142 PDF 回归样例

`mineru_pin_tables.json` 保留使用者提供的 WJ251969 手册第 2 页真实 MinerU 结果，包含三款器件的分列引脚号、组合接地编号、NA 和 I/O 被误识别为 1/0、1/O 的情况。目标 EMI8142MUTAG 对应 XDFN10、CASE 711AU（第 8 页）；EMI8141 和 EMI8143 的封装不可混入。

`reference.png` 是使用者明确提供的标准图，`reference_pins.json` 是对应的编号、名称、左右顺序。PDF 印刷名称为 In_1+ / Out_1+ 等；VN 在表格中明确定义为 Ground，标准图采用 IN1+ / OUT1+ / GND。

生产修复包含：匹配封装的物理图优先、封装章节参加选图、完整双排 DFN 转为符号左右排列、独立复核附带原始名称表，以及仅 TYPE 列中的 I/O OCR 纠正。生产代码没有本器件的引脚答案。

离线测试只用标准图验收位置；保留原始 PDF 标签。实机测试在位置已经独立识别正确后，按使用者标准图统一显示名称，变更与原值均记录在 PostgreSQL checkpoint 和确认记录，区别于 PDF 的自动识别结果。
