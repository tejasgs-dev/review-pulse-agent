"""The local engine with the real model2vec model (downloads about 30 MB once; free).

Run on demand:  pytest -m slow tests/test_local_live.py
"""

from collections import Counter

import pytest

from pulse.analysis.catalogue import load_catalogue
from pulse.analysis.engines.local_cluster import build_embedder, run_local_clustering
from pulse.analysis.engines.rules_compose import compose_pulse_rules
from tests.local_corpus import WINDOW, corpus, topic_of

pytestmark = pytest.mark.slow


def test_real_model_clusters_synthetic_topics(cfg):
    catalogue = load_catalogue(cfg.analysis.catalogue_path)
    reviews = corpus()
    result = run_local_clustering(reviews, cfg, week="2026-W46", window=WINDOW,
                                  embedder=build_embedder(cfg), catalogue=catalogue)
    assert 3 <= len(result.themes) <= 5
    # Each topic lands mostly in one theme.
    for topic in ("support", "charges", "withdrawals", "bugs"):
        labels = Counter(result.assignments[r.id].theme_id for r in reviews
                         if topic_of(r) == topic)
        assert labels.most_common(1)[0][1] / sum(labels.values()) >= 0.8, (topic, labels)
    composed = compose_pulse_rules(result, reviews, cfg, catalogue)
    assert composed.report.ok
