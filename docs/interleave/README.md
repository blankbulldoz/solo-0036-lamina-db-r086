# 在线目录变更 × 活动事务：交错实验

本目录是 `../online-catalog-tx-runbook.md` 的可执行证据，不包含任何产品代码改动。

## 文件

- `interleave_lab.py` — 仅用 Python 标准库编写的真实 HTTP 交错实验，7 个场景各自使用独立临时实例。
- `observed-interleavings.json` — 一次完整运行的留档输出（每步请求、HTTP 状态、响应体）。

## 运行

```sh
go build -o /tmp/lamina-build/laminad ./cmd/laminad
python3 -I interleave_lab.py --binary /tmp/lamina-build/laminad --out observed-interleavings.json
# 单跑一个场景：--scenario B（可选 A..G、all）
```

## 场景与手册结论对照

| 场景 | 交错主题 | 关键观测 |
| --- | --- | --- |
| A | 索引建在事务存活期 | 旧事务/`at` 历史查询走 scan，当前读走 index；旧事务仍可写旧关系并提交 |
| B | 后建唯一索引否决旧事务 | 构建只看已提交数据而成功；旧事务提交期 409、闭合后 404、写集零泄漏 |
| C | 索引先建的对照 | 并发抢同一唯一值 409；事务内唯一值互换允许；与他行现状冲突仍 409 |
| D | 写冲突与索引无关 | 非唯一索引交错下同键 first-committer-wins，输家附带写入整笔回滚 |
| E | 后建关系与重启 | 旧事务写新关系 409、查新关系 410；重启后事务不复活、DDL 序号保持 |
| F | 回收钉住与重启核对 | 活动读者把 vacuum 钉回其快照；关闭后水位推进；重启后唯一约束仍强制 |
| G | 失败 DDL 的原子性 | 唯一构建失败不发布目录、不推进序号；重名/未知列/重关系各有明确拒绝码 |

实验中的事务 id、时间戳每次运行不同；序号、HTTP 状态码、错误 `code`、计划访问路径和数据结论是稳定的。
