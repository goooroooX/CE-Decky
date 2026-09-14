# Third-party notices

CE Decky is licensed under GPL-3.0-or-later. The full project license and the
retained Decky Plugin Template BSD-3-Clause notice are in `LICENSE`.

Build/runtime interfaces intentionally follow the official Decky toolchain:

- SteamDeckHomebrew Decky Plugin Template: BSD-3-Clause. Initial scaffolding
  patterns were adapted from the template; its original notice is retained in
  `LICENSE`.
- `@decky/api` / `@decky/ui`: upstream package metadata declares LGPL-2.1.
  They are used through Decky's supported plugin API/UI interfaces. A verbatim
  LGPL-2.1 license copy is included at `licenses/LGPL-2.1.txt`; upstream source
  is available from `https://github.com/SteamDeckHomebrew/loader-api` and
  `https://github.com/SteamDeckHomebrew/decky-frontend-lib`.
- `@decky/rollup`: BSD-3-Clause, build-time only.
- TypeScript `tslib`: 0BSD license, used by the frontend toolchain.
- `httpx` 0.28.1 and `httpcore` 1.0.9: BSD-3-Clause; runtime HTTPS
  transport. Upstream: `https://github.com/encode/httpx` and
  `https://github.com/encode/httpcore`.
- `anyio` 4.14.2: MIT; asynchronous transport dependency.
- `h11` 0.16.0: MIT; HTTP/1.1 transport dependency.
- `idna` 3.18: BSD-3-Clause; internationalized hostname handling.
- `certifi` 2026.7.22: MPL-2.0; CA bundle fallback used only with verified
  TLS. Upstream: `https://github.com/certifi/python-certifi`.
- `beautifulsoup4` 4.15.0 and `soupsieve` 2.9.2: MIT; bounded provider HTML
  parsing and selectors.
- `defusedxml` 0.7.1: PSF-2.0; untrusted sitemap XML parsing.
- `typing_extensions` 4.16.0: PSF-2.0; compatibility dependency.
- CPython 3.11.7 `Lib/xml/etree`, `Lib/xml/parsers`, `Lib/html`, and
  `Lib/_markupbase.py`: PSF License Version 2 plus the notices retained in the
  source files. The pure-Python modules are copied verbatim from tag `v3.11.7`,
  commit `fa7a6f23036537567592647d15f043722c7144ad`, and are added to
  `sys.path` only when Decky Loader's PyInstaller runtime omits `xml.etree`,
  `html.parser`, or `_markupbase`. Only the subpackages CE Decky imports are
  copied. The full upstream license is included at
  `licenses/CPython-3.11.7.txt`. Upstream:
  `https://github.com/python/cpython/tree/v3.11.7/Lib`.

These pure-Python runtime packages are hash-locked in
`requirements-runtime.lock`. Packaging vendors their installed distributions,
including each distribution's `.dist-info` metadata and license files, into the
plugin ZIP. CE Decky does not install packages at runtime on SteamOS.
No Cheat Engine source or binary, third-party cheat table, provider page, or
third-party executable is copied into this repository or its Decky install ZIP.
Research documents reference upstream projects by URL/commit instead of
vendoring their code.
