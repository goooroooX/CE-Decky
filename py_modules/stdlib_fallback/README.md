This directory contains compatibility assets that CE Decky loads only when
Decky Loader's embedded Python omits the corresponding standard-library module.

Every file outside `python314/` is copied verbatim from CPython tag `v3.11.7`,
commit `fa7a6f23036537567592647d15f043722c7144ad`, under `Lib/xml`, `Lib/html`,
and `Lib/_markupbase.py`, and this directory is added to `sys.path` for them.
CE Decky does not modify them. The complete upstream license is retained at
`licenses/CPython-3.11.7.txt`.

`python314/pathlib_types.py` is copied verbatim from CPython tag `v3.14.8`,
`Lib/pathlib/types.py`, with its license at `licenses/CPython-3.14.8.txt`. It
is never on `sys.path`: `ce_decky/runtime_compat.py` loads that one file as
`pathlib.types`, and only on Python 3.14 when the native module is missing.

Only the subpackages CE Decky's own import graph reaches are kept: `xml/etree`
and `xml/parsers` for table and sitemap XML, and `html/` plus `_markupbase.py`
for bounded provider HTML. `xml/dom` and `xml/sax` are not copied - nothing in
the plugin or its vendored dependencies imports them, and they would add a
quarter of a megabyte to every install ZIP. Copy a further subpackage verbatim
from the same tag if that ever changes; do not edit these files to trim them.
