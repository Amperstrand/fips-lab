"""Thin shim — delegates to the nostr_publish package.

Install: pip install nostr-publish
Source: https://github.com/Amperstrand/nostr-publish-file-metadata-action

All logic now lives in nostr_publish.publisher.
"""

from nostr_publish.publisher import (  # noqa: F401
    publish_results,
    publish_single_file,
    main,
)

if __name__ == "__main__":
    import sys
    sys.exit(main())
