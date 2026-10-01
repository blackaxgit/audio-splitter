# Security Policy

This document explains how to report a vulnerability in Audio Splitter and what security properties the project does and does not provide.

## Supported Versions

| Version | Supported |
| ------- | --------- |
| Latest release (currently 1.0.1) | Yes |
| Older releases | No |

Security fixes ship only in the latest release.

## Reporting a Vulnerability

Report vulnerabilities privately through GitHub private vulnerability reporting:
<https://github.com/blackaxgit/audio-splitter/security/advisories/new>

Do not report vulnerabilities in public issues, pull requests or discussions.

Please include:

- The affected version or commit.
- A description of the issue and its impact.
- Steps to reproduce, or a sample file that triggers it.
- Your deployment details (Docker or Helm, and any configuration overrides).

This is a single-maintainer project, so reports are handled on a best-effort basis and no response or fix times are guaranteed.

Disclosure is coordinated: the fix is released first, then a GitHub Security Advisory is published crediting the reporter, unless the reporter prefers otherwise.

## Security Model

The API has no authentication and no rate limiting, by design. Deploy it only for trusted callers on an internal network, or behind a gateway or proxy that authenticates clients.

The Helm chart's NetworkPolicy is enabled by default, but it only restricts ports: ingress is limited to TCP 8000 and egress to UDP 53 (DNS), and only on clusters whose network plugin enforces NetworkPolicy. It does not restrict which clients can connect, because the ingress rule has no `from` selector and so accepts any source. Add `from` rules or an authenticating gateway to limit callers.

The service runs FFmpeg and ffprobe on uploaded media, which should be treated as untrusted. FFmpeg is therefore the main attack surface.

## Hardening in Place

- The container runs as a non-root user (uid/gid 1000, `USER appuser` in the `Dockerfile`). The Helm chart enforces `runAsNonRoot` with `runAsUser` and `runAsGroup` 1000.
- The Helm chart sets a read-only root filesystem, drops all capabilities and disables privilege escalation (`securityContext` in `helm/audio-splitter/values.yaml`).
- When the root filesystem is read-only, `/tmp` is an `emptyDir` volume with `sizeLimit: 2Gi`.
- Input limits (those with a named variable are configurable through environment variables):
  - Upload size: `MAX_FILE_SIZE_MB` (default 500).
  - File extension allow-list: mp3, wav, flac, ogg, m4a, aac, wma, opus.
  - Processing timeouts: `FFPROBE_TIMEOUT_SECONDS` (default 30) and `FFMPEG_TIMEOUT_SECONDS` (default 300).
  - Chunk count: `MAX_CHUNKS` (default 1000).
  - The output filename prefix is sanitized to letters, digits, `_` and `-`, and capped at 64 characters.
- Each `/split` request works in its own temporary directory, which the service deletes when the request finishes (best effort: a killed process can leave files behind).

## Known Issues and Out of Scope

These are already known, so please do not file them as new reports:

- **FFmpeg CVEs in the Debian package.** The image installs Debian 13's `ffmpeg` package (7.1.5-0+deb13u1 as of v1.0.1). Debian has deferred fixes (`fix_deferred`) for some of its CVEs. Rebuild with `docker build --pull` to pick up Debian fixes as they land.
- **Crafted-input amplification.** The number of chunks is derived from metadata declared by the uploaded file, and `MAX_CHUNKS` is checked only after FFmpeg has finished. A crafted file can therefore make FFmpeg write many files before the request is rejected. The processing timeouts limit this. With the Helm defaults, `/tmp` also has a 2Gi `emptyDir` size limit; Kubernetes evicts the pod once usage exceeds it, which is not a hard write cap. That limit does not apply to plain Docker, or when `persistence.enabled` moves the work directory to the persistent volume. These controls do not guarantee protection against resource exhaustion.
- **No authentication or rate limiting.** This is by design; see [Security Model](#security-model).

New ways to bypass the mitigations described in this document are in scope.
