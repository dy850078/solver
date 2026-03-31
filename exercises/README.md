# CP-SAT Modeling Exercises

## 如何開始

```bash
# 確保在 zs/cpsat-exercises branch
git checkout zs/cpsat-exercises

# 啟動虛擬環境
source .venv/bin/activate

# 跑單題測試（紅燈 → 你寫 code → 綠燈）
pytest exercises/tier1_foundations/test_ex1.py -v

# 跑某個 tier 的所有測試
pytest exercises/tier1_foundations/ -v

# 跑全部練習測試
pytest exercises/ -v
```

## 練習順序

| # | 題目 | 路徑 | 預計時間 |
|---|------|------|---------|
| 1 | 單機 Bin Packing | `tier1_foundations/ex1_bin_packing.py` | 10 min |
| 2 | 多機 VM Assignment | `tier1_foundations/ex2_multi_assignment.py` | 15 min |
| 3 | Activation Variable | `tier1_foundations/ex3_activation_var.py` | 15 min |
| 4 | Rack Anti-Affinity | `tier2_solver_ext/ex4_rack_anti_affinity.py` | 20 min |
| 5 | GPU Affinity | `tier2_solver_ext/ex5_gpu_affinity.py` | 25 min |
| 6 | Spec Preference | `tier3_splitter/ex6_spec_preference.py` | 30 min |
| 7 | Global VM Limit | `tier3_splitter/ex7_global_vm_limit.py` | 40 min |

## 驗證方式

1. **pytest 自動驗證**：跑測試看紅燈→綠燈
2. **參考解對照**：`solutions/sol{N}_*.py` 對照你的建模思路差異

## 建議

- 按順序做，每題都建立在前面的基礎上
- 先自己想，卡住了再展開 skeleton 裡的 hints
- 完成後跟參考解比較，看建模方式有什麼不同
