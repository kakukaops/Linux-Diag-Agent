# ADR-007 — 数据存储目录 = `<repo>/data/`（项目目录内，可配置重定向）

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M2, M3 |

## 上下文

v1 需要存储大量文件型数据：LKML mbox（~80GB）、syzbot 报告、内核源码 checkout、sosreport、PageIndex 节点描述、（v1.3）本地 LLM 权重。需选定根目录约定。

| 方案 | 路径 |
|------|------|
| A | 系统标准目录：`/var/lib/linux-diag-agent/`（生产） / `~/.local/share/linux-diag-agent/`（开发，遵循 XDG） |
| B | 项目目录内：`<repo>/data/` |

## 决策

**采用方案 B：`<repo>/data/` 作为默认数据根；通过 `configs/default.yaml` 的 `storage.data_root` 可重定向到任何路径**。

```yaml
# configs/default.yaml
storage:
  data_root: ./data                   # 默认（相对 repo 根）

# configs/production.yaml（生产可覆盖）
storage:
  data_root: /var/lib/linux-diag-agent
```

`.gitignore`：

```
/data/
!/data/.gitkeep
```

## 影响

| 维度 | 影响 |
|------|------|
| 开发体验 | ★ 简单：`cd repo && tree data/` 即可看全部数据；脚本可用相对路径 |
| 仓库整洁 | data/ gitignored，不污染仓库；但目录结构通过 .gitkeep + 文档保留 |
| 生产部署 | 推荐通过 `configs/production.yaml` 把 `data_root` 改为 `/var/lib/...` 或挂载点；保留灵活性 |
| 磁盘 | 仓库所在盘需要至少 200GB 空间（v1.2 末估算）；如不足需软链 / mount bind |
| 备份 | 备份脚本以 `data_root` 为参数，与路径无关 |
| FHS 合规 | 默认不合规（Filesystem Hierarchy Standard 要求数据在 /var）；但生产可重定向到合规路径 |
| 用户预期 | 与 git-tracked code 物理上紧邻；新开发者上手快 |

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| **A 系统标准目录** | 生产合规更好；但开发期路径混乱（`/var/lib` 需 sudo、`~/.local/share` 隐藏目录）、调试不便；v1 主要场景是开发与评测，开发友好度优先 |

## 注意事项

- **必须 gitignore**：`/data/` 全部内容不入版本控制（防止误 commit GB 级数据）
- **必须有 .gitkeep**：保留目录骨架，方便首次 clone 后脚本可写
- **配置驱动**：所有代码通过 `configs/default.yaml` 的 `storage.data_root` 解析路径，禁止硬编码 `./data`
- **生产 README**：部署文档明确指引把 `data_root` 改到独立挂载点
- **跨用户共享**：如多个开发者共用一台机器，`data_root` 可指向 `/srv/diag-agent-data` 公共目录

## 监控

- `df` 监控 `data_root` 所在盘空闲空间，<20% 告警

## 参考

- XDG Base Directory：https://specs.freedesktop.org/basedir-spec/basedir-spec-latest.html
- FHS：https://refspecs.linuxfoundation.org/fhs.shtml
