import os

NOSTR_RELAYS = [
    r.strip() for r in os.environ.get("NOSTR_RELAYS", "wss://relay.cashu.email").split(",") if r.strip()
]

BLOSSOM_SERVERS = [
    s.strip() for s in os.environ.get("BLOSSOM_SERVERS", "https://blossom.psbt.me").split(",") if s.strip()
]
