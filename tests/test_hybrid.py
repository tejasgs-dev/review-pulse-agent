"""The hybrid engine: local ML clustering decides themes and numbers, the LLM only writes.
The model is faked (no network); clustering uses the real local engine with a stub embedder."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pulse.analysis import llm as llm_mod
from pulse.analysis.catalogue import load_catalogue
from pulse.analysis.compose import ComposeError, build_contexts
from pulse.analysis.engines.hybrid_compose import (
    HybridTheme,
    HybridWriting,
    QuoteRef,
    compose_pulse_hybrid,
    fill_numbers,
    raw_number_issues,
    safe_samples,
)
from pulse.analysis.engines.local_cluster import run_local_clustering
from pulse.analysis.engines.rules_compose import NameFilter
from pulse.analysis.llm import GroqClient, LlmError, Usage
from pulse.analysis.validate import validate_pulse
from pulse.cli import ExitCode, app
from pulse.config import load_config
from tests.local_corpus import WINDOW, corpus
from tests.stub_embedder import HashingEmbedder
from tests.test_cli_local import store_corpus
from tests.test_groq import FakeGroqSdk, completion, rate_limited

runner = CliRunner()
_BLOCK = re.compile(r"^## \d+\. Theme `([^`]+)`", re.MULTILINE)
_REVIEW = re.compile(r"^Review (r\d+) \(", re.MULTILINE)
_SENTENCE = re.compile(r"^  \[(\d+)\] ", re.MULTILINE)


def good_answer(user: str, **overrides) -> HybridWriting:
    """A valid answer for the prompt: the first sample review of each theme, numbers only as
    placeholders. `overrides` maps a field to a function (theme_id -> text)."""
    themes = []
    parts = _BLOCK.split(user)[1:]
    for tid, body in zip(parts[::2], parts[1::2], strict=True):
        first = _REVIEW.split(body)
        rid, text = first[1], first[2]
        n = max(int(x) for x in _SENTENCE.findall(text))
        fields = {
            "summary": f"Reviewers keep running into the same {tid} problem",
            "action": f"Fix the {tid} problem that {{recent_count}} reviewers raised lately",
            "signal": f"fewer than {{recent_count}} {tid} reviews in the next two weeks",
        }
        fields.update({k: f(tid) for k, f in overrides.items()})
        themes.append(HybridTheme(theme_id=tid, quote=QuoteRef(
            review_id=rid, first_sentence=1, last_sentence=n), **fields))
    return HybridWriting(themes=themes)


class FakeWriter:
    """A StructuredLlm whose answers are computed from the prompt by `answers` (one per
    call; the last repeats). An exception in the list is raised instead."""

    def __init__(self, *answers):
        self.answers = list(answers) or [lambda call: good_answer(call["user"])]
        self.calls: list[dict] = []
        self.usage = Usage(provider="groq")

    def parse(self, *, name, system, prefix, user, schema, effort, max_tokens=None):
        call = {"name": name, "system": system, "user": user, "effort": effort}
        self.calls.append(call)
        self.usage.calls += 1
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a(call)


@pytest.fixture
def setup(cfg):
    catalogue = load_catalogue(cfg.analysis.catalogue_path)
    reviews = corpus()
    cluster = run_local_clustering(reviews, cfg, week="2026-W46", window=WINDOW,
                                   embedder=HashingEmbedder(), catalogue=catalogue)
    return SimpleNamespace(cfg=cfg, catalogue=catalogue, reviews=reviews, cluster=cluster)


def compose(s, llm):
    return compose_pulse_hybrid(llm, s.cluster, s.reviews, s.cfg, s.catalogue)


# ---------------------------------------------------------------------------- engine


def test_hybrid_pulse_passes_the_validator(setup):
    llm = FakeWriter()
    result = compose(setup, llm)
    assert result.report.ok, result.report.to_dict()
    assert (result.engine, result.composer) == ("hybrid", "llm")
    assert len(llm.calls) == 1 and llm.calls[0]["effort"] == "medium"
    # Re-validating the final pulse independently still passes every check.
    by_id = {r.id: r for r in setup.reviews}
    assignments = {rid: x.theme_id for rid, x in setup.cluster.assignments.items()}
    again = validate_pulse(result.pulse, reviews=by_id, assignments=assignments, cfg=setup.cfg)
    assert again.ok


def test_themes_counts_and_numbers_come_from_the_ml(setup):
    """The model answers with a made-up theme and its own numbers first; the pulse still has
    exactly the ML's themes, shares and counts, and only ML figures in actions and signals."""
    def cheating(call):
        answer = good_answer(call["user"], action=lambda t: f"Fix the 999 crashes in {t}")
        answer.themes.append(HybridTheme(theme_id="made_up", quote=answer.themes[0].quote,
                                         summary="x", action="y", signal="{recent_count}"))
        return answer

    llm = FakeWriter(cheating, lambda call: good_answer(call["user"]))
    result = compose(setup, llm)
    assert result.report.ok
    first = result.attempts[0]
    checks = {i["check"] for i in first["issues"]}
    assert "numbers_match_ml" in checks and not first["ok"]
    assert "999" in llm.calls[1]["user"] and "contains a digit" in llm.calls[1]["user"]

    pulse, stats = result.pulse, {s.theme_id: s for s in setup.cluster.ranked}
    assert [t.id for t in pulse.top_themes] == list(setup.cluster.top)
    labels = {t.id: t.label for t in setup.cluster.themes}
    for t in pulse.top_themes:
        assert t.label == labels[t.id]
        assert t.volume_share == stats[t.id].volume_share
        assert t.negative_share == stats[t.id].negative_share
        assert len(t.review_ids) == stats[t.id].count
    assert pulse.review_count == len(setup.reviews)
    for a in pulse.actions:
        figure = f"{stats[a.theme_id].recent_count:,}"
        assert figure in a.action and figure in a.signal
        assert set(re.findall(r"\d[\d,]*", a.action + " " + a.signal)) == {figure}


def test_a_model_that_keeps_typing_numbers_fails_after_the_rewrites(setup):
    llm = FakeWriter(lambda call: good_answer(call["user"],
                                              signal=lambda t: "fewer than 12 reviews"))
    with pytest.raises(ComposeError) as e:
        compose(setup, llm)
    assert len(llm.calls) == 3                     # 1 + MAX_RETRIES rewrites
    assert all(any(i["check"] == "numbers_match_ml" for i in a["issues"])
               for a in e.value.attempts)


def test_bad_json_is_retried_then_raised(setup):
    bad = LlmError("hybrid_write: the response did not match the schema", kind="schema")
    llm = FakeWriter(bad, bad, bad)
    with pytest.raises(LlmError) as e:
        compose(setup, llm)
    assert e.value.kind == "schema" and len(llm.calls) == 3
    assert "not valid JSON" in llm.calls[1]["user"]


def test_bad_json_then_a_good_answer_passes(setup):
    bad = LlmError("hybrid_write: the response did not match the schema", kind="schema")
    llm = FakeWriter(bad, lambda call: good_answer(call["user"]))
    result = compose(setup, llm)
    assert result.report.ok and len(result.attempts) == 2


def test_a_rate_limit_is_not_retried_by_the_engine(setup):
    llm = FakeWriter(LlmError("rate limited", kind="rate_limit"))
    with pytest.raises(LlmError):
        compose(setup, llm)
    assert len(llm.calls) == 1        # GroqClient already waited; the CLI falls back


def test_the_llm_sees_a_small_safe_sample_not_every_review(setup):
    llm = FakeWriter()
    compose(setup, llm)
    prompt = llm.calls[0]["user"]
    sent = _REVIEW.findall(prompt)
    per_theme = setup.cfg.analysis.hybrid.sample_per_theme
    assert len(sent) <= per_theme * setup.cfg.analysis.top_themes < len(setup.reviews)
    names = NameFilter((r.canonical_text for r in setup.reviews),
                       setup.cfg.analysis.local.name_allowlist)
    by_id = {r.id: r for r in setup.reviews}
    assert not any(names.names_person(by_id[rid].canonical_text) for rid in sent)
    assert "{recent_count} = " in prompt and "Top keywords:" in prompt


def test_named_reviews_never_reach_the_sample(setup, monkeypatch):
    """With the quote-fit check switched off, only the name filter can exclude the review."""
    from pulse.analysis.engines import hybrid_compose

    monkeypatch.setattr(hybrid_compose.QuoteFit, "reason", lambda self, r, tid, tokens: None)
    contexts = build_contexts(setup.cluster, setup.reviews, setup.cfg)
    victim = contexts[0].members[0]
    named = victim.__class__(**{**victim.__dict__,
                                "canonical_text": "Rahul from support closed my ticket and "
                                                  "never answered my question about it"})
    reviews = [named if r.id == victim.id else r for r in setup.reviews]
    contexts[0].members[0] = named
    safe_samples(contexts, setup.cluster, reviews, setup.cfg, setup.catalogue)
    assert all(r.id != victim.id for r in contexts[0].candidates)


def test_raw_number_rules():
    def theme(**kw):
        base = {"summary": "s", "action": "a {trend}", "signal": "fewer than {recent_count}"}
        return HybridTheme(theme_id="t", quote=QuoteRef(review_id="r1", first_sentence=1,
                                                        last_sentence=1), **{**base, **kw})

    ctx = [SimpleNamespace(id="t")]
    assert raw_number_issues(HybridWriting(themes=[theme()]), ctx) == []
    bad = {
        "summary": "Down 20% this week",
        "action": "Use {count} here",
        "signal": "fewer complaints",
    }
    for field, text in bad.items():
        issues = raw_number_issues(HybridWriting(themes=[theme(**{field: text})]), ctx)
        assert issues, field
    assert fill_numbers("fewer than {recent_count}, {trend}",
                        {"recent_count": "1,204", "trend": "rising"}) == "fewer than 1,204, rising"


# ---------------------------------------------------------------------------- CLI + fallback


def _run(tmp_config, tmp_path, monkeypatch, sdk=None):
    store_corpus(tmp_path)
    if sdk is not None:
        monkeypatch.setattr(llm_mod, "build_llm", lambda cfg, log_dir=None: GroqClient(
            cfg, client=sdk, log_dir=log_dir, sleep=lambda _s: None))
    result = runner.invoke(app, ["run", "--week", "2026-W46", "--dry-run", "--skip-ingest",
                                 "--config", str(tmp_config())])
    week = tmp_path / "runs" / "2026-W46"
    manifest = json.loads((week / "manifest.json").read_text(encoding="utf-8"))
    pulse = json.loads((week / "pulse.json").read_text(encoding="utf-8"))
    return result, manifest, pulse, week


def _groq_answer(kwargs):
    user = kwargs["messages"][1]["content"]
    return completion(good_answer(user).model_dump_json())


def test_cli_hybrid_with_groq_after_a_429(tmp_config, tmp_path, monkeypatch):
    sdk = FakeGroqSdk(rate_limited("2"), _groq_answer)
    result, manifest, pulse, week = _run(tmp_config, tmp_path, monkeypatch, sdk)
    assert result.exit_code == ExitCode.OK, result.output
    assert len(sdk.requests) == 2
    c = manifest["compose"]
    assert (c["engine"], c["composer"]) == ("hybrid", "llm") and "fallback" not in c
    assert c["usage"]["provider"] == "groq" and c["usage"]["estimated_usd"] == 0.0
    assert c["usage"]["rate_limit_waits"] == 1
    assert c["prompt_versions"] == {"write": "write_v2"}
    assert manifest["cluster"]["engine"] == "local"       # the ML did the clustering
    assert (pulse["engine"], pulse["composer"]) == ("hybrid", "llm")
    assert "Compose" in result.output and "llm (groq openai/gpt-oss-120b" in result.output
    (log,) = (week / "llm").glob("*hybrid_write*.json")
    assert json.loads(log.read_text(encoding="utf-8"))["response"]["tokens"]["total"] == 1500


def test_cli_bad_json_falls_back_to_rules(tmp_config, tmp_path, monkeypatch):
    sdk = FakeGroqSdk(completion('{"themes": [{"theme_id": '))
    result, manifest, pulse, _ = _run(tmp_config, tmp_path, monkeypatch, sdk)
    assert result.exit_code == ExitCode.OK, result.output
    assert len(sdk.requests) == 3                          # first try + 2 rewrites
    c = manifest["compose"]
    assert c["composer"] == "rules" and c["fallback"]["reason"] == "bad_output"
    assert pulse["composer"] == "rules" and pulse["fallback"] == "bad_output"
    assert "rules (fallback: bad_output)" in result.output


def test_cli_rate_limited_after_retries_falls_back(tmp_config, tmp_path, monkeypatch):
    sdk = FakeGroqSdk(rate_limited("1"))
    result, manifest, _, _ = _run(tmp_config, tmp_path, monkeypatch, sdk)
    assert result.exit_code == ExitCode.OK, result.output
    assert manifest["compose"]["fallback"]["reason"] == "rate_limited"
    assert "rules (fallback: rate_limited)" in result.output


def test_cli_without_a_key_falls_back_to_rules(tmp_config, tmp_path, monkeypatch):
    result, manifest, pulse, week = _run(tmp_config, tmp_path, monkeypatch)
    assert result.exit_code == ExitCode.OK, result.output
    assert manifest["compose"]["fallback"]["reason"] == "no_key_or_bad_key"
    assert pulse["composer"] == "rules"
    assert not (week / "llm").exists()


def test_hybrid_is_the_default_engine():
    assert load_config(load_env_file=False).analysis.engine == "hybrid"


def test_without_the_name_the_same_review_is_sampled(setup, monkeypatch):
    """Control for the test above: the unnamed version of the review is eligible."""
    from pulse.analysis.engines import hybrid_compose

    monkeypatch.setattr(hybrid_compose.QuoteFit, "reason", lambda self, r, tid, tokens: None)
    contexts = build_contexts(setup.cluster, setup.reviews, setup.cfg)
    # The most recent review is first in its rating band, so it is always sampled.
    victim = contexts[0].members[0]
    safe_samples(contexts, setup.cluster, setup.reviews, setup.cfg, setup.catalogue)
    assert any(r.id == victim.id for r in contexts[0].candidates)


def test_invented_targets_in_words_are_rejected():
    """'within one business day' is a figure the ML never produced; 'two weeks' is the window."""
    def theme(**kw):
        base = {"summary": "s", "action": "a", "signal": "fewer than {recent_count} in the next "
                                                       "two weeks"}
        return HybridTheme(theme_id="t", quote=QuoteRef(review_id="r1", first_sentence=1,
                                                        last_sentence=1), **{**base, **kw})

    ctx = [SimpleNamespace(id="t")]
    for action in ("Reply to every ticket within one business day", "Answer within two hours",
                   "Refund five rupees per failed order"):
        issues = raw_number_issues(HybridWriting(themes=[theme(action=action)]), ctx)
        assert any("invents a figure" in i for i in issues), action
    fine = [theme(), theme(summary="The app crashes several times a day for some reviewers"),
            theme(action="Give support one place to track every open ticket")]
    for t in fine:
        assert raw_number_issues(HybridWriting(themes=[t]), ctx) == []
