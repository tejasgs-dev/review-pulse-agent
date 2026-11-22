"""A deterministic, offline stand-in for the model2vec embedder: hashed word counts.

Reviews that share words get similar vectors, which is all the clustering tests need. No
download, no network, identical output on every machine.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

from pulse.analysis.engines.local_cluster import STOP_WORDS, clean_for_keywords


class HashingEmbedder:
    name = "stub-hashing@test"

    def __init__(self, n_features: int = 512) -> None:
        self._vec = HashingVectorizer(n_features=n_features, alternate_sign=False,
                                      preprocessor=clean_for_keywords, stop_words=STOP_WORDS,
                                      norm=None)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        return self._vec.transform(list(texts)).toarray().astype(np.float64) + 1e-6


class FixedEmbedder:
    """Returns the given vectors, in input order (for tests that place points by hand)."""

    name = "stub-fixed@test"

    def __init__(self, vectors: dict[str, np.ndarray]) -> None:
        self.vectors = vectors

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        return np.vstack([self.vectors[t] for t in texts])
