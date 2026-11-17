# 药品流通追溯码的批次谱系分段采集

追溯事件保留事件时间和接收时间，父子包装与退货不会被压成一个最新状态。采集按
追溯码分段进行：查询范围与游标冻结持久化，任意页中断后可从确认边界恢复重放。

## 模型

项目从冻结来源响应读取资料：

- `transport` 保证请求与原文字节身份，并支持按边界 URL 键控定位冻结步骤；
- `domain` 严格解释分页与领域字段，分类生产/退货/拆包/流通事件，计算内容指纹，
  判定证据链缺口；
- `ledger` 用 SQLite 保存收据原文、分段游标、分页凭证、事件证据与隔离区，
  只追加、不覆盖；
- `collector` 执行可恢复分段收集，已确认页幂等跳过，失效令牌停在待核边界；
- `view` 按码重放证据链，区分“平台尚未返回（platform_pending）”与
  “收到但待核（review_pending）”。

样例为合成资料，不访问真实监管站或企业系统。Python 3.11 以上、无第三方依赖；
在项目根目录执行：

    python -m unittest discover -s tests -v
    python -m compileall -q capture_service tests tools
    python -m capture_service.cli inspect data/normal.json
    python -m capture_service.cli resume  data/token-dead.json --db state.db
    python -m capture_service.cli chain  data/token-recover.json BX-3

## 关键语义

- **只追加**：较新接收到的出库/退货页面不能覆盖旧事件去向，证据链按业务时间
  排序，接收顺序由 `receive_seq` 另行留证；
- **退货/拆包不是生产**：只有 `produce`、`pack` 建立批号绑定，退货后的下游
  去向在平台闭环前不推断；
- **冲突留证不回滚**：分页令牌失效、原文损坏、同分页或同事件内容冲突时，问题
  原文进入隔离区并报告待核范围，已确认边界不动；
- **可恢复**：`--db` 持久化分段边界，进程重开后 `resume` 从最后确认游标续跑。

夹具由 `tools/make_fixtures.py` 生成（在项目根目录 `python tools/make_fixtures.py`）。
Dockerfile 提供 Linux 环境；`docker build -t trace-code .` 构建，
`docker run --rm trace-code` 运行样例。
