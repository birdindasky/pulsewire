# pulsewire 架构(当前真相)

> 这是描述系统**当前实际形态**的唯一架构文档(2026-06 立、events 引擎/重磅度闸/语义问答/GitHub 榜 v2/话题闸陆续上线;2026-07 换剪报本视觉 + 开源 MIT + 跨平台文档化)。早期的 VPS/Jina API/即时推等设想已作废,本文只写现在跑的样子。
> 配套:`DESIGN.md`(所有"为什么这么设计"合订本)· 仓库根 `STYLE.md`(视觉规范)。

## 一句话
pulsewire = 本地新闻情报引擎:抓多领域信源 → asyncio 并发 → 本地 embedding 语义去重 → 富化挂"可回源数字" → 兴趣召回+精排 → 双层总结(数字回源对账,**0 编造**)→ 出图 → 交付飞书/微信/网页 App。单体异步引擎,跑在**本地 Mac($0)**,launchd 每日触发。

## 蓝图:单体异步引擎
一个 Python async 程序 + 一个 Docker 容器(postgres)。Postgres(pgvector)存一切;模块边界按流水线一站一模块切干净。理由:单人维护、可调试性优先。(否决队列/编排框架=对个人用过度。)

## 流水线(10 站,顺序即数据流)
```
sources.yaml → fetch(并发) → dedup(本地向量) → enrich(挂数字+source_id)
 → rank(召回+热点直通 → 规则 → LLM主编精排 → 限额) → transcript(入选条目抓全文)
 → summarize(双层 headline/tldr/insight + {Fn}占位) → verify(数字回源对账)
 → github_board(开源热榜,独立伪兴趣) → threads(跨天归线,自吞异常不拖垮日报)
 → render(两张PNG) → deliver(飞书/微信/webapp)
每站结果 + 检查点入 PG;崩了从最后完成的站续跑(runs.stage,各站幂等)。
fetch/dedup/enrich 全局一次;rank/transcript/summarize/render 按 `run.domains` **逐领域各跑一遍**
(主领域 AI 失败冒泡、次领域 bio/geo 失败告警+跳过不拖垮主报);deliver 聚合各领域出一份 App。
```
- **CLI**:各站可单跑(`pulsewire fetch/dedup/enrich/rank/transcript/summarize/render/deliver`);`run` 串全程 + 检查点续跑 + 失败告警;`schedule` 生成 launchd plist。GitHub 热榜 + threads 归线站跟在 `run` 里;`pulsewire threads --rebuild [--days=N]` 从归档重放重建跨天线(独立 CLI)。

## 模块
- **config** — pydantic-settings;`config.yaml`(全局,env `PULSEWIRE_<段>__<键>` 可覆盖)+ `sources.yaml`(信源注册表,331 源:AI/生物医疗/国际局势/GitHub)。
- **sources/fetch** — 适配器(rss / HN-Algolia / GitHub-API / file://)+ 共享 httpx 中间件(重试/限速/ETag/SSRF防护/浏览器UA;源可 `user_agent`/`max_items` 覆盖)。
- **dedup** — 三级:URL规范化 → 内容指纹 → **本地 Qwen3-Embedding-0.6B/MLX**(1024维 L2归一,跑 Apple GPU/Metal,$0离线,`~/.cache/huggingface` 约633MB)语义近重复,阈值 **0.87 宁漏勿误**。**2026-06-23 从 jina-v3 迁来**(治 jina CPU 冷加载烤机卡死;`provider=mlx`,5 阈值重标,详见 `DESIGN.md`)。**跨平台**:`provider=local`(fastembed/ONNX,默认 jina-embeddings-v3)纯 CPU 跑,非 Apple Silicon 走这条(见 README「系统要求」)。
- **enrich** — 按源富化:HN points/评论、GitHub stars/forks → 挂 `value + source_id` 进 `facts.enriched`;正文 trafilatura(默认关)。
- **rank** — **双引擎,`rank.engine` 开关切换**:
  - **`events`(2026-06-19 切上线,当前默认)**——选稿核重做(详见 `DESIGN.md` §1 五柱):**事件为一等公民、热度作主排序轴、相关性仅做闸、新鲜度硬窗、单一全局池一事一卡**。流程=近窗簇 → **A 候选**(事件主体短语近邻 cosine ∪ 词法 Jaccard)+ **B 判官**(LLM 判同事件,**并发**,rank 25→9.6min)聚类 → 打分(log1p 加权源族 × velocity + 量级地板)→ 两道硬门(相关性闸 τ_rel / 新鲜度硬窗:bio/geo 48h、**AI 板 2026-06-28 起 144h 按域放宽**)→ 按源领域分板块(**综合源另走 `board_classifier` 读内容判板**,2026-06-28 上线开启治综合源乱跑,拿不准/非本领域就丢;**board_only 项目源〔GitHub 榜〕绝不进新闻板的成员/代表**)→ **话题闸**(2026-06-25 上线、**2026-07-01 加 3 票多数票**治单判跑题混入,`topic_judge.py`:判事件主题属不属于本板块、**只粗砍跨领域跑题**〔电视剧/航空/地缘串进 AI 板〕、沾边/拿不准/本领域商业金融政治角度全留,喂事件全文+板块画像判归属**不用余弦**,命门零误杀,`topic_gate_enabled` 开关)→ **重磅度闸**(2026-06-22 上线,`magnitude_judge.py`:真 B 档语义判官砍论坛闲聊/花边水货、留分析/预测/深源科普,移除不截断、回填靠 quotas,**6-25 改 3 次多数票治 flash 抽风偶发漏判**,`magnitude_gate_enabled` 开关回滚)→ **要闻够格闸**(2026-06-29 上线,`worthiness_judge.py`,纯优先「够格才上、不硬凑,少报也不塞边缘货」;不够格踢、预算耗尽 fail-closed,`worthiness_gate_enabled`)→ 限额(含同事件去重:共享显著实体即送判官,判同才折叠)→ `rankings`(同表口径,下游零改动)。治好"抓不到当下最热 / 全年度大名 / 同事件重复刷屏 / 前十混水货 / 跨领域跑题串板"。
  - **`legacy`(旧引擎,留作一行回滚网)**——兴趣→标签→embedding 召回 + 热点直通 → 领域夹回 → 规则粗排 → LLM 主编精排 → 新鲜度门 + 限额 + 同事件去重 → `rankings`。events 出问题改回 `legacy` 即恢复。
- **transcript** — 对精排入选条目抓网页正文/逐字稿(trafilatura)喂总结提质。
- **summarize** — 每条目产 **headline(带钩子标题)+ tldr(一句速读)+ insight(400-700字白话深度解析)**;**模型只见 label、数字走 {Fn} 占位**(从机制上 0 编造)。**分块**(`batch_size`/调用,+`max_tokens` headroom)防单次响应超模型输出上限被截断成坏 JSON;某块重试耗尽→跳过+`prune_summaries` 清旧总结(不冒充)、全块失败才冒泡。
- **verify** — 数字回源对账:{Fn} 换库内真实值;无来源裸数字标 `[待核实]`;headline/tldr/insight/digest 全过验数 + **破文本残留消毒**(`scrub_residual_markup`,6-25:render 后清 LLM 漏的字面花括号 `{…}`/损坏 token `F\d+S\d+`→`[待核实]`,治 06-16 `{F}` 破文本的新变体)。**高风险定性断言闸门**(2026-06-12):四类关键词(上市/IPO/估值、倍数话术、临床突破、战报/制裁)单源命中即 needs_review,多源同报(佐证=max(簇内源数,事件热度)≥`summarize.risk_min_sources`)放行;刻意不含裸"融资/收购"防徽标噪音。**LLM 断言审计治本层**(`summarize/audit.py`):单源且关键词放行的条目,独立一次 LLM 调用按事实核查员视角复审"传闻当事实",只拉 ok→needs_review;失败告警降级,不拖垮主报。
- **github_board** — 开源热榜:AI 源带 stars 的 repo,**按真·近期涨速排序**(v2 Phase A,2026-06-24:`_recent_velocity`=(当前 stars − 上一份快照 stars)/ 隔的天数,读 `item_timeline` 跨天快照;治"老仓翻红"——老巨仓被一辈子均速稀释、近期猛涨却能冒头;冷启动无历史则退回 `_star_velocity` 星÷仓龄,同单位 stars/day 平滑兜底)+ **名 token 主题去重**(`_name_tokens` 贪心:同生态变体如 hermes-agent/-desktop/-studio 只留涨最快的一个)取 top N(`github_board_exclude` 排除指定 repo;**跨板块同项目去重**:别领域已选的 github URL repo_key,**+ 6-25 加"成稿名 token 子集判同项目"**——拿其它板成稿标题词集 × repo 全部显著名 token,治新闻板讲某 repo + 热榜又单列同 repo〔如 Omnigent〕,⚠️须全部命中防 `deepseek-reasonix` 被新闻公司名误剔)。**快照扩到整个候选池**(非只 top-N,治"没上榜→永无历史→翻不了红"鸡生蛋)。源 = 6 条 `type:github` 搜索(ai-agent/coding-assistant/llm + v2 Phase B1 加 rag/llmops/mcp);`github_token`(keychain service=`GITHUB_TOKEN`)认证提搜索限速 10→30/min。复用 summarize(**6-25:给 github 条目专属写稿口径——老项目写"近期热门"不写"刚发布",治冒充新事**),固定伪兴趣 `ghboard`。
- **threads(跨天事件线)** — summarize 之后、render 之前归线(方案 B+A,详见 `DESIGN.md` §4):对今天进日报的簇,**A 层** LLM 抽「事件主体」短语(flash 档,跨语言英文锚点)预过滤候选在追线 → **B 判官** LLM 判接哪条线还是新开 → 写 `thread_clusters`(挂载即判定日志,冻结当天 headline/url/source/progress_date 支撑可重放)+ 刷新线的现状一句话/热度/活跃度,超期线转 dormant。**自吞异常**:失败告警但不拖垮日报。`pulsewire threads --rebuild` 从 `web/archive/daily/*.json`(35 天耐久史料,因 summaries 每跑被删)重放重建跨天线。配置 `threads.min_days`(露出门槛 2)/`dormant_after_days`(7)/`rebuild_concurrency`(8);热度反哺精排开关 `weights.thread`(0.1,0=关)。
- **render** — Jinja2 → 无头 Chrome(playwright)→ 剪报本涂鸦风 PNG:详读长图 + 速读卡(见 `STYLE.md`)。
- **deliver** — 飞书 webhook 卡片(主,仅主领域 AI)/ 微信 Server酱(best-effort)/ 网页 App(零后端 SPA,**聚合四领域**进一份 index.html,数据内联 file:// 可看);投递幂等键 `主领域 interest_key:date + channel + daily`(飞书/微信一天一份)。**webapp 已豁免幂等**(commit `079f980`):本地文件零副作用 → **始终重写**(`_ALWAYS_REWRITE`,不 record_delivery),当天重跑自动刷新页面 + 档案;仅飞书/微信仍按天幂等。
- **qa(语义问答翻历史)** — 非流水线站,按需查询(`pulsewire ask "问题" --json` + 桌面 App 档案页"问历史"面板)。RAG+引用式:`embed_query`(Qwen3 检索指令非对称)→ `recall_cards_by_vector`(查 `summaries.card_vec`,含软删卡=历史主体,硬过滤 `produced_by='pulsewire'`)→ LLM 只据召回卡答 + 每句标源 `[n]`。**零编造**(同数字回源基因):召回空→"没找到"不调 LLM、LLM 挂→报不可用、引用卡号越界→降级,全不编。card_vec 增量已接日报管道(`run_summarize` 写卡当场算,失败降级留空)。详见 `DESIGN.md`。
- **obs / schedule / run** — structlog + 每阶段指标 + 失败多通道告警;launchd plist 生成;run_pipeline 编排 + 检查点。LLM 计量基线(`obs/meter.py`);**降本增效线**(2026-06-22:summarize 写稿并发 463→127s 省73% + rank 抬并发 6→10 省39%,整跑 ~32→21.5min;只碰水电不碰内容)。

## 网页 App(零后端多视图 SPA,`deliver/webapp.py`)
- 顶栏 tab:今日 / 收藏 / 在追 / 检索。
- **点「今日」出下拉栏,四大领域:AI / GitHub / 生物医疗 / 国际局势**,**全部真内容**(各领域独立兴趣各 ~20 条;数据驱动:`DATA.domains` 列表驱动下拉,有内容才出现;跨领域 `_uid` 防 id 撞)。
- 详情=大编号+大标题+完整 insight+读原文+收藏+上下条(返回直接回主页);检索字面高亮;收藏 ★ 存 localStorage;GSAP 入场/悬停/ticker。
- **在追(事件线)= 已上线**:纵向时间轴卡(主体 + 现状一句话 + 日期→粉点→headline 演进轴,点 headline 开原文),达露出门槛 `threads.min_days`>=2 个不同日期才出;每卡右上 ✕「取消追踪」纯本机 localStorage 隐藏(后台仍照常归线)。**全史归档**已上线(`web/archive/`,跨天检索 headline/tldr/source)。FlexSearch / 人物访谈雷达仍 v2。

## 数据模型(PG + pgvector,SQLAlchemy async + Alembic)
`items`(确定性 item_id)· `clusters`(去重归簇,source_count=大事信号)· `embeddings`(vector(1024) + hnsw)· `rankings`(每兴趣精排)· `summaries`(tldr_*/insight_* 各 raw+rendered + 对账状态)· `digests` · `deliveries`(幂等唯一键)· `runs`(阶段检查点)· **`threads`(事件线:subject/status/summary/heat/domain/时间跨度)+ `thread_clusters`(线↔簇挂载即判定日志,留 subject/link_reason/confidence/run_id + 耐久落痕 headline/url/source/progress_date)** · `item_timeline`(每跑落 GitHub repo stars 快照,近期涨速排序用,v2 后扩到候选池全量)· `people`(v2 占位)· **events 引擎 5 表**(`events`/`event_members`/`event_heat_trace`/`repo_key`/`repo_timeline`,`rank.engine=legacy` 时不写;⚠️ `repo_timeline` 是死表没人写,GitHub 涨速实际用 `item_timeline`)。`summaries` 加 **`card_vec vector(1024)` + `produced_by`**(语义问答召回,迁移 0010)。当前迁移 head=**`0012_ranking_meta`**(…0009 events → 0010 card_vec → 0011 judgments 判决缓存 → 0012 rankings.meta 已剪记忆随行)。

## 数字回源对账(命根子,别破)
旧版踩过"数字编造/自我背书"的坑,这是 pulsewire 存在的理由:
- 富化数字带 `source_id`(`item_id:fact_type:field[:序号]`,来自入库事实,**不让模型生成**);模型只见 label + `{Fn}` 占位 → 编不出数字。
- verify 用库内真实值替换占位;正文逐字出现过的数字放行;其余裸数字(headline/tldr/insight/digest 全查,`%` 不剥)无来源→`[待核实]` + needs_review。
- 出图/推送**只用对账后成稿**(`*_rendered`);needs_review 标徽标不静默当真。
- (2026-06-10 codex 复审堵过三个洞:headline 漏查、`%` 被剥、digest 未验数。改 summarize/verify 时务必保此铁律。)
- **定性强断言同理**(2026-06-12 双审计后加):没有数字 fact 的大话("申请上市"传闻/"性能翻倍"营销/战报伤亡)单源不放行——verify 关键词闸门标待核实 + LLM 断言审计逮换措辞的漏网之鱼(独立调用防自我背书,只收紧不放行),summarize 提示词强制单源写『据报道』、newsletter 条目只总结主新闻(防多话题简报夹带跨域杂讯)。

## 锁定的决策(当前)
| 项 | 现状 |
|---|---|
| 部署 | **本地 Mac $0**,launchd 每日触发一次完整流水线;电脑睡就睡。(弃 VPS) |
| "大事" | 每日日报内的**显著度信号**(簇 source_count / 事件热度 ≥N → 标重点);**不单独高频即时推**。 |
| embedding | **本地 Qwen3-Embedding-0.6B/MLX**(Apple Silicon 默认,跑 Apple GPU,$0 离线)。**非苹果**切 `provider=local`(fastembed/ONNX jina-v3,纯 CPU 跨平台)。 |
| LLM | 全程 **DeepSeek**(litellm)。**2026-06-29 起判官+精排统一 `deepseek-v4-pro`**(治 flash 抽风,commit 3dbf7af):总结/断言审计/水货闸/话题闸/分板/够格闸/同事件去重/精排全 pro;**唯主体抽取 `subject_model` 仍 flash**(省档、抽主体简单活)。prompt caching = DeepSeek 服务端自动前缀缓存(命中 ~67-73%)。⚠️ pro 是推理模型,偶发 `finish_reason=length` 截断/返空 → 打 `llm.truncated_or_empty` 告警(下游各闸脏返回兜底接住)。 |
| PG | **永久全留** + 定期备份。 |
| 兴趣范围 | **多领域**:AI / 生物医疗 / 国际局势 各一份独立兴趣 + GitHub 热榜,聚合进 App「今日」下拉。源 331 个(AI/bio/geo + GitHub 搜索;v2 将扩 7 板块)。隔离=轻量无迁移(Source.domain + rank 内存过滤)。 |
| JSON Schema 失败 | 重试 → repair → 仍失败冒泡降级(标[待核实],不崩、不静默产空)。 |

## 铁律(别违反)
- **失败要冒泡**:任一站失败要记录+告警+可续跑,**绝不静默产空日报**。
- **数字回源**:见上。
- **视觉锁定**:改 PNG/App 视觉先读 `STYLE.md`,再同步 `render/templates.py` + `deliver/webapp.py`。
- **密钥**:全走 env / `.env` / Keychain(DeepSeek 走 `resolve_deepseek_key()`→Keychain service=`AI_API_KEY`),绝不进仓库。
- **新增推送通道真发** = 需自配 key + 知情同意;默认交付只写本地网页/档案,飞书推送是可选件。

## 技术栈
httpx+asyncio · feedparser · trafilatura · playwright(chromium)· **mlx-embeddings(本地 Qwen3-0.6B,Apple GPU)**· PostgreSQL+pgvector · SQLAlchemy(async)+Alembic · litellm(DeepSeek)· Jinja2 · pydantic-settings · structlog · uv · Docker(仅 postgres)· Electron(桌面 App,含问答面板)。
