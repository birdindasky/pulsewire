# 贡献指南 / Contributing

感谢你想给 pulsewire 出力。这份文档讲怎么搭开发环境、跑测试、提 PR。
*(English notes inline; the project's daily digest output is Chinese by design.)*

## 开发环境 / Dev setup

前置见 [README「系统要求」](README.md#系统要求):Docker Desktop · [uv](https://docs.astral.sh/uv/) · Python 3.12+。

```bash
git clone https://github.com/birdindasky/pulsewire.git && cd pulsewire
uv sync --extra dev              # 按 uv.lock 建虚拟环境
docker compose up -d postgres    # 测试要用的 postgres(pgvector)
uv run alembic upgrade head      # 建表
```

**非 Apple Silicon**(Intel / Linux / Windows / AMD):把 `config.yaml` 里 `dedup.provider` 从 `mlx` 改成 `local`(纯 CPU 的 fastembed/ONNX),否则会在向量那步报错。详见 README「系统要求」。

## 提交前必过 / Before you push

```bash
uv run ruff check src tests migrations    # 代码规范
uv run pulsewire validate-config          # 配置校验
uv run pytest -q                           # 测试(postgres 没起会跳集成测试)
```

CI 会在 Ubuntu 上跑同一套(`.github/workflows/ci.yml`)。**PR 必须绿。** postgres 起着时全套 600+ 测试全绿是基线;本地跳过的集成测试,CI 会替你跑。

## 几条焊死的规矩 / Non-negotiables

改代码前先知道这些——它们是 pulsewire 存在的理由,踩了 PR 不会合:

- **数字 0 编造**:模型永远见不到真实数字,只见 `{Fn}` 占位符;数字由 `verify` 层从库内真值回填。**绝不让模型生成数字。** 追不到来源的数字标「待核实」。
- **失败要冒泡,不静默产空**:任何一站挂了要告警 + 可续跑,**绝不返回空/假日报**。
- **密钥只走 `.env` / 环境变量 / Keychain,从不进仓库**。
- **改视觉先读 [`STYLE.md`](STYLE.md)**(今日剪报本视觉规范),两处渲染实现(PNG 模板 + 网页 App)同步改。
- **质量优先,不靠砍内容省成本**:换更笨的模型、砍质量闸、砍条数来省钱,都不收。

## 代码风格 / Code style

- 跟着周围代码的风格走(命名、注释密度、惯用法)。
- 注释写"约束/为什么",不写"这行干嘛"。
- 每道质量闸、每个配置开关旁都留一行回滚说明——照着这个习惯来。

## PR 流程 / Pull requests

1. 从 `main` 拉分支。
2. 小步提交,commit 信息说清「改了什么 + 为什么」。
3. 过完上面「提交前必过」再推。
4. 开 PR,说清动机 + 影响面;动到质量闸/选稿逻辑的,附上你怎么验证的(理想是独立的对照/测试,不是"我看着没问题")。

## 架构 / 设计 / Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 系统当前真相(流水线 / 数据模型 / 数字回源)。
- [`docs/DESIGN.md`](docs/DESIGN.md) — 每个子系统「为什么这么设计」。

## License

贡献即视为你同意以 [MIT](LICENSE) 授权你的改动。
