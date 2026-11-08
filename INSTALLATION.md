# 安装与版本

当前源码 Skill/核心 `2.7.0`，schema `skill_v2_003`。安装位置由使用者运行安装器时指定；此源码包不含本机安装记录、环境变量、数据库、业务资料或导出文件。

需要 Python 3.12 和 PostgreSQL 16，连接配置使用自己的本地环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e backend
```

将 `.env.example` 复制为本地 `.env` 并填写自己的 PostgreSQL 连接参数，然后执行：

```powershell
.\.venv\Scripts\python.exe scripts/b2b_leads.py capabilities
.\.venv\Scripts\python.exe scripts/b2b_leads.py prepare
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install-b2b-leads.ps1
```

### 密钥（spec b2b-leads-2.6 §3.5：宁可起不来，不要跑一半崩）

| 密钥 | 必填 | 缺失行为 |
| --- | --- | --- |
| `PROSPECT_SOURCE_RAW_ENVELOPE_SECRET` | 生产必填 | 生产拒绝启动；开发降级明文存信封并打 `dev-insecure` 标记 |
| `PROSPECT_CURSOR_SECRET` | 可选（S13 预埋，生成不用） | 不分页可不配；一旦配置，长度必须 ≥32 字节，否则启动即拒 |
| `PROVIDER_CREDENTIAL_KEY` | 接入付费源后必填 | 当前 `paid_sources=0`，状态 `n/a` |

密钥相互独立，不得混用。`capabilities` 输出 `secrets` 字段报告三者状态；
除 `capabilities` / `validate-input` / `stage-draft` 外，任何命令启动前都会校验，
生产缺信封密钥直接报 `SECRETS_MISSING`，不做半截执行。

安装器按当前源码生成独立发布标识；机器上的源码路径、业务数据库与安装目标由使用者配置。使用已安装 Skill 的 `scripts/control.py` 入口执行研究。

测试：`.venv/Scripts/python.exe -m pytest backend/tests -q`。数据库测试使用临时独立库，需要创建测试库权限；跳过不算验收通过。

本版运行效率实施、受控基准及已知边界见 [实施记录](docs/IMPLEMENTATION-runtime-efficiency-v2.6.md)，规范见 [优化 spec](docs/specs/SPEC-runtime-efficiency-v2.6.md)。这些基准来自固定 replay，不代表真实 200 家任务的交付时效已达标。
