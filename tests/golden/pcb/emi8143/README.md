# EMI8143 输入和来源

图纸来源为用户提供的 EMI8143MUTAG datasheet，第9页，XDFN16 3.5x1.35, 0.4P，
CASE 711AW。`footprint.png` 是用户提供的主推荐焊盘图。结果截图不作为识别输入。

`source_reading.json` 是按主图和 datasheet 人工核对的来源数据，明确标注
`datasheet_crosscheck_by_assistant`；它不是视觉模型输出。
`real_reading.json` 则是实际 Qwen 请求与主图闭合轮廓复核后的记录，约8秒返回。
它保留真实元数据、源图轮廓、量纲和未确认字段，不能把它标成全自动确认。
模型对无 EP 的表述保守，因此 `confirmed_fields.json` 明确补充 `ep_present=false`，
依据为完整底视图和推荐图均没有独立中心盘；两条长区域是地端子。

主图短盘是12×0.65，普通盘宽15×0.25，一脚宽0.55，横向中心距0.40，
上下外缘跨度1.55毫米。第3、6列原本上下连通；datasheet地脚3/14、6/11对应这些列。
用户明确选择16个独立焊盘，因此在确认阶段用普通短盘长度拆分，未复制结果参考图。

常规中心X为0.40×(列号−3.5)，Y为±(1.55−0.65)/2。
一脚右缘与上方窄盘对齐，因此中心X为−1.40−(0.55−0.25)/2=−1.55。
本体D=3.50 BSC，E=1.35 BSC，最大高度A=0.50毫米。

重放真实识别并生成脚本/预览：

```powershell
.venv/Scripts/python.exe scripts/test_pcb_emi8143.py --reading tests/golden/pcb/emi8143/real_reading.json --output output/pcb_agent/emi8143/generated_from_image --confirmed-spec tests/golden/pcb/emi8143/confirmed_fields.json
```

加 `--ssh` 会使用已配置的 Windows Cadence 工作机，在新建隔离目录运行
build/verify/props，下载带哈希校验的测试产物并执行逐焊盘回读判据。
这是一条测试路线，生产 API 的 full 模式仍调用本机 Allegro runner。

`live/` 保存 2026-10-09 的真实 Allegro 17.2 S067 回读日志及最终规格，53项判据
全部通过：16个脚号、逐脚阻焊 bbox 与中心、对象/层、padstack 落盘、DRA/PSM 编译、
存盘重开后的 PACKAGE_HEIGHT_MAX=0.5 MM。它们用于离线回归，不伪造原生验证。
