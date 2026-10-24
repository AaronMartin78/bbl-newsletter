from bbl_newsletter import ids


def test_canonical_url_strips_tracking_and_fragment():
    url = "HTTP://WWW.Example.de/news/artikel/?utm_source=x&b=2&a=1&fbclid=abc#section"
    assert ids.canonicalize_url(url) == "https://www.example.de/news/artikel?a=1&b=2"


def test_canonical_url_trailing_slash_and_root():
    assert ids.canonicalize_url("https://example.de/news/") == "https://example.de/news"
    assert ids.canonicalize_url("https://example.de") == "https://example.de/"


def test_article_id_stable_across_variants():
    a = ids.article_id("https://example.de/news/spiel?utm_campaign=t")
    b = ids.article_id("http://EXAMPLE.de/news/spiel/")
    assert a == b and len(a) == 64


def test_content_hash_ignores_whitespace_only_changes():
    a = ids.content_hash("Bayern  gewinnt\n\ngegen Jena.")
    b = ids.content_hash(" Bayern gewinnt gegen Jena. \n")
    c = ids.content_hash("Bayern verliert gegen Jena.")
    assert a == b
    assert a != c


def test_story_and_summary_ids_deterministic():
    arts = ["b" * 64, "a" * 64]
    s1 = ids.story_id("2026-10-12", arts)
    s2 = ids.story_id("2026-10-12", list(reversed(arts)))
    assert s1 == s2
    assert s1.startswith("2026-10-12-") and len(s1) == len("2026-10-12-") + 12

    ih = ids.input_hash(["c" * 64, "d" * 64])
    assert ih == ids.input_hash(["d" * 64, "c" * 64])
    assert ids.summary_id(s1, ih, "qwen2.5:14b", "v1") == ids.summary_id(
        s1, ih, "qwen2.5:14b", "v1"
    )
    assert ids.summary_id(s1, ih, "qwen2.5:14b", "v1") != ids.summary_id(
        s1, ih, "qwen2.5:14b", "v2"
    )


def test_game_uid_namespacing():
    assert ids.game_uid("bbl", 2006923) == "bbl:2006923"
    assert ids.game_uid("el", 2006923) != ids.game_uid("bbl", 2006923)
