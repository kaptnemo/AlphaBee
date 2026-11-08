"""参数治理测试包（P0：registry v0 + typed params + fail-open 解析）。

全部离线、纯函数：只读仓库内 ``params/registry.yaml`` 与临时 YAML 文件，
配置读取用 monkeypatch ``alphabee.config.get_settings``，不触发网络/LLM/真实 data 目录。
"""
