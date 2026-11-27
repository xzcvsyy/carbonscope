"""
CarbonScope ML evaluation (K-Means + Isolation Forest). READ-ONLY:
it reads activity_records through the mining job's own functions and
prints metrics. It does not write anything to Supabase.

Run from the backend folder, with the venv active:
    python eval_ml.py
"""
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest
from sklearn.metrics import adjusted_rand_score, davies_bouldin_score, silhouette_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

from app import mining
from app.mining import CLUSTER_LABEL_ORDER, FEATURE_COLUMNS, ISOLATION_CONTAMINATION, K_CLUSTERS

pd.set_option("display.width", 200)

df = mining.preprocess_data(mining.select_data())
n = len(df)
print(f"Records used: {n}  (note: Supabase returns at most 1,000 rows per query)")
if n < 20:
    raise SystemExit("Too few records for a meaningful evaluation.")
X = mining.transform_data(df)
raw = df[FEATURE_COLUMNS].to_numpy()

# Run the mining job's own mine() twice on copies -------------------------
d1, d2 = df.copy(), df.copy()
_, idx1 = mining.mine(X, d1)
mining.mine(X, d2)
d1["total"] = d1[FEATURE_COLUMNS].sum(axis=1)

print("\n== K-MEANS ==")
print(f"M-01 silhouette (k={K_CLUSTERS}, job's own metric): {mining.evaluate(X, idx1):.3f}")
print("M-01 silhouette and Davies-Bouldin by k (lower DB is better):")
for k in range(2, 7):
    lab = KMeans(n_clusters=k, random_state=42, n_init=10).fit_predict(X)
    print(f"      k={k}: silhouette={silhouette_score(X, lab):.3f}  DB={davies_bouldin_score(X, lab):.3f}")

print("\nM-02 cluster profile (mean kg CO2e per record):")
prof = d1.groupby("cluster_label")[FEATURE_COLUMNS + ["total"]].mean().round(1)
prof.insert(0, "records", d1["cluster_label"].value_counts())
prof = prof.reindex([c for c in CLUSTER_LABEL_ORDER if c in prof.index])
print(prof.to_string())
t = prof["total"].tolist()
print("      total CO2e increases low < moderate < high:", all(a < b for a, b in zip(t, t[1:])))

tr, va = train_test_split(raw, test_size=0.2, random_state=42)
sc = StandardScaler().fit(tr)
km = KMeans(n_clusters=K_CLUSTERS, random_state=42, n_init=10).fit(sc.transform(tr))
s_tr = silhouette_score(sc.transform(tr), km.labels_)
s_va = silhouette_score(sc.transform(va), km.predict(sc.transform(va)))
print(f"\nM-03 train(80%) silhouette={s_tr:.3f}   held-out(20%) silhouette={s_va:.3f}")

base = KMeans(n_clusters=K_CLUSTERS, random_state=42, n_init=10).fit_predict(X)
aris = [adjusted_rand_score(base, KMeans(n_clusters=K_CLUSTERS, random_state=s, n_init=10).fit_predict(X))
        for s in range(10)]
print(f"M-04 same seed twice, identical labels: {bool((d1['cluster_label'] == d2['cluster_label']).all())}")
print(f"M-04 10 other seeds vs seed 42 (ARI, 1.0 = identical): mean={np.mean(aris):.3f}  min={np.min(aris):.3f}")

print("\n== ISOLATION FOREST ==")
flag = d1["is_anomaly"].to_numpy()
print(f"M-05 flagged {int(flag.sum())} of {n} = {flag.mean()*100:.1f}%  (contamination setting {ISOLATION_CONTAMINATION*100:.0f}%)")

ext = np.abs(X).max(axis=1)
print(f"M-06 mean largest |z-score|: flagged={ext[flag].mean():.2f}  not flagged={ext[~flag].mean():.2f}")
print("M-06 top 5 flagged (lowest anomaly score):")
top = d1[d1["is_anomaly"]].sort_values("anomaly_score").head(5)
for _, r in top.iterrows():
    dom = max(FEATURE_COLUMNS, key=lambda c: r[c]).replace("_co2e", "")
    print(f"      total={r['total']:.1f} kg  score={r['anomaly_score']:.3f}  biggest category={dom}")
print(f"      dataset median total={d1['total'].median():.1f} kg, max={d1['total'].max():.1f} kg")

print(f"\nM-07 same seed twice, identical flags: {bool((d1['is_anomaly'] == d2['is_anomaly']).all())}")
base_flag = IsolationForest(contamination=ISOLATION_CONTAMINATION, random_state=42).fit_predict(X) == -1
for s in range(5):
    f = IsolationForest(contamination=ISOLATION_CONTAMINATION, random_state=s).fit_predict(X) == -1
    jac = (f & base_flag).sum() / max((f | base_flag).sum(), 1)
    print(f"      seed {s}: flagged={int(f.sum())}  overlap with seed 42 (Jaccard)={jac:.2f}")
for c in (0.01, 0.02, 0.05):
    f = IsolationForest(contamination=c, random_state=42).fit_predict(X) == -1
    print(f"      contamination {c}: flagged={int(f.sum())}")
