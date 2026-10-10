"""驱动画像研究代理（Tier B）：LLM 生成标的特异的驱动假设 + 可执行研究议程。

本包**刻意不在 ``__init__`` 里 import 子模块**（与 ``agents/verify_hypotheses`` 同构）：
``agent`` 会拉起 ``tools.tushare_query`` → ``collectors.tushare`` 的 ``ts.set_token``
（写 ``$HOME/tk.csv``）等副作用，只想消费 schema / prompt 的调用方不该付这个代价。
消费方请直接 import 具体模块（``agent`` / ``prompts`` / ``output``）。
"""
