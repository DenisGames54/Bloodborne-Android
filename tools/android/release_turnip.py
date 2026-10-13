#!/usr/bin/env python3
"""Package the Turnip builds of build_turnip.sh as zips the app's driver installer takes, and
optionally publish them as GitHub releases.

    release_turnip.py [main|gen8|gen8v37 ...] [--publish [--append-to TAG]] [--repo OWNER/NAME]

Each zip is flat: the library, its Vulkan manifest (library_path is the bare file name), meta.json
(name, driverVersion and the build's provenance) and Mesa's licenses. Zips are deterministic: the
same build gives the same bytes. The patches the library was built with travel beside it in patches-*.tar.gz (some
are not committed anywhere else). --publish creates the release turnip-<variant>-<version>-<sha>
and never touches one that exists. With --append-to TAG it instead uploads the zip and the
patches tarball to the existing release TAG (never replacing an asset: no --clobber) and appends
this build's notes to the release's. gen8v37 contains StevenMXZ's GPL-3.0 V37 recipe, so its
tarball carries the recipe files and the license text.
"""
import argparse
import glob
import gzip
import hashlib
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'out/arm64/turnip-release'
PATCHES = 'tools/android/mesa-patches'
# Mirrors build_turnip.sh: the patch globs in its order, and where each variant is built.
VARIANTS = {
    'main': {
        'dir': ROOT / 'out/arm64/turnip',
        'src': ROOT / '.local-deps/android/src/mesa',
        'patches': [f'{PATCHES}/*.patch'],
        'library': 'libvulkan_freedreno.so',
        'manifest': 'freedreno_icd.aarch64.json',
        'name': 'Turnip main (Adreno 6xx/7xx)',
        'gpu': 'a6xx-a7xx',
        'adreno': 'Adreno 6xx/7xx',
    },
    'gen8': {
        'dir': ROOT / 'out/arm64/turnip-gen8',
        'src': ROOT / '.local-deps/android/src/mesa-gen8',
        'patches': [f'{PATCHES}/gen8/*.patch', f'{PATCHES}/*.patch'],
        'library': 'libvulkan_freedreno_gen8.so',
        'manifest': 'freedreno_gen8_icd.aarch64.json',
        'name': 'Turnip gen8 (Adreno 8xx)',
        'gpu': 'a8xx',
        'adreno': 'Adreno 8xx',
    },
}
VARIANTS['gen8v37'] = {
    'dir': ROOT / 'out/arm64/turnip-gen8v37',
    'src': ROOT / '.local-deps/android/src/mesa-gen8v37',
    'patches': None,  # no reconstruction: only a build with BUILD-INFO.json can be packaged
    'library': 'libvulkan_freedreno_gen8v37.so',
    'manifest': 'freedreno_gen8v37_icd.aarch64.json',
    'name': 'Turnip gen8 V37 recipe (Adreno 8xx)',
    'gpu': 'a8xx',
    'adreno': 'Adreno 8xx',
    'recipe': True,
}
V37 = f'{PATCHES}/v37'
RECIPE_REPO = 'https://github.com/StevenMXZ/Adreno-Tools-Drivers'
RECIPE_COMMIT = 'b3b32ad'
# The gen8v37 build also needs these to be understood: how each step was applied, what each is.
APPLY_TEXT = {
    'patch3': 'patch -p1 -N --fuzz=3 --no-backup-if-mismatch',
    'gen8': 'patch -p1 -N --fuzz=4 --no-backup-if-mismatch -r - (exactly one hunk is rejected: '
            'ir3_compiler.h hunk 2, replaced by the next step)',
    'git': 'git apply',
    'python': 'python3 -I',
}
STEP_TEXT = {
    'a8xx-cube-coord-sanitize.patch': 'V37 recipe: sanitize cube map directions on A8XX (hang with NaN/zero vectors)',
    'a8xx-bindless-invalidate.patch': 'V37 recipe: invalidate bindless descriptors through the A8XX registers',
    'a8xx-kgsl-ib-vbo-alias.patch': 'V37 recipe: fetch A8XX command streams through a virtual BO alias (KGSL)',
    'a8xx_gen8.patch': 'V37 recipe: the turnip/gen8 stack (A8xx family configs and compiler tuning, A825/A829/A830 '
                       'entries, DECK_EMU, and the rest of whitebelyash/mesa-unified turnip/gen8)',
    'a8xx_shared_mem.py': 'V37 recipe: compute shared memory 32 KiB to 64 KiB on every GPU entry',
    'a840v2.py': 'V37 recipe: Adreno 840v2 (chip id 0x44050A21)',
}
DEFAULT_REPO = 'ducvd89/Bloodborne-Android'
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git(tree, *args):
    return subprocess.run(['git', '-C', str(tree), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def provenance(variant, spec):
    """BUILD-INFO.json of the build, else (builds that predate it) rebuilt from the source tree."""
    library = (spec['dir'] / spec['library']).read_bytes()
    info_file = spec['dir'] / 'BUILD-INFO.json'
    if info_file.is_file():
        info = json.loads(info_file.read_text())
        if info['librarySha256'] != sha256(library):
            raise SystemExit(f"{spec['library']} is not the library BUILD-INFO.json describes; rebuild it")
        info['provenance'] = 'build-info'
        return info, library
    src = spec['src']
    if spec['patches'] is None:
        raise SystemExit(f'No BUILD-INFO.json in {spec["dir"]}: build it with build_turnip.sh {variant}')
    if not (src / '.git').exists():
        raise SystemExit(f'No BUILD-INFO.json in {spec["dir"]} and no Mesa tree at {src}')
    # build_turnip.sh applies the patches uncommitted: a clean tree means they were not applied.
    if not git(src, 'status', '--porcelain', '--untracked-files=no'):
        raise SystemExit(f'{src} has no local modifications: the patches are not applied there')
    # Each glob's matches sorted, the globs in build_turnip.sh's order (gen8 patches first).
    patches = [str(Path(match).relative_to(ROOT)) for pattern in spec['patches']
               for match in sorted(glob.glob(str(ROOT / pattern)))]
    return {
        'variant': variant,
        'mesaRepository': git(src, 'remote', 'get-url', 'origin'),
        'mesaCommit': git(src, 'rev-parse', 'HEAD'),
        'mesaVersion': (src / 'VERSION').read_text().strip(),
        'patches': [{'file': p, 'sha256': sha256((ROOT / p).read_bytes())} for p in patches],
        'library': spec['library'],
        'librarySha256': sha256(library),
        'provenance': 'reconstructed',
    }, library


def patch_subject(path):
    """The one-line description of a patch: its Subject: header (which may wrap), minus [PATCH]."""
    if Path(path).name in STEP_TEXT:
        return STEP_TEXT[Path(path).name]
    lines = (ROOT / path).read_text(errors='replace').splitlines()
    for number, line in enumerate(lines):
        if line.startswith('Subject:'):
            subject = line[len('Subject:'):].strip()
            for more in lines[number + 1:]:
                if not more.startswith((' ', '\t')):
                    break
                subject += ' ' + more.strip()
            return re.sub(r'^\[PATCH[^\]]*\]\s*', '', subject)
    return Path(path).stem


def license_text(src, commit):
    """Mesa's license files from the source tree, verbatim: docs/license.rst, then licenses/*."""
    if git(src, 'rev-parse', 'HEAD') != commit:
        print(f'warning: {src} is not at {commit[:7]}; its license text may differ', file=sys.stderr)
    names = ['docs/license.rst'] + sorted(
        str(f.relative_to(src)) for f in (src / 'licenses').rglob('*') if f.is_file())
    if not (src / names[0]).is_file() or len(names) < 2:
        raise SystemExit(f'No Mesa license files in {src}')
    return '\n\n'.join(f'==== {name} ====\n\n{(src / name).read_text()}' for name in names)


def recipe_license():
    """The GPL-3.0 text the V37 recipe comes under, with a header saying what it covers."""
    return ('\n\n==== GNU General Public License v3 (covers the V37 recipe patches) ====\n\n'
            f'The V37 recipe files in this build (the patches and Python scripts of {RECIPE_REPO}\n'
            f'at {RECIPE_COMMIT}, listed in patches-gen8v37-*.tar.gz and meta.json) are GPL-3.0; their text follows.\n'
            'Mesa itself is under the licenses above. The patches are in the tarball next to this zip.\n\n'
            + (ROOT / V37 / 'LICENSE.GPL-3.0').read_text())


def commit_url(repository, commit):
    base = repository.removesuffix('.git')
    return f'{base}/-/commit/{commit}' if 'gitlab' in base else f'{base}/commit/{commit}'


def write_zip(path, files):
    """files: name -> bytes. Fixed times and modes, sorted names: the same input, the same zip."""
    with zipfile.ZipFile(path, 'w') as zf:
        for name in sorted(files):
            entry = zipfile.ZipInfo(name, ZIP_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            zf.writestr(entry, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def write_patches(path, info, meta):
    """The exact patch files of meta.json, as the source that goes with the binary."""
    for patch in meta['patches']:
        if sha256((ROOT / patch['file']).read_bytes()) != patch['sha256']:
            raise SystemExit(f"{patch['file']} changed since the build (sha256 differs from meta.json)")
    recipe = 'recipe' in meta
    if recipe:
        # Steps with how they were applied (BUILD-INFO.json: apply), the V37 recipe files with
        # their license, and the script whose options and asserts drive them.
        order = '\n'.join(
            f"{n}. {Path(p['file']).relative_to('tools/android')}  sha256 {p['sha256']}\n"
            f"   applied with: {APPLY_TEXT.get(p.get('apply'), p.get('apply'))}"
            for n, p in enumerate(meta['patches'], 1))
        skipped = ''.join(f"- {Path(k['file']).relative_to('tools/android')}: {k['reason']}\n"
                          for k in info.get('skipped', []))
        text = (f"Turnip {info['variant']}, Mesa {info['mesaVersion']}\n"
                f"Repository: {info['mesaRepository']}\nCommit: {info['mesaCommit']}\n\n"
                f"Steps, in the order applied, each from the Mesa root (after the checkout of the commit above):\n{order}\n\n"
                f"Not applied (already in Mesa main, or replaced by the steps above):\n{skipped}\n"
                f"patches/build_turnip.sh is the script that ran these steps and the asserts after them.\n"
                f"mesa-patches/v37/ is StevenMXZ's V37 recipe ({RECIPE_REPO} at {RECIPE_COMMIT}), GPL-3.0:\n"
                f"see mesa-patches/v37/SOURCE.md and mesa-patches/v37/LICENSE.GPL-3.0.\n")
    else:
        order = '\n'.join(f"{n}. {Path(p['file']).relative_to('tools/android')}  sha256 {p['sha256']}"
                          for n, p in enumerate(meta['patches'], 1))
        text = (f"Turnip {info['variant']}, Mesa {info['mesaVersion']}\n"
                f"Repository: {info['mesaRepository']}\nCommit: {info['mesaCommit']}\n\n"
                f"Patches, in the order applied (git apply, from the Mesa root):\n{order}\n")
    files = {'SOURCE.txt': text.encode()}
    for patch in meta['patches']:
        files['patches/' + str(Path(patch['file']).relative_to('tools/android'))] = (ROOT / patch['file']).read_bytes()
    if recipe:
        for extra in (f'{V37}/LICENSE.GPL-3.0', f'{V37}/SOURCE.md', 'tools/android/build_turnip.sh'):
            files['patches/' + str(Path(extra).relative_to('tools/android'))] = (ROOT / extra).read_bytes()
    # Reproducible: sorted names, no times or owners, a gzip header without a name or time.
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', mtime=0, filename='', compresslevel=9) as gz, \
            tarfile.open(fileobj=gz, mode='w', format=tarfile.GNU_FORMAT) as tar:
        for name in sorted(files):
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(files[name]), 0o644
            tar.addfile(entry, io.BytesIO(files[name]))
    path.write_bytes(buffer.getvalue())


def release_notes(spec, info, meta, base, zip_sha, patches_name, patches_sha):
    mesa = meta['mesa']
    patches = '\n'.join(f"- `{Path(p['file']).name}`: {patch_subject(p['file'])}" for p in info['patches'])
    built = f"- Built: {info['buildDate']}\n" if 'buildDate' in info else ''
    note = ('\nThe build predates `BUILD-INFO.json`, so its provenance was reconstructed from the '
            'patched Mesa source tree it was built from.\n' if info['provenance'] == 'reconstructed' else '')
    recipe = spec.get('recipe')
    title = f"# {meta['name']}: Mesa {mesa['version']} ({mesa['commit'][:7]})"
    lead = f"""Turnip, Mesa's open-source Vulkan driver for Qualcomm {spec['adreno']} GPUs (KGSL kernel driver),
as shipped in the Bloodborne Android app."""
    if recipe:
        lead = f"""Turnip, Mesa's open-source Vulkan driver for Qualcomm {spec['adreno']} GPUs (KGSL kernel driver),
built from Mesa main `{mesa['commit'][:7]}` with [StevenMXZ's V37 A8xx recipe]({RECIPE_REPO}/tree/{RECIPE_COMMIT})
([{RECIPE_REPO.removeprefix('https://github.com/')}]({RECIPE_REPO}) at `{RECIPE_COMMIT}`, the `a8xx-patchs2` variant;
**GPL-3.0**) and this app's own Mesa patches.

**Not tested on hardware yet.** It has been built and checked as a library (aarch64 ELF, the
rootfs's libraries only, Vulkan entry points, the A8xx device table), but not run on an Adreno 8xx device.
Use `gen8` if you need the known-working driver."""
        skipped = '\n'.join(f"- `{Path(k['file']).name}`: {k['reason']}" for k in info.get('skipped', []))
        recipe_text = f"""
Applied to Mesa main, the recipe needed a few adaptations (all in `patches/mesa-patches/v37/ours/`
of `{patches_name}`, none skipped silently): one rejected hunk of `a8xx_gen8.patch` rebased, the
removed `magic_regs` argument of the A825 entry dropped, and our A840 wildcard patch rebased after `a840v2.py`.
Not applied, because Mesa main already has them:

{skipped}

The V37 recipe files are licensed GPL-3.0: they and the license text are in `{patches_name}`
(`mesa-patches/v37/`, with `SOURCE.md`), and the license is also appended to `LICENSE-mesa.txt` in the zip.
"""
    else:
        recipe_text = ''
    return f"""{title}

{lead}

This is a **Linux (glibc, aarch64) build for this app's runtime**, not an adrenotools / Android
driver: it loads in the game's Linux rootfs and will not work in other Android emulators.

## Install

In the app: menu → GPU driver → Download, or Install zip (pick `{base}.zip`).

## Build

- GPUs: {spec['adreno']}
- Mesa: [{mesa['repository'].removesuffix('.git')} @ {mesa['commit'][:7]}]({commit_url(mesa['repository'], mesa['commit'])}), commit `{mesa['commit']}`, version {mesa['version']}
- Vulkan API version: {meta['vulkanVersion']}
{built}{note}
Patches applied on top, in order:

{patches}
{recipe_text}
Source: Mesa commit above + patches in `{patches_name}` (with `SOURCE.txt` giving the order).

## Checksums (SHA-256)

- `{base}.zip`: `{zip_sha}`
- `{patches_name}`: `{patches_sha}`
- `{info['library']}`: `{info['librarySha256']}`

Mesa is licensed under the MIT license (individual files may carry their own); the license texts are in
`LICENSE-mesa.txt` in the zip.
"""


def package(variant):
    spec = VARIANTS[variant]
    info, library = provenance(variant, spec)
    version = re.sub(r'-devel$', '', info['mesaVersion'])
    short = info['mesaCommit'][:7]
    base = f'Turnip-{variant}-{version}-{short}'
    icd = json.loads((spec['dir'] / spec['manifest']).read_text())
    # The installer writes its own manifest: the zip's names the library by bare file name.
    icd['ICD']['library_path'] = info['library']
    meta = {
        'schemaVersion': 1,
        'name': spec['name'],
        'description': (f"Mesa Turnip (Vulkan for Qualcomm {spec['adreno']}, KGSL) for the Bloodborne "
                        f"Android app's Linux (glibc) runtime, with the app's patches."
                        if not spec.get('recipe') else
                        f"Mesa Turnip (Vulkan for Qualcomm {spec['adreno']}, KGSL) from Mesa main with StevenMXZ's "
                        f"V37 A8xx recipe (GPL-3.0) and the app's patches, for the Bloodborne Android app's "
                        f"Linux (glibc) runtime. Not tested on hardware yet."),
        'author': 'ducvd89',
        'packageVersion': '1',
        'vendor': 'Mesa',
        'driverVersion': (f"Mesa {info['mesaVersion']} {short} + V37 recipe" if spec.get('recipe')
                          else f"Mesa {info['mesaVersion']} {short}"),
        'minApi': 28,
        'libraryName': info['library'],
        'platform': 'linux',
        'architecture': 'aarch64',
        'libc': 'glibc',
        'variant': variant,
        'gpu': spec['gpu'],
        'vulkanVersion': icd['ICD']['api_version'],
        'mesa': {'repository': info['mesaRepository'], 'commit': info['mesaCommit'],
                 'version': info['mesaVersion']},
        'patches': info['patches'],
        'sha256': info['librarySha256'],
        'provenance': info['provenance'],
    }
    if 'buildDate' in info:
        meta['buildDate'] = info['buildDate']
    if spec.get('recipe'):
        meta['recipe'] = {'name': 'V37 (a8xx-patchs2)', 'author': 'StevenMXZ', 'repository': RECIPE_REPO,
                          'commit': RECIPE_COMMIT, 'license': 'GPL-3.0'}
        meta['skipped'] = info.get('skipped', [])
    dump = lambda value: (json.dumps(value, indent=2) + '\n').encode()
    OUT.mkdir(parents=True, exist_ok=True)
    zip_path = OUT / f'{base}.zip'
    write_zip(zip_path, {
        info['library']: library,
        spec['manifest']: dump(icd),
        'meta.json': dump(meta),
        'LICENSE-mesa.txt': (license_text(spec['src'], info['mesaCommit'])
                             + (recipe_license() if spec.get('recipe') else '')).encode(),
    })
    zip_sha = sha256(zip_path.read_bytes())
    # Not Turnip-*.zip: the app's downloader picks that name, and this is not a driver.
    patches_path = OUT / f'patches-{variant}-{version}-{short}.tar.gz'
    write_patches(patches_path, info, meta)
    patches_sha = sha256(patches_path.read_bytes())
    notes_path = OUT / f'{base}.md'
    notes_path.write_text(release_notes(spec, info, meta, base, zip_sha, patches_path.name, patches_sha))
    print(f'{variant}: {zip_path} ({zip_path.stat().st_size:,} bytes), sha256 {zip_sha} [{info["provenance"]}]')
    print(f'{variant}: {patches_path} ({patches_path.stat().st_size:,} bytes), sha256 {patches_sha}')
    return {'tag': f'turnip-{variant}-{version}-{short}',
            'title': f"Turnip {variant} {version} ({short}) — {spec['adreno']}",
            'zip': zip_path, 'patches': patches_path, 'notes': notes_path}


def publish(release, repo):
    probe = subprocess.run(['gh', 'release', 'view', release['tag'], '--repo', repo],
                           capture_output=True, text=True)
    if probe.returncode == 0:
        print(f"Release {release['tag']} already exists in {repo}; skipping")
        return
    if 'not found' not in probe.stderr.lower():
        raise SystemExit(f"gh release view failed: {probe.stderr.strip()}")
    head = git(ROOT, 'rev-parse', 'HEAD')
    command = ['gh', 'release', 'create', release['tag'], str(release['zip']), str(release['patches']),
               '--repo', repo,
               '--title', release['title'], '--notes-file', str(release['notes']),
               '--latest=false', '--target', head]
    print('+', shlex.join(command))
    subprocess.run(command, check=True)


def publish_append(release, repo, tag):
    """Add this build's zip and tarball to the existing release TAG and append its notes."""
    show = ['gh', 'release', 'view', tag, '--repo', repo, '--json', 'body,assets']
    print('+', shlex.join(show))
    probe = subprocess.run(show, capture_output=True, text=True)
    if probe.returncode != 0:
        raise SystemExit(f'gh release view {tag} failed: {probe.stderr.strip()}')
    current = json.loads(probe.stdout)
    have = {a['name'] for a in current['assets']}
    files = [release[key] for key in ('zip', 'patches')]
    new = [f for f in files if f.name not in have]
    for f in files:
        if f.name in have:
            print(f'{tag} already has {f.name}; not uploading it again')
    if new:
        # No --clobber: an existing asset is never replaced.
        command = ['gh', 'release', 'upload', tag, *map(str, new), '--repo', repo]
        print('+', shlex.join(command))
        subprocess.run(command, check=True)
    section = release['notes'].read_text()
    if section.splitlines()[0] in current['body'].splitlines():
        print(f"The notes of {tag} already have the section \"{section.splitlines()[0]}\"; not appending it")
        return
    body = current['body'].rstrip('\n') + '\n\n---\n\n' + section
    notes = release['notes'].with_name(f"{release['notes'].stem}.{tag}.notes")
    notes.write_text(body)
    command = ['gh', 'release', 'edit', tag, '--repo', repo, '--notes-file', str(notes)]
    print('+', shlex.join(command))
    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('variants', nargs='*', metavar='{main,gen8,gen8v37}',
                        help='variants to package (default: main and gen8)')
    parser.add_argument('--publish', action='store_true', help='create the GitHub releases')
    parser.add_argument('--append-to', metavar='TAG',
                        help='with --publish: upload the zip and tarball to the existing release TAG and append '
                             "the build's notes to its notes, instead of creating a release (one variant)")
    parser.add_argument('--clobber', action='store_true', help='refused: published assets are never replaced')
    parser.add_argument('--repo', default=DEFAULT_REPO, help=f'GitHub repository (default {DEFAULT_REPO})')
    args = parser.parse_args()
    for variant in args.variants:
        if variant not in VARIANTS:
            parser.error(f'unknown variant {variant}')
    if args.clobber:
        parser.error('--clobber is refused: a published asset is never replaced')
    if args.append_to and not args.publish:
        parser.error('--append-to needs --publish')
    if args.append_to and len(args.variants) != 1:
        parser.error('--append-to takes exactly one variant')
    releases = [package(variant) for variant in args.variants or ['main', 'gen8']]
    if args.publish:
        for release in releases:
            if args.append_to:
                publish_append(release, args.repo, args.append_to)
            else:
                publish(release, args.repo)


if __name__ == '__main__':
    main()
