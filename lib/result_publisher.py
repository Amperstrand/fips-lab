#!/usr/bin/env python3
"""Thin shim — delegates to the nostr_publish package.

Install: pip install git+https://github.com/Amperstrand/nostr-publish-file-metadata-action.git
Called via: python3 -m lib.result_publisher (from scripts/publish-results.py)
"""
from nostr_publish import publish_results, main

if __name__ == "__main__":
    raise SystemExit(main())
