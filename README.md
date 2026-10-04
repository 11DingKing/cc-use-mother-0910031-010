# 医美项目分级备案

本项目维护医美项目分级备案的领域约定与 Python 后端：管理项目定义、别名、分级规则版本、设备与人员依赖；备案申请先做完整性检查，再进入多角色有序复核；分类更正、项目拆分、依赖替代与紧急暂停形成可追溯的规则版本链；历史服务永久指向备案当时的规则快照；接口支持分类差异比较与规则变更对机构授权的影响分析。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/medical_filing/`：备案后端（仅标准库，SQLite 持久化）。
  - `database.py`：建表与示例数据（名称相近但分级不同的项目、两家机构）。
  - `catalog.py`：项目定义、别名解析、机构场地/设备/人员资源。
  - `rules.py`：规则版本链（更正 / 拆分 / 依赖替代 / 紧急暂停）、差异比较、影响分析。
  - `filings.py`：完整性检查、多角色复核状态机、备案快照冻结、历史服务。
  - `api.py`：基于 `http.server` 的 REST 接口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约与后端回归测试（`unittest`，无需外部依赖）。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```

## 启动 HTTP 服务

```bash
PYTHONPATH=src python3 -m medical_filing.api --port 8000 --db data/filing.db --seed
```

## 关键接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/procedures` | 登记项目并发布 V1 分级规则 |
| GET | `/api/procedures/resolve?ref=名称/别名/编码` | 精确解析项目（相近名称不混淆） |
| POST | `/api/procedures/{code}/aliases` | 维护别名（全局唯一，不可共用） |
| POST | `/api/procedures/{code}/corrections` | 分类更正，生成新版本 |
| POST | `/api/procedures/{code}/substitutions` | 设备/人员依赖替代 |
| POST | `/api/procedures/{code}/suspension` | 紧急暂停（阻断新备案，历史不动） |
| POST | `/api/procedures/{code}/splits` | 项目拆分（原项目停用，新项目回链） |
| GET | `/api/procedures/{code}/rules` | 查看规则版本链 |
| GET | `/api/rules/diff?a=&b=` | 比较两个规则版本的分类差异 |
| GET | `/api/procedures/{code}/impact` | 列出规则变更影响的机构授权 |
| POST | `/api/completeness-checks` | 备案前置完整性检查 |
| POST | `/api/filings` | 提交备案（强制先过检查，冻结规则快照） |
| POST | `/api/filings/{id}/reviews` | 多角色按序复核（执业人员→复核专家→监管人员） |
| POST | `/api/filings/{id}/archive` | 授权归档（快照仍可追溯） |
| POST | `/api/filings/{id}/services` | 登记历史服务 |
| GET | `/api/services/{id}` | 历史服务及其当时备案快照 |

## 领域规则要点

1. **完整性检查前置**：备案提交必须先通过场地、必备设备、必备人员三项检查；项目处于暂停/拆分状态时不予受理。
2. **多角色有序复核**：前序角色通过后当前角色才能操作；任一角色驳回则流程终止，后续任务标记跳过；三角色全部通过才授权。
3. **版本链**：每次更正/替代/暂停/拆分都产生新版本，`replaces_rule_id` 与 `superseded_by_rule_id` 双向串联；拆分出的新项目 V1 标记 `split_derivative` 并回链原规则。
4. **历史冻结**：备案行固化 `rule_id` 与风险等级，规则后续变更不改写备案与已登记服务；归档后仍可追溯。
5. **影响分析**：以最新规则重算每个已授权/已归档机构的满足情况，给出缺口与受影响原因（含紧急暂停），便于监管侧通知整改。
