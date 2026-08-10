"""Market-regime collectors: normalize external market data into canonical fields.

Each collector is a *source-specific fetcher*: it is allowed to reference
external API/column names (see the ``alphabee/adapters/*/market_regime_mapping.yaml``
files), but it must return canonical fields only.
"""
