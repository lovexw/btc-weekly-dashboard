# 比特币周报 · 五仪表盘

每周看一次的比特币数据网站。明亮、可部署、纯标准库(无第三方依赖)。

**在线版**: 部署后为 `https://btc-weekly.pages.dev`(Cloudflare Pages)。

## 每周流程(约 30 分钟)

1. **更新价格**: 下载新的日线 CSV 放进 `~/Downloads/`(文件名含 `btc-price` 即可), 或什么都不做——脚本会自动从 CoinMetrics 补齐缺失日期。
2. **记一笔 ETF 净流入**: 在 `etf_flows.csv` 追加一行 `日期,净流入(百万美元)`。可以直接在 GitHub 网页上编辑提交(推荐, 手机也行)。
3. **刷新**: 本地双击 `刷新周报.command`; 或推送到 GitHub 后由 Actions 自动重建(每周一 09:00 北京时间自动跑, 也可手动触发)。
4. **看报告**: 打开 `index.html`(本地) 或 pages.dev 在线版, 按 **1→3→2→4→5** 顺序过五张仪表卡, 对照"姿态参考表"。

## 五个仪表

| # | 仪表 | 管什么 | 数据源 |
|---|---|---|---|
| ① | 全球美元流动性 | 方向 | FRED: WALCL / M2SL / DTWEXBGS |
| ② | ETF 净流入 vs 新增供给 | 幅度 | 手工记录 etf_flows.csv |
| ③ | 稳定币总市值 | 节奏 | DefiLlama |
| ④ | 矿工 / 交易所 / MVRV 位置 | 尾部 | blockchain.info + CoinMetrics |
| ⑤ | 幂律分位走廊 (估值) | 赔率 | 自建模型(创世日锚点) |

页面上的每张卡都带"一句话人话"和"为什么看它"的折叠解释, 底部有完整名词表。

## 部署到 Cloudflare Pages

方式 A(推荐, 零命令行): Cloudflare Dashboard → Workers & Pages → Create → Pages → **Connect to Git** → 选本仓库 →
Build command 填 `python3 build.py`, Build output directory 填 `/`。之后每次 push(含每周 Actions 的自动重建提交)都会自动部署。

方式 B(命令行): `wrangler login` 后运行

```bash
wrangler pages project create btc-weekly --production-branch main
wrangler pages deploy . --project-name btc-weekly --branch main
```

全自动(可选): 在 GitHub 仓库 Settings → Secrets 添加 `CLOUDFLARE_API_TOKEN` 和 `CLOUDFLARE_ACCOUNT_ID`,
 weekly workflow 会在每周重建后自动把新页面推到 Cloudflare。不配置也不影响, 只是部署需要手动/走方式 A。

## 仓库结构

```
build.py            # 一键构建: 抓数据 → 算指标 → 生成 index.html (纯标准库)
index.html          # 生成的自包含网页(提交进仓库, CI 每周更新)
etf_flows.csv       # 每周手工记录 ETF 净流入
data/btc_price_used.csv   # 价格基线(CI 环境无 Downloads 时从此文件 + CoinMetrics 补齐)
刷新周报.command     # macOS 一键刷新
.github/workflows/weekly.yml  # 每周自动重建(可加自动部署)
```

## 说明与限制

- 全部数据源为免费公开接口, 无需 API key; 抓取失败自动回退最近缓存(卡片标注数据龄)。
- CoinMetrics 社区版有数周~数月滞后, 卡片上标注数据截止日; LTH 供给等深度链上指标需付费数据源, 未接入。
- 走廊为全样本拟合, 用于估值定位; 方法论与样本外检验详见对话记录。
- 所有内容为数据整理与研究参考, **不构成投资建议**。
