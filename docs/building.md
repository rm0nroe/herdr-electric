# Building Electric

The initial target is macOS arm64. Builders need Python 3.12+, git, full Xcode,
Rust 1.96.1 (Herdr), Rust 1.95.0 (Codex), and Zig 0.16.0. End users do not need
these build tools. Set `ZIG` to the Zig executable when it is not on PATH.

```sh
python3.12 scripts/build-electric.py /path/to/new-build-directory
```

Release archives come from `.github/workflows/build-electric.yml`, which runs this
script on a GitHub-hosted `macos-26` runner with Xcode 26.2 and attests the archive's
build provenance. Check a download with
`gh attestation verify <archive> --repo rm0nroe/herdr-electric`.

The script fetches pinned public source revisions, applies the checked-in patches,
by default builds both native executables (Codex with 16 codegen units at opt-level 2 so
the final thin-LTO link fits in memory), and verifies the official Codex helper archive
before assembly. Builds run sequentially with one Cargo worker by default to
limit memory pressure. It removes local home-directory prefixes from Rust/C compilation,
strips custom executables, and applies ad-hoc signatures. These binaries are not
Apple-notarized. The tar archive has normalized ownership and timestamps; a
bit-identical compiler output across SDK versions has not been established.

For Herdr-only releases, the workflow's `reuse_codex` input verifies the
v1.9.1 archive's GitHub attestation under the repository name used to sign it,
`rm0nroe/herdr-electrified`. The builder checks the pinned archive digest,
Codex revision, patch, helper package and executable digest before reusing its
signed binary. The manifest records the source archive. A full rebuild remains
the default when dispatching the workflow.

Herdr's Zig dependencies come from its pinned Ghostty package inventory.
The builder verifies Zig's content hash for each package and builds in offline
mode. The small source archive in `scripts/vendor/` is the unmodified
[upstream translate-c archive](https://codeberg.org/vancluever/translate-c/archive/4e879eb8aba615de112eabd1231ea6e01920cead.tar.gz).
Codeberg timed out repeatedly on clean builds.

Before archiving, assembly scans every shipped file for the builder's home path,
any `/Users/<name>` path and macOS temp paths, and stops on a hit. The one
exception is OpenAI's CI paths (`/Users/runner`, and the CI temp dir under
`/var/folders/` in the voice libraries) inside files that are byte-identical to
the pinned upstream package. Unshipped build outputs are not
scanned: their debug map holds object paths that stripping removes.

The tested SDK is full Xcode's macOS 26.2 SDK. The Command Line Tools macOS 27 SDK
failed to link Zig 0.15.2's build runner with unresolved Darwin C symbols.
`DEVELOPER_DIR` is scoped to the build process; no global SDK selection is changed.

| Component | Pinned source |
| --- | --- |
| Herdr 0.9.5 | `herdrdev/herdr@2ecf19a92fc3a7fff75b6ce3655ef04f4625ec01` |
| Codex 0.162.0 | `openai/codex@c1382380de69521303b416720a52f42d51af6248` |
| Codex helpers | official `rust-v0.162.0` macOS arm64 package |
| Font | Nerd Fonts `v3.5.1` `JetBrainsMono.tar.xz` (Mono Regular, Bold, Italic, BoldItalic) |
| Zig | 0.16.0 |

## Releasing a new bundle

1. Bump `BUNDLE_VERSION` in `scripts/build-electric.py` and commit the patches.
2. Run the `build-electric` workflow on GitHub (`workflow_dispatch`, about 2 to 2.5 hours)
   and download its `electric-bundle` artifact.
3. In `src/herdr_electrified/electric.py`, set `BUNDLE` to the new name, append the
   version to `VERSIONS`, and set `BUNDLE_SHA` to the archive's SHA-256 from `SHA256SUMS`.
4. Bump the package version, then attach the archive and `SHA256SUMS` to that
   package's GitHub release: `BUNDLE_URL` points at `v{package version}`.
5. Update the README and reference, then point the formula in `rm0nroe/homebrew-tap` at the published source tag with its SHA-256. Run `brew audit`, install the formula, and run `brew test herdr-electrified`.

The manifest records upstream revisions, patch digests, helper-package and font-archive digests,
and each bundle file's checksum. The release includes the original upstream
notices, native dependency notices, and MPL component sources. See
`licenses/components.json` and `licenses/native/*sources.json` for provenance.

Run the installer suite with `PYTHONPATH=src python -m unittest discover -s tests -v`.
`tests/native_check.py` accepts the built Herdr binary for config validation. Pass `--stock` first to check upstream Herdr at the bundle's version (0.9.5), which does not know `[theme.terminal]`; older stock Herdr cannot parse the Electric config's sidebar `rules` (added in 0.9.2), which only the bundled Herdr reads.
Native session, rendering, clean-machine, and public-install checks are manual
and not part of this suite.
