---
name: swe-numpy-standard-stats
description: Use when computing, optimizing, or reviewing summary statistics (mean, median, std, var, min/max, percentiles, IQR) on numerical arrays with NumPy, managing multi-dimensional axes, handling NaN/missing values, or resolving ddof discrepancies.
---

# NumPy Standard Statistics Reference & Best Practices

Compute descriptive summary statistics for 1D and multi-dimensional arrays using NumPy, avoiding common pitfalls around missing data (`NaN`), degrees of freedom (`ddof`), and array broadcasting.

---

## 1. Quick Reference: Core vs. NaN-Safe Functions

Standard NumPy statistical functions propagate `NaN` values (returning `nan` if a single missing value exists). Use the `nan*` counterparts when dealing with datasets that may contain missing entries:

| Metric | Standard Function | NaN-Safe Equivalent | Notes |
| :--- | :--- | :--- | :--- |
| **Mean** | `np.mean(a)` | `np.nanmean(a)` | Arithmetic average |
| **Median** | `np.median(a)` | `np.nanmedian(a)` | 50th percentile (robust to outliers) |
| **Std Deviation** | `np.std(a, ddof=...)` | `np.nanstd(a, ddof=...)` | Default `ddof=0` (population) |
| **Variance** | `np.var(a, ddof=...)` | `np.nanvar(a, ddof=...)` | Default `ddof=0` (population) |
| **Min / Max** | `np.min(a)`, `np.max(a)` | `np.nanmin(a)`, `np.nanmax(a)` | Range and extreme bounds |
| **Percentile** | `np.percentile(a, q)` | `np.nanpercentile(a, q)` | `q` in `[0, 100]` |
| **Quantile** | `np.quantile(a, q)` | `np.nanquantile(a, q)` | `q` in `[0.0, 1.0]` |
| **Weighted Mean** | `np.average(a, weights=w)` | N/A (filter masks first) | Computes $\sum(w_i x_i) / \sum(w_i)$ |

---

## 2. Common Pitfalls & How to Avoid Them

### A. Population vs. Sample Degrees of Freedom (`ddof`)

> [!WARNING]
> **NumPy and Pandas have opposite defaults for degrees of freedom!**
> - NumPy: `np.std(x)` and `np.var(x)` default to `ddof=0` (population statistics, dividing by $N$).
> - Pandas: `df['col'].std()` and `df['col'].var()` default to `ddof=1` (sample statistics, dividing by $N - 1$).

To match sample statistics (unbiased estimator) in NumPy:

```python
import numpy as np

sample = np.array([10.0, 20.0, 30.0, 40.0, 50.0])

# Population (default ddof=0) -> divides by N (5)
pop_std = np.std(sample, ddof=0)      # 14.1421...
pop_var = np.var(sample, ddof=0)      # 200.0

# Sample (unbiased, ddof=1) -> divides by N - 1 (4)
sample_std = np.std(sample, ddof=1)   # 15.8113... (Matches pandas default)
sample_var = np.var(sample, ddof=1)   # 250.0      (Matches pandas default)
```

### B. Missing Data & NaN Propagation

```python
data_with_nans = np.array([10.0, 20.0, np.nan, 40.0, 50.0])

# Wrong: Returns nan
bad_mean = np.mean(data_with_nans)          # nan

# Correct: Ignores NaNs
safe_mean = np.nanmean(data_with_nans)      # 30.0
safe_std = np.nanstd(data_with_nans, ddof=1)# 18.2574...
```

### C. Broadcasting Reductions with `keepdims=True`

When normalizing or standardizing multi-dimensional arrays, use `keepdims=True` so the resulting array retains shape `(N, 1)` or `(1, M)` and broadcasts cleanly back against the original array without manual reshaping.

```python
# Matrix of shape (5, 2): 5 samples, 2 features
features = np.array([
    [150.0, 50.0],
    [160.0, 60.0],
    [170.0, 65.0],
    [180.0, 80.0],
    [190.0, 90.0],
])

# Compute feature-wise mean and std across rows (axis=0)
feat_mean = np.mean(features, axis=0, keepdims=True)  # Shape: (1, 2)
feat_std = np.std(features, axis=0, ddof=1, keepdims=True)  # Shape: (1, 2)

# Z-score normalization directly broadcasts without error
z_scores = (features - feat_mean) / feat_std
```

---

## 3. Percentiles and Outlier Detection (IQR)

Use `np.percentile` or `np.nanpercentile` for robust dispersion analysis:

```python
scores = np.array([12, 14, 15, 18, 19, 21, 22, 25, 28, 30, 95])

# Calculate 25th, 50th (median), and 75th percentiles simultaneously
q25, q50, q75 = np.percentile(scores, [25, 50, 75])
iqr = q75 - q25

# Tukey's fences for outlier thresholds
lower_bound = q25 - 1.5 * iqr
upper_bound = q75 + 1.5 * iqr

outliers = scores[(scores < lower_bound) | (scores > upper_bound)]
# outliers -> array([95])
```

---

## 4. Performance & Numerical Precision Guidelines

1. **Precision Accumulation**: For `float32` arrays with large element counts, avoid overflow or precision loss during summation by specifying `dtype=np.float64`:
   ```python
   mean_val = np.mean(large_fp32_arr, dtype=np.float64)
   ```
2. **Empty Slices**: Reducing an empty array (or an array of all NaNs with `nanmean`) emits a `RuntimeWarning: Mean of empty slice` and returns `np.nan`. Always guard empty arrays if input sizes can be zero:
   ```python
   mean_val = np.mean(arr) if arr.size > 0 else 0.0
   ```
3. **In-place Output Allocation**: When running tight loops or real-time pipelines, use the `out` parameter to avoid reallocating intermediate arrays:
   ```python
   out_buffer = np.empty(5, dtype=np.float64)
   np.mean(large_matrix, axis=1, out=out_buffer)
   ```
