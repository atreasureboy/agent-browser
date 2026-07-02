# Vendored assets

## axe.min.js (v4.10.2)

**Source**: https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js
**License**: MPL 2.0 (Deque Systems, Inc.)
**Size**: ~540 KB minified

Used by `controller.a11y_audit()` — injected into the page via `add_script_tag`
then `axe.run()` runs synchronously to produce WCAG 2.0/2.1 A/AA violations.

Vendored (not CDN-loaded) so:
- offline operation works
- no rate-limit / CDN-down dependency for a critical capability
- reproducible audits (same axe version = same rules)

To upgrade: download new version, replace file, update version in this README.
MPL 2.0 allows bundling unmodified; the license header is preserved at top of file.