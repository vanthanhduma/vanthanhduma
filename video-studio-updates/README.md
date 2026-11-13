# Góc Review Phim Video Studio — Public Update Channel

This directory is reserved for the public binary-update channel used by the desktop application.

- Distribution repository: https://github.com/vanthanhduma/vanthanhduma
- Source repository: https://github.com/vanthanhduma/Goc-Review-Phim-Video-Studio (may remain private)
- End-user updater: anonymous HTTPS access only; no GitHub PAT or login.
- Every Stable release must include:
  - `update-manifest.json`
  - `update-manifest.sig` (Ed25519 detached signature)
  - `Goc_Review_Phim_Video_Studio_v<VERSION>_UPDATE.zip`

The application embeds the Ed25519 public key and verifies the signed manifest, package SHA-256, each payload file hash, package paths, and the supported base version before applying anything.

Do not upload source code, private signing keys, credentials, projects, user data, or unreviewed builds here. Only publish signed binary update assets after the Windows release acceptance checklist passes.

The updater checks the latest published non-draft, non-prerelease GitHub Release in this repository. No account connection is required on user machines.
