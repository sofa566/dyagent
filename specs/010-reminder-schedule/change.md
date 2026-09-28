# 010-reminder-schedule 變更提案

## 目標

將「腎友回報催報」提升為 dyagent 全系統可重用的提醒排程能力，提供統一的：

1. 規則定義（Policy）
2. 派送任務（Job）
3. 投遞稽核（Delivery Log）
4. Crontab 任務排程（Celery + Redis + RedBeat）
5. 後台作業 UI（查詢、補發、policy 管理、投遞紀錄、Crontab 任務）

## 背景

- `009-renal-care` 已落地腎友場景提醒資料模型與 API。
- 既有背景 dispatcher 採固定時段輪詢，缺少可由後台動態定義 Cron 的能力。
- 本次採 Celery + Redis + RedBeat 作為排程執行層，FastAPI 負責任務定義與執行授權。

## 本次要解決的問題

1. 不同業務流程若各自實作提醒，會造成重複開發與維運分裂。
2. 缺少可配置 Crontab 任務，營運無法自行定義啟動時間與執行動作。
3. 缺少跨場景觀測指標，難以營運管理。

## 不在本次範圍

1. 多通道（Email、SMS、推播）完整上線。
2. 複雜事件編排器（例如 DAG / 規則引擎 DSL）。
3. 跨租戶計費與商業化限流策略。

## 交付原則

1. 先以 LINE 催報改造驗證通用 Crontab 任務框架。
2. 以資料表為核心，所有提醒都可追溯。
3. 任務執行與業務流程解耦，透過任務型別與 payload 擴展。
