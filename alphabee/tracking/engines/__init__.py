"""L2 引擎子包（研究连续体 P6 / §8；``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §15.6-B）。

**这是 ``tracking/`` 下唯一允许 import ``alphabee.orchestrator.agent`` 的子包**，且该 import
**必须写在方法体内**（§15.0 B-2 / C-6）：模块顶层 import 会拉起
``orchestrator.collectors`` → ``tushare.set_token``（写 ``$HOME/tk.csv``）等副作用，
而 ``import alphabee.tracking`` 必须保持零副作用（``tests/tracking/test_engine.py`` 用
**子进程实测 + AST 断言**双重钉住）。

本 ``__init__`` **不**自动 import 任何宿主图（``pipeline_engine`` 自身也只在方法体内 import
orchestrator），因此 ``import alphabee.tracking.engines`` 与 ``import alphabee.tracking`` 同样零副作用。
"""

from __future__ import annotations

from alphabee.tracking.engines.pipeline_engine import PipelineEngine

__all__ = ["PipelineEngine"]
