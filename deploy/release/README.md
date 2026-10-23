# C-Check binary release tooling

This directory is publisher-side source, not an assembled binary release. Do not
copy it to a recipient and describe that copy as a completed installation package.
For a new Linux publisher, start with [PUBLISHER-LINUX.md](PUBLISHER-LINUX.md).
`publisher.py` restores approved inputs without requiring the old host's virtual
environment paths. See [baselines/README.md](baselines/README.md) for provenance,
licensing and reproducibility limits. Recipient installation is documented in
`INSTALL-RTX5880.md`; building a release and installing it are separate workflows.

## Scheme Two: lightweight online delivery

Scheme Two is maintained alongside the original offline release pipeline below.
It ships a small signed bootstrap and downloads fixed-version components during
deployment instead of bundling all image archives and model weights.

- [Design, version management and deployment guide (Chinese)](../../docs/方案二_轻量在线发布部署与版本管理.md)
- [Online release builder](build_online_release.py)
- [Recipient installation template](ONLINE-INSTALL.template.md)
- [External site configuration example](site-online.example.json)
- [Download planning, resumable cache and component verification](cli/ccheck/delivery.py)
- [Delivery regression tests](tests/test_delivery.py)

Use `ccheck download-plan` to inspect required downloads, `ccheck prepare-assets`
to prefetch verified components, and `ccheck install` to deploy with the reviewed
site configuration. See the guide for complete commands and trust verification.
Image digests, Python dependency locks and model file hashes are bound to the
signed release. Python dependencies run inside the fixed images, not in the host's
Python environment. A smaller release download does not reduce the disk space or
network traffic needed for uncached images and models on first installation.

The original offline releases remain supported and are not removed or replaced
by this delivery option. Signing keys and site secrets must never enter Git.

## Release pipeline

1. Export an approved committed source snapshot with `pack_source.py`.
2. Use `prepare_build.py` on the trusted Linux builder to generate reviewed exact
   dependency locks, wheel hashes and immutable per-file model manifests.
   This is the advanced dependency-refresh route. For the preserved RC13 inputs,
   use `publisher.py prepare` and the staged Linux publisher procedure instead.
3. Build the final images with `build_images.py`.
4. Run `validate_images.py`: baseline/compiled test parity, CLI tests, core loading,
   frontend tests, CUDA/runtime checks, image-layer audits and a deleted-source
   leakage negative test. Do not substitute source-only tests for this gate.
5. Assemble with `build_release.py` using the matching gate result and an external
   Ed25519 signing key. Verify the complete resulting package.
6. Test an installation in a previously absent owned directory, model supplement
   downloads, real review/RAG/export, repeat installation and lifecycle operations.
7. Approve distribution manually. `upload_github_release.py` is a dry run unless
   `--publish-draft` is explicitly supplied. It rejects worktree snapshots for
   remote publication. No publishing command is part of recipient installation.

Use `--help` on these scripts for actual arguments. Worktree snapshots are for
explicit RC evaluation only. Never ship signing keys, source build contexts,
compiler intermediate images, company code, passwords or raw diagnostic logs.

## Version and configuration boundaries

- Application version: `v1.0.0`; Git tag: `release-v1.0.0`.
- Never replace the contents of an existing signed version.
- The signed package binds source identity, images, dependency locks and models.
- Site-specific values belong in an external YAML/JSON configuration file.
- Secrets, model caches, reports and database data belong to the instance, not to
  the replaceable application release.
- Default `runtime_source: image` imports fixed runtime images. Explicit `build`
  uses the signed Qwen recipe and fixed wheels inside a container, not host pip.
- Unchanged runtime components may be reused only after identity/input checks;
  application release gates and real installation acceptance still apply.
- Updates and rollbacks currently require compatible schema epoch and identical
  model manifest. Model replacement/vector-dimension migration is not transparent.

## Recipient installation

First verify the publisher identity through a separate trusted channel, then
verify the installer and the complete package as described in the operator
manual. Do not use an unverified package's own verifier as the first trust anchor.
With prerequisites satisfied and a reviewed external site configuration:

```bash
/path/to/release-v1.0.0/ccheck install \
  --config /path/to/site.yaml \
  --release /path/to/release-v1.0.0 \
  --trusted-key /trusted/publisher.pem --version v1.0.0
```

The host needs Linux amd64, Python 3.10+, OpenSSL 3+, Docker/Compose, compatible
NVIDIA drivers and Container Toolkit, sufficient disk/RAM/VRAM and approved GPU
access. Installation does not configure shared drivers, Docker, NAT, DNS or TLS.
Models require the relevant authorization; an installation flag grants no license.

Use the same approved newer CLI to manage a rollback to older application images.
Do not silently downgrade the deployment manager with the application.

## Tests and completion

From the repository root:

```bash
python -m unittest discover -s deploy/release/tests -v
```

These are focused deployment unit/contract tests, not end-to-end CUDA acceptance.
Some checks require Linux file permissions or OpenSSL and may skip on Windows.
Inspect the actual per-test outcome. Full image gates and actual model tasks are
separate requirements.

RC12 has completed publisher gates and small-task installation/cache acceptance
on an RTX 4090 Ada 48GB evaluation host. This is not an RTX 5880 hardware test or
a sustained-load/accuracy benchmark. RC13 adds explicit existing-model paths and
fail-closed offline model validation; see INSTALL-RTX5880.md and the example site
configuration. Each release still requires its own matching committed-source
gates, signed assembly and publication verification. A queued job or an old
progress file is not evidence of completion.

Compiled modules discourage casual source reading; they do not prevent a host
administrator from copying images or reverse engineering binaries. Rootless and
the company's RTX 5880 host have not completed the evaluation described here.
