# 交易复盘功能开发路线图

## 背景

在现有 8848 股票分析系统上，新增一套"交易复盘 + 个人成长"功能，目标是构建一个会学习用户交易习惯的个性化 AI 交易教练。

核心闭环：
```
每次操作 → 录入 + 想法
    ↓
AI复盘 → 发现问题模式
    ↓
提炼为"个人skill" → 交易弱点/习惯档案
    ↓
日常分析时 → AI带着对用户的了解给建议
```

---

## 已完成

### 第一步：trade_log 表 + 录入页面 ✅
- `trade_log` 表（app.py `init_db()`）
- `GET /review` 页面：录入表单 + 历史列表
- `POST /review/add`：保存一笔操作
- 字段：股票代码、操作时间、方向（买入/卖出/加仓/减仓）、价格、手数、当时的想法、情绪状态（冷静/有点冲动/很纠结）

### 第二步：单笔复盘 ✅
- `POST /review/analyze`：单笔复盘，调用 `call_ai_model_with_tools()`
- 辅助函数 `get_klines_around_date()`：取操作日前后各10个交易日 K 线
- 辅助函数 `build_review_prompt()`：构建复盘专用 system/user prompt
- 复盘结果持久化写回 `trade_log.review_result`（不依赖 temp_results）
- 复盘结果用 marked.js 渲染展示在 `/review` 页面
- "提炼到个人画像"按钮已占位（disabled）

### 个人画像基础设施 ✅
- `load_skills()` 扩展：支持读取 `skills/personal/*.md`，个人画像置于末尾（AI 更容易记住）
- `skills/personal/trading_profile.md`：个人画像初始模板（待积累）

---

## 待开发

### 第三步：阶段复盘（跨股找规律）

**功能描述：**
选择时间范围，把该范围内所有 trade_log 记录（可跨多只股票）一起交给 AI，找出行为模式和规律。

**关键设计决策：**

1. **入口位置**：在 `/review` 页面顶部或历史列表上方，加一个"阶段复盘"区域，包含开始日期、结束日期两个输入框 + "开始阶段复盘"按钮

2. **两阶段 AI 调用**（解决 token 量问题）：
   - 第一阶段：对每笔操作逐一简评（每笔只传关键指标，不传完整 K 线）
   - 第二阶段：把所有简评汇总，再做整体行为模式归纳
   - 或者：一次调用，但 K 线数据只传操作日当天 ±3 天，不传前后10天

3. **prompt 设计重点**：
   - 让 AI 从"你作为一个交易者的整体习惯"角度分析，而不是逐笔评判
   - 重点找：是否总在涨停板追入、止损执行率、大盘弱势时是否仍频繁操作、情绪与胜率的关系
   - 输出格式：先逐笔简评表格，再整体规律总结

4. **路由设计**：
   - `POST /review/stage_analyze`：接收 start_date、end_date，查询范围内所有 trade_log，构建 prompt，调用 AI
   - PRG 模式：结果存 temp_results，redirect 回 `/review?result_id=xxx&type=stage`
   - GET `/review` 需要区分展示单笔复盘结果还是阶段复盘结果（用 `type` 参数或 payload 里的字段区分）

5. **数据量控制**：
   - 阶段复盘时，每笔操作只附带：操作日当天的 K 线（开高低收）+ 操作日前5天的收盘价趋势
   - 不传完整前后10天数据，避免 token 爆炸

**新增内容：**
- `build_stage_review_prompt(trades, klines_map)` 辅助函数
- `POST /review/stage_analyze` 路由
- `/review` 页面新增阶段复盘表单区域
- 阶段复盘结果展示区（和单笔复盘结果区分开）

---

### 第四步：个人 skill 生成与更新

**功能描述：**
复盘后 AI 提议"建议更新个人画像如下"，用户确认后写入 `skills/personal/trading_profile.md`。

**核心设计原则：AI 提议，人工确认，才写入。** 不能让 AI 自动覆盖个人画像。原因：
- 防止 AI 基于少数几次操作过度归纳
- 用户自己也在这个过程中加深自我认知
- 保持用户对自己画像的掌控感

**交互流程：**
```
单笔/阶段复盘完成
    ↓
用户点击"提炼到个人画像"按钮（现在是 disabled 占位）
    ↓
POST /review/extract_profile（传入复盘结果文本）
    ↓
AI 读取现有 trading_profile.md + 本次复盘结果
AI 生成"建议修改内容"（diff 形式：新增哪些条目、修改哪些条目）
    ↓
展示"建议修改预览"页面（显示现有内容 vs 建议修改后的内容）
    ↓
用户点"确认写入" → POST /review/confirm_profile → 写入文件
用户点"放弃" → 不做任何操作
```

**路由设计：**
- `POST /review/extract_profile`：接收复盘结果，调用 AI 生成画像更新建议，PRG 到预览页
- `GET /review/profile_preview?result_id=xxx`：展示现有画像 vs 建议修改
- `POST /review/confirm_profile`：接收确认，写入 `skills/personal/trading_profile.md`

**prompt 设计重点：**
- system prompt：告知 AI 它的任务是"提炼交易者的行为模式到个人画像文件"
- user prompt：现有 `trading_profile.md` 全文 + 本次复盘结果
- 要求 AI 输出：完整的新版 `trading_profile.md` 内容（不是 diff，直接输出完整文件，方便直接写入）
- 要求 AI 说明：相比旧版，新增了哪些内容、修改了哪些内容（供用户确认时参考）

**新增内容：**
- `POST /review/extract_profile` 路由
- `GET /review/profile_preview` 页面（或在 review.html 里用 modal 展示）
- `POST /review/confirm_profile` 路由（写文件）
- "提炼到个人画像"按钮从 disabled 改为可用

---

### 第五步：日常分析融入个人画像（已部分完成）

`load_skills()` 已经扩展支持读取 `skills/personal/*.md`，个人画像会自动注入到日常 AI 分析的 system prompt 中。

待确认：`skills/personal/trading_profile.md` 内容充实后，验证日常 `/ai_analyze` 的输出是否有明显的个性化体现。

---

## 文件结构参考

```
app.py                          # 所有后端逻辑
templates/
  index.html                    # 主分析页面
  review.html                   # 复盘页面（已创建）
skills/
  01_*.md ~ 11_*.md             # 阿狼体系（通用）
  personal/
    trading_profile.md          # 个人交易画像（待积累）
```

## 技术约定

- 所有 POST 路由使用 PRG 模式：`save_temp_result(rid, payload)` → `RedirectResponse(303)`
- AI 调用统一使用 `call_ai_model_with_tools(system_prompt, user_prompt)`
- 数据库迁移：新字段用 `ALTER TABLE ADD COLUMN` + `try/except "duplicate column name"` 实现幂等
- 前端 Markdown 渲染：`marked.js`，`marked.parse(raw)`
- Bootstrap 5.3，无 JS 框架，服务端渲染
