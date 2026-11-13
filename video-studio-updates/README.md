# Góc Review Phim Video Studio — Public Update Channel

This directory serves signed binary updates for the Windows desktop application.

- Public distribution repository: https://github.com/vanthanhduma/vanthanhduma
- Private source repository: https://github.com/vanthanhduma/Goc-Review-Phim-Video-Studio
- End-user updater uses anonymous HTTPS only. No PAT, GitHub login, account connection, or manual Release setup is required on user machines.

## Static Stable feed

The application reads:

- `video-studio-updates/stable/latest.json`
- `video-studio-updates/stable/latest.sig`
- Package assets from `video-studio-updates/stable/assets/<asset_name>`

The updater verifies the Ed25519 signature with the public key embedded in the app, validates version/base version and safe paths, then checks the package SHA-256 and each file hash before applying. App data, projects, autosave, .venv, user settings, branding, downloads and renders are outside the replacement allowlist.

## Publishing a Stable update

1. Build an incremental ZIP with `tools/build_update.py`.
2. Sign the exact manifest bytes with the offline Ed25519 private key.
3. Commit the ZIP, manifest as `latest.json`, and signature as `latest.sig` in one commit.
4. Test the public raw HTTPS URLs and perform a Windows 10/11 end-to-end update/rollback acceptance run before marking the release Stable.

Never commit private signing keys, source code intended to remain private, user data, credentials, projects, videos, or unreviewed builds.
