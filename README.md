# 药品流通追溯码的批次谱系分段采集

追溯事件保留事件时间和接收时间，父子包装与退货不会被压成一个最新状态。

现有项目从冻结来源响应读取资料，由 transport 保证请求与原文字节身份，domain 解释本领域字段，ledger 保存版本和原始收据，view 展示已确认内容与缺口。样例为合成资料，不访问真实监管站或企业系统。

Python 3.11 以上；在项目根目录执行：

    python -m unittest discover -s tests -v
    python -m compileall -q capture_service tests
    python -m capture_service.cli inspect data/normal.json

Dockerfile 提供 Linux 环境；docker build -t trace-code . 构建，docker run --rm trace-code 运行样例。
