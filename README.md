# 跨区域水资源使用权分配与转让（生态预留台）

仅使用 Python 标准库实现的水权账户、计量、转让审批、干旱情景与**生态预留**服务。SQLite 保存账户额度、取水记录、季节规则、上下游影响规则、生态预留、冲突台账和完整审计日志。

## 代码分层

资料、判定、保存和页面分开整理，互不依赖第三方库：

- `water/schema.py`：资料层，全部建表语句。
- `water/domain.py`：判定层，日期区间交叠、窗口内预留锁定量、扣减后许可额度、被挤转让排序、提前解除可恢复量等纯函数。
- `water/store.py`：保存层，SQLite 事务读写与冲突台账维护。
- `app.py`：HTTP 路由层，只做请求解析、身份头传递与响应序列化；`seed_demo` 也在此。
- `static/index.html`：页面层，生态预留台（预检 → 确认 → 待处理区 → 生效/解除）。

## 运行

```bash
python app.py --init
python app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 创建北区水库和河口灌区两个示例账户，添加 7 月季节上限、最小留存规则、一条 7–9 月待生效的夏季生态预留。数据库默认 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。

## 生态预留规则

- 账户按**起止日期、预留水量、依据、经办人**登记；同一账户的预留日期区间（闭区间，首尾相接不算）不能交叠。
- 申请先调 `preview`：返回可用额度前后变化、生态预留锁定量和会被挤到的待审转让（按生效日排列并给出缺口），**预检不落库**。
- 确认后记录为待生效（pending）；**生效**（activate）当天起才真正扣减额度，同时把放不下的待审转让写入冲突表并留在**待处理区**。冲突未解除前这些转让不能批准；转让被退回/批准或额度松动后，冲突自动转为 resolved 并留痕。
- 生效期内转让、取水（含月份季节上限）和干旱分配一律按**扣减生态预留后的许可额度**计算；许可额度本身不划转，窗口结束自动释放。
- **提前解除**（release）只恢复解除日之后仍未被后续取水、待审转让占用的部分（`restored_amount`），其余跟着原取水/转让记录继续走；解除日之前仍按原区间锁定。

## API

请求头 `X-User` 和 `X-Role` 模拟身份。角色：`editor`（生态调度员/配额管理员）、`reviewer`、`meter`、`viewer`。

- `POST /api/accounts`：建立账户（额度、优先级、有效期）。
- `POST /api/rules/season`、`POST /api/rules/impact`：季节比例上限、上下游最小留存比例。
- `POST /api/reservations/preview`：预留预检（可用额度变化与被挤待审转让）。
- `POST /api/reservations`：确认登记预留（待生效）。
- `POST /api/reservations/{id}/activate`：确认生效（body 可带 `as_of`）。
- `POST /api/reservations/{id}/release`：提前解除，返回恢复量与残余冲突。
- `GET /api/reservations`、`GET /api/reservation-conflicts?status=open|all`：预留记录与待处理区。
- `POST /api/transfers`：发起转让；待审批金额立即预占。
- `POST /api/transfers/{id}/approve|reject`：审核；发起人不能审批自己的记录。
- `POST /api/usage`：按计量事件登记取水，同一账户同一事件编号只入账一次。
- `GET /api/accounts/{id}/available?as_of=YYYY-MM-DD`：扣减预留、实际用量和待审批预占后的可用额度。
- `GET /api/accounts?as_of=YYYY-MM-DD`：账户列表（含各日期的锁定量与扣减后额度）。
- `GET /api/drought/simulate?supply=1000&reduction=0.3&as_of=YYYY-MM-DD`：高优先级先分配，同级按扣减后剩余额度比例分配。
- `GET /api/audit`：完整操作审计。

余额、生效、解除、审批等判断都在 `BEGIN IMMEDIATE` 事务内完成，并发提交不会绕过额度检查。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖转让审批与实际计量、重复计量事件、季节/最小留存规则、预占余额与自审冲突，以及生态预留的区间交叠、预检挤占、生效冲突台账、扣减后转让/取水/干旱分配、提前解除只恢复未占用部分。
