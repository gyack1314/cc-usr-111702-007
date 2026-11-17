# 流通追溯事件资料约定

code 与 page 定位分页来源；event_id 是事件身份，event_time 为业务时间，received_at 是本地可见时间。next 只说明此响应仍有续页。

normal.json 保存两个按顺序接收的来源，conflict.json 在首份原文之后给出同一实体版本的不同内容。相同身份的版本冲突由账本事务拒绝；未闭合的来源不能被解释成最终处置结果。
