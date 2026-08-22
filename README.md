# blitz.tailscale

![blitz.tailscale](screenshot.png)

Omarchy bar chip for Tailscale. Uses whatever account `tailscale status` is logged into, plus any extra `/run/tailscale-*.sock` daemons (a second tailnet, a userspace instance, etc.). No account names are hardcoded.

```bash
omarchy plugin add https://github.com/itz4blitz/blitz.tailscale.git --enable
omarchy plugin update blitz.tailscale
```

```bash
python3 test_tailscale_collect.py
```
