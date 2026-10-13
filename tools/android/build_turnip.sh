#!/usr/bin/env bash
# Turnip (Mesa's Adreno Vulkan driver) for the Thor's Debian trixie arm64 rootfs, KGSL only,
# cross-compiled with Clang, in one of three variants (README in tools/android/mesa-patches):
#   main: Mesa at the pinned commit + mesa-patches/*.patch -> out/arm64/turnip
#         (libvulkan_freedreno.so, freedreno_icd.aarch64.json, drirc.d/)
#   gen8: the turnip/gen8 branch (Adreno 8xx) + mesa-patches/gen8/*.patch (upstream KGSL fixes it
#         predates) + mesa-patches/*.patch -> out/arm64/turnip-gen8 (libvulkan_freedreno_gen8.so,
#         freedreno_gen8_icd.aarch64.json); run-thor.sh picks it on Adreno 8xx.
#   gen8v37: upstream Mesa main at the pinned commit + StevenMXZ's V37 A8xx recipe
#         (mesa-patches/v37, GPL-3.0) + our patches (mesa-patches/*.patch, and v37/ours for the
#         ones the recipe moved out from under) -> out/arm64/turnip-gen8v37
#         (libvulkan_freedreno_gen8v37.so, freedreno_gen8v37_icd.aarch64.json). Not shipped in
#         the runtime bundle; release_turnip.py packages it as a downloadable driver zip.
# build_release_runtime.py ships main and gen8.
set -euo pipefail
cd -- "$(dirname -- "$0")/../.."
variant=${1:-main}
root=$PWD
case $variant in
    main)
        repo=https://gitlab.freedesktop.org/mesa/mesa.git
        MESA_COMMIT=${MESA_COMMIT:-35b085c4c067f198ce0b6437d94678e47b9c645f}
        patches=(tools/android/mesa-patches/*.patch)
        suffix= ;;
    gen8)
        repo=https://github.com/whitebelyash/mesa-unified.git
        MESA_COMMIT=${MESA_COMMIT:-9c7e022677dfa3abb356c2b6732cbd2e25783d01}
        patches=(tools/android/mesa-patches/gen8/*.patch tools/android/mesa-patches/*.patch)
        suffix=_gen8 ;;
    gen8v37)
        repo=https://gitlab.freedesktop.org/mesa/mesa.git
        MESA_COMMIT=${MESA_COMMIT:-1dee441c7ff6bbccc4da5221ca7b146339f317dc}
        v37=tools/android/mesa-patches/v37
        ours=tools/android/mesa-patches
        # mode:file, in the order applied (see apply_v37 below for the modes):
        #  1-3 the KGSL/A8xx fixes of the recipe's common/apply_common.sh that Mesa main lacks,
        #  4   the gen8 stack, 5-6 what of it no longer fits Mesa main (a rejected hunk, a removed
        #  argument), 7-8 the recipe's Python steps, 9 our A840 wildcard (rebased after 8),
        #  10-14 our patches.
        patches=(
            "patch3:$v37/common/a8xx-cube-coord-sanitize.patch"
            "patch3:$v37/common/a8xx-bindless-invalidate.patch"
            "patch3:$v37/common/a8xx-kgsl-ib-vbo-alias.patch"
            "gen8:$v37/a8xx_gen8.patch"
            "git:$v37/ours/0001-ir3-a8xx-helper-functions-rebased-onto-mesa-main.patch"
            "git:$v37/ours/0002-freedreno-a825-drop-removed-magic_regs-argument.patch"
            "python:$v37/a8xx_shared_mem.py"
            "python:$v37/a840v2.py"
            "git:$v37/ours/0003-freedreno-a840-kgsl-patchid-wildcard-after-a840v2.patch"
            "git:$ours/0005-wsi-x11-sw-wait-for-the-gpu-on-the-present-thread.patch"
            "git:$ours/0006-tu-kgsl-power-constraint-from-the-environment.patch"
            "git:$ours/0007-wsi-x11-sw-frames-straight-to-the-app.patch"
            "git:$ours/0008-vulkan-in-process-opaque-fd-for-emulated-timelines.patch"
            "git:$ours/0009-tu-kgsl-wake-pending-syncobj-waits-on-any-device-submit.patch"
        )
        # What the recipe or our earlier builds apply that Mesa main makes redundant (BUILD-INFO.json).
        export TURNIP_SKIPPED_JSON='[
          {"file": "tools/android/mesa-patches/v37/common/apply_common.sh", "reason": "replaced by the steps above with the same post-apply asserts (build_turnip.sh); two of its five patches are in Mesa main"},
          {"file": "tools/android/mesa-patches/v37/common/kgsl-syncobj-merge-ts-fd.patch", "reason": "Mesa main rewrote kgsl_syncobj_merge: it converts the aggregate timestamp to an fd (kgsl_syncobj_ts_to_fd(&ret)) before merging, and no longer reads sync->fd of a timestamp syncobj"},
          {"file": "tools/android/mesa-patches/v37/common/kgsl-zero-timeout-poll.patch", "reason": "Mesa main polls on a zero timeout in wait_timestamp_safe (kgsl_poll_timestamp, KGSL_TIMESTAMP_RETIRED) instead of calling the wait ioctl"},
          {"file": "tools/android/mesa-patches/gen8/0001-tu-kgsl-Prevent-overflow-in-kgsl-queue-wait-fence.patch", "reason": "in Mesa main (os_time_get_absolute_timeout in kgsl_queue_wait_fence)"},
          {"file": "tools/android/mesa-patches/gen8/0002-tu-kgsl-Distinguish-timestamp-and-sync-wait-errors.patch", "reason": "in Mesa main (kgsl_timestamp_error, device loss on EPROTO/ENOENT/EDEADLK)"},
          {"file": "tools/android/mesa-patches/gen8/0003-tu-kgsl-Prevent-indefinite-wait-times-for-0-timeout-waits.patch", "reason": "in Mesa main (git apply -R --check passes)"},
          {"file": "tools/android/mesa-patches/gen8/0004-tu-kgsl-Always-round-up-the-wait-timeout.patch", "reason": "in Mesa main (git apply -R --check passes)"},
          {"file": "tools/android/mesa-patches/0001-freedreno-a840-kgsl-patchid-wildcard.patch", "reason": "does not apply after a840v2.py (context moved); replaced by v37/ours/0003, same entry"}
        ]'
        suffix=_gen8v37 ;;
    *) echo 'usage: build_turnip.sh [main|gen8|gen8v37]' >&2; exit 2 ;;
esac
rootfs=/data/data/com.ducvd89.bloodborne/files/rootfs
deps=$PWD/.local-deps/android
sysroot=$deps/mesa-sysroot
src=$deps/src/mesa${suffix/_/-}
build=$PWD/out/arm64/turnip${suffix/_/-}-build
out=$PWD/out/arm64/turnip${suffix/_/-}

# Only libraries the rootfs has: no xcb-keysyms (it would add a NEEDED the Thor lacks).
if [[ ! -f $sysroot/packages.json ]]; then
    python3 tools/android/fetch_arm64_sysroot.py --out "$sysroot" --packages \
        libc6-dev libstdc++-14-dev libgcc-14-dev linux-libc-dev zlib1g-dev libzstd-dev \
        libexpat1-dev libxcb1-dev libx11-xcb-dev libxcb-xfixes0-dev libxcb-randr0-dev \
        libxcb-dri3-dev libxcb-present-dev libxcb-sync-dev libxcb-shm0-dev libxshmfence-dev \
        libx11-dev libxrandr-dev libdrm-dev
fi
rm -f "$sysroot/usr/lib/aarch64-linux-gnu/pkgconfig/xcb-keysyms.pc"

# Mesa's build tools (meson, mako) in a local virtual environment, not the host's Python.
venv=$deps/mesa-venv
if [[ ! -x $venv/bin/meson ]]; then
    python3 -m venv "$venv"
    "$venv/bin/python" -m pip install -q meson mako pyyaml packaging
fi
export PATH="$venv/bin:$PATH"

# A pristine tree at the pinned commit, then the patches.
if [[ ! -d $src/.git ]]; then
    git init -q "$src"
    git -C "$src" remote add origin "$repo"
fi
git -C "$src" fetch -q --depth 1 origin "$MESA_COMMIT"
git -C "$src" checkout -q --force FETCH_HEAD
git -C "$src" clean -q -fdx
# gen8v37: the V37 recipe's own way of applying each step (the same patch/fuzz options as its
# build_turnip.sh and common/apply_common.sh), run from the Mesa root. Our patches use git apply
# (no fuzz), so a hunk that does not apply fails the build; the one hunk of the recipe's
# a8xx_gen8.patch that Mesa main rejects is named below and replaced by v37/ours/0001.
apply_v37() {
    local mode=$1 file=$2 out rc=0 failed
    case $mode in
        git) git apply "$file" ;;
        patch3)
            patch -p1 -N --fuzz=3 --no-backup-if-mismatch < "$file" ;;
        gen8)
            out=$(patch -p1 -N --fuzz=4 --no-backup-if-mismatch -r - < "$file" 2>&1) || rc=$?
            echo "$out"
            failed=$(awk '/^patching file /{f=$3} /^Hunk #[0-9]+ FAILED/{print f ": " $0}' <<< "$out")
            if [[ $rc != 0 && $failed != 'src/freedreno/ir3/ir3_compiler.h: Hunk #2 FAILED at 396.' ]]; then
                echo "a8xx_gen8.patch: unexpected failed hunks (patch exit $rc): $failed" >&2
                return 1
            fi
            [[ $rc == 0 ]] && { echo "a8xx_gen8.patch applied fully: v37/ours/0001 is not needed" >&2; return 1; }
            return 0 ;;
        python) python3 -I "$file" ;;
        *) echo "unknown step mode $mode" >&2; return 1 ;;
    esac
}
if [[ $variant == gen8v37 ]]; then
    for step in "${patches[@]}"; do
        echo "[v37] ${step%%:*} ${step#*:}"
        (cd "$src" && apply_v37 "${step%%:*}" "$root/${step#*:}") || { echo "step failed: $step" >&2; exit 1; }
    done
    # The recipe's post-apply asserts (common/apply_common.sh), for the fixes applied above ...
    need() { grep -qF -- "$1" "$src/$2" || { echo "post-apply assert failed: '$1' not in $2" >&2; exit 1; }; }
    (need "cube_coord_hang_quirk = True" src/freedreno/common/freedreno_devices.py
    need "SP_GFX_BINDLESS_INVALIDATE" src/freedreno/vulkan/tu_cmd_buffer.h
    need "KGSL_MEMFLAGS_VBO" src/freedreno/vulkan/tu_knl_kgsl.cc
    # ... the two it asserts that Mesa main carries itself (same behavior, different code),
    # and the four KGSL wait fixes of gen8/ ...
    need "return kgsl_poll_timestamp(device, context_id, timestamp);" src/freedreno/vulkan/tu_knl_kgsl.cc
    need "int fd = kgsl_syncobj_ts_to_fd(&ret);" src/freedreno/vulkan/tu_knl_kgsl.cc
    need "os_time_get_absolute_timeout(timeout_ns)" src/freedreno/vulkan/tu_knl_kgsl.cc
    need "kgsl_timestamp_error(device" src/freedreno/vulkan/tu_knl_kgsl.cc
    need "relative_ns % 1000000 != 0" src/freedreno/vulkan/tu_knl_kgsl.cc
    # ... the gen8 stack, shared memory, A840v2, and our patches
    need "ir3_get_gpu_profile" src/freedreno/ir3/ir3_compiler.h
    need "A8XX HELPER FUNCTIONS" src/freedreno/ir3/ir3_compiler.h
    need "cs_shared_mem_size = 64 * 1024" src/freedreno/common/freedreno_devices.py
    if grep -qF "cs_shared_mem_size = 32 * 1024" "$src/src/freedreno/common/freedreno_devices.py"; then
        echo "post-apply assert failed: 32 KiB shared memory left" >&2; exit 1
    fi
    need "chip_id=0x44050A21" src/freedreno/common/freedreno_devices.py
    need "chip_id=0xffff44050Aff" src/freedreno/common/freedreno_devices.py
    need "FEXDROID_PRESENT" src/vulkan/wsi/wsi_common_x11.c
    need "TU_KGSL_PWR_CONSTRAINT" src/freedreno/vulkan/tu_knl_kgsl.cc
    need "tu_submit_wakeup_broadcast" src/freedreno/vulkan/tu_queue.cc
    need "vk_sync_timeline_exports" src/vulkan/runtime/vk_sync_timeline.c
    ) || exit 1
else
    for patch in "${patches[@]}"; do
        git -C "$src" apply "$PWD/$patch"
    done
fi

# cpu 'aarch64': the manifest is then freedreno_icd.aarch64.json with an absolute library_path.
cross=$build.cross
mkdir -p "$(dirname "$cross")"
cat > "$cross" <<EOF
[binaries]
c = ['clang', '--target=aarch64-linux-gnu', '--sysroot=$sysroot']
cpp = ['clang++', '--target=aarch64-linux-gnu', '--sysroot=$sysroot']
c_ld = 'lld'
cpp_ld = 'lld'
ar = 'llvm-ar'
strip = 'llvm-strip'
pkg-config = 'pkg-config'
[built-in options]
c_args = ['-O2']
cpp_args = ['-O2']
[properties]
sys_root = '$sysroot'
pkg_config_libdir = ['$sysroot/usr/lib/aarch64-linux-gnu/pkgconfig', '$sysroot/usr/share/pkgconfig']
[host_machine]
system = 'linux'
cpu_family = 'aarch64'
cpu = 'aarch64'
endian = 'little'
EOF
rm -rf "$build" "$out"
meson setup "$build" "$src" --cross-file "$cross" --prefix="$rootfs/usr" \
    --libdir=lib/aarch64-linux-gnu --wrap-mode=nofallback --buildtype=release -Db_ndebug=true \
    -Dplatforms=x11 -Dvulkan-drivers=freedreno -Dfreedreno-kmds=kgsl -Dgallium-drivers= \
    -Dopengl=false -Degl=disabled -Dgles1=disabled -Dgles2=disabled -Dglx=disabled -Dgbm=disabled \
    -Dllvm=disabled -Dvalgrind=disabled -Dlibunwind=disabled -Dlmsensors=disabled \
    -Dzstd=enabled -Dexpat=enabled -Dvulkan-layers= -Dtools= -Dbuild-tests=false \
    > "$build.log" 2>&1 || { tail -40 "$build.log" >&2; exit 1; }
ninja -C "$build" >> "$build.log" 2>&1 || { tail -40 "$build.log" >&2; exit 1; }
DESTDIR="$build/install" meson install -C "$build" --no-rebuild >> "$build.log" 2>&1

installed=$build/install$rootfs/usr
mkdir -p "$out"
library=libvulkan_freedreno$suffix.so
manifest=freedreno${suffix}_icd.aarch64.json
llvm-strip --strip-debug -o "$out/$library" \
    "$installed/lib/aarch64-linux-gnu/libvulkan_freedreno.so"
sed "s|/libvulkan_freedreno.so\"|/$library\"|" \
    "$installed/share/vulkan/icd.d/freedreno_icd.aarch64.json" > "$out/$manifest"
grep -q "/$library\"" "$out/$manifest" || { echo "Manifest does not name $library" >&2; exit 1; }
cp -r "$installed/share/drirc.d" "$out/drirc.d"
if readelf -d "$out/$library" | grep -q keysyms; then
    echo "$library needs libxcb-keysyms, which the rootfs lacks" >&2
    exit 1
fi
# Provenance for release_turnip.py: exactly what went into this library. Steps are bare file
# names (main, gen8: all git-applied patches) or mode:file (gen8v37: the apply mode is recorded,
# and the scripts are listed with the patches, in order).
python3 - "$variant" "$repo" "$(git -C "$src" rev-parse HEAD)" "$(<"$src/VERSION")" "$library" \
    "$out" "${patches[@]}" <<'PY'
import datetime, hashlib, json, os, sys
variant, repo, commit, version, library, out, *patches = sys.argv[1:]
sha256 = lambda path: hashlib.sha256(open(path, 'rb').read()).hexdigest()
def step(token):
    mode, sep, path = token.partition(':')
    if not sep:
        return {'file': token, 'sha256': sha256(token)}
    return {'file': path, 'sha256': sha256(path),
            'kind': 'script' if mode == 'python' else 'patch', 'apply': mode}
info = {
    'variant': variant, 'mesaRepository': repo, 'mesaCommit': commit, 'mesaVersion': version,
    'buildDate': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'patches': [step(p) for p in patches],
    'library': library, 'librarySha256': sha256(f'{out}/{library}'),
}
if os.environ.get('TURNIP_SKIPPED_JSON'):
    info['skipped'] = json.loads(os.environ['TURNIP_SKIPPED_JSON'])
with open(f'{out}/BUILD-INFO.json', 'w') as f:
    json.dump(info, f, indent=2)
    f.write('\n')
PY
echo "Turnip $variant: $out (Mesa $(git -C "$src" rev-parse --short HEAD) + ${#patches[@]} patches)"
