"""stock-tracker package init.

Forces stdlib `ssl`/`urllib` (used by e.g. pandas.read_html in universe.py)
to verify against certifi's CA bundle instead of the OS certificate store.
On some `uv`-managed standalone Python builds on Windows, the OS store isn't
read correctly for plain urllib requests, which surfaces as a misleading
"certificate has expired" error even though the same URL loads fine in a
browser or via PowerShell's Invoke-WebRequest. The `requests` library
already defaults to certifi and is unaffected; this just makes urllib
consistent with it.
"""

import os

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
