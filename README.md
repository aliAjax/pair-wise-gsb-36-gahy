# 水权账户与生态预留台

一个仅使用 Python 标准库实现的水权账户、计量、转让审批、**生态预留**和干旱情景服务。SQLite 保存账户额度、取水记录、季节/影响规则、生态预留、预留冲突和完整审计日志。

## 运行

```bash
python3 app.py --init
python3 app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 创建北区水库、河口灌区两个示例账户，写入 7 月季节上限、最小留存规则，以及北区水库一条 **7/1–8/31、150 单位的夏季生态预留**。数据库默认 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。

## 分层结构（资料 / 判定 / 保存 / 页面分开）

- `waterright/materials.py` — **资料层**：登记资料的读取与校验（账户、转让、取水、预留、解除），不碰数据库。
- `waterright/rules.py` — **判定层**：纯函数。区间交叠、某日预留持有量、可用额度分解、待审转让挤占、季节/留存约束、提前解除可恢复量、干旱分配。
- `waterright/storage.py` — **保存层**：SQLite 表结构与全部 SQL（含 `reservations`、`reservation_conflicts` 两张表）。
- `waterright/service.py` — 用例编排，事务边界（`BEGIN IMMEDIATE`）与审计都在这里。
- `app.py` — **页面/HTTP 层**：路由、`X-User`/`X-Role` 身份头、静态页面，不含业务规则。

## 生态预留怎么工作

- 账户按**起止日期**登记预留水量、**依据**（调度文件/政策）和**经办人**；同一账户的日期区间（含提前解除后仍持有的段落）不能交叠。
- `POST /api/reservations/preview` **提交试算**：返回预留前后的可用额度、扣减后额度变化，以及会被挤到的待审转让（按提交先后排队，含缺口），不落库。
- `POST /api/reservations/confirm` **确认登记**：被挤到的待审转让不自动驳回，而是在**待处理区**生成冲突记录。
- 预留生效后，**转让、取水、干旱分配都按扣减预留后的额度**计算；普通批准会被引导到预留台先处理冲突。
- 待处理区：
  - `POST /api/reservations/conflicts/{id}/occupy`：审核人确认该转让**动用预留**，缺口部分计入预留占用并随转让划转；
  - `POST /api/reservations/conflicts/{id}/dismiss`：仅在待处理区登记人工结论；
  - 转让被退回时，其冲突记录自动关闭。
- `POST /api/reservations/{id}/release` **提前解除**：只恢复**未被后续操作占用**的部分；已占用部分跟随原转让记录继续，不再恢复。到期记录自动失效，无需解除。

## API

请求头 `X-User` 和 `X-Role` 模拟身份。角色：`editor`（配额/预留）、`reviewer`（审核）、`meter`（计量）、`viewer`。

- `POST /api/accounts`、`POST /api/rules/season`、`POST /api/rules/impact`
- `POST /api/transfers`、`POST /api/transfers/{id}/approve|reject`
- `POST /api/usage`
- `GET /api/accounts`、`GET /api/accounts/{id}/available?as_of=YYYY-MM-DD`（含 `ecology_reserved`、`effective_quota`）
- `POST /api/reservations/preview|confirm`、`POST /api/reservations/{id}/release`
- `GET /api/reservations`、`GET /api/reservations/conflicts?status=pending`
- `POST /api/reservations/conflicts/{id}/occupy|dismiss`
- `GET /api/drought/simulate?supply=1000&reduction=0.3&as_of=YYYY-MM-DD`
- `GET /api/audit`

余额/审批使用 `BEGIN IMMEDIATE`，把余额判断与写入放在同一事务中。最小留存按转出账户**扣减生态预留后**的额度计算。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

- `tests/test_app.py`：转让审批与计量、重复计量事件、季节/留存规则、预占导致余额不足、发起人自审冲突。
- `tests/test_reservation.py`：预留登记/交叠、试算额度变化与挤占、冲突进入待处理区、生效后转让/取水/干旱按扣减额度计算、动用预留、提前解除只恢复未占用部分。
