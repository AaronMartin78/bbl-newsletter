"""League profile: everything that differs between the leagues the pipeline covers.

One newsletter runs per league, each with its own config file, DuckDB and
registry. The profile below is read from the config's optional `league`
block; a config without one is the BBL, so the existing BBL setup keeps its
exact behaviour (including the summarize prompt and its PROMPT_VERSION).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Per source language: words that betray an untranslated summary, the
# source-language names of cup rounds, ticket/promo title words, and the
# placeholder for a source article without a title. German is the
# original BBL set, unchanged.
LEXICON = {
    "de": {
        "name": "German",
        "markers": r"und|nicht|gegen|wurde|wurden|dass|beim|einen|eine[rm]?|spiel|sieg|niederlage",
        "rounds": {"round of 16": ("achtelfinale",), "quarterfinal": ("viertelfinale",),
                   "semifinal": ("halbfinale",)},
        "promo": r"vorverkauf|dauerkarten?|ticketverkauf|ticketshop|gewinnspiel|verlosung",
        "untitled": "ohne Titel",
    },
    "es": {
        "name": "Spanish",
        "markers": r"y|que|con|para|una|los|las|del|partido|victoria|derrota",
        "rounds": {"round of 16": ("octavos",), "quarterfinal": ("cuartos",),
                   "semifinal": ("semifinal",)},
        "promo": r"abonos?|entradas a la venta|venta de entradas|sorteo",
        "untitled": "sin título",
    },
    "fr": {
        "name": "French",
        "markers": r"et|les|des|une|avec|pour|dans|est|match|victoire|défaite",
        "rounds": {"round of 16": ("huitièmes",), "quarterfinal": ("quarts",),
                   "semifinal": ("demi-finale", "demi-finales")},
        "promo": r"billetterie|abonnements?|jeu concours|places à gagner",
        "untitled": "sans titre",
    },
    "el": {
        "name": "Greek",
        # Greek script: any run of Greek letters in the output is untranslated.
        "markers": r"[Ͱ-Ͽἀ-῿]{3,}",
        "rounds": {"quarterfinal": ("προημιτελικ",), "semifinal": ("ημιτελικ",)},
        "promo": r"εισιτήρια|διαρκείας|διαγωνισμός",
        "untitled": "χωρίς τίτλο",
    },
    "sh": {
        "name": "Serbian, Croatian, Bosnian, Montenegrin or Slovenian",
        "markers": r"i|je|su|na|za|od|utakmica|tekma|pobjeda|pobeda|poraz|[Ѐ-ӿ]{3,}",
        "rounds": {"round of 16": ("osmina", "osmin"), "quarterfinal": ("četvrtfinal", "cetvrtfinal"),
                   "semifinal": ("polufinal", "polfinal")},
        "promo": r"ulaznice|sezonske karte|vstopnice|nagradna igra",
        "untitled": "bez naslova",
    },
}


@dataclass(frozen=True)
class League:
    code: str = "bbl"
    name: str = "easyCredit BBL"
    description: str = "German professional basketball (easyCredit BBL)"
    source_language: str = "de"
    # game_uid prefix -> competition code in the box-score database
    competitions: dict = field(default_factory=lambda: {"bbl": "BBL", "pokal": "BBL_CUP"})
    # where games and teams come from: the BBL website API, or the league database
    games_source: str = "bbl_api"
    # str.format template on the box-score game_id (e.g. "{game_id:08X}" for hex);
    # empty: no official-page citation
    official_game_url: str = "https://www.easycredit-bbl.de/spiele/{game_id}"
    # column in game_player_stats that identifies a player within one game
    player_identity_column: str = "bbl_season_player_id"
    edition_title: str = "BBL Daily"
    # extra team-name tokens for article tagging, keyed by provider club id
    aliases: dict = field(default_factory=dict)
    # extra relevance keywords for mixed-sport regional sources
    keywords: tuple = ()

    @property
    def lexicon(self) -> dict:
        return LEXICON[self.source_language]

    @property
    def posts_dir(self) -> str:
        return f"{self.code}/posts"

    def official_url(self, game_id) -> str | None:
        return self.official_game_url.format(game_id=game_id) if self.official_game_url else None

    def markers(self) -> re.Pattern:
        return re.compile(r"\b(" + self.lexicon["markers"] + r")\b", re.IGNORECASE)

    @classmethod
    def from_config(cls, cfg: dict) -> "League":
        block = dict(cfg.get("league") or {})
        if "keywords" in block:
            block["keywords"] = tuple(block["keywords"])
        if "aliases" in block:
            block["aliases"] = {str(k): list(v) for k, v in block["aliases"].items()}
        league = cls(**block)
        if league.source_language not in LEXICON:
            raise SystemExit(f"league.source_language {league.source_language!r} has no lexicon")
        if league.games_source not in ("bbl_api", "league_db"):
            raise SystemExit(f"league.games_source must be bbl_api or league_db")
        return league


BBL = League()
