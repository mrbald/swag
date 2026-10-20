# Licensing

Copyright (C) 2026 swag authors.

The public repository's original code and documentation are licensed under
**AGPL-3.0-only**, with commercial licenses offered separately. [LICENSE](LICENSE)
contains the unmodified GNU Affero General Public License v3.0 text;
[COMMERCIAL.md](COMMERCIAL.md) is the commercial-availability notice. AGPL permits
commercial use subject to its terms; a separate agreement is an alternative,
not a restriction added to the AGPL.

As in dsl41, per-file license headers are deliberately omitted. The root license
and Python project metadata identify the public license.

## Contributions and commercial rights

External contributions require a signed CLA that preserves the copyright
holders' ability to offer commercial licenses. Have counsel prepare that
agreement before accepting the first external contribution; do not merge an
external contribution without it. Commercial terms are set in a separate signed
agreement, not by this repository's availability notice.

## Third-party components

The project license does not replace dependencies' licenses or notices. A swag
commercial license does not grant a commercial Qt license or remove obligations
for Qt, VNC, browser assets, the base image or authentication sidecars.

The following is a bounded inventory checked on 2026-10-20, not a complete
container redistribution audit:

| Component | Checked license information |
| --- | --- |
| aiohttp 3.14.5 | `Apache-2.0 AND MIT`, from installed Python metadata. |
| SQLAlchemy 2.1.5 | MIT, from installed Python metadata. |
| Python runtime dependencies | aiohappyeyeballs/typing-extensions: PSF-2.0; aiosignal/frozenlist/multidict/propcache/yarl: Apache-2.0; attrs: MIT; idna: BSD-3-Clause, from installed metadata. |
| Playwright 1.58.0 (development checks) | Apache-2.0, from installed Python metadata. Chrome is a separately installed test prerequisite. |
| Qt 5.15.3 base/GUI packages | EL8 RPM metadata reports `LGPLv2 with exceptions or GPLv3 with exceptions`. Preserve packaged notices and check the actual modules/terms; [Qt's licensing documentation](https://doc.qt.io/archives/qt-5.15/licensing.html) describes its licensing options. |
| TigerVNC and Openbox | EL8 RPM metadata reports `GPLv2+`; these run as separate processes. |
| noVNC 1.7.0 | Core JavaScript: MPL-2.0. Its bundled `LICENSE.txt` identifies separate licenses for HTML/CSS, fonts, images and incorporated code. Preserve those notices. |
| websockify 0.13.0 (rendering probe) | LGPLv3, from installed Python metadata. The authenticated workspace uses its own WebSocket bridge. |

The Python dependencies checked above have permissive licenses; their required
notices still apply. Separately running programs and container contents keep
their own source/notice obligations. Review the complete shipped image before
redistribution, and recheck licensing when adding modules or dependencies.

For the public license's source obligations, follow [the AGPL text](LICENSE) and
[GNU's application guidance](https://www.gnu.org/licenses/gpl-howto.en.html).
In particular, a modified network-interactive version must provide the source
offer required by section 13; repository visibility alone is not the UI offer.
