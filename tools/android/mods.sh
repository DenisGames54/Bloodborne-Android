#!/bin/sh
# Mod and game helper for the app (bbport/mods.sh); prints the JSON of mods_android.py:
#   mods.sh list | validate <name>          read bbport.ini and the mods folder only
#   mods.sh install-pkg <pkg> <name>        unpack a PS4 fake PKG as a mod (needs BB_GAME_DIR to compare
#                                           an eboot.bin in it); `progress <done> <total> <label>` lines, then JSON
#   mods.sh install-game <dest> <pkg> [--replace]
#                                           unpack a game PKG (base game or update) into <dest>/<TITLE_ID>
set -eu
files=${FXD_FILES:-/data/data/com.ducvd89.bloodborne/files}
host="$files/rootfs"
base="$files/bbport"
export PATH="$host/usr/bin:$host/bin:/system/bin"
config_file="$base/bbport.ini"
mods_dir=$(sed -n 's/^mods_dir=//p' "$config_file" 2>/dev/null | tail -n 1)
[ -n "$mods_dir" ] || mods_dir=/storage/emulated/0/Bloodborne/mods
command=${1:-}
case $command in
    list)
        set -- list --mods-dir "$mods_dir" --config "$base/mods.json" ;;
    validate)
        [ -n "${2:-}" ] || { echo 'usage: mods.sh validate <name>' >&2; exit 2; }
        set -- validate --mods-dir "$mods_dir" --name "$2" ;;
    install-pkg)
        [ -n "${2:-}" ] && [ -n "${3:-}" ] || { echo 'usage: mods.sh install-pkg <pkg> <name>' >&2; exit 2; }
        set -- install-pkg --pkg "$2" --name "$3" --mods-dir "$mods_dir" --tool "$base/arm64/bin/ps4-pkg-tool" \
            --game "${BB_GAME_DIR:-}" ;;
    install-game)
        # The app passes --dest <dest> --pkg <pkg>; by position works too.
        if [ "${2:-}" = --dest ] && [ "${4:-}" = --pkg ]; then
            dest=${3:-}; pkg=${5:-}; [ $# -ge 5 ] && shift 5 || shift $#
        else
            dest=${2:-}; pkg=${3:-}; [ $# -ge 3 ] && shift 3 || shift $#
        fi
        [ -n "$dest" ] && [ -n "$pkg" ] || { echo 'usage: mods.sh install-game <dest> <pkg> [--replace]' >&2; exit 2; }
        set -- install-game --dest "$dest" --pkg "$pkg" --tool "$base/arm64/bin/ps4-pkg-tool" "$@" ;;
    *) echo 'usage: mods.sh list | validate <name> | install-pkg <pkg> <name> | install-game <dest> <pkg> [--replace]' >&2; exit 2 ;;
esac
if [ -x "$host/usr/bin/python3.13" ]; then
    PYTHONHOME="$host/usr" exec "$host/usr/bin/python3.13" "$base/scripts/mods_android.py" "$@"
fi
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$base/scripts/mods_android.py" "$@"
fi
case $command in
    install-*) echo '{"ok": false, "problem": "Python runtime missing"}' ;;
    *) echo '{"modsDir": "", "exists": false, "mods": [], "error": "Python runtime missing"}' ;;
esac
