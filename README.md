# CL / BZ 等桶数价差网格（模拟版）

从 Variational Omni 的前端接口读取真实行情，在本地模拟 CL、BZ 等桶数双腿市价成交。以 **BZ − CL 的最近 7 日平均价差**为中枢。无需钱包私钥；使用已经登录的 `vr-token` Cookie 读取行情。

程序只支持 `paper`。网络出口只允许会话检查、合约元数据、K 线、指示性报价四种接口，**没有真实下单、撤单、转账或提款功能**。资金与仓位完全属于本地模拟账本。

## 直接运行

需要 Python 3.11+，运行和测试均使用标准库，无第三方运行依赖。在项目目录中执行：

```powershell
python -m variational_grid init-session --curl-file "C:/path/to/variational-me.txt"
python -m variational_grid run
```

输入文件是 Chrome 中 `GET https://omni.variational.io/api/me` 请求的 **Copy as cURL (bash)**，保存为本地 UTF-8 文本。程序只解析文本，绝不执行其中的命令；只提取 `vr-token` 和普通 User-Agent。也可以运行 `python -m variational_grid init-session`，在隐藏输入提示中粘贴 Cookie 的令牌值。

首次导入自动创建 `config.local.json` 和 `data/session.json`。运行时可用 Ctrl+C 停止；再次启动使用原账本续跑，不会把已有模拟持仓重复建仓。停止期间没有行情采样，不会补造历史成交。

```powershell
python -m variational_grid run --once
python -m variational_grid run --iterations 3
python -m variational_grid status
python -m variational_grid export --output output/fills.csv
```

`status` 提供最后采样时间、过期标记、两腿桶数、权益、累计手续费、已实现与总盈亏。`run` 每轮输出一行 JSON，适合接日志和后续监控。`export` 导出全部双腿成交；为保护已有文件，目标文件必须尚不存在。

不需要网络和令牌的确定性演示：

```powershell
python -m variational_grid demo
python -m unittest discover -s tests -v
```

演示使用合成行情，仅验证正反向网格行为，**不是历史回测或收益预测**。再次演示须指定新的 `--state-file data/demo2.sqlite3`，已有数据不会被覆盖。

## 三种间距同时模拟

默认对照 **0.15、0.20、0.25 USDC/桶**，配置见 [experiments.example.json](experiments.example.json)。三组共同读取 `config.local.json`；除间距外参数相同，默认各有 1000 USDC 模拟资金、每格每腿 1 桶、最多 8 组持仓。

间距是 **BZ−CL 价差的绝对变化，不是百分比**。例如中枢为 3.50、间距 0.20 时，做空价差的层级是 3.70、3.90、4.10……，做多价差的层级是 3.30、3.10、2.90……。当前实现还把间距用作每桶净止盈目标：每腿 1 桶的一组仓位，扣除买卖点差、滑点和手续费后利润达到 0.20 USDC 才止盈。三组对照会同时改变入场间距与净止盈目标。

导入会话后，启动三组只需：

```powershell
python -m variational_grid compare
```

同一份 CL/BZ 报价先整体校验，再交给三组独立账本；不会为每组重复请求同数量报价。任一行情无效时所有组一起暂停，避免采样时间不同造成偏差。

```powershell
python -m variational_grid compare-status
python -m variational_grid compare-stop
```

在另一个终端执行 `compare-stop`，将在当前行情请求结束后停止，保留模拟持仓。也可以用 Ctrl+C；再次执行 `compare` 从原状态继续，不补造停机期间的成交。电脑关机或进程退出后不会继续采样。

对照页面是 `data/comparison-015-020-025/public/index.html`，浏览器打开即可；每 10 秒刷新，显示共同采样时间、过期状态、总盈亏、已实现盈亏、累计最大回撤、已开/已平组数、保证金及曲线。也可仅将该 `public` 子目录作为本机只读页面服务：

```powershell
python -m http.server 0 --bind 127.0.0.1 --directory data/comparison-015-020-025/public
```

端口 `0` 会自动选择空闲端口，访问终端显示的 `http://127.0.0.1:端口/` 地址即可。`public/summary.json` 提供相同结果，均不包含令牌。页面使用自带 SVG、无外部资源，桌面与手机分别布局；三组曲线共用纵轴，断网缺口不会画成连续行情。页面只展示最近 360 次采样，累计统计不受这个展示窗口影响；没有根据短期结果自动挑选“最佳参数”。

共同行情、各组账本和汇总存放于同一个实验目录，完整保留共同采样记录。意外中断后重放已记录但尚未完成处理的共同行情，不会重复记账。备份时先停止进程，再整体复制实验目录；不可单独替换某一组账本。更改间距、资金、数量或组别时，在实验文件里指定新的 `output_dir`，不要混入原实验。可通过 `--experiments <文件路径>` 使用其他实验配置。

## 策略口径

`S = mark(BZ) − mark(CL)`，单位 USDC/桶。中枢 `C` 是最近 **168 根已收盘 UTC 小时 K 线**的 `close(BZ) − close(CL)` 算术平均，每小时更新一次。两腿时间戳必须完整、一一对齐；不使用尚未收盘 K 线、不补齐缺口。

| 条件 | 等桶数双腿方向 |
|---|---|
| `S − C ≥ n × 网格间距` | 卖出 BZ，买入 CL，做空价差 |
| `C − S ≥ n × 网格间距` | 买入 BZ，卖出 CL，做多价差 |
| 未偏离一格 | 不建立新仓 |

每个方向和层级最多一组持仓，一次采样最多新开一组；跳过多格时分多轮加入，最多达到 `max_levels`。旧方向尚有持仓时，不建立反方向的新仓。

每组独立退出：按当前双腿可执行买卖价估算，**扣除四笔模拟手续费、买卖点差和滑点后的利润达到“每腿桶数 × 网格间距”**时，双腿同时在模拟账本平仓。每组记录进场中枢；滚动中枢变化不重写成本或强制亏损止盈。平掉的层级必须先回到阈值内，才会重新允许该层级入场；同一轮平仓后不立即开仓。

买入按 `ask × (1 + 滑点)`、卖出按 `bid × (1 − 滑点)`计价；新建和退出都执行这个规则。报价请求数量等于每腿配置桶数，并检查交易所返回的数量步长、最小量和最大量。`cash_usdc` 是初始资金加已实现盈亏、减尚未平仓的入场手续费；保证金不直接扣现金。`equity_usdc` 进一步计入现有持仓按当前可平价格估值及预计退出手续费。

等桶数抵消共同的每桶价格变动，并不意味着美元名义价值完全相等，也不消除原油品种间的价差风险。

## 参数

编辑 `config.local.json`；字段详见 [config.example.json](config.example.json)。以下只是用于运行演示的默认值。

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `paper_balance_usdc` | 1000 | 独立模拟初始资金 |
| `quantity_barrels` | 1 | 每格、每腿桶数 |
| `grid_step_usdc_per_barrel` | 0.20 | 入场层级间距和扣成本后的每桶止盈目标 |
| `max_levels` | 8 | 最多同时持有的配对组数 |
| `paper_leverage` | 5 | 模拟保证金估算杠杆 |
| `max_margin_fraction` | 0.80 | 新开仓后估算保证金不得超过“权益与初始资金较小值”的该比例 |
| `max_drawdown_fraction` | 0.20 | 相对历史权益峰值的最大回撤；达到后模拟平仓并锁定停机 |
| `max_holding_hours` | 168 | 单组最长持仓时间；超过后按下一份有效报价退出 |
| `slippage_bps_per_leg` | 1 | 每腿每次成交滑点；1 bp = 0.01% |
| `fee_bps_per_leg` | 0 | 每腿每次模拟手续费，可自行设置 |
| `poll_seconds` | 10 | 采样周期，至少 5 秒；失败时退避最多 60 秒 |
| `max_quote_age_seconds` | 15 | 报价最多允许滞后时间 |
| `max_pair_skew_seconds` | 5 | CL 与 BZ 报价时间差上限 |

价格、资金、数量用十进制计算。数据文件路径相对于配置文件所在目录解析。改变资金、数量、网格、成本或风控参数时，必须同时改用新的 `state_file`，避免重新解释旧账本；采样和报价时间限制允许调整后继续原账本。回撤锁定会持久保存，不会重启后自行解锁；另开模拟实验时换用新的账本。

## 异常和模拟限制

- K 线不完整、报价过期、双腿时间差过大、合约定义变化、任一市场关闭、会话失效或服务异常时暂停模拟成交。恢复有效数据后再继续；不存在用旧价止损的假成交。市场仅允许减仓时只模拟退出。
- 账本采用 SQLite 事务，双腿记账要么全部完成，要么全部回滚；同一账本只允许一个运行进程。保存每组持仓、所有成交和事件；常规行情快照只保留最近 7 日。
- 报价是 **indicative**，不是交易所承诺成交价。模拟假设双腿按对应报价成交；没有模拟真实双腿之间的延迟、部分成交、冲击、拒单和补腿风险。同一轮多组退出分别使用每组数量的报价，未估算合并退出对深度的影响。
- 盈亏字段明确标为 `before_funding`：**未计实际资金费**。杠杆保证金是本地估算，不复制交易所风险引擎、维持保证金或强平机制；这不是实盘收益评估。
- JWT 的 `exp` 仅用作本地过期提醒，签名与有效性通过服务端 `/me` 检查。会话文件每次请求重新读取，更新令牌不必重启正常循环。401/403 或过期后重新导入会话即可；首次启动验证失败会退出。

凭据仅保存于忽略提交的本地文件；POSIX 下会话文件要求 `0600`。Windows 使用当前账户的 [DPAPI 加密](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)，不接受明文会话文件，也不会在加密失败时退回明文。换账户或部署到 Linux 时应重新导入令牌，不要直接复制加密会话文件。原始 cURL 捕获文件仍由你保管，程序不会删除它。日志不打印令牌、原始服务端错误或请求头。程序不需要钱包私钥。

## Linux 一键部署与升级

适用于 Debian 12+ / Ubuntu 24.04+、systemd：

```bash
curl -fsSL https://raw.githubusercontent.com/hxx344/variational-cl-bz-grid/main/install.sh | sudo bash -s -- --compare
```

这一条命令会自动安装 Python 与 Git、下载并验证代码、创建独立服务账户、配置开机启动和失败后自动重启，并启动 **0.15 / 0.20 / 0.25 三组对照模拟**。首次安装只需按提示粘贴 `vr-token`（隐藏输入），不需要钱包私钥。重复执行同一命令升级，保留配置、会话与账本；旧代码版本保留在 releases 中。新版本测试失败时不会切换正在运行的代码，未完成的解压目录自动清理。

已经下载仓库时，也可直接运行：

```bash
sudo bash install.sh --compare
```

首次安装不带参数也默认运行三组；已有安装不带参数会保留所选模式，`--compare` 明确切换为三组。实验配置放在 `/etc/variational-grid/experiments.json`，结果放在 `/var/lib/variational-grid/comparison-015-020-025/`。已有实验配置不会被覆盖；用 `--single` 切回原单组模式，各自账本保留。仅部署模拟进程，不自动开放公网网页端口。

- 参数：`/etc/variational-grid/config.json`
- 会话、账本：`/var/lib/variational-grid/`；可改文件名，但服务配置要求路径保持在该目录内。
- 查看日志：`journalctl -u variational-grid -f`
- 停止：`sudo systemctl stop variational-grid`
- 修改配置后：`sudo systemctl restart variational-grid`
- 刷新会话：重复执行安装命令；有效会话保留，已过期会话重新提示输入。

安装流程有 Ubuntu 隔离测试：使用临时目录和本地 Git 源验证首次安装、重复升级、模式切换、配置与数据保留，以及新版本测试失败时保留旧版本。测试替代系统服务管理和远端会话检查，不访问真实账户；尚未在你的服务器上部署。

## 接口依据

2026-09-19 根据 Omni 前端及当前会话验证：`GET /api/me`、`GET /api/metadata/supported_assets?cex_asset=...`、`GET /api/candles?cex_asset=...&period=1h&start=...&end=...`、`POST /api/quotes/indicative`。CL、BZ 的 instrument 均为 `perpetual_rwa_future` / `commodity` / `USDC`。

这些是当前网页使用的接口，不是承诺稳定的公开交易 API。字段变化会停止相应数据处理，需要更新适配器。官方说明：[报价、指数和标记价格](https://docs.variational.io/omni/trading/quoted-index-and-mark-prices)、[API 文档](https://docs.variational.io/technical-documentation/api)。
