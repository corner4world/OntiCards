# 上线迁移 SOP（OntiCards_Api）

> 适用场景：把本地开发的结构变更安全地应用到**已存在的线上库**。
> 三条路径（全新装库 / 本地开发库 / 线上库）最终必须抵达同一个结构，本文只管第三条。

---

## 零、先理解一件事：线上库靠什么"知道"自己该升到哪

线上库不读 `init.sql`，也不看你的模型代码。它只认两样东西：

```
alembic_version 表里的一行  +  migrations/versions/ 目录里的脚本链
```

每次 `flask db upgrade`，Alembic 做的是：

```
读 alembic_version         → 假设是 0001_baseline
扫 migrations/versions/    → 发现 head 是 x7f2a，链是 0001 → a1b2 → x7f2a
执行 0001 之后的所有迁移
更新 alembic_version       → x7f2a
```

**所以「本地 init.sql 已经是最新的、线上库还是旧的」是正常且期望的状态。**
init.sql 里那个种子版本号只服务「全新装库」，与线上升级无关。

---

## 一、首次接管：线上库是手工建的

线上库如果是手工建表、从来没有 `alembic_version`，需要一次性接管。**推荐用 init.sql 落地，不要手写 stamp。**

### 1.1 备份（不可跳过）

```bash
pg_dump -h <host> -U <user> -d <db> -Fc \
  -f backup_before_takeover_$(date +%Y%m%d_%H%M%S).dump
```

### 1.2 执行 init.sql

```bash
psql -h <host> -U <user> -d <db> -v ON_ERROR_STOP=1 -f init.sql
```

**为什么可以重复执行**：init.sql 里的 DDL 全部写成
`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` / `COMMENT ON ...`（幂等），
已存在的表不会被重建、数据不会丢。

### 1.3 版本记录会自动写入

init.sql 第 44-46 行：

```sql
INSERT INTO "public"."alembic_version" ("version_num")
SELECT '0001_baseline'
WHERE NOT EXISTS (SELECT 1 FROM "public"."alembic_version");
```

**只在表为空时插入**，所以对已有版本记录的库完全无害。

### 1.4 验证

```bash
flask db current     # 应输出 0001_baseline
flask db heads       # 应与 init.sql 第 45 行的值一致
```

两者不一致时，以 **init.sql 的种子值** 为准修正：

```bash
flask db stamp <init.sql第45行的值>
```

> `stamp` 是**认账**，不执行任何 DDL。执行前必须确认线上结构确实等于 init.sql，否则等于谎报版本。

- [ ] 备份文件已生成且非空
- [ ] `flask db current` 有输出，且等于 init.sql 种子
- [ ] `flask db heads` 与 current 一致

---

## 二、日常发版（每有新迁移时）

### 2.1 本地：发版前自检（必做）

```bash
python migrations/deploy_check.py
```

闸门必须全绿。它会拦住这几类事故：

| 检查项 | 拦住的事故 |
|---|---|
| 种子 == head | 全新装库时重放历史迁移 → `relation already exists` |
| 迁移链单一 head | 出现分叉，upgrade 报 `Multiple heads` |
| 无孤儿迁移 | `down_revision` 指向不存在的版本 |
| 空迁移告警 | `flask db migrate` 什么都没检测到，等于这次变更没写进迁移 |
| 三线一致 | init.sql / 模型 / 库 三者有漂移，上线后必出问题 |

### 2.2 本地：完整跑通一遍

```bash
flask db upgrade
flask db current      # 应 = head
python scripts/three_way_check.py    # 十一项全 0
```

**这一步的意义**：本地 upgrade 是这条迁移的**第一次执行**，生产是**第二次**。
本地跑不通的，生产一定跑不通。绝不要跳过本地验证直接上生产。

### 2.3 提交物清单

```
models/xxx.py                       ← 模型（新增或修改）
migrations/versions/yyy_xxx.py      ← 迁移脚本（只增不改）
init.sql                            ← DDL + 第 45 行种子
controllers/  services/             ← 业务代码（与迁移无关，但同批上线）
```

`migrations/` 整个目录是自包含的，首次部署需要全部提交；之后只需提交新增的 `versions/*.py`。

- [ ] `deploy_check.py` 全绿
- [ ] 本地 `upgrade` + `three_way_check` 通过
- [ ] `git status` 里上面四类文件都在

### 2.4 部署：容器启动时自动迁移

`docker/entrypoint.sh` 已在 `gunicorn` 之前插入：

```bash
flask db current || true
flask db upgrade
```

所以**部署动作就是重新构建镜像 + 重启容器**，迁移自动完成，无需手工进容器操作。

顺序（不可颠倒）：

```
① 备份生产库
② 构建新镜像
③ 停旧容器 → 起新容器（entrypoint 内执行 upgrade）
④ 验证
```

**为什么不能先起新代码再迁移**：新代码若访问尚不存在的列，会在迁移完成前的窗口期直接报错。
entrypoint 的方案天然避免了这个问题 —— 迁移失败则容器不启动，不会出现"半可用"状态。

### 2.5 验证（部署后立刻做）

```bash
docker logs <container> | grep entrypoint        # 确认迁移日志
docker exec <container> flask db current         # 应等于 head
```

另外用一个只读接口或健康检查确认应用能正常查到新字段。

---

## 三、回滚

### 3.1 先判断能不能回滚

| 变更类型 | 能否 `downgrade` | 说明 |
|---|---|---|
| 加表 | ✅ | downgrade 会 drop_table |
| 加列 | ✅ | downgrade 会 drop_column |
| 加可空列 + 回填 | ⚠ | 列能删，**回填的数据回不来** |
| 删列 | ❌ | downgrade 只能加回**空列**，数据永久丢失 |
| 删表 | ❌ | downgrade 只能重建**空表**，数据永久丢失 |
| 改类型/缩窄 | ⚠ | 回滚后可能因数据超长失败 |

**凡涉及删列 / 删表的版本，上线前必须单独备份那张表**：

```bash
pg_dump -h <host> -U <user> -d <db> -t <表名> \
  -f backup_<表名>_$(date +%Y%m%d_%H%M%S).sql
```

### 3.2 代码侧回滚

```bash
# 方式 A：回滚镜像到上一个版本（推荐，一步到位）
docker stop <new> && docker run <old_image>

# 方式 B：只回滚数据库
docker exec <container> flask db downgrade -1
docker exec <container> flask db current
```

**注意**：只回滚数据库而不回滚代码，旧代码读新结构通常没事；但回滚代码而不回滚数据库，
新代码依赖的列会消失。**稳妥顺序是代码和数据库一起回滚**（用旧镜像）。

### 3.3 应急：迁移失败但服务必须起来

```bash
docker run -e RUN_DB_MIGRATION=false ... <image>
```

跳过迁移直接启动，先把服务拉起来减少影响，再人工排查。**排查完务必修好迁移并正常重启**，
否则下次重启又会被拦住。

---

## 四、失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 容器启动后日志停在 `执行 flask db upgrade` | 迁移 SQL 本身报错 | 看完整 traceback；本地复现后改迁移（**前提：该迁移还没在别的环境执行过**） |
| `Target database is not up to date` | 库版本 < 脚本目录中某个起点 | 说明有人跳过了中间迁移，`flask db upgrade` 逐条补齐 |
| `Can't locate revision` | `alembic_version` 里的版本在 versions/ 中不存在 | 迁移文件被删了。恢复文件，或 `flask db stamp <正确版本>` 认账 |
| `Multiple heads` | 两条迁移的 `down_revision` 相同（并行开发） | 新建一条 merge 迁移：`flask db merge -m "merge" <head1> <head2>` |
| `relation already exists` | 表已存在但 Alembic 认为要新建 | 全新装库时种子没跟上 init.sql，导致重放历史迁移 |
| `column ... contains null values` | 给有数据的表直接加 NOT NULL 列 | 拆三步：加可空 → 回填 → 收紧非空 |

### 改已执行的迁移：只有这一种情况允许

```
该迁移还没有在「任何其他环境」执行过（只有你本地跑过）
    → 可以改，改完本地 downgrade + upgrade 重跑
已经在别人的库 / 生产执行过
    → 绝不能改。新建一条迁移补：
      flask db revision -m "fix xxx"
      在 upgrade() 里写修正逻辑
```

**改动已上线的迁移 = 让两个环境的结构永久分叉**，这是最难排查的一类故障。

---

## 五、多副本部署（当前未启用，提前了解）

现在的单容器部署下，entrypoint 里的 `flask db upgrade` 天然只执行一次。一旦改成多副本：

- docker-compose 多实例、K8s `replicas > 1` → 多个容器**同时** upgrade
- Alembic **没有分布式锁**，并发执行会冲突（轻则报错，重则版本记录错乱）

届时的正确做法是把迁移从 entrypoint 拆出去：

```yaml
# K8s 示例
initContainers:
  - name: migrate
    image: <same-image>
    command: ["flask", "db", "upgrade"]
# 主容器设 RUN_DB_MIGRATION=false
```

或用一个单独的 Job 在部署流水线里先跑完迁移，再滚动更新 Pod。

---

## 六、一页速查

```bash
# ---- 首次接管（线上库无 alembic_version）----
pg_dump ... -f backup.dump
psql ... -f init.sql
flask db current                     # == init.sql 种子

# ---- 日常发版 ----
python migrations/deploy_check.py     # 全绿才继续
flask db upgrade && flask db current  # 本地跑通
git add models/ migrations/versions/ init.sql
# → 构建镜像 → 部署（entrypoint 自动 upgrade）

# ---- 验证 ----
docker logs <c> | grep entrypoint
docker exec <c> flask db current

# ---- 回滚 ----
docker run <上一个镜像>                # 代码 + 库一起退
```

**三条不可破的规矩**

1. `migrations/versions/` 只能新增文件，已上线的不能改
2. 每次改 init.sql 的 DDL，第 45 行种子必须同步改成新 head
3. 删列 / 删表前单独备份那张表 —— downgrade 救不回数据
