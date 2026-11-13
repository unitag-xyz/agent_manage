---
name: nginx-delivery
description: Deploy explicitly public user-facing files, HTML pages, previews, and static assets through nginx. Use whenever a task creates or updates a file that the user must open through a public URL; local paths, IPv6-only URLs, or attachment-only delivery are not complete.
---

# Nginx Delivery

1. Confirm the files are intended to be public. Never publish secrets, credentials, private configuration, source archives, or logs without explicit review.
2. Ensure `PATH` includes `/usr/local/sbin:/usr/sbin:/sbin`. Inspect the running nginx process, listening ports, and active config; do not assume systemd, default ports, or a default config path.
3. Reuse existing nginx. Only install it when truly missing, system installation is authorized, and no service conflicts exist. Use the existing service manager or supervisor; do not start a foreground service in the task terminal.
4. Name files with lowercase URL-safe English words and hyphens: `<topic>-<content>.<ext>`. Add `-preview` before the extension for previews. The two standard showcase shots keep their fixed names in step 8.
5. Verify the runtime-declared public root against the active config for the target URL; resolve mismatches before publishing. `/home/node/.openclaw/workspace/public/` and `/var/www/html/` are defaults, not guaranteed active roots.
6. Deploy only the required files under that public root and update its `index.html` without discarding existing entries. Preserve unrelated files and avoid overwriting an existing deliverable unless updating it is intended. Ensure nginx worker access (typically files 644, directories 755). Compress web images; target ≤500 KB each unless needed otherwise.
7. A new public page also ships a 1200×630 share poster (≤300 KB), properly typeset—not AI-drawn Chinese or a page screenshot—and wired to `og:image` and `twitter:card=summary_large_image`. Use page-specific assets to avoid collisions. Standalone assets and private pages skip this.
8. Every public site ships two showcase previews inside its own assets directory:
   - `<site>/assets/preview-desktop.jpg` — 2880×1600 (1440×800 viewport at 2× DPR).
   - `<site>/assets/preview-mobile.jpg` — 1170×1890 (390×630 viewport at 3× DPR).
   Capture the real rendered page, first screen only, without browser chrome, as sRGB JPEG at ≤500 KB each. Keep these names stable: no date, version, device, or dimension suffix. Re-capture and overwrite both files whenever the first screen changes. Private pages skip this.
9. Static deployment normally needs no config change or reload. Verify the process, actual port, deployed file, HTTP status, content type, and relevant logs. Only after config changes, test with the actual binary, user, and `-p`/`-c` arguments. Run network checks separately with bounded timeouts and IPv4, for example `curl -4 --max-time 5`.
10. Return the verified public URL reachable over IPv4 for every delivered file. Prefer HTTPS when configured; otherwise return the actual HTTP URL. Never return a local path, IPv6 URL, or invented URL as the delivery result.
