"""Synthetic pages for collector tests. Article URL shapes follow the real
patterns observed 2026-10-12 (cms.easycredit-bbl.de) and typical WordPress /
news-sitemap layouts."""

ARTICLE_BODY_DE = """
<p>Der FC Bayern Basketball hat am Sonntag gegen Science City Jena mit 89:76
gewonnen. Vor 6.500 Zuschauern im BMW Park dominierten die Münchner das dritte
Viertel mit einem 24:12-Lauf.</p>
<p>Trainer Herbert lobte anschließend die Defensive: die Gäste aus Thüringen
trafen nur 38 Prozent aus dem Feld. Bester Werfer der Partie war Carsen
Edwards mit 21 Punkten, bei Jena überzeugte Neuzugang Miller mit 18 Punkten
und sieben Rebounds.</p>
<p>Für die Münchner geht es am Donnerstag in der EuroLeague weiter, Jena
empfängt am Samstag die Towers aus Hamburg zum Kellerduell. Beide Teams
müssen dabei auf verletzte Stammspieler verzichten, deren Rückkehr noch
offen ist.</p>
"""


def article_html(title: str = "Bayern schlägt Jena 89:76",
                 date_meta: str | None = None) -> str:
    """Article page; date_meta ('2026-10-11') adds the standard publish-time
    meta tag real club CMSes carry. Default has no date signal at all."""
    meta = (f'\n<meta property="article:published_time" content="{date_meta}T20:30:00+02:00">'
            if date_meta else "")
    return f"""<!doctype html>
<html lang="de"><head><title>{title}</title>
<meta property="og:title" content="{title}">{meta}
</head><body>
<nav><a href="/">Start</a> <a href="/impressum">Impressum</a> <a href="/datenschutz">Datenschutz</a></nav>
<main><article>
<h1>{title}</h1>
{ARTICLE_BODY_DE}
</article></main>
<footer>Impressum | Datenschutz | Newsletter abonnieren</footer>
</body></html>"""


def wp_feed(site: str, full_body: bool, titles: tuple[str, str] | None = None) -> str:
    t1, t2 = titles or ("Bayern schlägt Jena 89:76", "Neuer Trikotsponsor vorgestellt")
    content = (
        f'<content:encoded><![CDATA[{ARTICLE_BODY_DE}]]></content:encoded>'
        if full_body
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>{site} News</title>
  <link>https://{site}/</link>
  <item>
    <title>{t1}</title>
    <link>https://{site}/2026/10/erster-artikel/?utm_source=rss</link>
    <pubDate>Sun, 11 Oct 2026 20:30:00 +0200</pubDate>
    {content}
  </item>
  <item>
    <title>{t2}</title>
    <link>https://{site}/2026/10/zweiter-artikel/</link>
    <pubDate>Sat, 10 Oct 2026 10:00:00 +0200</pubDate>
  </item>
</channel>
</rss>"""


def news_sitemap(site: str, video: bool = False) -> str:
    video_url = f"""
  <url>
    <loc>https://{site}/sport/video-niners-roundup-104.html</loc>
    <news:news>
      <news:title>Basketball: Niners im Video-Roundup</news:title>
      <news:publication_date>2026-10-11T22:00:00+02:00</news:publication_date>
    </news:news>
  </url>""" if video else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">{video_url}
  <url>
    <loc>https://{site}/sport/basketball-bayern-jena-100.html</loc>
    <news:news>
      <news:title>Basketball: Bayern gewinnt gegen Jena</news:title>
      <news:publication_date>2026-10-11T21:00:00+02:00</news:publication_date>
    </news:news>
  </url>
  <url>
    <loc>https://{site}/sport/fussball-derby-102.html</loc>
    <news:news>
      <news:title>Fußball: Derby endet torlos</news:title>
      <news:publication_date>2026-10-11T19:00:00+02:00</news:publication_date>
    </news:news>
  </url>
  <url>
    <loc>https://{site}/sport/basketball-alt-098.html</loc>
    <news:news>
      <news:title>Basketball: Saisonvorschau</news:title>
      <news:publication_date>2026-09-01T09:00:00+02:00</news:publication_date>
    </news:news>
  </url>
</urlset>"""


def newscenter_listing() -> str:
    return """<!doctype html>
<html><body>
<a href="/">Start</a>
<a href="https://cms.easycredit-bbl.de/de/n/news/2026/oktober/trier-empfaengt-meister-berlin">Trier empfängt Meister Berlin</a>
<a href="https://cms.easycredit-bbl.de/de/n/news/2026/oktober/europapokal-ergebnisse-2026-27">Europapokal-Ergebnisse</a>
<a href="https://cms.easycredit-bbl.de/de/n/news/2026/oktober/trier-empfaengt-meister-berlin">Trier empfängt Meister Berlin (duplicate)</a>
<a href="https://cms.easycredit-bbl.de/de/n/news/2026/oktober/dritter-artikel?a=1&amp;b=2">Dritter Artikel</a>
<a href="https://easycredit-bbl.de/tabelle">Tabelle</a>
</body></html>"""
