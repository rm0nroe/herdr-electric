#!/usr/bin/env python3
"""Build the pinned macOS arm64 renderers. Requires Python 3.12+, git, Rust, Zig 0.16.0 and full Xcode."""
import argparse
import gzip
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.request

REPO = Path(__file__).resolve().parents[1]
SOURCES = {'herdr': ('https://github.com/herdrdev/herdr.git', '2ecf19a92fc3a7fff75b6ce3655ef04f4625ec01'),
           'codex': ('https://github.com/openai/codex.git', 'c1382380de69521303b416720a52f42d51af6248')}
PACKAGE_URL = 'https://github.com/openai/codex/releases/download/rust-v0.162.0/codex-package-aarch64-apple-darwin.tar.gz'
PACKAGE_SHA = '5809ee90a9c3b59d438bb2663aefa0b43d86f825438b65d4504b31f82343628b'
FONT_URL = 'https://github.com/ryanoasis/nerd-fonts/releases/download/v3.5.1/JetBrainsMono.tar.xz'
FONT_SHA = '04d5e8f903693f9dd13e16f867e994834e681eb3c72c0d337a770dcda09010cf'
FONTS = [f'JetBrainsMonoNerdFontMono-{style}.ttf' for style in ('Regular', 'Bold', 'Italic', 'BoldItalic')]
BUNDLE_VERSION = '0.11.0'
PREVIOUS_BUNDLE = 'herdr-electrified-0.10.0-macos-arm64'
PREVIOUS_SHA = '3f58d6396d079adc0c2064a7f59c9aa63b86ed900888d87ab6162509d46124e5'


def build_env():
    env = os.environ.copy()
    env['DEVELOPER_DIR'] = env.get('DEVELOPER_DIR', '/Applications/Xcode.app/Contents/Developer')
    env.pop('SDKROOT', None)
    env.pop('CARGO_TARGET_DIR', None)
    env.setdefault('CARGO_BUILD_JOBS', '1')
    env['CARGO_ENCODED_RUSTFLAGS'] = '--remap-path-prefix=' + str(Path.home()) + '=/build'
    env['CFLAGS'] = '-ffile-prefix-map=' + str(Path.home()) + '=/build'
    env['CXXFLAGS'] = env['CFLAGS']
    return env


def run(argv, cwd):
    subprocess.run(argv, cwd=cwd, env=build_env(), check=True)


def prepare_ghostty(work, source):
    root = source / 'vendor/libghostty-vt'
    packages = json.loads((root / 'build.zig.zon.json').read_text())
    needed = {'uucode', 'translate_c', 'aro', 'pixels', 'zlib', 'iterm2_themes', 'highway', 'wuffs'}
    for expected, package in packages.items():
        if package['name'] not in needed:
            continue
        print(f"Fetching Zig dependency {package['name']}", flush=True)
        if package['name'] == 'translate_c':
            # The pinned Codeberg archive repeatedly stalls on clean macOS runners.
            archive = REPO / 'scripts/vendor/translate-c-4e879eb8.tar.gz'
        else:
            archive = work / (package['name'] + '.tar.gz')
            run(['curl', '--fail', '--location', '--silent', '--show-error', '--max-time', '90', '--retry', '2',
                 '--output', str(archive), package['url']], root)
        actual = subprocess.check_output([os.environ.get('ZIG', 'zig'), 'fetch', str(archive)], cwd=root, text=True).strip()
        if actual != expected:
            raise ValueError(f"Zig package checksum mismatch: {package['name']}")
        needed.remove(package['name'])
    if needed:
        raise ValueError(f'missing Zig dependencies: {sorted(needed)}')
    os.environ['LIBGHOSTTY_VT_ZIG_SYSTEM_DIR'] = str(root / 'zig-pkg')


def released_codex(work, archive):
    with archive.open('rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != PREVIOUS_SHA:
            raise ValueError('unexpected previous Electric archive')
    with tarfile.open(archive) as tar:
        manifest = json.load(tar.extractfile(PREVIOUS_BUNDLE + '/manifest.json'))
        source = manifest['sources']['codex']
        url, revision = SOURCES['codex']
        patch = hashlib.sha256((REPO / 'patches/codex.patch').read_bytes()).hexdigest()
        if source != {'url': url, 'revision': revision, 'patch_sha256': patch}:
            raise ValueError('Codex sources changed; run a full build')
        if manifest['upstream_package'] != {'url': PACKAGE_URL, 'sha256': PACKAGE_SHA}:
            raise ValueError('Codex helper package changed; run a full build')
        data = tar.extractfile(PREVIOUS_BUNDLE + '/codex/bin/codex').read()
        if hashlib.sha256(data).hexdigest() != manifest['files']['codex/bin/codex']:
            raise ValueError('previous Codex binary checksum mismatch')
    executable = work / 'codex-electric'
    executable.write_bytes(data)
    executable.chmod(0o755)
    run(['/usr/bin/codesign', '--verify', '--strict', str(executable)], work)
    return executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('work', type=Path, help='new empty build directory')
    parser.add_argument('--released-codex', type=Path, metavar='ARCHIVE', help='reuse Codex from the pinned v1.9.1 Electric archive')
    args = parser.parse_args()
    if (platform.system(), platform.machine()) != ('Darwin', 'arm64'):
        parser.error('macOS arm64 is required')
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    for name, (url, revision) in SOURCES.items():
        if name == 'codex' and args.released_codex:
            codex = released_codex(work, args.released_codex)
            continue
        source = work / name
        source.mkdir()
        for argv in (['git', 'init', '-q'], ['git', 'fetch', '--depth=1', url, revision],
                     ['git', 'checkout', '--detach', 'FETCH_HEAD'], ['git', 'apply', str(REPO / 'patches' / (name + '.patch'))]):
            run(argv, source)
        if name == 'herdr':
            prepare_ghostty(work, source)
        cwd = source / 'codex-rs' if name == 'codex' else source
        # Codex: smaller codegen units and opt-level 2 keep the final thin-LTO link within memory.
        build = (['rustc', '-p', 'codex-cli', '--release', '--locked', '--bin', name, '--', '-C', 'codegen-units=16', '-C', 'opt-level=2']
                 if name == 'codex' else ['build', '--release', '--locked', '--bin', name])
        if name == 'codex':
            codex = cwd / 'target/release/codex'
        run(['cargo', '+1.95.0' if name == 'codex' else '+1.96.1', *build], cwd)
    package, fonts = work / 'upstream-codex.tar.gz', work / 'fonts.tar.xz'
    for url, sha, path in ((PACKAGE_URL, PACKAGE_SHA, package), (FONT_URL, FONT_SHA, fonts)):
        urllib.request.urlretrieve(url, path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError(f'checksum mismatch: {url}')
    assemble(work, work / 'herdr/target/release/herdr', codex, package, fonts, reused_codex=bool(args.released_codex))


def privacy_scan(bundle, upstream, home=str(Path.home())):
    """Shipped files only. /Users/runner and /var/folders/ (upstream CI) are allowed only in files byte-identical to the pinned package."""
    pattern = re.compile(re.escape(home.encode()) + rb'|/Users/[A-Za-z0-9._-]{0,64}|/private/var/folders/|/var/folders/')
    findings = []
    for path in sorted(bundle.rglob('*')):
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            continue
        name = str(path.relative_to(bundle))
        with path.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            hits = {m.group() for m in pattern.finditer(data)}
            if hits <= {b'/Users/runner', b'/var/folders/'} and upstream.get(name) == hashlib.sha256(data).hexdigest():
                continue
        if hits:
            findings.append({'file': name, 'hits': sorted(h.decode(errors='replace') for h in hits)})
    return findings


def assemble(work, herdr, codex, upstream, fonts, *, reused_codex=False):
    """Also used by release verification with already-built exact patched sources."""
    work = Path(work)
    bundle = work / f'herdr-electrified-{BUNDLE_VERSION}-macos-arm64'
    bundle.mkdir(exist_ok=False)
    (bundle / 'codex').mkdir()
    with tarfile.open(upstream) as archive:
        archive.extractall(bundle / 'codex', filter='data')
    upstream_hashes = {str(p.relative_to(bundle)): hashlib.sha256(p.read_bytes()).hexdigest() for p in bundle.rglob('*') if p.is_file()}
    shutil.copy2(herdr, bundle / 'herdr')
    shutil.copy2(codex, bundle / 'codex/bin/codex')
    executables = [bundle / 'herdr']
    if not reused_codex:
        executables.append(bundle / 'codex/bin/codex')
    for executable in executables:
        run(['/usr/bin/strip', '-S', '-x', str(executable)], work)
        run(['/usr/bin/codesign', '--force', '--sign', '-', str(executable)], work)
    (bundle / 'fonts').mkdir()
    with tarfile.open(fonts) as archive:
        for name in FONTS:
            (bundle / 'fonts' / name).write_bytes(archive.extractfile(name).read())
    shutil.copytree(REPO / 'licenses', bundle / 'licenses')
    for name in ('LICENSE', 'NOTICE'):
        shutil.copy2(REPO / name, bundle / name)
    (bundle / 'themes').mkdir()
    shutil.copy2(REPO / 'src/herdr_electrified/data/codex-electric.tmTheme', bundle / 'themes/codex-electric.tmTheme')
    manifest = {'version': BUNDLE_VERSION, 'target': 'aarch64-apple-darwin',
                'sources': {name: {'url': url, 'revision': rev, 'patch_sha256': hashlib.sha256((REPO / 'patches' / (name + '.patch')).read_bytes()).hexdigest()} for name, (url, rev) in SOURCES.items()},
                'upstream_package': {'url': PACKAGE_URL, 'sha256': PACKAGE_SHA}, 'fonts': {'url': FONT_URL, 'sha256': FONT_SHA},
                'files': {str(p.relative_to(bundle)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(bundle.rglob('*')) if p.is_file()}}
    if reused_codex:
        manifest['codex_bundle'] = {'release': 'v1.9.1', 'archive': PREVIOUS_BUNDLE + '.tar.gz', 'sha256': PREVIOUS_SHA}
    (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    findings = privacy_scan(bundle, upstream_hashes)
    if findings:
        raise ValueError(f'privacy scan failed: {findings}')
    output = work / (bundle.name + '.tar.gz')
    with output.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as archive:
        for path in sorted(bundle.rglob('*')):
            info = archive.gettarinfo(str(path), arcname=str(path.relative_to(bundle.parent)))
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ''
            if path.is_file():
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
    (work / 'SHA256SUMS').write_text(hashlib.sha256(output.read_bytes()).hexdigest() + '  ' + output.name + '\n')
    print(output)


if __name__ == '__main__':
    main()
