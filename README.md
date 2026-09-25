# Variational Grid · 跨品种网格与对冲模拟

提供 CL/BZ 价差网格、CL/BZ 库存组合，以及 **Lighter QQQ 只做多挂单剥头皮 + Variational US100 对冲**三种独立纸面策略。QQQ 模式取消新开仓距离门槛，对照三种止盈比例，共三组，统一净敞口阈值 3000 USDC，单组最多 30 个批次、每批 1000 USDC；部署和统计口径见文末。

CL/BZ 模式从 Variational Omni 的前端接口读取真实行情，在本地模拟 CL、BZ 等桶数双腿市价成交。以 **BZ − CL 的最近 3 日平均价差**为中枢。无需钱包私钥；该模式使用已经登录的 `vr-token` Cookie 请求与每腿桶数相匹配的指示性买卖报价。

**仅获取公开行情不需要令牌。** 2026-09-19 无 Cookie 实测：前端 K 线、合约元数据以及官方公开 `GET /metadata/stats` 均返回 200，CL/BZ 的 `POST /api/quotes/indicative` 返回 403；同一报价接口使用现有会话成功。CL/BZ 模式选择后一种按数量报价，并在启动时检查登录状态，所以仍需令牌。官方公开统计也有买卖价，但按固定名义金额分档，文档允许最长 600 秒缓存；不能直接当成按当前每腿桶数取得的实时成交报价。QQQ 模式现已切换为 Var token 鉴权报价，Lighter 侧仍读取公开行情，详见文末。

程序只支持 `paper`。CL/BZ 客户端只允许会话检查、合约元数据、K 线、指示性报价四种接口；QQQ 模式另读取 Lighter 公开盘口和逐笔成交。**没有真实下单、撤单、转账或提款功能**。资金、挂单与仓位完全属于本地模拟账本。

## 工作台入口

仓库现名为 `variational-grid`（原 `variational-cl-bz-grid`）。现有安装器会把已知旧远端迁移到新地址；配置、会话、账本、服务名和现有策略模式继续保留。

同机部署 [Project Aggregation 工作台](https://github.com/hxx344/project-aggregation) 时，可用一条命令更新两个项目：

```bash
curl -fsSL https://raw.githubusercontent.com/hxx344/project-aggregation/main/install-all.sh | sudo bash -s -- --only variational,hub
```

工作台自动添加 `Variational Grid`，通过服务器 `127.0.0.1:9876` 显示原始页面；只需转发工作台 `3100`。项目管理无需填写用户名、密码或行情 token；原模块保持只监听本机，通过工作台访问时由工作台登录和页面授权保护。首次安装默认 CL/BZ 三组对照，已有策略模式保留；首次/令牌失效时在 SSH 终端按提示隐藏输入 `vr-token`。

新增只读 `GET /api/hub/summary?schemaVersion=2`（无参数也返回 v2）：仅取最新发布采样和进程状态，不调用行情、不读取 token、不扫描历史或各组仓位。各组本轮累计模拟盈亏分别显示，单位 USDC，不计入工作台真实资产；暂停、停止、过期、重置及合成行情均保留相应状态。QQQ 摘要时间同时受已有 US100 仓位估值时间限制。三种监控页在工作台中隐藏时暂停前台刷新，重新进入立即补查，后台模拟继续运行。

## 直接运行

需要 Python 3.11+，运行使用标准库，无第三方运行依赖。前端逻辑测试另用 Node.js，部署运行无需 Node.js。在项目目录中执行：

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
python -m variational_grid check-session
python -m variational_grid export --output output/fills.csv
```

`status` 提供最后采样时间、过期标记、两腿桶数、权益、累计手续费、已实现与总盈亏，以及累计成交桶数、成交额与成交笔数。`run` 每轮输出一行 JSON，适合接日志和后续监控。`export` 导出全部双腿成交；为保护已有文件，目标文件必须尚不存在。

不需要网络和令牌的确定性演示：

```powershell
python -m variational_grid demo
python -m unittest discover -s tests -v
```

演示使用合成行情，仅验证正反向网格行为，**不是历史回测或收益预测**。再次演示须指定新的 `--state-file data/demo2.sqlite3`，已有数据不会被覆盖。

## 三种间距同时模拟

默认对照 **0.5%、1%、2%**，配置见 [experiments.example.json](experiments.example.json)。三组共同读取 `config.local.json`；默认均为**格数不限、持仓金额不限、保证金使用不限、100 倍模拟杠杆**。超出原来的 ±30% 后继续向外扩展，每格一组。各有 1000 USDC 模拟初始资金、每格每腿 1 桶，数量和成本参数一致。

间距按 **三日平均价差中枢的绝对值 × 百分比**计算。`grid_step_percent: "1"` 表示 1%。例如中枢为 4 USDC/桶：

| 配置 | 当前一格 / USDC/桶 | 格数 | 超过原 30% 后的第一格 |
| --- | --- | --- | --- |
| 0.5% | 0.02 | 不限 | 第 61 格，距中枢 30.5% |
| 1% | 0.04 | 不限 | 第 31 格，距中枢 31% |
| 2% | 0.08 | 不限 | 第 16 格，距中枢 32% |

触发线为 `C ± |C| × 格距百分比 × 格位编号`，没有固定最外层。每个方向和格位只持有一组，每轮最多新增一组；仍保留回撤、最长持仓及只减仓规则。100 倍杠杆只用于保证金估算，例如双腿名义金额合计 194 USDC，则估算保证金为 1.94 USDC；保证金用量不再阻止开仓，成交量与损益也不额外乘以杠杆。

中枢每小时更新，入场层级和范围随之变化；每笔仓位的净止盈目标固定为 **每腿桶数 × 开仓时的一格**，扣除买卖点差、滑点与开平手续费后才判断止盈。中枢为负时取绝对值计算间距；中枢为零时暂停开仓，已有仓位仍按原止盈及风控条件退出。

每组新增 **累计成交量（桶）、累计成交额（USDC）、成交笔数**。统计双腿所有开仓和平仓：每腿 1 桶完整开平一组，共 4 桶、4 笔成交；成交额为各笔桶数 × 模拟成交价之和，不含手续费。只统计账本已记成交，不把报价、持仓估值当成交；自本次实验开始累计，不随图表时间范围改变，重启或故障重放不重复累计。

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

### 可视化监控

保持模拟进程运行，在另一个终端启动页面：

```powershell
python -m variational_grid dashboard --port 9876
```

打开 [本机监控页](http://127.0.0.1:9876/)。端口被占用时可以用 `--port 0` 自动选择空闲端口，访问终端输出的地址。使用自定义实验文件时，监控命令也加上对应的 `--experiments <文件路径>`。

- **策略对照**：0.5%、1%、2% 各自的累计损益、已实现损益、浮盈亏、最大回撤、持仓组数、累计成交额和桶数，以及实际双腿持仓金额、保证金估算、杠杆和“格数不限”状态；旧有限网格仍显示原覆盖范围，独立账户不相加。
- **两张图表**：同轴比较三组累计损益，以及 BZ−CL 价差与三日中枢。支持 1 小时、24 小时、7 天，点击图表或使用键盘采样滑块查看历史点。
- **网格与仓位**：实心格代表已有持仓，点击策略或格位筛选；列出各层 CL/BZ 方向、桶数、开仓价、开仓时间、保证金估算及浮盈亏。
- **平仓记录**：查看双腿开平价格、净损益、退出原因；可导出当前筛选结果 CSV。每策略加载最近 100 笔并分页展示，页面会标明导出条数；不是全历史导出。

页面每 10 秒更新，隐藏标签页停止轮询；暂停、过期和断线均保留上次数据并明确标注。显示北京时间，金额为 USDC、价格为 USDC/桶。时间范围以最后有效行情结束，最多绘制 900 点，降密保留区间高低点和时间缺口，累计指标不受图表窗口影响。策略、时间范围和标签页保存在 URL 中，刷新后保留。手机布局保留三组摘要、图表和按行展开的仓位。

网页进程读取本地已发布的共同采样和各组账本，并支持提交模拟重置请求，不读取登录令牌、不请求交易所、不提供下单或修改配置入口。仓位按共同采样时间截断，浮盈亏按该时刻的数量对应报价和成本估值。模拟报价仍由原进程获取，网页不需要另输入令牌。

本次实验的静态简表保存为 `data/comparison-pct-05-1-2-center3d-unbounded-grid-100x/public/index.html`，也可以从页面的“数据与模拟口径”打开。仅查看静态简表可执行：

```powershell
python -m http.server 0 --bind 127.0.0.1 --directory data/comparison-pct-05-1-2-center3d-unbounded-grid-100x/public
```

端口 `0` 会自动选择空闲端口，访问终端显示的地址即可。`public/summary.json` 提供相同结果，均不包含令牌。静态简表只展示最近 360 次采样；新监控页支持上述更长时间范围。两种页面都使用自带 SVG、无外部资源，没有根据短期结果自动挑选“最佳参数”。

共同行情、各组账本和汇总存放于同一个实验目录，完整保留共同采样记录。意外中断后重放已记录但尚未完成处理的共同行情，不会重复记账。备份时先停止进程，再整体复制实验目录；不可单独替换某一组账本。更改间距、资金、数量或组别时，在实验文件里指定新的 `output_dir`，不要混入原实验。可通过 `--experiments <文件路径>` 使用其他实验配置。

## 策略口径

`S = mark(BZ) − mark(CL)`，单位 USDC/桶。中枢 `C` 是最近 **72 根已收盘 UTC 小时 K 线**的 `close(BZ) − close(CL)` 算术平均，每小时更新一次。两腿时间戳必须完整、一一对齐；不使用尚未收盘 K 线、不补齐缺口。

| 条件 | 等桶数双腿方向 |
|---|---|
| `S − C ≥ n × 网格间距` | 卖出 BZ，买入 CL，做空价差 |
| `C − S ≥ n × 网格间距` | 买入 BZ，卖出 CL，做多价差 |
| 未偏离一格 | 不建立新仓 |

每个方向和层级最多一组持仓，一次采样最多新开一组；跳过多格时分多轮加入；`max_levels: null` 时可持续增加格位，旧有限配置仍遵守数值上限。旧方向尚有持仓时，不建立反方向的新仓。

每组独立退出：按当前双腿可执行买卖价估算，**扣除四笔模拟手续费、买卖点差和滑点后的利润达到“每腿桶数 × 开仓时网格间距”**时，双腿同时在模拟账本平仓。每组记录进场中枢；滚动中枢变化不重写成本或强制亏损止盈。平掉的层级必须先回到阈值内，才会重新允许该层级入场；同一轮平仓后不立即开仓。

买入按 `ask × (1 + 滑点)`、卖出按 `bid × (1 − 滑点)`计价；新建和退出都执行这个规则。报价请求数量等于每腿配置桶数，并检查交易所返回的数量步长、最小量和最大量。`cash_usdc` 是初始资金加已实现盈亏、减尚未平仓的入场手续费；保证金不直接扣现金。`equity_usdc` 进一步计入现有持仓按当前可平价格估值及预计退出手续费。

等桶数抵消共同的每桶价格变动，并不意味着美元名义价值完全相等，也不消除原油品种间的价差风险。

## 参数

编辑 `config.local.json`；字段详见 [config.example.json](config.example.json)。以下只是用于运行演示的默认值。

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `paper_balance_usdc` | 1000 | 独立模拟初始资金 |
| `quantity_barrels` | 1 | 每格、每腿桶数 |
| `center_hours` | 72 | 三日中枢；旧版无此字段的配置按 168 小时识别，升级时另开账本 |
| `grid_step_percent` | 1 | 三日平均价差中枢绝对值的百分比；三组对照分别覆盖为 0.5、1、2 |
| `max_levels` | null | 格数和持仓组数不限，随价差向外扩展；数值可保留旧有限网格 |
| `paper_leverage` | 100 | 保证金估算为双腿名义金额除以 100；不改变桶数、成交量或损益 |
| `max_margin_fraction` | null | 默认取消持仓金额与保证金预算限制；设为 0～1 之间的数值可启用旧版额度规则 |
| `max_drawdown_fraction` | 0.20 | 相对历史权益峰值的最大回撤；达到后模拟平仓并锁定停机 |
| `max_holding_hours` | 168 | 单组最长持仓时间；超过后按下一份有效报价退出 |
| `slippage_bps_per_leg` | 1 | 每腿每次成交滑点；1 bp = 0.01% |
| `fee_bps_per_leg` | 0 | 每腿每次模拟手续费，可自行设置 |
| `poll_seconds` | 10 | 采样周期，至少 5 秒；失败时退避最多 60 秒 |
| `max_quote_age_seconds` | 15 | 报价最多允许滞后时间 |
| `max_pair_skew_seconds` | 5 | CL 与 BZ 报价时间差上限 |

旧配置没有 `grid_step_percent` 或设为 `null` 时，仍按 `grid_step_usdc_per_barrel` 的绝对间距解释，旧账本可继续读取。百分比模式下旧绝对间距字段不参与策略计算。

价格、资金、数量用十进制计算。数据文件路径相对于配置文件所在目录解析。改变资金、数量、网格、成本或风控参数时，必须同时改用新的 `state_file`，避免重新解释旧账本；采样和报价时间限制允许调整后继续原账本。回撤锁定会持久保存，不会重启后自行解锁；另开模拟实验时换用新的账本。

## 异常和模拟限制

本节说明 CL/BZ 模式；QQQ 模式的 Maker 排队、部分成交和跨平台对冲口径见文末。

- K 线不完整、报价过期、双腿时间差过大、合约定义变化、任一市场关闭、会话失效或服务异常时暂停模拟成交。恢复有效数据后再继续；不存在用旧价止损的假成交。市场仅允许减仓时只模拟退出。
- 账本采用 SQLite 事务，双腿记账要么全部完成，要么全部回滚；同一账本只允许一个运行进程。保存每组持仓、所有成交和事件；常规行情快照只保留最近 7 日。
- 报价是 **indicative**，不是交易所承诺成交价。模拟假设双腿按对应报价成交；没有模拟真实双腿之间的延迟、部分成交、冲击、拒单和补腿风险。同一轮多组退出分别使用每组数量的报价，未估算合并退出对深度的影响。
- 盈亏字段明确标为 `before_funding`：**未计实际资金费**。杠杆保证金是本地估算，不复制交易所风险引擎、维持保证金或强平机制；这不是实盘收益评估。
- JWT 的 `exp` 仅用作本地过期提醒，签名与有效性通过服务端 `/me` 检查。会话文件每次请求重新读取，更新令牌不必重启正常循环。401/403 或过期后重新导入会话即可；首次启动验证失败会退出。

凭据仅保存于忽略提交的本地文件；POSIX 下会话文件要求 `0600`。Windows 使用当前账户的 [DPAPI 加密](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)，不接受明文会话文件，也不会在加密失败时退回明文。换账户或部署到 Linux 时应重新导入令牌，不要直接复制加密会话文件。原始 cURL 捕获文件仍由你保管，程序不会删除它。日志不打印令牌、原始服务端错误或请求头。程序不需要钱包私钥。

## Linux 一键部署与升级

适用于 Debian 12+ / Ubuntu 24.04+、systemd：

```bash
curl -fsSL https://raw.githubusercontent.com/hxx344/variational-grid/main/install.sh | sudo bash -s -- --compare
```

这一条命令会自动安装 Python 与 Git、下载并验证代码、创建独立服务账户、配置开机启动和失败后自动重启，并启动 **0.5% / 1% / 2% 三组对照模拟**。首次安装只需按提示粘贴 `vr-token`（隐藏输入），不需要钱包私钥。重复执行同一命令升级，保留配置、会话与账本。部署前后自动回收旧代码，通常只保留当前版本和一份可回退版本；网页或引擎仍在运行的更早版本会额外保留，等相关服务正常升级后再回收。新版本预检失败时不会切换正在运行的代码，未启用的失败候选与本轮临时目录自动清理。

重复部署会按变化处理，输出会说明跳过的步骤：

- 系统依赖已安装时跳过 `apt update/install`；缺少哪个包才安装哪个。
- 先查询远端提交，所需 Git 对象已在本地时跳过拉取；已有对应 release 时跳过解压。
- 服务器部署只做离线快速预检：源码语法与模块导入、示例配置、网页资源和 SQLite 支持。完整回归测试及安装器集成测试由 GitHub Actions 执行，首次安装和代码升级均不在服务器运行 unittest，也无需安装 Node.js。
- 快速预检按运行代码、部署脚本、示例配置、包配置及 Python/SQLite 环境缓存；测试和文档变化可直接复用缓存，检查失败不写成功标记。已有用户配置仍会在每次切换前验证。
- 服务配置内容未变时不重写、不重复加载；代码与配置未变、服务正常时不重启。仅网页资源或 dashboard 模块变化只重启网页服务，共用运行代码变化重启相关服务。
- 每次仍执行轻量配置校验和会话有效性检查；停止的服务会恢复启动。中断的服务配置加载会在下次执行时重试，已有配置和账本始终保留。
- 下载和解压前检查可用容量与 inode；空间不足时提前停止，不切换服务。检查保护两个服务实际运行目录、源码目录、配置及数据路径；无法确认运行目录时停止清理。未知目录、符号链接和包含挂载点的目录不删除，过期验证缓存仅在没有保留版本引用时回收。

只回收旧部署、无需升级或重启服务时，可执行：

```bash
curl -fsSL https://raw.githubusercontent.com/hxx344/variational-grid/main/install.sh | sudo bash -s -- --cleanup
```

清理不会删除 `/etc/variational-grid`、`/var/lib/variational-grid` 中的配置、会话、账本、历史实验或迁移备份。早期部署没有成功标记，首次清理会保守保留一份较新的已验证旧代码及实际运行版本；此后的新部署仅把成功完成的版本作为回退候选。现有单个 Git 源码缓存继续复用，不使用全局缓存清理。

从旧安装脚本首次升级也只执行快速预检，不再补跑全套测试；之后相同内容与环境直接跳过预检。输出会分别说明依赖、下载、预检和服务重启的执行或跳过情况。

已经下载仓库时，也可直接运行：

```bash
sudo bash install.sh --compare
```

首次安装不带参数也默认运行三组；已有安装不带参数会保留所选模式，`--compare` 明确切换为三组。实验配置放在 `/etc/variational-grid/experiments.json`，新实验结果放在 `/var/lib/variational-grid/comparison-pct-05-1-2-center3d-unbounded-grid-100x/`。升级检测到原来的 `step-0.15 / step-0.20 / step-0.25` 绝对间距配置时，自动备份为 `experiments.absolute-015-020-025.json`，直接升级为每侧 30% 的百分比配置并使用独立新账本；上一版 `step-0.5pct / step-1pct / step-2pct` 也会迁移到 60/30/15 层，原配置备份为 `experiments.before-range30.json`。旧目录和账本原样保留，资金、数量、成本与回撤规则保留；本版最终迁移为三日中枢、格数及金额不限、100 倍杠杆（见下文）。网格范围迁移仅识别上述旧默认组，已迁移后的自定义层数和金额限制不会被重复安装覆盖；用 `--single` 切回原单组模式，各自账本保留，同时停用三组网页服务。

比较模式自动安装并启动 `variational-grid-web.service`，只监听服务器 `127.0.0.1:9876`。在**自己的电脑**打开 PowerShell 或终端，替换服务器登录名和 IP 后执行：

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=6 -L 127.0.0.1:18765:127.0.0.1:9876 root@你的服务器IP
```

保持此终端打开，浏览器访问 [服务器监控页（SSH 转发）](http://127.0.0.1:18765/)。不需要域名，也不需要开放 9876 公网端口。升级已有服务器仍然只需重复上方一键命令，页面显示当前配置所指向的实验；发生策略迁移时显示新实验，旧账本仍在原目录。网页本身不要求粘贴令牌。

升级时保留原隧道，无需重复创建。Windows 需要断线自动恢复时，使用下方的 [SSH 隧道恢复脚本](#ssh-tunnel-recovery)。

- 参数：`/etc/variational-grid/config.json`
- 会话、账本：`/var/lib/variational-grid/`；可改文件名，但服务配置要求路径保持在该目录内。
- 查看日志：`journalctl -u variational-grid -f`
- 网页日志：`journalctl -u variational-grid-web -f`；重启网页：`sudo systemctl restart variational-grid-web`
- 停止：`sudo systemctl stop variational-grid`
- 修改配置后：`sudo systemctl restart variational-grid`
- 刷新会话：重复执行安装命令；有效会话保留，已过期会话重新提示输入。

首次安装出现 `Cannot read session; run init-session first` 表示服务器尚未保存会话，接下来会提示隐藏输入令牌。它不表示交易所拒绝公开行情。新版使用 `check-session` 输出简短状态；旧版的长堆栈及 Ubuntu `apport` 的 `/-c` 报错是安装预检查未捕获异常引起的，可以 Ctrl+C 后重新执行上方安装命令更新。

安装流程由现有 Ubuntu CI 运行隔离测试：使用临时目录和本地 Git 源验证首次安装、重复执行跳过耗时步骤、文档与前端更新、配置变化、模式切换、停止服务恢复、配置加载中断恢复、配置与数据保留，以及旧版回收、两个服务分别保留旧代码、空间不足提前退出和失败候选清理。存储辅助逻辑另用原生 Python 测试路径保护、挂载点和验证缓存引用。测试替代系统服务管理和远端会话检查，不访问真实账户。本地只使用 Windows 原生工具，不使用 WSL；CI 通过不代表已在你的服务器上执行升级。

## 接口依据

2026-09-19 根据 Omni 前端及当前会话验证：`GET /api/me`、`GET /api/metadata/supported_assets?cex_asset=...`、`GET /api/candles?cex_asset=...&period=1h&start=...&end=...`、`POST /api/quotes/indicative`。CL、BZ 的 instrument 均为 `perpetual_rwa_future` / `commodity` / `USDC`。

这些是当前网页使用的接口，不是承诺稳定的公开交易 API。字段变化会停止相应数据处理，需要更新适配器。官方说明：[报价、指数和标记价格](https://docs.variational.io/omni/trading/quoted-index-and-mark-prices)、[API 文档](https://docs.variational.io/technical-documentation/api)。

## 三日中枢、重置与持仓额度

执行当前 `main/install.sh` 的一键升级会把旧七日实验迁移为三日实验：备份 `experiments.before-center3d.json`，切换到新的 `-center3d` 数据目录，旧目录及模拟持仓记录完整保留。单策略模式备份 `config.before-center3d.json` 并改用新的 `*-center3d.sqlite3`。新一轮不继承旧损益，重复升级跳过已完成迁移，资金、数量、费用和格距等参数保留。最长持仓仍为 168 小时，与中枢的 72 小时窗口分别配置。

页面右上角 **重置模拟** → **归档并重新开始**，会重置全部对比策略（不受页面筛选影响）。先归档每份 SQLite 账本，再恢复初始资金、清空仓位/损益/成交量/回撤停机状态；不记作平仓成交，不改变参数或登录会话。归档位于当前 `output_dir/archives/<归档编号>/`，保留原始账本和策略身份。下一份有效行情开始新统计；市场关闭时仍可完成重置并等待开市。

网页仅排队请求，由模拟进程在完整采样之间执行。重复点击合并，旧页面代次的请求被拒绝。中断后按持久化阶段继续，归档失败不清空原数据。若模拟服务已停止，页面请求会等待其启动；也可在项目目录执行以下命令，离线时会取得同一写锁并完成重置：

```bash
python3 -m variational_grid compare-reset --experiments /etc/variational-grid/experiments.json --confirm
```

默认使用 `max_levels: null`、`max_margin_fraction: null`、`paper_leverage: "100"`：**取消持仓组数、持仓金额与保证金预算限制**。三组格距仍为三日中枢绝对值的 **0.5% / 1% / 2%**，超出原 ±30% 后继续扩展，每格一组。初始资金仍用于权益和损益计算，20% 回撤退出、最长持仓及只减仓规则继续生效。

页面显示“格数不限”“无固定覆盖范围”“金额不限”和 100 倍估算杠杆，继续统计实际双腿持仓金额、保证金和累计成交量。网格图每段最多显示 60 格，可翻页查看；这个显示窗口不限制持仓，全部持仓仍在明细分页中。

一键升级会备份 `experiments.before-unbounded-grid-100x.json`，在各场景中显式写入上述三个参数，并改用 `-unbounded-grid-100x` 后缀的新实验目录。比较模式保留共享基础配置；单组模式对应备份 `config.before-unbounded-grid-100x.json` 并改用新 SQLite。旧账本完整保留，新一轮从零统计，重复安装不反复迁移，也不覆盖完成迁移后自行设置的参数。更早版本可能先产生范围、中枢和金额迁移备份，最终以当前配置的 `output_dir` 为准。

旧配置省略 `max_levels`、`paper_leverage`、`max_margin_fraction` 时仍按 8 层、5 倍、0.80 解释，以便读取历史账本。显式设置数值时，旧额度规则为 `min(预计开仓后权益, 初始资金) × max_margin_fraction × paper_leverage`；默认旧参数对应双腿约 4,000 USDC。手动更改该字段时仍须改用新账本，不能直接改变旧持仓的计算规则。

## 库存组合模拟：五组净桶数阈值

库存模式在同一段 CL/BZ 行情下，对照 **0%、5%、10%、20% 和不限**五组库存阈值，各组基础资金、数量、费用和策略参数相同。配置见 [inventory.example.json](inventory.example.json)，`kind: "inventory"` 明确选择这套组合模拟；原有单组和三组价差网格继续保留。

库存比例按账户实际持仓的净桶数计算：`abs(qCL + qBZ) / (abs(qCL) + abs(qBZ)) × 100%`，多头为正、空头为负，空仓记作 0%。它不是按美元金额计算的 Delta，也不是把各策略虚拟持仓直接相加后显示。超过所选阈值时，两腿向同一方向调整相同桶数，以半阈值为目标并按数量步长舍入，同时保持 CL/BZ 价差敏感度；不限组不触发这项库存调整。

组合共用最近 **72 根已收盘 UTC 小时 K 线**的 CL 均价、BZ 均价和 BZ−CL 差价中枢，包含 **CL 多/BZ 空剥头皮、CL 与 BZ 各自的普通价格网格、固定 BZ 多/CL 空的反向价差网格**。先聚合各策略虚拟信号，再按账户净订单记录外部模拟成交和费用，内部抵消不重复计算成交。净收益包含未平仓浮盈亏与预计退出成本；不计资金费及真实强平，不调用实盘下单 API。

`inventory.example.json` 的默认策略参数如下，五组仅库存阈值不同：

| 参数 | 默认值 | 信号或统计口径 |
| --- | --- | --- |
| `scalp_step_percent` | 0.15% | 按 CL/BZ 各自价格幅度触发；CL 下跌新增多头、BZ 上涨新增空头，每个策略账本每次新增一份基础桶数 |
| `scalp_take_profit_percent` | 0.10% | 剥头皮持仓有利方向的 mark 价格幅度，触发虚拟退出信号；不表示净收益率 |
| `scalp_cooldown_seconds` | 60 秒 | 每个剥头皮策略账本的开仓冷却时间 |
| `ordinary_step_percent` | 0.5% | 各品种 72 小时均价的百分比；低于均价做多、高于均价做空，每层一组 |
| `spread_step_percent` | 1% | 72 小时 BZ−CL 价差中枢绝对值的百分比 |
| `spread_direction` | `long` | 固定在价差低于中枢时多 BZ、空 CL；该层价差回升一格时产生退出信号 |
| `execution_step_barrels` | 0.01 桶 | 库存调整的配置步长；实时指示性报价还会结合两品种数量 tick 和最小下单量 |
| `reset_fraction` | 0.5 | 超过库存阈值后，以阈值的一半作为调整目标，按有效步长舍入 |
| `risk_reference_percent` | 20% | 五组统一使用的库存比例超标时长统计基准，不是各组库存控制阈值 |
| `enable_scalp` / `enable_ordinary` / `enable_spread` | `true` | 默认同时启用三类虚拟信号 |

信号按 mark 价格触发，实际账户成交按对应数量的 bid/ask、滑点和手续费计算，信号退出条件不能当作净止盈承诺。实时模拟使用兼容双方 tick 且不低于双方最小数量的有效步长；因此 0% 阈值等目标可能无法精确达到，页面会保留残差并显示 `quantity_limited`。

账户总损益满足 `total_pnl_usdc = realized_pnl_usdc + unrealized_pnl_usdc`，其中未实现损益已经扣除预计退出成本，不能再重复扣减。另一种归因是 `total_pnl_usdc = direction_pnl_usdc + spread_pnl_usdc - execution_cost_usdc - exit_cost_reserve_usdc`；净订单成交成本和退出成本储备都计入组合账户。

库存模式继续不设格数、持仓金额和保证金预算上限。估算杠杆读取基础配置的 `paper_leverage`：新基础模板为 100 倍，已有配置的数值原样沿用；杠杆只影响保证金估算，不放大桶数和损益，也不代表已经设置交易所实盘杠杆。

在项目目录中，可用同一份库存实验配置启动、查看状态、打开网页或归档重置；基础配置与登录会话应先按前文准备：

```powershell
python -m variational_grid compare --experiments inventory.example.json
python -m variational_grid dashboard --experiments inventory.example.json --port 9876
python -m variational_grid compare-status --experiments inventory.example.json
python -m variational_grid compare-reset --experiments inventory.example.json --confirm
```

模拟与网页命令分别保持在两个终端运行，浏览器打开 [本地库存监控页](http://127.0.0.1:9876/)。重置作用于这份配置的全部五组，先归档原账本，再开始新一轮。

在 Linux 服务器执行一条命令，即可安装或切换到库存模式：

```bash
curl -fsSL https://raw.githubusercontent.com/hxx344/variational-grid/main/install.sh | sudo bash -s -- --inventory
```

首次创建 `/etc/variational-grid/inventory.json`，并将现有 `config.json` 复制为独立的 `/etc/variational-grid/inventory-base.json`，保存基础资金、数量、费用及杠杆。登录会话仍指向原文件；之后切换旧模式不会改变库存模拟的经济参数。独立数据目录为 `/var/lib/variational-grid/inventory-pct-0-5-10-20-v1/`。库存策略参数由 `inventory.json` 保存，不执行旧价差网格迁移，不改写旧配置和账本。修改经济参数时需使用新 `output_dir`，不能把不同参数下的结果混在原账本中；页面重置用于相同参数下重新开始。

重复执行会保留库存配置和数据；不带参数时继续运行已保存的模式。未变更的依赖、Git 对象、验证结果与正常服务继续复用；示例配置和网页资源变化会进入验证缓存判断，已有用户配置不会被新示例覆盖。`--compare` 切回原三组，`--single` 切回单组，之后仍可用 `--inventory` 返回库存模拟；各自账本保留。`--cleanup` 也会保护 `inventory.json` 指向的数据目录。

库存模式复用 `variational-grid.service` 和 `variational-grid-web.service`，网页仍只监听服务器 `127.0.0.1:9876`。在自己的电脑保持以下 SSH 转发，再打开 [库存监控页](http://127.0.0.1:18765/)：

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=6 -L 127.0.0.1:18765:127.0.0.1:9876 root@你的服务器IP
```

也可以在项目目录生成三类独立合成路径，用于检查组合成交、库存调整与损益口径：

```powershell
python -m variational_grid inventory-demo --output output/inventory-oscillation --trajectory oscillation
python -m variational_grid inventory-demo --output output/inventory-trend --trajectory trend
python -m variational_grid inventory-demo --output output/inventory-divergence --trajectory divergence
```

每次指定一个新的输出目录，分别对应震荡、趋势和两品种分化。该命令验证合成行情下的行为，不是历史回测，也不代表真实市场收益。

## Lighter QQQ 剥头皮与 US100 对冲：三组

三个独立模拟账户分别使用 **0.05% / 0.1% / 0.2%** 止盈比例，新开仓不再要求与已有批次保持价格距离；QQQ 只做多，最多 30 个占用批次，每批计划 1,000 USDC。US100 用于按需对冲，每组统一 **3,000 USDC 净敞口阈值**。参数见 `qqq-hedge.example.json`，各组独立保存两腿损益、持仓、成交数量和成交额。`grid_step_percent` 保留用于历史配置兼容及缺省 TP，v2/v3 不用它判断新开仓资格。

新模型 `perp_dex_scalper_v3` 保留 v2 取消开仓距离门槛的规则，并修复止盈单被严格 Maker 限制拦截的问题。开仓、等待与逐批止盈参考 [perp-dex-tools 的 TradingBot](https://github.com/your-quantguy/perp-dex-tools/blob/4679a339b8cdc9998707feeda3c5d8b84fb8681f/trading_bot.py) 和 [Lighter 适配器](https://github.com/your-quantguy/perp-dex-tools/blob/4679a339b8cdc9998707feeda3c5d8b84fb8681f/exchanges/lighter.py)：一次只挂一张近盘口开仓单，成交后逐批挂独立止盈。30 指最多占用批次，并非同时预挂 30 张买单。下一笔开仓价根据当时盘口计算，批次编号单调递增。

基础等待 `scalper.wait_seconds=450`，从上一笔开仓完成（或部分成交撤单确认）开始计时。未平仓批次数为 0–4 / 5–9 / 10–19 / 20–29 时，等待分别为 **112.5 / 225 / 450 / 900 秒**；等待时间需严格超过该值。若未平仓批次数比上一次开仓决策减少，本轮跳过冷却；v2/v3 无价格距离资格检查，但仍需行情可用、已有批次有止盈单及 Maker 挂单条件成立。达到 30 个占用批次暂停加仓。

取消资格门槛不改变挂单取价，`tp` 是止盈百分比除以 100：

```text
新开仓资格：不检查与已有批次的价格距离
候选开仓价：min(盘口中价, 所有已有止盈价 − 一个 tick)
每批止盈价：实际开仓限价 × (1 + tp)，向下取整到 tick
```

候选开仓价按 tick 四舍五入，再限制到 `ask − tick`，保持 Maker。未成交开仓单在提交 20 秒后首次检查，随后每 5 秒检查：候选价格上涨则撤单，撤单生效后重新判断入场；持平或下降则继续等待。部分成交先保留剩余开仓单，完全成交或撤单确认后，仅为实际成交数量挂止盈。行情恢复时重建止盈，不补造缺口期间的成交；止盈暂时无法挂出时暂停继续加仓。

原适配器实际使用 GTT 限价单。**v3 开仓保留 Maker 模拟，止盈允许作为普通限价单成交**：确认开仓完成或部分成交撤单后提交 TP，等待提交延迟结束且盘口源时间晚于提交时间的新盘口，再按不低于止盈限价的可见买盘从高到低逐档模拟成交；同账户各 TP 共用本帧深度。剩余量进入 Maker 队列，后续由公开逐笔驱动，不反复扫描盘口；新 TP 不使用提交帧的旧逐笔或缓存盘口成交。成交记录区分 Maker/Taker、价格及盘口来源时间，手续费沿用配置的 `lighter_fee_bps`，未单独区分费率。低于最小数量或金额的首次 TP 会保留库存、明确显示原因并暂停加仓；不假定小额平仓可豁免交易所限制。已合法提交的 TP 部分成交后，即使余量低于新单门槛仍继续挂单。三组最多各占用 30 批库存，满批计划入场金额约 30,000 USDC，市值随价格变化，US100 对冲金额另计。旧配置未指定 `scalper` 时继续按旧固定锚点模型读取和运行。

历史 v1/v2 保留严格 Maker 成交模型。`perp_dex_scalper_v1` 保留原距离规则：有未平仓批次时要求 `min(已有止盈价) / [当前 ask × (1 + tp)] > 1 + step`，失败会消耗本轮冷却豁免；未指定 `scalper.model` 的已有剥头皮配置仍解释为 v1。新旧模型不能直接续写同一账本。

对冲按美元净名义金额计算，数量带方向：

```text
A = beta × QQQ数量 × QQQ盘口中价
N = A + US100数量 × US100标记价
|N| <= 3000：保持现有US100仓位
|N| > 3000：目标净敞口 = sign(N) × 1500
目标US100数量 = (目标净敞口 - A) / US100标记价
```

默认 `beta=1`，重置比例 `hedge_reset_fraction=0.5`。数量按市场步长取整，面板显示真实残差。QQQ归零后也遵循同一金额规则，可能保留阈值内的US100空头；这不表示组合已完全中性。历史九组配置仍支持原来的净/总敞口百分比规则，两种配置不能混在同一实验中。

US100 模拟买卖价按**半点差0.0015%（0.15 bps）**计算，全点差0.003%；额外滑点默认0，避免重复加上原先的1 bps：

```text
参考中价 = (源bid + 源ask) / 2
模拟买入价 = 参考中价 × 1.000015
模拟卖出价 = 参考中价 × 0.999985
```

两边手续费默认0，可配置。估值与对冲目标仍使用源报价的mark，成交使用上述买卖价。QQQ与US100的跟踪误差、交易时间和报价延迟会影响组合表现。未计实际资金费、隔夜费、股息调整或强平。

### 持久报价与刷新

QQQ模式只读Lighter公开盘口/成交，Variational 切为携带 **vr-token** 的 `POST /api/quotes/indicative`，需要有效登录会话，无需钱包私钥。三个账户共用一份0.01 index unit参考报价，并按各自数量估算模拟价格；不是每笔数量分别获得的交易所成交价，也不模拟随下单规模变化的额外价格冲击。

当前共享报价模式的 Var 请求只有两类，网页刷新只读取本地账本：

| 请求 | 用途 | 默认频率 |
| --- | --- | --- |
| `POST /api/quotes/indicative`，`US100S`、`qty=0.01` | 三组共用的指示性参考价与数量限制 | 源报价超过 3 秒时，在下一次采样尝试刷新；默认 2 秒采样通常约 4 秒一次，网络延迟和报价时间会影响实际间隔 |
| `GET /api/metadata/supported_assets?cex_asset=US100S` | 合约定义、交易时段、休市和只减仓状态 | 报价前检查，本地正常缓存约 30 秒；不会为每组分别请求 |

模拟开仓、止盈及 US100 对冲不会发送真实订单；共享参考价也不会按三组各自成交数量追加询价。运行时不轮询 `/api/me` 或 `/api/candles`，安装时用 `/api/me` 验证保存的会话。Metadata 同样经固定 Var 域名携带会话读取；它仍属于公开合约信息，不作为成交价格。下面的请求间隔是程序自身预算，不代表平台公布的 API 限额。

`pricing.refresh_after_seconds=3`：源报价年龄不超过3秒直接复用，超过3秒尝试更新。`pricing.max_age_seconds=60`：刷新遇到排队、429或网络失败时，默认最多使用60秒缓存，随后暂停新网格和对冲。原始报价写入 `quote-cache.json`，重启、升级和重置不改写源时间。行情已知休市、metadata超过120秒或到达休市时间时，缓存也不可用于模拟成交。QQQ盘口和逐笔时效要求不放宽。旧公共报价缓存不再用于新成交；每次启动或更换 token 后须先取得一份有效鉴权报价，才允许复用持久缓存。token 缺失、过期或 HTTP 401/403 时缓存也停用，暂停新开仓与模拟对冲，已有 Lighter 订单的已确认模拟成交继续记账。不会回退到公共报价。

HTTP冷却独立于缓存可用性。每次Var请求占用3秒预算，429后6秒；遵守 `Retry-After`，失败至少60秒退避，连续429按60/120/240/480/900秒增加，平台要求更久则等更久。冷却持久保存，换到新三组账本会继承旧冷却，不绕过限流，切换 token 通道也不代表获得限流豁免。账户不会为每个微小数量变化再发一次请求。

页面显示源报价年龄、是否使用缓存、刷新失败原因；有效缓存存在时模拟可继续。US100成交时间是本轮模拟时间，另存原报价时间、源数量、源bid/ask、半点差及缓存标记，CSV同样保留这些字段。参数页显示当前报价口径开始时间与累计缓存估价次数。报价年龄和费用口径影响模拟损益，不能将这些结果当成新鲜按量报价的实际成交表现。

### 一键部署与升级

```bash
curl -fsSL https://raw.githubusercontent.com/hxx344/variational-grid/main/install.sh | sudo bash -s -- --qqq-hedge
```

单独更新行情会话不会重置账本；本次 v3 升级改变止盈成交口径，识别到默认旧配置时会按下文迁入新一轮模拟。安装器复用 `/etc/variational-grid/config.json` 的 `session_file`（默认 `/var/lib/variational-grid/session.json`），缺失或过期时在服务器终端隐藏输入 vr-token。运行中更新该文件后自动恢复，无需重启。

配置在 `/etc/variational-grid/qqq-hedge.json`，首次安装默认数据目录 `/var/lib/variational-grid/qqq-hedge-scalper-v3/`。识别到默认 v1/v2 剥头皮三组，或历史默认九组/三组固定锚点配置时，安装器备份原配置为 `qqq-hedge.before-scalper-v3.json`，将原目录名加上 `-scalper-v3` 后开始支持 GTT 止盈的新三组模拟。旧配置、旧持仓与损益账本、已有 v1/v2 备份和 CL/BZ 数据均原样保留；新一轮从空仓与零统计开始，仅继承有效鉴权报价缓存和限流冷却。自定义时序、TP、组合或经济参数不会被自动覆盖；自定义配置需保留原参数，将 `scalper.model` 改为 `perp_dex_scalper_v3` 并指定新的空 `output_dir` 后再运行。重复执行采用增量更新，未变化时复用代码和验证结果，不重复重启；服务器仅执行快速离线部署检查，完整测试在 CI 执行。

QQQ 监控页右上角的 **更新 Var token** 可直接粘贴新令牌。页面使用遮挡输入，服务先向固定 `/api/me` 验证，成功才原子替换受保护会话文件；失败保留原会话。保存后策略自动读取，无需重启或重置模拟。输入不会写入浏览器存储、URL 或日志。此操作仍通过 localhost / SSH 转发访问；服务仅接受同源及页面随机校验令牌，不接受自定义路径或接口地址。每次手动提交最多一次会话验证，不随页面刷新询问 Var。

网页沿用服务 `variational-grid-web.service`，监听服务器 `127.0.0.1:9876`。在自己电脑保持SSH转发：

```bash
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=6 -L 127.0.0.1:18765:127.0.0.1:9876 USER@SERVER_IP
```

浏览器打开 [监控页](http://127.0.0.1:18765/)。各账户图表、两腿损益与成交量分别显示；新三组的敞口图单位为USDC，阈值线为±3000，旧百分比实验保持原单位。剥头皮卡片显示下一次开仓的冷却进度、百分比与剩余秒数，按各组当前档位的总等待时间计算，正常运行时每秒推算，无额外行情请求。100%仅表示冷却已到，实际开仓仍需策略采样确认批次容量、行情与 Maker 条件；历史 v1 另检查价格距离。已有开仓单时显示处理中，不套用上一轮冷却。行情过期、服务停止或页面断连时恢复最后采样值并标注暂停推算。v2/v3 明确显示门槛已取消，并按三档止盈目标命名账户；历史页保留原模型语义。

```bash
python -m variational_grid compare --experiments qqq-hedge.example.json
python -m variational_grid dashboard --experiments qqq-hedge.example.json --port 9876
python -m variational_grid compare-status --experiments qqq-hedge.example.json
python -m variational_grid compare-reset --experiments qqq-hedge.example.json --confirm
```

服务器命令将实验路径换成 `/etc/variational-grid/qqq-hedge.json`。重置先归档当前全部账户账本，不产生模拟平仓。修改间距、止盈、开仓等待、金额阈值、费用或半点差须使用新的 `output_dir`；刷新周期与缓存上限可调整，逐帧记录实际采用的口径。需复查旧模拟时，可在另一个端口以备份配置启动 dashboard。

<a id="ssh-tunnel-recovery"></a>
### SSH 隧道断线恢复（Windows）

`client_loop: send disconnect: Connection reset` 表示 SSH 传输连接被重置，需要重新建立连接。它与网页服务重启期间的 `channel ... connect failed: Connection refused` 不同：后者通常只影响一次转发请求，SSH 进程仍在，网页会继续重试。根据 [OpenSSH 文档](https://man.openbsd.org/ssh_config#ExitOnForwardFailure)，`ExitOnForwardFailure` 不会因转发目标暂时无法连接而关闭已有 SSH 连接。安装脚本仅重启发生变化的模拟及网页服务，没有重启 SSH 或网络服务的命令；历史重置原因仍需结合断线时的服务器和网络日志判断。

`scripts/dashboard-tunnel.ps1` 在本机保持 SSH 保活，对明确的连接重置、超时或传输关闭按 5／10／20／40／60 秒重连，最长退避 60 秒。身份验证、主机指纹、端口占用及未知错误会停止并显示原因。保活不能保证网络不断线；此脚本负责断线恢复。网页服务尚未启动时，隧道可能已经建立，但页面仍需等服务恢复。

在**自己电脑的 PowerShell** 执行，替换 `USER@SERVER_IP`。下面保留本地 **9876**，浏览器继续访问 `http://127.0.0.1:9876/`；如果原本使用本地 18765，将末尾端口改为 18765。首次换用脚本前，先在旧隧道窗口按 Ctrl+C；脚本不会关闭占用端口的其他进程。

```powershell
$tunnelScript = Join-Path $env:TEMP 'variational-dashboard-tunnel.ps1'; Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/hxx344/variational-grid/main/scripts/dashboard-tunnel.ps1' -OutFile $tunnelScript; powershell.exe -NoProfile -ExecutionPolicy Bypass -File $tunnelScript -SshHost USER@SERVER_IP -LocalPort 9876
```

已有本地仓库也可直接运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dashboard-tunnel.ps1 -SshHost USER@SERVER_IP -LocalPort 9876`。SSH 别名、认证方式和服务器端口沿用用户已有 SSH 配置；特殊 SSH 端口可加 `-SshPort 2222`。不修改全局执行策略或 SSH 配置，不保存密码或私钥，不关闭主机指纹检查。密码、MFA 或腾讯云微信扫码登录在重连时可能需要再次操作；只有服务器允许密钥或 ssh-agent 无交互认证时，才能完整自动恢复，脚本不会绕过登录要求。保留窗口，Ctrl+C 停止脚本；此本机改动无需重新部署服务器。
