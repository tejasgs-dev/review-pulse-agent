"""Default clustering engine: sentence embeddings + k-means, no paid API (architecture §4.4).

Pipeline (plan Phase 3, L1–L8):

L1  General praise by rule (4–5★, a praise word, no specific word), before clustering.
L2  Embed `canonical_text` with model2vec (pinned revision, L2-normalized, sorted input).
L3  Over-cluster with k-means (`micro_clusters`, `n_init`, fixed seed).
L4  Route micro-clusters by their c-TF-IDF keywords: praise → `general_praise`; vague or loose
    → `other`; the rest are theme micro-clusters.
L5  Cluster the theme reviews again for k = 5, 4, 3 and keep the largest k whose clusters all
    hold at least `min_theme_share` of reviews and whose median ARI against three other seeds
    is at least `min_stability`.
L6  Per review: low similarity to its centroid → `other`; a near second centroid → secondary.
    If `other` is above `max_other_share`, run once more with `micro_clusters_retry`.
L7  Label each theme from its keywords via the theme catalogue; themes that match the same
    entry are merged; unmatched themes are named after their top two keywords.
L8  Stats and ranking (`rank.py`, unchanged), warnings, and quality figures.

Everything is deterministic: input sorted by ID, fixed seeds, single-threaded BLAS/OpenMP
during clustering, and ties broken by ID. No review text leaves the machine.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import numpy as np
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, CountVectorizer
from sklearn.metrics import adjusted_rand_score
from threadpoolctl import threadpool_limits

from pulse.analysis.catalogue import Catalogue
from pulse.analysis.cluster import ClusterError, ClusterResult, Label, ThemeDef
from pulse.analysis.llm import Usage
from pulse.analysis.rank import (
    GENERAL_PRAISE,
    OTHER,
    RESERVED,
    rank_themes,
    theme_stats,
    top_themes,
)
from pulse.config import LocalEngineConfig, PulseConfig
from pulse.models import CleanReview

KEYWORDS_PER_THEME = 10
STABILITY_SEEDS = 3          # seeds seed+1 … seed+3 are compared with `seed`
MIN_PRAISE_RATE = 0.5        # share of a micro-cluster that is 4–5★ with a praise word
MAX_PRAISE_NEGATIVE = 0.25   # … and at most this share rated 1–2★
MIN_VAGUE_KEYWORDS = 3       # of the top 5 keywords
EXAMPLES_PER_THEME = 3

_REDACTION = re.compile(r"\[(?:email|phone|id|number|link|handle)\]")
_NON_WORD = re.compile(r"[^a-z&\s]")
# Words that say nothing about a product area, on top of scikit-learn's English stop words.
DOMAIN_STOP = frozenset({
    "app", "apps", "groww", "grow", "application", "use", "using", "used", "good", "very",
    "best", "nice", "also", "just", "even", "one", "get", "got", "really", "like", "time",
    "please", "pls", "plz", "dear", "team", "sir", "make", "made", "will", "can", "dont",
    "don", "didn", "doesn", "im", "ive", "u", "ur", "s", "t", "ve", "ll", "re", "d", "m",
    "giving", "give", "given", "going", "want", "need", "know", "think", "say", "said",
    "doing", "does", "did", "done", "thing", "things", "lot", "much", "many", "now", "still",
    "ok", "okay", "yes", "guys", "people", "user", "users",
    # Time words say when, not what: keep them out of theme keywords.
    "day", "days", "times", "week", "weeks", "month", "months", "year", "years",
    "today", "yesterday", "daily", "hours", "hour", "minutes", "ago",
})
STOP_WORDS = sorted(ENGLISH_STOP_WORDS | DOMAIN_STOP)


class LocalEngineError(ClusterError):
    """The local engine could not run (for example, the embedding model is unavailable)."""


# ---------------------------------------------------------------------------- embeddings


class Embedder(Protocol):
    name: str

    def encode(self, texts: Sequence[str]) -> np.ndarray: ...


def _model_dir(cfg: PulseConfig) -> Path:
    return cfg.paths.data_path / "models"


def model_is_cached(cfg: PulseConfig) -> bool:
    local = cfg.analysis.local
    if local is None:
        return False
    snapshot = (_model_dir(cfg) / f"models--{local.model.replace('/', '--')}" / "snapshots"
                / local.model_revision)
    return snapshot.is_dir() and any(snapshot.iterdir())


class Model2VecEmbedder:
    """model2vec static embeddings, pinned by revision and cached in data/models/."""

    def __init__(self, local: LocalEngineConfig, cache_dir: Path) -> None:
        from huggingface_hub import snapshot_download
        from model2vec import StaticModel

        self.name = f"{local.model}@{local.model_revision[:7]}"
        try:  # offline first; download only when the pinned revision is not cached yet
            path = snapshot_download(local.model, revision=local.model_revision,
                                     cache_dir=str(cache_dir), local_files_only=True)
        except Exception:  # noqa: BLE001 - any cache miss means "try the network"
            try:
                path = snapshot_download(local.model, revision=local.model_revision,
                                         cache_dir=str(cache_dir))
            except Exception as e:  # noqa: BLE001
                raise LocalEngineError(
                    f"embedding model {local.model} (revision {local.model_revision[:7]}) is "
                    f"not cached in {cache_dir} and could not be downloaded: {e}. Check the "
                    "network, or analysis.local.model / model_revision in pulse.yaml") from e
        self._model = StaticModel.from_pretrained(path, force_download=False)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        return np.asarray(self._model.encode(list(texts), use_multiprocessing=False),
                          dtype=np.float64)


def build_embedder(cfg: PulseConfig) -> Embedder:
    """The real embedder. The CLI calls this through the module so tests can patch it."""
    return Model2VecEmbedder(cfg.analysis.local, _model_dir(cfg))


def normalize_rows(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.where(norms == 0, 1.0, norms)


# ---------------------------------------------------------------------------- text helpers


def word_pattern(words: Sequence[str]) -> re.Pattern[str]:
    """Whole-word, case-insensitive match for any of `words` (multi-word entries allowed)."""
    alts = sorted({re.escape(w.strip().lower()) for w in words if w.strip()}, key=len,
                  reverse=True)
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.I)


def clean_for_keywords(text: str) -> str:
    return _NON_WORD.sub(" ", _REDACTION.sub(" ", text.lower()))


def is_general_praise(r: CleanReview, praise: re.Pattern[str], specific: re.Pattern[str]) -> bool:
    t = r.canonical_text
    return r.rating >= 4 and bool(praise.search(t)) and not specific.search(t)


def ctfidf_keywords(texts: Sequence[str], labels: Sequence[object],
                    n: int = KEYWORDS_PER_THEME) -> dict[object, list[str]]:
    """Class-based TF-IDF (BERTopic's labelling): the top terms of each label's reviews,
    weighted down when they are common to every class."""
    labels = np.asarray(list(labels), dtype=object)
    classes = sorted(set(labels.tolist()), key=str)
    for min_df in (2, 1):
        cv = CountVectorizer(preprocessor=clean_for_keywords, stop_words=STOP_WORDS,
                             ngram_range=(1, 2), min_df=min_df)
        try:
            counts = cv.fit_transform(texts)
            break
        except ValueError:  # empty vocabulary on a tiny input
            continue
    else:
        return {c: [] for c in classes}
    terms = np.array(cv.get_feature_names_out())
    tf = np.vstack([np.asarray(counts[labels == c].sum(axis=0)).ravel()
                    for c in classes]).astype(float)
    tf = tf / np.where(tf.sum(axis=1, keepdims=True) == 0, 1, tf.sum(axis=1, keepdims=True))
    idf = np.log(1 + tf.shape[0] / (1 + (tf > 0).sum(axis=0)))
    score = tf * idf
    out = {}
    for i, c in enumerate(classes):
        order = np.lexsort((terms, -score[i]))  # score descending, then alphabetical
        out[c] = [str(terms[j]) for j in order[:n] if score[i, j] > 0]
    return out


# ---------------------------------------------------------------------------- k-means


def _kmeans(x: np.ndarray, k: int, n_init: int, seed: int) -> KMeans:
    return KMeans(n_clusters=k, n_init=n_init, random_state=seed).fit(x)


def _cosine_to_centroids(x: np.ndarray, centers: np.ndarray) -> np.ndarray:
    return x @ normalize_rows(centers).T


@dataclass
class _Choice:
    k: int
    labels: np.ndarray
    centers: np.ndarray
    aris: list[float]
    tried: list[dict]


def choose_k(x: np.ndarray, *, total_reviews: int, local: LocalEngineConfig, min_share: float,
             max_k: int, min_k: int) -> _Choice:
    """L5: the largest k (max_k down to min_k) that is big enough and stable."""
    tried = []
    for k in range(min(max_k, len(x)), min_k - 1, -1):
        base = _kmeans(x, k, local.n_init, local.seed)
        shares = np.bincount(base.labels_, minlength=k) / total_reviews
        aris = [float(adjusted_rand_score(base.labels_,
                                          _kmeans(x, k, local.n_init, local.seed + i).labels_))
                for i in range(1, STABILITY_SEEDS + 1)]
        median = float(np.median(aris))
        ok = bool(shares.min() >= min_share and median >= local.min_stability)
        tried.append({"k": k, "smallest_share": round(float(shares.min()), 4),
                      "median_ari": round(median, 4), "accepted": ok})
        if ok:
            return _Choice(k, base.labels_, base.cluster_centers_, aris, tried)
    raise LocalEngineError(
        f"no stable clustering with at least {min_k} themes (tried k = "
        + ", ".join(f"{t['k']}: smallest {t['smallest_share']:.1%}, ARI {t['median_ari']:.2f}"
                    for t in tried) + ")")


# ---------------------------------------------------------------------------- labelling


def _snake(words: Sequence[str]) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", "_".join(words).lower()).strip("_")
    return s[:36] or "theme"


def _fallback_label(keywords: Sequence[str]) -> str:
    parts = [k.title() for k in keywords[:2]] or ["Other topics"]
    label = " & ".join(parts)
    return label if len(label.split()) <= 4 else parts[0]


def _stem(word: str) -> str:
    """Crude singular form, so "stock" and "stocks" count as one word."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def text_keywords(keywords: Sequence[str], label: str, vague: re.Pattern[str],
                  n: int = 3) -> list[str]:
    """Keywords for template text: skip words already in the label, vague words, and terms
    whose words were all used already, so "customer, support, customer support" does not
    read as three keywords."""
    label_words = {_stem(w.strip("&").lower()) for w in label.split()}
    chosen: list[str] = []
    used: set[str] = set()
    for k in keywords:
        words = {_stem(w) for w in k.split()}
        if words <= label_words or vague.fullmatch(k) or words <= used:
            continue
        chosen.append(k)
        used |= words
        if len(chosen) == n:
            break
    return chosen or list(keywords[:n])


# ---------------------------------------------------------------------------- the stage


def run_local_clustering(
    reviews: Sequence[CleanReview], cfg: PulseConfig, *, week: str,
    window: tuple[date, date], embedder: Embedder, catalogue: Catalogue,
) -> ClusterResult:
    a, local = cfg.analysis, cfg.analysis.local
    if local is None:
        raise LocalEngineError("analysis.local is missing from pulse.yaml")
    if not reviews:
        raise ClusterError("no reviews to cluster")
    ordered = sorted(reviews, key=lambda r: r.id)
    total = len(ordered)
    praise_re = word_pattern(local.praise_words)
    specific_re = word_pattern(local.specific_words)
    vague_re = word_pattern(local.vague_words)

    # L1: general praise by rule.
    labels: dict[str, Label] = {}
    rest = []
    for r in ordered:
        if is_general_praise(r, praise_re, specific_re):
            labels[r.id] = Label(r.id, GENERAL_PRAISE)
        else:
            rest.append(r)
    praise_by_rule = total - len(rest)
    if len(rest) < a.top_themes:
        raise LocalEngineError(f"only {len(rest)} reviews are left after general praise; "
                               f"the pulse needs {a.top_themes} themes")

    # L2: embed.
    vectors = normalize_rows(embedder.encode([r.canonical_text for r in rest]))
    texts = [r.canonical_text for r in rest]

    with threadpool_limits(limits=1):
        attempt = 0
        for k0 in (local.micro_clusters, local.micro_clusters_retry):
            outcome = _cluster_once(rest, texts, vectors, k0, cfg, praise_re, vague_re, total)
            other_share = (outcome["other_count"] + 0) / total
            if other_share <= a.max_other_share or attempt == 1:
                break
            attempt += 1
    rediscovered = attempt == 1

    # L6/L7 results → labels for every review.
    assign: dict[str, str] = outcome["assign"]
    secondary: dict[str, str | None] = outcome["secondary"]
    similarity: dict[str, float] = outcome["similarity"]
    raw_keywords: dict[object, list[str]] = outcome["keywords"]
    cluster_ids = outcome["clusters"]

    # L7: label each cluster from the catalogue; merge clusters that map to the same entry.
    theme_of_cluster: dict[int, str] = {}
    defs: dict[str, dict] = {}
    unmatched: list[str] = []
    for c in cluster_ids:
        kws = raw_keywords.get(c, [])
        entry = catalogue.match(kws)
        if entry is not None:
            tid, label = entry.id, entry.label
        else:
            tid, label = "kw_" + _snake(kws[:2]), _fallback_label(kws)
            while tid in defs and defs[tid]["catalogue"]:
                tid += "_x"
            unmatched.append(label)
        theme_of_cluster[c] = tid
        defs.setdefault(tid, {"label": label, "catalogue": entry is not None, "clusters": []})
        defs[tid]["clusters"].append(c)
    for r in rest:
        lab = assign[r.id]
        if isinstance(lab, int):
            sec = secondary.get(r.id)
            sec_tid = theme_of_cluster.get(sec) if sec is not None else None
            tid = theme_of_cluster[lab]
            labels[r.id] = Label(r.id, tid, sec_tid if sec_tid != tid else None)
        else:
            labels[r.id] = Label(r.id, lab)

    # Keywords per final theme (recomputed after merging) and theme definitions.
    final_labels = [labels[r.id].theme_id for r in rest]
    theme_keywords = ctfidf_keywords(texts, final_labels)
    themes = []
    for tid, d in defs.items():
        members = [r for r in rest if labels[r.id].theme_id == tid]
        if not members:
            continue
        kws = theme_keywords.get(tid, [])
        examples = sorted(members, key=lambda r: (-similarity.get(r.id, 0.0), r.id))
        themes.append(ThemeDef(
            id=tid, label=d["label"],
            description="Reviews mentioning " + ", ".join(kws[:5]) if kws else d["label"],
            include=", ".join(kws), exclude="",
            example_ids=[r.id for r in examples[:EXAMPLES_PER_THEME]]))
    if len(themes) > a.max_themes:  # cannot happen (k ≤ max_themes); a hard guarantee
        raise LocalEngineError(f"{len(themes)} themes exceed analysis.max_themes")

    # L8: stats, ranking, warnings.
    ids = [t.id for t in themes]
    primary = {rid: x.theme_id for rid, x in labels.items()}
    sec_map = {rid: x.secondary_theme_id for rid, x in labels.items()}
    stats = theme_stats([*ids, *RESERVED], ordered, primary, window_end=window[1],
                        recency_days=a.recency_days, secondary=sec_map)
    ranked = rank_themes([s for s in stats if s.theme_id in ids], top_n=a.top_themes)
    buckets = {s.theme_id: s for s in stats if s.theme_id in RESERVED}

    warnings: list[dict[str, str]] = []
    if buckets[OTHER].volume_share > a.max_other_share:
        warnings.append({"code": "other_share_high", "message":
                         f"`other` holds {buckets[OTHER].volume_share:.0%} of reviews "
                         f"(limit {a.max_other_share:.0%}) even after re-clustering"})
    for s in ranked:
        if s.volume_share < a.min_theme_share:
            warnings.append({"code": "small_theme", "message":
                             f"{s.theme_id} holds {s.volume_share:.1%} of reviews "
                             f"(under {a.min_theme_share:.0%})"})
    if unmatched:
        warnings.append({"code": "unmatched_label", "message":
                         "themes with no catalogue entry (consider adding one to "
                         f"{a.theme_catalogue}): " + ", ".join(unmatched)})

    choice: _Choice = outcome["choice"]
    quality = {
        "engine": "local",
        "embedder": embedder.name,
        "general_praise_by_rule": praise_by_rule,
        "micro_clusters": outcome["k0"],
        "micro_cluster_routes": outcome["routes"],
        "k": choice.k,
        "k_search": choice.tried,
        "stability": {"median_ari": round(float(np.median(choice.aris)), 4),
                      "aris": [round(x, 4) for x in choice.aris]},
        "retries": int(rediscovered),
        "keywords": {tid: theme_keywords.get(tid, []) for tid in ids},
    }
    return ClusterResult(
        week=week, window=window, themes=themes, taxonomy_changes=[], assignments=labels,
        ranked=ranked, buckets=buckets,
        top=[s.theme_id for s in top_themes(ranked, a.top_themes)],
        discovery_input=len(rest), review_count=total, rediscovered=rediscovered,
        warnings=warnings, quality=quality, usage=Usage(), model=embedder.name,
        engine="local",
        keywords={tid: text_keywords(theme_keywords.get(tid, []), defs[tid]["label"], vague_re)
                  for tid in ids},
        centroid_similarity=similarity,
    )


def _cluster_once(rest: Sequence[CleanReview], texts: Sequence[str], vectors: np.ndarray,
                  k0: int, cfg: PulseConfig, praise_re: re.Pattern[str],
                  vague_re: re.Pattern[str], total: int) -> dict:
    """L3–L6 for one micro-cluster count."""
    a, local = cfg.analysis, cfg.analysis.local
    k0 = max(1, min(k0, len(rest)))
    micro = _kmeans(vectors, k0, local.n_init, local.seed)
    mlabels = micro.labels_
    mcos = np.einsum("ij,ij->i", vectors, normalize_rows(micro.cluster_centers_)[mlabels])
    mkeys = ctfidf_keywords(texts, mlabels.tolist(), n=5)

    # L4: route each micro-cluster.
    route: dict[int, str] = {}
    for c in range(k0):
        idx = np.where(mlabels == c)[0]
        if len(idx) == 0:
            route[c] = OTHER
            continue
        members = [rest[i] for i in idx]
        negative = np.mean([r.rating <= 2 for r in members])
        praise_rate = np.mean([r.rating >= 4 and bool(praise_re.search(r.canonical_text))
                               for r in members])
        vague = sum(1 for k in mkeys.get(c, [])[:5] if vague_re.fullmatch(k))
        if negative <= MAX_PRAISE_NEGATIVE and praise_rate > MIN_PRAISE_RATE:
            route[c] = GENERAL_PRAISE
        elif vague >= MIN_VAGUE_KEYWORDS or np.median(mcos[idx]) < local.loose_cohesion:
            route[c] = OTHER
        else:
            route[c] = "theme"
    theme_idx = np.array([i for i in range(len(rest)) if route[int(mlabels[i])] == "theme"])
    if len(theme_idx) < a.top_themes:
        raise LocalEngineError("too few reviews left in theme clusters after routing")

    # L5: choose k and cluster the theme reviews.
    choice = choose_k(vectors[theme_idx], total_reviews=total, local=local,
                      min_share=a.min_theme_share, max_k=a.max_themes, min_k=a.top_themes)

    # L6: per-review `other` and secondary theme.
    cos = _cosine_to_centroids(vectors[theme_idx], choice.centers)
    assign: dict[str, object] = {}
    secondary: dict[str, int | None] = {}
    similarity: dict[str, float] = {}
    for j, i in enumerate(theme_idx):
        rid = rest[i].id
        c = int(choice.labels[j])
        own = float(cos[j, c])
        if own < local.other_cosine:
            assign[rid] = OTHER
            continue
        assign[rid] = c
        similarity[rid] = round(own, 6)
        others = [(float(cos[j, o]), o) for o in range(choice.k) if o != c]
        best_other = max(others) if others else None
        near = (best_other is not None and best_other[0] >= own - local.secondary_margin
                and best_other[0] >= local.other_cosine)
        secondary[rid] = best_other[1] if near else None
    for i, r in enumerate(rest):
        if r.id not in assign:
            assign[r.id] = route[int(mlabels[i])]
    clusters = sorted({c for c in assign.values() if isinstance(c, int)})
    labels_for_keywords = [assign[r.id] if isinstance(assign[r.id], int) else assign[r.id]
                           for r in rest]
    keywords = ctfidf_keywords(texts, labels_for_keywords)
    return {
        "k0": k0, "assign": assign, "secondary": secondary, "similarity": similarity,
        "choice": choice, "clusters": clusters, "keywords": keywords,
        "routes": dict(Counter(route.values())),
        "other_count": sum(1 for v in assign.values() if v == OTHER),
    }
