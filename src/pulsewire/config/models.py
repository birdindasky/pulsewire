"""数据契约:全局配置与信源注册表的 typed 模型(pydantic)。

阶段 0 只覆盖配置层;items / source_id / cluster_id 等流水线数据契约在阶段 1 落表时补全。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------- #
# 全局配置(config.yaml)                                                       #
# --------------------------------------------------------------------------- #
class AppCfg(BaseModel):
    name: str = "pulsewire"
    environment: Literal["dev", "prod"] = "dev"
    timezone: str = "Asia/Shanghai"


class DatabaseCfg(BaseModel):
    host: str = "localhost"
    port: int = 5432
    name: str = "pulsewire"
    user: str = "pulsewire"
    password: str = "pulsewire"  # 覆盖:PULSEWIRE_DATABASE__PASSWORD
    connect_timeout: float = Field(10.0, gt=0)  # 建连超时(秒):机器睡醒/postgres 没起时快速失败,不无限挂(2026-06-15 二⑧)

    @property
    def async_dsn(self) -> str:
        """SQLAlchemy async(asyncpg)连接串。"""
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )


class FetchCfg(BaseModel):
    concurrency: int = Field(32, gt=0)
    timeout_seconds: int = Field(20, gt=0)
    retry_max: int = Field(3, ge=0)
    rate_limit_per_host: int = Field(4, gt=0)
    # 个别主机限流凶(reddit 对未认证 .rss 集体并发会 429):给慢速覆盖,host→次/秒(后缀匹配:
    # host==key 或 .key 结尾;缺省用全局 rate_limit_per_host)。reddit 默认 0.5/秒(每 2 秒 1 个),
    # 从源头避免「一股脑并发把 reddit 惹毛 → 20 源集体 429」(2026-06-28:AI 板大户 reddit 全挂致只 3 条)。
    slow_hosts: dict[str, float] = Field(default_factory=lambda: {"reddit.com": 0.5})
    # 每源每次最多取多少条(取 feed 靠前=最新的 N 条),兜住吐全量历史的源(如 OpenAI 992 条);源可覆盖
    max_items_per_source: int = Field(50, gt=0)
    # 浏览器 UA:部分源(如小宇宙 xyzfm)对非浏览器 UA 返回 403;feed 阅读器用浏览器 UA 是常规做法
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )


class EmbeddingCfg(BaseModel):
    enabled: bool = True
    # local=fastembed(jina-v3,CPU,$0)| mlx=mlx-embeddings(Qwen3,Apple GPU,$0,治烤机)| jina=API(未实现)
    provider: Literal["local", "jina", "mlx"] = "local"
    model: str = "jinaai/jina-embeddings-v3"  # 本地多语模型(1024 维,对上 schema)。mlx 档用 mlx-community/Qwen3-Embedding-0.6B-8bit
    # mlx 档:QA 问题侧检索指令前缀(Qwen3 非对称)。None=用 MlxEmbedder 内置中文默认。
    query_instruction: str | None = None
    # 模型缓存目录(local 后端):None=默认 ~/.cache/pulsewire/fastembed(稳定,不放系统临时目录免被清)
    cache_dir: str | None = None
    # CPU 线程上限(local fastembed):None=库默认抓满所有核→CPU 100%/发烫/卡 UI。设个数留核给界面,
    # 同模型同向量(不碰红线),只是别霸满 CPU。建议 ≤ 性能核数(M 系 Pro 约 5)。
    threads: int | None = 4
    similarity_threshold: float = Field(0.88, ge=0.0, le=1.0)  # 真实数据校准:同源异库重复≈1.0、相关但不同(如两个相似项目)≈0.76;取 0.88 宁漏勿误
    recency_window_hours: int = Field(72, gt=0)  # 语义近邻只在近 N 小时内找(去重窗口)


class DedupCfg(BaseModel):
    url_normalize: bool = True
    fingerprint: bool = True
    embedding: EmbeddingCfg = Field(default_factory=EmbeddingCfg)


class EnrichCfg(BaseModel):
    """富化:给条目挂 value + source_id(来自入库事实,不让模型编)。"""

    fulltext: bool = False  # 正文全文抓取(trafilatura,走网络较慢);默认关,用 `enrich --fulltext` 开
    fulltext_max_chars: int = Field(20000, gt=0)  # 全文截断上限(进 facts.fulltext)
    # ---- 按源全文富化(2026-07 源升级 P1):sources.yaml 里 enrich 含 "fulltext" 的源,
    #      不开全局开关也对其"瘦正文"条目回源抓全文(治 39 个 summary-only 源 body_rate≈0
    #      → 过不了事件空壳护栏 → 永远出不了稿)。有界成本四闸:近期窗 + 每 run 硬顶 +
    #      只抓瘦正文 + 已有 facts.fulltext 不重抓(配合 upsert facts 按键合并才守得住)。----
    fulltext_recency_hours: int = Field(48, gt=0)  # 只抓最近 N 小时内**新入库**(fetched_at)的条目
    fulltext_max_per_run: int = Field(300, gt=0)  # 每 run 全文抓取条数硬顶(计尝试数,防失控)
    fulltext_min_content_chars: int = Field(300, ge=0)  # 原始 content 短于此才值得回源(瘦正文判据)
    fulltext_concurrency: int = Field(6, gt=0)  # 回源抓全文并发(共享 FetchClient,受每主机限速)


class RankWeights(BaseModel):
    """规则打分各因子权重(召回相似度 / 源权重 / 新鲜度 / 大事信号 / 持续关注)。"""

    recall: float = Field(0.5, ge=0.0)
    source_weight: float = Field(0.2, ge=0.0)
    recency: float = Field(0.2, ge=0.0)
    event: float = Field(0.1, ge=0.0)
    thread: float = Field(0.1, ge=0.0)  # 持续关注:属于多天在追线的簇加分(step6-B 反哺;0=关)


class EventPoolCfg(BaseModel):
    """选稿引擎 v2「事件池」可调参数(仅 rank.engine=events 生效)。

    打分/闸口径与 LLM 成本硬闸见 docs/DESIGN.md §1。**数值=建议起点,
    Phase 4 A/B 对照 + 用户签字后锁**(注:A+B 聚类保真参数 cosine 0.55/jaccard 0.5/top-K 8/判官 v2/
    截断 500 是 Phase 1a 锁定值,硬编码在 events/cluster.py,不在此 config——改须重过校准门)。
    """

    # 🔴 拼错 key 就崩、别静默吞(2026-07-01):闸开关/票数拼错(如 worthiness_gate_enable 少个 d)在顶层
    #    Settings 的 extra=ignore 下会被静默丢弃 → 闸悄悄按默认值走(纯优先关键闸静默失效),正是 DomainSpec
    #    那类"验证路径≠生产路径"老巢。本段是纯优先命门配置,改成启动即报错(fail-loud 好过 fail-silent)。
    model_config = ConfigDict(extra="forbid")

    # 两道硬门
    relevance_gate: float = Field(0.5, ge=0.0, le=1.0)  # τ_rel:事件够不够沾该板块兴趣(闸,非排序轴)
    magnitude_floor_gate: float = Field(0.3, ge=0.0, le=1.0)  # τ_floor:量级首发(白名单实体)豁免 τ_rel 但仍需的最低相关
    freshness_window_hours: int = Field(48, gt=0)  # 新鲜度硬窗:主峰超此即出局(除非今日有实质更新)
    # 空壳源护栏:某事件**全部成员都没抓到真文章正文**(清洗后正文剔掉标题词后 < 此字符数,基本只剩
    # 标题/跳转链,如 google-news 跳转卡 TradingKey)→ 该事件不可靠,排除出榜(下游 summarize 只拿标题
    # 自由发挥写出 10 倍错数字,2026-06-18 实锤)。量法见 events/cleantext.body_chars_beyond_title。
    # 默认 24:远高于空壳噪声(实测 ≤2 字,如残留的源名 "TradingKey")、远低于最短真新闻(实测 77 字),
    # 间隔极大不敏感;非"正文短就杀",是"压根没真正文才杀"。0=关闭护栏。
    min_body_chars: int = Field(24, ge=0)
    # 热度主轴
    velocity_window_recent_hours: int = Field(6, gt=0)  # 加速度近窗
    velocity_window_base_hours: int = Field(24, gt=0)  # 加速度基窗
    velocity_max: float = Field(1.0, ge=0.0)  # V_max:velocity 加成上限,防爆点条无限拉分
    magnitude_floor_bonus: float = Field(1.1, ge=0.0)  # B_floor:白名单实体一手首发的热度地板(≈log1p(2),约 2 个普通源量级,有上限不盖过真多源)
    magnitude_whitelist: list[str] = Field(default_factory=list)  # 大厂/官方机构实体白名单(小写匹配);空=无豁免
    # LLM 成本硬闸(运行级;闭 codex F3)
    max_subject_clusters: int = Field(500, gt=0)  # 每 run 最多对多少簇(按 source_count desc+新鲜度)抽主体
    max_judge_pairs_per_run: int = Field(800, gt=0)  # 每 run 判官调用全局硬顶;超限=停合并保守留开+告警
    # 重磅度闸(B 档语义"水货"筛;仅 engine=events 生效;设计见 docs/DESIGN.md §2;默认关,A/B+用户签字才开)
    # 治 /eval 唯一未过的"够热门":前十偶混水货(论坛闲聊/冷知识花边/纯表态呼吁无真事/营销软文)。插点=分板块后、
    # 限额前:对 heat 头部判水货、把判定 water 的事件从 board_evs 移除,回填靠 apply_event_quotas 自身 heat 降序贪心。
    # 判决缓存(S1/f20):判官裁决按 (item_hash, judge_name, prompt_hash) 落库,跨天/跨轮同条目同
    # prompt 直接读缓存不调 LLM(≈余额寿命翻倍)。默认关,A/B 证选稿零变化再开,一行回滚。
    judgment_cache_enabled: bool = False
    magnitude_gate_enabled: bool = False  # 默认关;同 rank.engine 回滚哲学,一行回滚
    # 每板块只判 heat 头部 N 条(有界成本)。须 ≥ final_limit,否则 final_limit 尾部的水货漏判、回填落空(闭 codex m3);
    # 实现再加兜底 max(N, final_limit+5) clamp。默认 25 = final_limit 20 + buffer 5。
    magnitude_judge_top_n: int = Field(25, gt=0)
    # 2026-07-02 提速批 200→300:抬到最坏用量(3 板 × head~25 × 3 票 = 225)之上永不撞顶(并发多票撞顶时
    # 票位跨事件"摊薄"、饿死非确定;撞不到顶则偏差不存在;真用量 ~112/run)。
    max_water_judges_per_run: int = Field(300, ge=0)  # 每 run 水货判官全局硬顶;超限=停判、剩下全留+告警(保守:绝不因预算耗尽误杀)
    # 每条判几次取多数票(治 flash 抽风偶发返错漏水货:2026-06-25 实测同条连判 6 次=1 假5 真,06:00 单判抽到假就漏)。
    # 奇数;drop 需严格多数(3→需 2 票)既补漏又不增误杀(单条假阳要 2 次才误杀,比单判更稳);1=回退旧单判。
    # 提前停:够多数即停 / 剩余票凑不够多数即停,典型每条 ~2 次调用。计入 max_water_judges_per_run。
    magnitude_judge_votes: int = Field(3, ge=1)
    # 话题闸(P0:治 AI 板混入非 AI 内容;仅 engine=events 生效;判官 events/topic_judge.py;默认关,A/B+用户签字才开)。
    # 板块归属唯一判据是「成员源多数领域」(source_domain==d.key),综合源(IT之家只标 cn,domain 默认 ai)整批塞进 AI 板,
    # 短中文标题余弦又分不开话题 → 加一道喂事件全文+板块画像、按归属判的 LLM 闸。插点=分板块后、限额前(与水货闸并排,先判跑题)。
    topic_gate_enabled: bool = False  # 默认关;同 magnitude 回滚哲学,一行回滚
    # 每板块只判 heat 头部 N 条(有界成本)。须 ≥ final_limit,实现再加兜底 max(N, final_limit+5) clamp。默认 25。
    topic_judge_top_n: int = Field(25, gt=0)
    # 2026-07-02 提速批 200→300:同 max_water(最坏 225 > 200 会撞顶;真用量 ~120/run)。
    max_topic_judges_per_run: int = Field(300, ge=0)  # 每 run 话题判官全局硬顶(跨所有板块共享);超限=停判、剩下全留+告警(保守:绝不误杀)
    # 话题闸多数票(2026-07-01 加,治单判抖动:pro 偶发返空/误判一次就把跑题货放进 AI 板〔电视剧/群星那类〕)。
    # 奇数;判跑题需严格多数(3→需 2 票 off_topic 才踢),凑不齐=KEEP(保守,与话题闸"宁错放别错杀"同向)。
    # 计入 max_topic_judges_per_run(投票→调用约翻倍;DeepSeek 余额紧时可设 1 回退单判省 token)。1=旧单判。
    topic_judge_votes: int = Field(3, ge=1)
    # 每板块正/反相关画像(可选):board key → 一段文字,补强 label/interest 不够清楚的板块归属边界。空=只靠 label+interest+tags。
    topic_portraits: dict[str, str] = Field(default_factory=dict)
    # 分板分类器(治 50 个综合源乱跑;仅 engine=events 生效;判官 events/board_classifier.py;默认关,A/B+用户签字才开)。
    # 综合源(source.mixed=true)的源 domain 标签不可信(36氪标 ai 却发财经)→ 按源标签分板会乱跑、还挤占专源候选名额。
    # 设计见 docs/DESIGN.md §2.6:mixed 源从专源名单摘出、走独立旁路池;全 mixed 的 cluster 单簇成事件打 is_mixed,
    # LLM 读内容判板归属(带 confidence/abstain + member 证据),归对板/other-abstain-低置信-fail 一律丢弃。专源主路零回归。
    board_classifier_enabled: bool = False  # 默认关;同 topic/magnitude 回滚哲学,一行回滚;关时 mixed 源退回当普通专源(零行为变化)
    # mixed 池独立候选预算(不挤专源 per_dom_cap);起点 ≈ per_dom_cap×域数 的 20%,A/B 标定。0=不取 mixed 候选(等于关)。
    mixed_cap: int = Field(40, ge=0)
    max_board_judges_per_run: int = Field(120, ge=0)  # 每 run 分板判官全局硬顶(跨所有板共享);超限=停判、剩下 is_mixed 事件丢弃(无可信 domain 不能放行)
    board_judge_top_n: int = Field(40, gt=0)  # 只判 heat 头部 N 个 is_mixed 事件(有界成本);须 ≥ mixed_cap,否则 mixed 尾部(31-N)不判就保持 None 被丢=静默漏真新闻(2026-07-01 对齐 mixed_cap=40;配 votes=3 恰好等于 max_board_judges 120)
    # 每条判 N 次取多数(治 flash 抽风:同条边缘货〔超算/SK海力士 ADR 这类芯片沾边但本质非 AI〕单判时上次丢这次留)。
    # 奇数;归某板需该板得严格多数票(3→需 2 票同板),否则丢弃(保守=drop,与 magnitude 同哲学,2026-06-29 加)。
    # 提前停:某板已够多数即停。计入 max_board_judges_per_run。1=回退旧单判。
    board_judge_votes: int = Field(3, ge=1)
    # 要闻够格闸(2026-06-29 用户"纯,没有就不报,所有板块一样"):判"够不够格当今日要闻",不够格踢、不硬凑数。
    # 比水货闸更严:连小众论文/传闻否认/论坛炫耀/蹭关键词也踢。方向=默认踢够格才留;fail-safe=留(不因故障误杀)。
    # 判官 events/worthiness_judge.py;默认关,A/B+盲考官(双向验:纯不纯 + 没误杀真货)+用户签字才开。回滚=false。
    worthiness_gate_enabled: bool = False
    worthiness_judge_votes: int = Field(3, ge=1)  # 每条判 N 次,够格票<多数→踢(pro 多数票)
    worthiness_judge_top_n: int = Field(25, gt=0)  # 只判 heat 头部 N 条(有界成本);须 ≥ final_limit
    # 2026-07-02 提速批 200→300:抬到最坏用量(3 板 × head~25 × 3 票 = 225)之上永不撞顶——考官挖出
    # 并发多票下预算撞顶时票位被跨事件"摊薄"(最坏整板饿死在第二票),撞不到顶则偏差不存在;真用量 ~113/run。
    max_worthiness_judges_per_run: int = Field(300, ge=0)  # 每 run 够格判官全局硬顶;超限=剩余未判**丢弃**(2026-07-01 改:纯优先"宁缺毋滥",预算耗尽的尾部宁少报也不补成"够格"塞边缘货;LLM 单条失败仍 fail-safe 留,两者不同)
    # 已剪记忆闸(2026-07-05 用户拍板"全上",治日报逐日重复:实测相邻两日重合 39%,病根=选稿对
    # "昨天已剪过什么"零记忆)。账本=threads 挂线痕(零新表);候选事件成员簇命中账本=已剪过 →
    # 材料全旧直接踢、有新材料交 novelty 判官判"有没有值得再剪一刀的新进展",没有→踢(腾位给新事)。
    # 判官 events/clip_memory.py;fail-open(账本/判官故障、预算耗尽一律留,最坏=回到重复现状)。
    # 回滚=false 只停①选稿闸+③增量写稿;②"追·第N天"章是代码层视觉改动(走既有在追线数据)不随开关。
    # 已知取舍(codex 2026-07-05 中危1):--force 全量重跑时线已挂今天(linked_today)→ 该跑不写
    # rankings.meta.clip、③退回全文写法(方向无害:不砍稿只丢增量收益;检查点续跑不重跑 rank 不受影响)。
    clip_memory_enabled: bool = False
    clip_window_days: int = Field(14, gt=0)  # 只认最近 N 天内剪过的;更久远的旧事重浮=当新事重报
    novelty_judge_votes: int = Field(3, ge=1)  # 踢需"无新进展"严格多数(3→2);凑不齐=留(宁多报一天别误杀)
    novelty_judge_top_n: int = Field(25, gt=0)  # 只对 heat 头部 N 条既往事件花判官预算;头部外原样留(本就进不了 final_limit)
    max_novelty_judges_per_run: int = Field(300, ge=0)  # 每 run novelty 判官全局硬顶;超限=剩余全留+告警(🔴 fail-open,与 worthiness 的 fail-closed 相反:这里砍了=丢真新闻,放行=回到重复现状)


class RankCfg(BaseModel):
    """兴趣分类:embedding 召回 → 规则粗排 → LLM 精排 + 新鲜度门 + 限额。"""

    # 选稿引擎:legacy=现 per-domain 召回+精排(默认) | events=全局事件池(见 docs/DESIGN.md §1,五柱:
    # 事件为单位/热度主轴/相关性闸/新鲜度硬窗/一事件一卡)。默认 legacy,A/B 对照 + 用户签字后才切 events。
    # 见 docs/DESIGN.md §1。
    engine: Literal["legacy", "events"] = "legacy"
    recall_limit: int = Field(120, gt=0)  # 召回候选上限(够大,留足新鲜候选给新鲜度门筛)
    recall_recency_hours: int = Field(720, gt=0)  # 召回只取近 N 小时内(或无日期)的相似项,避免召回池被陈旧相似项占满(默认 30 天)
    final_limit: int = Field(20, gt=0)  # 最终输出上限
    # 精排后端:deepseek=litellm 调 DeepSeek(需 key) | rule=只用规则分(无 key 可跑)
    rerank_provider: Literal["deepseek", "rule"] = "deepseek"
    rerank_model: str = "deepseek-v4-flash"  # 实名;deepseek-chat 别名 2026/07/24 下线
    rerank_blend: float = Field(0.6, ge=0.0, le=1.0)  # 终分 = blend*LLM相关度 + (1-blend)*规则分
    request_timeout: float = Field(120.0, gt=0)  # LLM 精排请求超时(秒),防 DeepSeek 卡住连接无限挂死整条流水线(2026-06-15 教训,同 summarize/threads 口径);失败走下面 json_schema_retry 外层重试
    # 内容领域分类(三③):对已选中条目按内容判领域,确信不属本域的剔掉(纠正"领域跟着源走"错放)。
    # 默认【开】:2026-06-16 已验过——真实 60 条选品仅删 1 条(比利时反种族集会),独立蒙眼 agent
    # 也判它"非地缘",删得准;模糊项(SpaceX多话题/能源韧性)保守保留;故意放错 3 条全删对。
    # 护栏:丢弃>classify_max_drop_ratio→疑抽风全留;LLM 失败/无 key→保留全部(fail-safe)。关=设 false。
    content_classify: bool = True
    classify_model: str = "deepseek-v4-flash"  # 分拣是简单活,省档 flash
    classify_max_drop_ratio: float = Field(0.4, ge=0.0, le=1.0)  # 单域单次最多剔掉的比例;超过=疑分类器抽风→全留+告警
    per_category_limit: int = Field(8, gt=0)  # 各类限额(防单一类目刷屏)
    old_item_age_hours: int = Field(168, gt=0)  # 超过此时长算"老项"
    old_item_limit: int = Field(5, ge=0)  # 老项限额(防陈旧内容占位)
    whitelist_recent_limit: int = Field(60, ge=0)  # 白名单源每轮最多拉多少近期条目进候选池(0=关闭白名单直通)
    whitelist_recall_floor: float = Field(0.55, ge=0.0, le=1.0)  # 白名单条目若未被语义召回到,给的召回相似度地板(让其在精排里有竞争力)
    per_source_limit: int = Field(2, gt=0)  # 单源限额(防单一源刷屏,如官方技术博客一天发 N 篇)
    # 硬核学术论文限额(2026-06-18 用户裁:纯论文每板块最多 1-2 条,免 arxiv 黑话标题把"一眼看懂"拖垮;
    # 是"够前沿"与"够看懂"的折中——两位独立考官都卡在 Finsler 几何/并发漏洞这类纯论文标题)。仅 events 引擎 apply_event_quotas 生效。
    academic_paper_limit: int = Field(2, ge=0)  # 每板块纯学术论文上限(0=不限)
    academic_source_prefixes: list[str] = Field(default_factory=lambda: ["arxiv"])  # 判"纯学术论文"的源 id 前缀(小写)
    select_sim_dedup: float = Field(0.70, ge=0.0, le=1.0)  # 选稿去重:与已选条目相似度≥此值视为同一事件,跳过(同一事件日报只出一条;镜像源/重复报道都靠它压)。真实标定见 config.yaml(2026-06-14:0.8→0.70 修「同事件漏合并」)
    # 语义同事件复判(2026-06-16 方案 B):余弦在 [event_dedup_min_sim, select_sim_dedup) 这一"中等相似"带的
    # 候选,词法折叠看不出但可能是同一件事的不同角度(实锤:美伊停火 #1「敲定」/#2「细节曝光」)。多问一次 LLM
    # 「同一件事?」,是→折叠。判官保守(拿不准=不同,宁可留两条也别误合);LLM 失败/无 key/超 max_judges → 不折叠
    # (退回纯词法现状,无回归)。复用 threads.judge_model。关=event_dedup_judge=false。
    event_dedup_judge: bool = True
    event_dedup_min_sim: float = Field(0.55, ge=0.0, le=1.0)  # 复判带下界;低于此=明显不同事件,不浪费 LLM
    event_dedup_max_judges: int = Field(24, ge=0)  # 单域单轮最多问几次(成本闸);超过=保守不折叠。2026-06-18:12→24,加了「共享实体」候选门后候选对变多(同公司不同角度都得问判官),给足预算别让真·同事件因预算耗尽漏掉
    # 事件热度:近窗内"多少个不同源在报相似内容"(宽松阈值只计数、不合并存储;补齐 dedup 0.88 不聚同事件的盲区)
    heat_window_hours: int = Field(36, gt=0)  # 热度统计窗口(只看"正在发生"的)
    heat_sim_threshold: float = Field(0.75, ge=0.0, le=1.0)  # 宽松相似度阈值(同事件不同措辞≈0.76,取 0.75)
    heat_min_sources: int = Field(3, gt=1)  # ≥N 个源在报才算热点(热点直通通道的门槛)
    heat_top_reps: int = Field(15, ge=0)  # 热点直通:每轮最多补多少个热点代表进候选池(0=关闭)
    github_board_limit: int = Field(20, ge=0)  # GitHub 开源热榜:取 stars 最高的前 N 个 AI repo(0=关闭热榜)
    github_board_recency_days: int = Field(30, gt=0)  # 热榜只取近 N 天内(或无日期)推送过的 repo(避开陈年项目)
    github_board_exclude: list[str] = Field(default_factory=list)  # 热榜排除名单(owner/repo,大小写不敏感);默认空=不排除
    weights: RankWeights = Field(default_factory=RankWeights)
    event_pool: EventPoolCfg = Field(default_factory=EventPoolCfg)  # 选稿引擎 v2 事件池参数(rank.engine=events 时生效)


class QaCfg(BaseModel):
    """语义检索·问答翻历史(v2 主线B②;见 docs/DESIGN.md §3)。档案大白话问 + 引用式回答。

    召回(embed_query→summaries.card_vec 近邻)零成本(本地模型);回答层调 DeepSeek(按需,几分钱/问)。
    铁律:回答只据召回的真卡、每句标源、召回空→"没找到",绝不编(同数字回源)。
    """

    enabled: bool = True
    top_k: int = Field(12, gt=0)  # 召回头部 N 张卡喂上下文
    relevance_floor: float = Field(0.3, ge=0.0, le=1.0)  # τ_qa:余弦低于此=不相关不喂 LLM;全低→直接"没找到"
    max_context_cards: int = Field(12, gt=0)  # 喂 LLM 的卡数上限(防上下文爆)
    answer_model: str = "deepseek-v4-pro"  # 回答脸面活用强档(精排粗活才用 flash)
    answer_max_tokens: int = Field(1500, gt=0)
    request_timeout: int = Field(60, gt=0)  # 独立超时(不隐式继承 threads,闭 codex MIN-2)


class RenderCfg(BaseModel):
    """出图(Jinja2 → 无头 Chrome → PNG)。"""

    width: int = Field(1080, gt=0)  # 图卡固定宽度(v2-B 深墨头设计)
    output_dir: str = "web/rendered"  # 相对仓库根;已 gitignore
    use_system_chrome: bool = False  # True=用系统 Chrome(channel=chrome);False=playwright 自带 chromium
    settle_ms: int = Field(800, ge=0)  # set_content 后等字体/布局稳定再截图
    timeout_ms: int = Field(60_000, gt=0)  # 单次截图操作超时(set_content/screenshot);机器忙时给足余量(默认 60s,>playwright 自带 30s)
    retries: int = Field(2, ge=0)  # 截图超时/失败自动重试次数(0=不重试);整页重渲幂等可重试,耗尽才冒泡


class EventCfg(BaseModel):
    """'大事'定义(已锁:多源汇聚为主)。"""

    min_sources: int = Field(3, gt=1)
    window_minutes: int = Field(90, gt=0)
    scan_interval_minutes: int = Field(15, gt=0)


class SummarizeCfg(BaseModel):
    # 后端:api=litellm 按 token 调(DeepSeek/Opus/GPT);cli=调本地登录的 claude/codex(走订阅,灰色风险自负)
    backend: Literal["api", "cli"] = "api"
    provider: Literal["deepseek"] = "deepseek"
    model: str = "deepseek-v4-flash"  # 默认轻档,实名(deepseek-chat 别名 2026/07/24 下线);config.yaml 覆盖为 v4-pro
    json_schema_retry: int = Field(2, ge=0)
    prompt_caching: bool = True
    # 喂给模型的每条目正文上限:insight 深度解析要更多素材;有全文/逐字稿时给足上下文,无则就是短摘要
    prompt_content_max_chars: int = Field(6000, gt=0)
    # 分块总结:每次 LLM 调用最多产出几条。20 条挤一次 ≈ 1.5 万字输出 > DeepSeek 8192 token 上限
    # → 响应被截断成坏 JSON、重试也救不回 → 整跑崩。分块把每次响应压回限内(治本)。
    batch_size: int = Field(6, gt=0)
    # 单次 LLM 输出 token 上限(headroom;DeepSeek-chat 上限 8192)。配合 batch_size 双保险防截断。
    max_tokens: int = Field(8192, gt=0)
    # LLM 请求超时(秒)+ 瞬时失败内部重试次数。防 DeepSeek 卡住连接无限挂死整条流水线
    # (2026-06-15 真跑教训:github_board 的 LLM 调用无超时,卡在 deliver 前 1h+ → 飞书发不出)。
    request_timeout: float = Field(120.0, gt=0)
    request_retries: int = Field(2, ge=0)
    chunk_fail_alert_ratio: float = Field(0.34, ge=0.0, le=1.0)  # 分块失败比例≥此值→告警(成片内容丢失可见性,2026-06-15 二⑦)
    # backend=cli 时:调哪个本地 CLI(claude / codex),失败是否回退到 api(强烈建议 true,防开天窗)
    cli_command: Literal["claude", "codex"] | None = None
    cli_fallback_to_api: bool = True
    # 高风险定性断言闸门(上市/IPO、倍数话术、临床突破、战报制裁):条目"多源同报"佐证数
    # ≥ 此值才放行,否则标待核实。1=关闭闸门(佐证恒≥1)。佐证口径=max(簇内源数,事件热度)。
    risk_min_sources: int = Field(2, ge=1)
    # LLM 断言审计(闸门治本层):对单源且关键词闸门放行的条目,独立一次 LLM 调用复审
    # "传闻是否被写成既成事实",逮换措辞的漏网之鱼。失败降级为只剩关键词闸门(告警,不拖垮主报)。
    llm_audit: bool = True
    # 标题错位护栏(2026-06-17 eval 实锤:地缘一批 LLM 把 headline 写串位=张冠李戴漏到飞书)。
    # 出稿后语义比对每条 headline 与自己 tldr+insight:错位的按"标题↔正文最佳余弦"在错位集内重配,
    # 配不回的用该条自己的 tldr 派生安全标题兜底。失败降级=不改(告警),不拖垮主报。详见 summarize/coherence.py。
    headline_coherence_check: bool = True
    headline_coherence_floor: float = Field(0.5, ge=0.0, le=1.0)  # 自配余弦低于此且有更优归属才判错位
    headline_coherence_margin: float = Field(0.1, ge=0.0, le=1.0)  # 更优归属须比自配高出此差,防噪声误判


class ThreadsCfg(BaseModel):
    """事件线 A 层:从簇标题抽「事件主体短语」(subject),做跨天归线的预过滤键。

    subject 抽取是廉价结构化活,固定直连 DeepSeek flash(不走 summarize 的 cli 后端)。
    A 层只缩候选(同主体的在追线),真正"接哪条线/新开"由 B 判官(step 3)定,故匹配宽松。
    见 docs/DESIGN.md §4。
    """

    enabled: bool = True
    subject_model: str = "deepseek-v4-flash"  # 省档(flash):抽主体是简单活
    # ⚠️ DeepSeek-v4 是推理模型,会先出推理 token 再出正文。128 太小→推理就把预算耗尽、
    # finish_reason=length、正文空(char 0)→ 一直被误当"flash 抽风"。max_tokens 是上限不是目标,
    # 模型想完即停,调大在常态零增本、只防截断。这是 2026-06-15 归线/重建大批失败的真因(非 flash/key)。
    subject_max_tokens: int = Field(2048, gt=0)  # 给推理+JSON 留足(实测 128→6/6空,512+→0/6);2026-06-20 1024→2048 进一步压抽风:基线 479 次 8 抽风全因推理+JSON 撞 1024 顶被截断(6 返空+2 坏JSON),翻倍留头、常态零增本
    judge_model: str = "deepseek-v4-flash"  # B 判官:接哪条线/新开,也是逻辑活,省档
    judge_max_tokens: int = Field(2048, gt=0)  # 同 subject:推理模型预留,256 太小会被推理耗尽返空(2026-06-15);2026-06-20 1024→2048 同压抽风
    json_schema_retry: int = Field(2, ge=0)  # 退避重试兜底剩余偶发空返回
    request_timeout: float = Field(90.0, gt=0)  # A/B LLM 调用超时(秒),防无超时挂死;失败走上面退避重试
    match_threshold: float = Field(0.5, ge=0.0, le=1.0)  # "文本高度接近"的 token Jaccard 阈值
    # A 层语义召回:词法 Jaccard 够不着"同故事换措辞"(冲突/和平/战争 token 不重叠)→ 裂线。
    # 补一层 embedding 余弦召回(复用 dedup 的 jina-v3,当场算不存库),把语义相近的老线也塞进候选,
    # 最终接不接仍由 B 判官定(B 兜底防过度合并)。关掉=完全退回纯词法。
    semantic_match: bool = True  # 开关:语义召回补词法(治"同故事裂多条线")
    semantic_threshold: float = Field(0.70, ge=0.0, le=1.0)  # 主体短语 embedding 余弦 >= 此值进候选(宽松靠 B 筛)
    semantic_top_k: int = Field(5, gt=0)  # 语义召回最多补几条候选(控 B 的候选规模,守"B 只看少数候选")
    dormant_after_days: int = Field(7, gt=0)  # 线超 N 天无新进展 → status=dormant(前端折叠)
    subject_fail_alert_ratio: float = Field(0.3, ge=0.0, le=1.0)  # 抽主体失败率≥此值→告警(「在追」静默退化可见性,2026-06-15 二③)
    min_days: int = Field(2, gt=0)  # 前端「在追」露出门槛:线须跨 >= N 个不同日期(防单日小新闻刷屏)
    rebuild_concurrency: int = Field(8, gt=0)  # --rebuild 重放时 A 抽主体的并行度(flash blocking 丢线程池)


class ChannelToggle(BaseModel):
    enabled: bool = True


class WechatCfg(ChannelToggle):
    best_effort: bool = True


class FeishuCfg(ChannelToggle):
    # webhook = 自定义机器人文字卡(无需公网图);app = 自建应用图片推送(自建应用凭证,
    # 把 PNG 发到用户 open_id 私信,webhook 发不了图)。
    mode: Literal["webhook", "app"] = "webhook"


class DeliverCfg(BaseModel):
    feishu: FeishuCfg = Field(default_factory=FeishuCfg)
    wechat: WechatCfg = Field(default_factory=WechatCfg)
    webapp: ChannelToggle = Field(default_factory=ChannelToggle)


class RetentionCfg(BaseModel):
    mode: Literal["forever"] = "forever"
    backup_enabled: bool = True


class DomainCfg(BaseModel):
    """一个领域 = 一份独立兴趣(rank→transcript→summarize→render 各跑一遍),对应 App「今日」下拉一栏。

    key 既是 App 下拉标识,也对上 sources.yaml 里源的 domain(rank 按它过滤候选)。
    required=True 的领域(主报 AI)失败则整跑失败;False 的次要领域(bio/geo)失败仅告警跳过,
    不拖垮主报——守住"绝不静默产空日报"同时让次领域可独立失败。
    """

    key: str  # 领域键(ai/bio/geo);对上源 domain 与 App 下拉
    label: str  # App 显示名(AI / 生物医疗 / 国际局势)
    interest: str  # 自然语言兴趣(驱动该领域 rank/summarize)
    tags: list[str] = Field(default_factory=list)
    required: bool = False  # True=主报失败即整跑失败;False=次领域失败仅告警跳过
    enabled: bool = True
    # 该领域新鲜窗覆盖(小时);None=用 event_pool.freshness_window_hours 全局值(48h)。
    # AI 板放宽到 144h(6 天)填满——真鲜 AI 货稀 + AI 源 65% 翻炒旧货,48h 窗只剩 3 条;bio/geo 留 None。
    # 见 docs/DESIGN.md §2.7。仅 rank.engine=events 生效。
    freshness_window_hours: int | None = Field(default=None, gt=0)


def _default_domains() -> list[DomainCfg]:
    return [
        DomainCfg(key="ai", label="AI", interest="AI 编程助手与大语言模型",
                  tags=["llm", "ai"], required=True),
        DomainCfg(key="bio", label="生物医疗", interest="生物医疗与生命科学前沿突破",
                  tags=["bio", "health"], required=False),
        DomainCfg(key="geo", label="国际局势", interest="国际局势与地缘政治",
                  tags=["geo", "politics"], required=False),
    ]


class RunCfg(BaseModel):
    """一次完整流水线跑(fetch→…→deliver)的入口配置。

    多领域:run.domains 列出各领域(各出一份日报,聚合进同一份网页 App 的「今日」下拉)。
    interest/tags 保留为单兴趣 back-compat(`pulsewire run "<兴趣>"` 显式传或 domains 为空时回退)。
    """

    interest: str = "AI 编程助手与大语言模型"  # 自然语言兴趣(驱动 rank/summarize/render/deliver)
    tags: list[str] = Field(default_factory=lambda: ["llm", "ai"])  # 兴趣标签
    domains: list[DomainCfg] = Field(default_factory=_default_domains)  # 多领域(空=回退单 interest)
    trigger_type: Literal["daily", "event"] = "daily"  # run_id 与投递幂等键的触发类型
    fulltext: bool = False  # 富化是否抓全文(走网络较慢,默认关)
    # 精排后对入选条目抓正文/网页逐字稿(只抓 ~final_limit 条,控 token);喂给总结提升质量
    transcript: bool = True
    # 整跑总超时(分钟)看门狗:单站超过剩余预算 → 超时冒泡走失败告警链,不无限挂(2026-06-15 二⑦)。
    # 正常整跑约 12–18 分;默认 40 给足余量,异常拖死才触发。
    total_timeout_minutes: float = Field(40.0, gt=0)
    # 开跑前 DeepSeek 余额预检(2026-07-02 E1:余额烧干→判官全线 fail-open 出毒日报 + 每5分钟
    # 全量重试风暴)。开跑前查一次余额,确切低于阈值就不跑 + 告警;查不到放行(best-effort)。
    # 阈值单位随账户币种(用户为 CNY)。回滚 = preflight_balance_enabled: false。
    preflight_balance_enabled: bool = True
    preflight_min_balance: float = Field(5.0, ge=0)


# --------------------------------------------------------------------------- #
# 信源注册表(sources.yaml)                                                    #
# --------------------------------------------------------------------------- #
class SourceType(str, Enum):
    rss = "rss"
    hackernews = "hackernews"
    github = "github"
    hf_papers = "hf_papers"  # HuggingFace 每日精选论文 API(2026-07-05 卷一)
    ossinsight = "ossinsight"  # OSS Insight 涨速榜(GitHub 摘星榜第 5 路;2026-07-05 卷二)
    reddit = "reddit"
    youtube = "youtube"
    html = "html"
    file = "file"


class Source(BaseModel):
    id: str
    type: SourceType
    url: str
    # 人类可读名(展示用,替代机器 slug)。留空 → load_sources() 从 sources.yaml 行内注释回填;
    # 仍缺 → source_label() 启发式美化 slug。保证用户永远看不到 GEO-…-GOOGLE-NEWS 这类机器串。
    display_name: str | None = None
    # 领域(App「今日」下拉的一栏 = 一个独立兴趣):ai / bio / geo。默认 ai → 现有 150 AI 源不必逐条改。
    # rank 按 domain 内存过滤候选,防跨领域串味;用 str(非 Literal)留扩展空间,新增领域只改数据不改码。
    domain: str = "ai"
    category: str = "general"
    region: str = "global"
    lang: str = "en"
    weight: float = Field(0.5, ge=0.0, le=1.0)
    freshness_hours: int = Field(24, gt=0)
    enrich: list[str] = Field(default_factory=list)
    enabled: bool = True
    # 白名单直通:高价值源(实验室官方/大佬访谈)的近期条目即使语义召回排不进前列,
    # 也强制进精排候选池(仍受新鲜度门 + LLM 相关度判定,不保证一定进终版)。
    whitelisted: bool = False
    # 每源 UA 覆盖:个别源对全局浏览器 UA 敏感(如 Meta 对浏览器 UA 返 400),给它单独配 UA
    user_agent: str | None = None
    # 每源取条上限覆盖(None=用 fetch.max_items_per_source 全局默认);高频/想多留的源可调大
    max_items: int | None = None
    # 只供 GitHub 热榜板(github_board)、不进新闻领域板的事件选稿。
    # 用于 type=github 的"项目榜"源:它们与 GitHub 热榜板同质(都是"哪些 repo 火"),
    # 剥离出 AI 新闻板防两板打架;仍照常 fetch(github_board 候选池靠它们)。2026-06-27:AI/GitHub 两板分开。
    board_only: bool = False
    # 综合源(一个 feed 混发 AI/bio/geo/财经……):源的 domain 标签不可信,必须读内容判板。
    # board_classifier(events/board_classifier.py)开启时:这类源从专源候选名单摘出(不挤专源名额、不投票),
    # 走独立旁路池 → 全 mixed 的 cluster 单簇成事件、LLM 判它真正属于哪个板(归对板/判不出丢弃)。
    # ⚠️ 仅 rank.event_pool.board_classifier_enabled=true 时生效;关时 mixed 源退回当普通专源(零行为变化)。
    # 名单=综合源 LLM 分板清单(约 50)。2026-06-27 设计见 docs/DESIGN.md §2.6。
    mixed: bool = False
    # 发布时间不可信源(2026-07 源升级 P1,治 date_suspect_rate=1.0 假日期):false = 入库时
    # published_at 一律存 NULL(feed 给的日期是抓取时间冒充/压根没有,回落抓取时间就是"旧闻装新")。
    # NULL 在下游按"无日期"处理:events 新鲜窗 passes_freshness_window(None)=False,永远当不了新鲜锚,
    # 但仍可当互证成员。适用:meta-research-blog / bio-nibib-news / geo-nikkei-asia。
    trust_published_at: bool = True
    # 条目级排除过滤(2026-07 源升级 P1;fetch 阶段在"取条上限"截断**之前**应用,免垃圾占坑):
    # - url_exclude_patterns:条目 URL 命中任一正则(re.search)→ 丢弃。
    #   用途:huggingface-blog 官方 feed 混入海量社区博文(/blog/<user>/<slug> 两段路径),按路径踢。
    # - title_exclude_patterns:条目标题命中任一正则 → 丢弃。
    #   用途:openai-codex-releases 被 alpha 预发布刷屏,按 '\\b(alpha|beta|rc|nightly)\\b' 踢。
    # 正则编译失败 = 配置错误,fetch 时 fail-loud(冒泡该源失败),不静默放行。
    url_exclude_patterns: list[str] = Field(default_factory=list)
    title_exclude_patterns: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_no_space(cls, v: str) -> str:
        if not v or " " in v:
            raise ValueError("source id 不能为空且不能含空格")
        return v


class SourcesFile(BaseModel):
    sources: list[Source]

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, v: list[Source]) -> list[Source]:
        ids = [s.id for s in v]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"重复的 source id:{sorted(dupes)}")
        return v
