"""符号生成 Agent 的外部调用层。

这里的东西都会**碰外部世界**：网络（MinerU、视觉模型）、磁盘（页面渲染）、
子进程（Cadence tclsh）。它们从 :mod:`backend.agents.symbol.contracts`
拿纯数据，把副作用挡在自己这一层里。
"""
