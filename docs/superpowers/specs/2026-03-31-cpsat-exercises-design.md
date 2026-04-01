# CP-SAT Modeling Exercises — Design Spec

## Overview

為 solver 專案開發者提供 7 道漸進式 CP-SAT 建模練習題，從基礎模式到真實 solver/splitter 擴展。每題附 pytest 自動驗證 + 參考解。

## Branch & 隔離策略

- Branch: `zs/cpsat-exercises`，從 `main` 建立
- 所有檔案放在 `exercises/` 目錄下，不修改任何既有 code
- Tier 2-3 透過 **繼承** 現有 class 擴展，不直接改原始檔案

## 目錄結構

```
exercises/
├── README.md                         # 練習指南
├── conftest.py                       # 共用 fixtures（獨立複製）
├── tier1_foundations/
│   ├── ex1_bin_packing.py            # skeleton + 完整 docstring
│   ├── ex2_multi_assignment.py
│   ├── ex3_activation_var.py
│   ├── test_ex1.py
│   ├── test_ex2.py
│   └── test_ex3.py
├── tier2_solver_ext/
│   ├── ex4_rack_anti_affinity.py
│   ├── ex5_gpu_affinity.py
│   ├── test_ex4.py
│   └── test_ex5.py
├── tier3_splitter/
│   ├── ex6_spec_preference.py
│   ├── ex7_global_vm_limit.py
│   ├── test_ex6.py
│   └── test_ex7.py
└── solutions/
    ├── sol1_bin_packing.py
    ├── sol2_multi_assignment.py
    ├── sol3_activation_var.py
    ├── sol4_rack_anti_affinity.py
    ├── sol5_gpu_affinity.py
    ├── sol6_spec_preference.py
    └── sol7_global_vm_limit.py
```

## 每題 Skeleton 檔案格式

每個 `exN_*.py` 包含：

```
1. 模組 docstring:
   - 題目標題 & Tier
   - 學習目標（1-3 條，具體說明練到什麼 CP-SAT 模式）
   - 背景故事（用實際場景說明為什麼需要這個建模）
   - 預計時間
   - 前置知識（需要先完成哪些練習或讀哪些檔案）
   - 相關閱讀（solver 專案內的檔案 + OR-Tools 官方文件連結）
   - 預期效益（完成後你會具備的能力）

2. 函式/類別 skeleton:
   - 完整的 type hints 和函式簽名
   - 參數說明
   - 回傳值說明
   - 範例 input/output

3. 分步提示（由淺入深的 hints）:
   - Hint 1: 大方向提示（應該建什麼變數）
   - Hint 2: 約束提示（應該加什麼 constraint）
   - Hint 3: 目標函數提示（Maximize/Minimize 什麼）
   - Hint 4: 解提取提示（怎麼從 solver result 讀出答案）
   每個 hint 放在 collapsed block 裡，讓你自己決定要不要看

4. raise NotImplementedError("YOUR CODE HERE")
```

## 題目明細

### Tier 1: Foundations（獨立檔案）

| # | 題目 | 核心模式 | 時間 |
|---|------|---------|------|
| 1 | 單機 Bin Packing | BoolVar + capacity + Maximize | 10 min |
| 2 | 多機 VM Assignment | assign matrix + one-per-vm + capacity | 15 min |
| 3 | Activation Variable Pattern | pre-allocate + BoolVar activation + symmetry breaking | 15 min |

### Tier 2: Solver Extensions（繼承 VMPlacementSolver）

| # | 題目 | 核心模式 | 時間 |
|---|------|---------|------|
| 4 | Rack-Level Anti-Affinity | 按 topology 分組 constraint + 繼承擴展 | 20 min |
| 5 | GPU Affinity Constraint | 條件式 constraint + 整數算術 + solve pipeline 擴展 | 25 min |

### Tier 3: Splitter & Joint Model（繼承 ResourceSplitter / 擴展 orchestration）

| # | 題目 | 核心模式 | 時間 |
|---|------|---------|------|
| 6 | Spec Preference Soft Constraint | 多 objective term 平衡 + 繼承 splitter | 30 min |
| 7 | 跨 Requirement 全域 VM 上限 | 跨模組 constraint + orchestration 理解 | 40 min |

## 驗證方式

- **pytest**: `pytest exercises/tier1_foundations/test_ex1.py -v`
- **全部跑**: `pytest exercises/ -v`
- **參考解**: `exercises/solutions/sol1_bin_packing.py`

## 不在範圍內

- 不修改任何既有 `app/`、`tests/`、`docs/` 檔案
- 不新增 production dependencies
- 不涉及 HTTP/API 層面的練習
