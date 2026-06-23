# Auto Invest

一个轻量级多市场量化研究系统，初期支持 A 股、港股、美股和 BTC 的统一标的模型、CSV 行情接入、简单策略和本地回测。

原始远端仓库定位：A structured repository for learning investment theories and applying them in practice.

## 架构

```text
src/auto_invest/
  domain/       # 标的、K 线、组合、交易等核心领域模型
  ports/        # 数据源、交易执行等外部依赖接口
  adapters/     # CSV、本地配置等适配器
  strategies/   # 策略接口与简单策略
  backtest/     # 回测引擎、撮合、绩效报告
  apps/         # CLI 等应用入口
```

核心思路是 Clean Architecture + Ports and Adapters：策略只关心市场快照和目标仓位，回测引擎负责组合再平衡，数据源通过端口替换。

## 快速运行

```bash
PYTHONPATH=src python3 -m auto_invest.apps.backtest_cli \
  --bars data/sample/bars.csv \
  --instruments configs/universe.example.json \
  --strategy ma-cross \
  --fast-window 2 \
  --slow-window 3 \
  --initial-cash 100000
```

运行测试：

```bash
python3 -m unittest discover -s tests
```

## 初期能力

- 统一市场抽象：A 股、港股、美股、BTC。
- 统一 K 线格式：`timestamp,instrument_id,open,high,low,close,volume`。
- 支持简单策略：买入持有、均线交叉。
- 支持按市场规则做数量步长约束，例如 A 股 100 股一手、BTC 0.0001。
- 输出基础绩效：期末权益、总收益、最大回撤、交易数。

## 下一步扩展

- 接入 AkShare/Tushare、IBKR、币安等真实数据适配器。
- 增加事件驱动撮合、订单簿、风控和组合优化模块。
- 增加因子研究、特征仓库和 walk-forward 验证。
