# systemd user units

Two **independent peer services** (neither owns the other):

- **`vasco-browser.service`** — the persistent Camoufox browser server.
- **`vascod.service`** — the resident vasco daemon (`vasco serve`): the full fetch
  API over `$XDG_RUNTIME_DIR/vasco/vascod.sock`, with cross-consumer single-flight
  + per-domain rate-limiting. It connects to the browser server as a client.

Both run the **installed uv tool**, not the dev checkout. After changing vasco
code, redeploy and restart:

```sh
uv tool install --reinstall .
systemctl --user restart vascod.service          # and vasco-browser.service if its code changed
```

## Install

```sh
uv tool install .                                  # puts `vasco` on ~/.local/bin
install -Dm644 contrib/systemd/vascod.service ~/.config/systemd/user/vascod.service
install -Dm644 contrib/systemd/vasco-browser.service ~/.config/systemd/user/vasco-browser.service
systemctl --user daemon-reload
systemctl --user enable --now vasco-browser.service vascod.service
```

`vascod` is **always-on** (not socket-activated): it binds the socket itself and
stays up. Consumers (claudinho, MCP) just connect; if the socket is briefly absent
during a restart, the `DaemonClient` reconnects once, and claudinho degrades a
failed fetch to skip-this-run.

## Verify

```sh
systemctl --user status vascod.service
vasco fetch https://example.com | jq '.from_cache, .mode_used'   # CLI is in-process; shares the cache
# Through the daemon explicitly:
python -c "import asyncio; from vasco.service.client import DaemonClient; \
print(asyncio.run(DaemonClient().request('fetch', url='https://example.com')).get('mode_used'))"
```
