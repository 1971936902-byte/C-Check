# Approved publisher inputs

`rc13/` is a curated export of the trusted builder used for the published
`release-v0.1.0-rc.13`, not a copy of its home directory or virtual environments.
The origin release manifest SHA256 is recorded in `rc13/baseline.json`.

It preserves exact Python locks, wheel filenames/hashes/sizes and HTTPS origins,
model file manifests/notices (NOT weights), six OCI base/service image digests,
and installed system-package inventories. `publisher.py` verifies every listed
baseline file before preparing a new release. Git attributes preserve these bytes.

The sole vendored wheel is the locally built Nuitka 2.7.12 CPython 3.12 Linux amd64
wheel (3,491,910 bytes). Rebuilding a wheel from the same source version does not
guarantee the same hash, so the actually used compiler artifact is retained.
Nuitka is Apache-2.0; its LICENSE and bundled third-party licenses are retained
inside the unmodified wheel, including `nuitka-2.7.12.dist-info/licenses/LICENSE.txt`.
This is third-party compiler tooling, not C-Check's compiled core or model weights.

Other wheels are obtained from exact HTTPS URLs or matching PyPI metadata, always
with the recorded size/SHA256. PyTorch's CPU wheel comes from its official CPU
index. There is no resolver fallback to a newer package or silently rebuilt wheel.
Upstream deletion causes failure; retain a private verified wheel cache as well.

These inputs support a controlled functional rebuild, not bit-for-bit image
reproduction. Existing Dockerfiles still obtain apt packages from live repositories;
inventories are comparison evidence, not an apt snapshot or a guarantee old debs
remain available. Compiler outputs, timestamps and npm/apt infrastructure can also
vary. New builds need their own full gates and recipient hardware acceptance.

Do not edit this baseline to make a failing checksum pass. Review dependencies,
create a separate baseline directory and new version, then validate the change.
