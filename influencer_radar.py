"""Optional RSS monitoring; set INFLUENCER_RSS_TEMPLATE with {handle}.
No X API. Only explicit cashtags, recent timestamped posts and unique Solana
contracts qualify. Public RSS sources may be delayed, unavailable or inaccurate.
"""
import os
import re
import time
import logging
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

LOG = logging.getLogger("radar.influencers")
PROFILES = {
    "elonmusk": "Elon Musk", "realDonaldTrump": "Donald Trump",
    "blknoiz06": "Ansem", "MustStopMurad": "Murad Mahmudov",
    "Cobratate": "Andrew Tate", "IGGYAZALEA": "Iggy Azalea",
}
INTERVAL = max(120, int(os.getenv("INFLUENCER_INTERVAL", "120")))
TEMPLATE = os.getenv("INFLUENCER_RSS_TEMPLATE", "").strip()
CASHTAG = re.compile(r"(?<![\w])\$([A-Za-z][A-Za-z0-9]{1,14})\b")


def fetch_feed(handle):
    url = TEMPLATE.replace("{handle}", quote(handle, safe=""))
    with urlopen(Request(url, headers={"User-Agent": "SolanaRadar/3.5"}), timeout=12) as response:
        root = ET.fromstring(response.read(1000000))
    for item in root.findall(".//item"):
        identifier = item.findtext("guid") or item.findtext("link") or item.findtext("title")
        content = (item.findtext("title") or "") + " " + (item.findtext("description") or "")
        published = item.findtext("pubDate")
        try:
            timestamp = parsedate_to_datetime(published).timestamp()
        except (TypeError, ValueError):
            timestamp = None
        if identifier:
            yield identifier, content, timestamp


def poll(state, gmgn_search_tokens, passes_filters, was_sent, telegram_send,
         register_alert, alert_message, num, esc):
    if not TEMPLATE or "{handle}" not in TEMPLATE:
        return False
    seen = state.setdefault("influencer_posts", {})
    now = time.time()
    changed = False
    for handle, name in PROFILES.items():
        try:
            for identifier, content, timestamp in list(fetch_feed(handle))[:15]:
                key = handle + ":" + identifier
                if key in seen:
                    continue
                seen[key] = now
                changed = True
                if timestamp is None or not 0 <= now - timestamp <= 1800:
                    continue
                for symbol in set(s.upper() for s in CASHTAG.findall(content)):
                    pairs = gmgn_search_tokens(symbol)
                    pairs = [
                        p for p in pairs
                        if (p.get("baseToken") or {}).get("symbol", "").upper() == symbol
                    ]
                    mints = {
                        (p.get("baseToken") or {}).get("address")
                        for p in pairs
                    }
                    if len(mints) != 1:
                        continue
                    pair = max(
                        pairs,
                        key=lambda p: num(
                            (p.get("liquidity") or {}).get("usd")
                        )
                    )
                    mint = (pair.get("baseToken") or {}).get("address")
                    if not mint or was_sent(mint) or not passes_filters(pair, "new"):
                        continue
                    prefix = ("🚨 <b>RADAR DE INFLUENCIADORES — CASHTAG</b>\n"
                              + "👤 " + esc(name) + " | $" + esc(symbol) + "\n"
                              + "⚠️ Feed externo não confirma autoria nem endosso.\n\n")
                    if telegram_send(prefix + alert_message(pair, "new")):
                        register_alert(mint, pair, "new")
        except Exception as exc:
            LOG.warning("Feed %s falhou: %s", handle, type(exc).__name__)
    for key, ts in list(seen.items()):
        if now - ts > 86400:
            del seen[key]
            changed = True
    return changed
