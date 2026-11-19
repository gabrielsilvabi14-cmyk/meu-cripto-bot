"""Radar de temas virais: classificação local, sem alegar notícias verificadas."""
import os
import re
import unicodedata

DEFAULT = "GTA 6|gta6,gta vi,grand theft auto;Jogos|gaming,game,playstation,xbox,nintendo;IA|artificial intelligence,ai agent,chatgpt,grok"
def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "", "".join(c for c in value.lower() if not unicodedata.combining(c)))
def themes():
    raw = os.getenv("VIRAL_THEMES", DEFAULT)
    for entry in raw.split(";"):
        if "|" not in entry:
            continue
        label, terms = entry.split("|", 1)
        terms = [normalize(t) for t in terms.split(",") if len(normalize(t)) >= 3]
        if label.strip() and terms:
            yield label.strip()[:50], terms
def classify(pair):
    base = pair.get("baseToken") or {}
    name = normalize(base.get("name"))
    symbol = normalize(base.get("symbol"))
    if not name and not symbol:
        return None
    for label, terms in themes():
        if any(t == symbol or (len(t) >= 4 and t in name) for t in terms):
            return label
    return None
