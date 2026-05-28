"""Render mermaid code blocks to PNG so they can be embedded in Feishu cards.

Claude/Codex frequently emit ```mermaid``` fenced blocks in their answers. Feishu
markdown rendering ignores those — to the user they show up as raw `graph TD …`
text, which is unreadable. This module turns each block into a real image:

  1. Preferred: shell out to `mmdc` (Mermaid CLI, `@mermaid-js/mermaid-cli`).
  2. Fallback: HTTP GET https://mermaid.ink/img/<base64> (no install needed,
     but requires outbound network and a public service).
  3. If both fail, return None — the caller is expected to keep the original
     fenced text so the user still sees the diagram source.

Renders are cached on disk by content hash. The same diagram emitted across
many turns / flushes only renders once. Cache files outlive the daemon
process so a restart doesn't repay the rendering cost.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Regex for ```mermaid fenced code blocks. We match a few fence variants:
#   ```mermaid …```
#   ~~~mermaid …~~~
# The language tag is case-insensitive (mermaid / MERMAID / Mermaid).
# We DO NOT use a global compiled regex here — turn.py owns block discovery.
# This module only knows: "given the code text, produce a png path".

_MMDC_TIMEOUT_S = 15
_INK_TIMEOUT_S = 10
_INK_URL = "https://mermaid.ink/img/{payload}"


def _content_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]


def _mmdc_available() -> bool:
    """Cheap PATH check. Cached implicitly via shutil.which lookup cost."""
    return shutil.which("mmdc") is not None


def _try_mmdc(code: str, out_path: Path) -> bool:
    """Invoke `mmdc -i <code.mmd> -o <out.png> -b white`. Returns True on success.

    `mmdc` writes its own stdout/stderr noise even on success (puppeteer logs),
    so we don't gate on output content — only exit code AND a non-empty file.
    """
    if not _mmdc_available():
        return False
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".mmd", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(code)
        src_path = Path(fh.name)
    try:
        proc = subprocess.run(
            ["mmdc", "-i", str(src_path), "-o", str(out_path), "-b", "white"],
            capture_output=True,
            text=True,
            timeout=_MMDC_TIMEOUT_S,
        )
        if proc.returncode != 0:
            logger.info(
                "mmdc render failed (exit %d): %s",
                proc.returncode,
                (proc.stderr or proc.stdout or "")[:200],
            )
            return False
        if not out_path.exists() or out_path.stat().st_size == 0:
            logger.info("mmdc returned 0 but produced no output file")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("mmdc timed out after %ds", _MMDC_TIMEOUT_S)
        return False
    except OSError as e:
        logger.info("mmdc invocation failed: %s", e)
        return False
    finally:
        try:
            src_path.unlink()
        except OSError:
            pass


def _try_ink(code: str, out_path: Path) -> bool:
    """Fallback: fetch https://mermaid.ink/img/<base64-of-code>.

    mermaid.ink encodes the diagram source as URL-safe base64 in the path.
    Returns True on success (a non-empty PNG was written).
    """
    try:
        encoded = base64.urlsafe_b64encode(code.encode("utf-8")).decode("ascii").rstrip("=")
        url = _INK_URL.format(payload=encoded)
        req = Request(url, headers={"User-Agent": "feishu-bot-codex/mermaid"})
        with urlopen(req, timeout=_INK_TIMEOUT_S) as resp:
            data = resp.read()
        if not data:
            return False
        out_path.write_bytes(data)
        return out_path.stat().st_size > 0
    except (URLError, TimeoutError, OSError) as e:
        logger.info("mermaid.ink fetch failed: %s", e)
        return False
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("mermaid.ink fetch unexpected error: %s", e)
        return False


def render_mermaid_to_png(code: str, cache_dir: Path) -> Optional[Path]:
    """Render mermaid `code` to PNG. Returns the cached png path or None on failure.

    Cache key is the SHA-256 of the source code (truncated). Both `mmdc` and
    the `mermaid.ink` fallback can fill the same cache slot — once any
    backend succeeds, subsequent calls hit the cache without spawning a
    subprocess or making a network request.
    """
    if not code or not code.strip():
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    h = _content_hash(code)
    out_path = cache_dir / f"mermaid-{h}.png"
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    if _try_mmdc(code, out_path):
        return out_path
    if _try_ink(code, out_path):
        return out_path
    # Both backends failed — clean up any zero-byte file mmdc may have left
    # so the next call doesn't think we have a cached success.
    try:
        if out_path.exists() and out_path.stat().st_size == 0:
            out_path.unlink()
    except OSError:
        pass
    return None


def default_cache_dir() -> Path:
    """Default cache location: ~/.cache/feishu-bot-codex/mermaid (or env override)."""
    override = os.environ.get("FEISHU_BOT_MERMAID_CACHE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "feishu-bot-codex" / "mermaid"
