# RiNG Roadmap

> 定稿:2026-07-08。定位:RiNG 的核心價值是縮短「session 在等你 → 你回覆它」的迴圈:
> **知道**(通知/看板)→ **分辨**(該先回誰)→ **抵達**(跳到正確終端)→ 回覆。
> 本計劃沿這個迴圈補最弱的環:先讓「抵達」可靠,再讓「分辨」聰明,然後守住地基。
> 每一項都寫成可獨立交辦的規格:動機/做法/驗收條件/規模/依賴。實作交給 subagent,
> 完成後由 fresh-context 審查者複驗到 PASS;一項一 commit,不混包。

## 執行順序總覽

| # | 項目 | 規模 | 依賴 |
|---|------|------|------|
| 1 | Session 配對地基(hook 綁定 + process tree 消歧) | L | — |
| 2 | `ring focus` 補完(既有指令的收尾) | S | 1 |
| 3 | 分辨層:等待原因分類 + 跳最久等待 | M | — |
| 4 | Hook heartbeat 健康監控 | M | — |
| 5 | `dd` 對 synthetic proc 列的語意修正 | S | — |
| 6 | 離席摘要(digest) | M | 3 |
| 7 | 🟡 閒置 session 管理 | S | — |

1 → 2 是主線,必須依序;3、4、5、7 彼此獨立,可穿插;6 依賴 3 的等待原因分類。

---

## 1. Session 配對地基(最高槓桿)

**動機**:`registry.py` 的 `_tmux_targets()` 用 cwd 猜 pane,`mapping.setdefault(path, target)` 讓同一資料夾的多個 session 只有第一個拿得到位置——同 cwd 多 session 時跳轉會跳錯或跳不到。跳錯一次,使用者就不再信任看板;這是整個工具可信度的地基。

**做法**:

- **hook session(精準綁定)**:hook process 跑在該 session 的終端環境裡。事件當下把 `$TMUX_PANE`、tty(`os.ttyname` 或 `ps` 查詢)、hook 的 PID 一併寫進該 session 的 registry 檔。focuser 端優先讀綁定資料直接跳,cwd 推測降為 fallback。
- **scan-only session(process tree 消歧)**:同 cwd 有多個候選 pane 時,從各 pane 的 PID 往子孫 process 找 claude / codex process,比對其 cwd 與啟動參數來決定歸屬,取代「第一個佔位」。
- 綁定資料要能失效:pane 已不存在(tmux 查無此 pane id)時視為無綁定,走 fallback,不硬跳。
- `tui.py` 的 `_has_cwd_collision()` 提示邏輯更新:hook session 已有精準綁定時不再顯示「裝 hook 才精準」的警告。

**驗收條件**:

- [ ] hook 事件寫入的 registry 檔含 pane/tty 欄位(單元測試以假環境變數驗證)
- [ ] 同 cwd 兩個 hook session,focuser 對兩者各自解析出不同的 target(單元測試)
- [ ] 同 cwd 兩個 scan-only session,process tree 消歧測試(mock process 樹)
- [ ] 綁定的 pane 已死時 fallback 不 crash、不誤跳(測試)
- [ ] 手動實測:tmux 同資料夾開兩個 Claude Code session,TUI 各按 Enter 跳轉,各自正確
- [ ] `uv run pytest`、`uv run poe lint`、`scripts/i18n_check.py` 全綠

**明確不做**:不動 dd/隱藏機制;不改 focuser plugin 介面(除非綁定資料必須透過它傳遞,屆時保持向後相容)。

## 2. `ring focus` 補完(既有指令的收尾)

**現況(2026-07-08 查證)**:`ring focus <session-id>` **已經實作**——`src/ring/commands/focus.py`(`run_focus`),接線於 `src/ring/cli.py:558`,重用 `ring.focus.jump` 與 live-TUI 的 IPC handoff。本項只剩收尾,不是新功能。

**動機**:CLI 跳轉是「通知從告知變入口」的接點(tmux key binding、SwiftBar 點擊、腳本都靠它),值得把邊角補齊。可靠度本身依賴第 1 項的配對地基。

**做法**:盤點既有實作的缺口再補:錯誤 id / 無法配對時的錯誤訊息與 exit code 是否清楚;session id 前綴匹配是否支援(若無,評估加上);README 中英文與 `ring completion` 是否涵蓋此指令。

**驗收條件**:

- [ ] 錯誤 id / 模糊前綴 / 無法配對三種情況各有明確錯誤訊息與 non-zero exit(缺的補上,含測試)
- [ ] README 中英文含指令說明;`ring completion` 補全含 focus(缺的補上)
- [ ] 第 1 項完成後手動實測:同 cwd 兩 session 各 `ring focus` 正確抵達
- [ ] 標準驗證全綠

## 3. 分辨層:等待原因分類 + 跳最久等待

**動機**:🔴 只說「在等」,沒說「等什麼」。等 permission、等回答問題、等 plan 批准的回覆成本差很多;分類後掃一眼就知道哪個 30 秒能打發。

**做法**:

- hook 攔事件時解析等待型態(permission prompt / AskUserQuestion / plan 批准 / 一般 idle),連同問題或工具呼叫的一行摘要寫進 registry。
- TUI 列上顯示等待型態圖示 + 摘要 + 已等待時長;既有的「waiting 詳情」視圖顯示完整問題文本。
- TUI 新增 hotkey(建議 `w`;注意 `g`/`G` 已被 grid 的 scroll-top/bottom 占用、`q`/`r`/`a`/`n`/`d`/`j`/`k` 也已用,實作時以 `tui.py` 現況為準再確認):直接跳到等待最久的 🔴 session——inbox 的 zero 鍵。

**驗收條件**:

- [ ] 各等待型態的 hook payload 解析有測試(用真實 payload 樣本)
- [ ] 無法分類時顯示一般 🔴,不 crash、不顯示錯誤型態(測試)
- [ ] hotkey 在無等待 session 時給提示而非 no-op(測試)
- [ ] 新字串走 i18n 流程,`.po`/`.mo` 同步
- [ ] 標準驗證全綠

## 4. Hook heartbeat 健康監控

**動機**:hook 掛掉的失敗模式是「狀態默默凍住」——看板顯示舊資料但外觀正常,比壞掉更危險。

**做法**:hook 每次成功執行時在 registry 記 heartbeat 時戳;看板對「狀態是活躍(🟢/🔴)但 heartbeat 距今超過門檻」的 session 標示「hook 可能失效」;`ring doctor` 加一項 heartbeat 檢查。門檻要考慮長時間工作中(🟢 跑長任務)本來就沒有事件——以「該 session 來源檔仍有更新但 hook 無心跳」為異常判準,不是單純時間。

**驗收條件**:

- [ ] heartbeat 寫入與 stale 判定有測試(含「長任務無事件」不誤報的案例)
- [ ] TUI 顯示 stale 標示(測試 render 輸出)
- [ ] `ring doctor` 新檢查項有測試
- [ ] 標準驗證全綠

## 5. `dd` 對 synthetic proc 列的語意修正

**動機**:2026-07-08 複驗遺留:`registry.py:756` 的 `synthetic:{cwd}` 列每次重建都填 `last_active=time.time()`,`dd` 後下一輪輪詢即復活。與其讓它「藏得掉」,不如誠實。

**做法**:對 source 為 proc 的 synthetic 列按 `dd` 時,TUI 提示「這是活的 process,無法隱藏」(或設計成有時限的 snooze,二擇一,以實作簡單者為準);不寫入隱藏清單,避免殘留條目。

**驗收條件**:

- [ ] synthetic 列按 `dd` 的行為有測試(不進隱藏清單 + 提示訊息)
- [ ] 新字串走 i18n 流程
- [ ] 標準驗證全綠

## 6. 離席摘要(digest)

**動機**:回座第一眼不該是掃整個看板,而是讀一段話:「你不在的 40 分鐘:A 跑完測試、B 等 permission 已 20 分鐘、C 已離場」。素材同時可餵每日 review 流程。

**做法**:新增 `ring digest [--since <時間>]`:彙整各 session 自指定時間以來的狀態變化(從 registry 的時戳與 stats 既有資料推導,不新增常駐記錄程序);`--format json` 供腳本用。TUI 啟動時若偵測到距上次開啟超過門檻,頂部顯示一行摘要入口。

**驗收條件**:

- [ ] digest 對「有等待中/有完成/有離場」混合情境的輸出有測試
- [ ] `--format json` schema 有測試
- [ ] 無資料時輸出友善空狀態,exit 0(測試)
- [ ] README 補指令;標準驗證全綠

**依賴**:等待原因分類(第 3 項)先做,digest 才能說出「B 在等 permission」而不只是「B 在等」。

## 7. 🟡 閒置 session 管理

**動機**:跑完停著的 session 佔 context 與注意力。超過閒置門檻就該被提醒處理,而不是永遠掛在看板中段。

**做法**:🟡 超過可設定門檻(config,預設建議 1–2 小時)時發一次通知(重用既有 notifier 管線,只發一次不重複轟炸);TUI 列上顯示已閒置時長。處理動作重用既有能力(跳轉過去收掉、或 `dd` 隱藏),不新造。

**驗收條件**:

- [ ] 門檻觸發只通知一次的去重邏輯有測試
- [ ] 門檻可由 config 設定、預設值合理(測試)
- [ ] 標準驗證全綠

---

## 明確不做(2026-07-08 定案)

- **遠端回覆**(從通知直接把選擇打回終端):使用者明確不要。
- **Session launcher / 範本啟動**:「開 session」不是痛點,且正面撞上 Claude Code 官方 surface 的演進方向。
- **Web 儀表板**:ntfy 已覆蓋離機場景,第二套 UI 是維護稅。
- **跨機器彙整**:等第二台機器常態跑 agent 再議。
- **Windows 支援**:focuser 生態(tmux/iTerm2/macOS/wmctrl)不在那裡。

## 維護說明

- 每完成一項:勾掉該項驗收條件、更新總覽表(可加「完成日期」欄),一項一 commit。
- 砍項目或改優先序屬產品決策,先與使用者確認再改本檔。
- 「明確不做」要復活某項,同樣先問使用者。
