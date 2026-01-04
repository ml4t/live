# API Reference

## Core Classes

::: ml4t.live.engine.LiveEngine
    options:
      show_root_heading: true

::: ml4t.live.config.LiveRiskConfig
    options:
      show_root_heading: true

::: ml4t.live.broker.SafeBroker
    options:
      show_root_heading: true

## Brokers

::: ml4t.live.brokers.ib.IBBroker
    options:
      show_root_heading: true

::: ml4t.live.brokers.alpaca.AlpacaBroker
    options:
      show_root_heading: true

## Data Feeds

::: ml4t.live.feeds.ib.IBDataFeed
    options:
      show_root_heading: true

::: ml4t.live.feeds.databento.DatabentoFeed
    options:
      show_root_heading: true

## Exceptions

::: ml4t.live.exceptions
    options:
      show_root_heading: true
      members:
        - BrokerConnectionError
        - RiskLimitExceeded
        - DataFeedError
