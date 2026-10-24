"""Shared seeding for cluster/summarize/render tests."""
from datetime import date, datetime

from bbl_newsletter import ids

EDITION = date(2026, 10, 12)
TIPOFF = datetime(2026, 10, 11, 18, 30)

RECAP_DE = """Der FC Bayern Basketball hat gegen Science City Jena mit 89:76
gewonnen. Die Münchner dominierten das dritte Viertel. Trainer Herbert lobte
die Defensive, Jena traf nur 38 Prozent aus dem Feld. Carsen Edwards war mit
21 Punkten bester Werfer der Partie im BMW Park vor 6500 Zuschauern."""

INJURY_DE = """Verletzungsupdate beim FC Bayern Basketball: Guard Carsen
Edwards fällt mit einer Sprunggelenksverletzung mehrere Wochen aus. Der
Klub teilte mit, dass eine Rückkehr frühestens im November realistisch ist.
Edwards hatte sich im Training verletzt, eine Operation ist nicht nötig."""

SPONSOR_DE = """Der FC Bayern Basketball hat einen neuen Partner vorgestellt.
Das Unternehmen wird künftig auf den Trikots der Münchner zu sehen sein.
Die Vereinbarung läuft über drei Jahre und umfasst auch die Jugendarbeit,
wie der Klub am Montag mitteilte. Über die Summe wurde nichts bekannt."""

EUROLEAGUE_DE = """In der EuroLeague wartet auf den FC Bayern Basketball am
Donnerstag ein Auswärtsspiel. Die Münchner reisen mit breitem Kader an und
wollen den zweiten Sieg in der EuroLeague-Saison holen, wie der Klub vor
dem Abflug mitteilte. Das Hinspiel hatte Bayern knapp verloren."""


def seed_base(con):
    con.execute("INSERT INTO sources VALUES ('club', 'Club', 'club', NULL, 1)")
    con.execute("INSERT INTO sources VALUES ('regional', 'Regional', 'regional_public', NULL, 1)")
    con.execute("INSERT INTO teams VALUES ('bayern', '10', 'FC Bayern München Basketball', 'München', ['FC Bayern Basketball'])")
    con.execute("INSERT INTO teams VALUES ('jena', '30', 'Science City Jena', 'Jena', [])")
    con.execute("INSERT INTO teams VALUES ('hamburg', '40', 'Veolia Towers Hamburg', 'Hamburg', ['Towers'])")
    con.execute("INSERT INTO seasons VALUES ('bbl-2026-27', 'bbl', '2026-27', NULL, NULL)")
    con.execute(
        "INSERT INTO games VALUES ('bbl:1', 'bbl-2026-27', 5, ?, 'bayern', 'jena', 89, 76, 'final')",
        [TIPOFF],
    )


def add_article(con, tmp_path, aid_seed, source, title, body, published,
                stale=False):
    url = f"https://{source}.example/{aid_seed}"
    aid = ids.article_id(url)
    chash = ids.content_hash(body)
    p = tmp_path / f"{aid_seed}.txt"
    p.write_text(body, encoding="utf-8")
    con.execute(
        """
        INSERT INTO articles (article_id, content_hash, source_id, url, title,
                              published_at, observed_at, language, body_path, stale)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'de', ?, ?)
        """,
        [aid, chash, source, url, title, published, published, str(p), stale],
    )
    return aid


def seed_articles(con, tmp_path):
    a = {}
    a["recap_club"] = add_article(con, tmp_path, "recap-club", "club",
        "Bayern schlägt Jena", RECAP_DE, datetime(2026, 10, 11, 21, 0))
    a["recap_regional"] = add_article(con, tmp_path, "recap-reg", "regional",
        "Basketball: Sieg gegen Jena", RECAP_DE + " Auch die Fans feierten.",
        datetime(2026, 10, 12, 8, 0))
    a["preview"] = add_article(con, tmp_path, "preview", "regional",
        "Vorschau: Bayern empfängt Jena",
        "Der FC Bayern Basketball empfängt am Samstag Science City Jena. "
        "Die Gastgeber gehen als Favorit in die Partie, Jena reist mit "
        "Personalsorgen an. Tipoff ist um 18:30 Uhr im BMW Park in München, "
        "die Partie wird live übertragen und gilt als richtungsweisend.",
        datetime(2026, 10, 10, 10, 0))
    a["injury"] = add_article(con, tmp_path, "injury", "club",
        "Verletzungsupdate: Edwards fällt aus", INJURY_DE,
        datetime(2026, 10, 12, 9, 0))
    a["injury_copy"] = add_article(con, tmp_path, "injury-copy", "regional",
        "Edwards fällt aus", INJURY_DE + " (dpa)",
        datetime(2026, 10, 12, 10, 0))
    a["sponsor"] = add_article(con, tmp_path, "sponsor", "club",
        "Neuer Partner", SPONSOR_DE, datetime(2026, 10, 12, 11, 0))
    a["euroleague"] = add_article(con, tmp_path, "el", "club",
        "EuroLeague: Bayern auswärts", EUROLEAGUE_DE,
        datetime(2026, 10, 12, 12, 0))
    return a
