# kh-pcpatch

Applies KHPCPatchManager patches (`.kh1pcpatch`, `.kh2pcpatch`, `.compcpatch`, `.bbspcpatch`, `.dddpcpatch`) to the `.hed`/`.pkg` archives of the Kingdom Hearts HD collections, without Windows or .NET. The PC release (Epic, Steam) and the native Nintendo Switch release use the same archive format.

It has been tested on a Switch with the German voice patches for Kingdom Hearts Final Mix and Kingdom Hearts II Final Mix. Other games and patches use the same code paths but have not been tried.

## Requirements

[uv](https://docs.astral.sh/uv/). The script declares its dependencies inline, so `uv` installs them on the first run.

## Usage

```sh
./kh-pcpatch.py apply GAME_DIR PATCH [PATCH ...] -o OUTPUT [options]
./kh-pcpatch.py verify DIRECTORY
```

`GAME_DIR` is the folder with the game's `<name>.hed` and `<name>.pkg` files, such as `Image/en` on PC. `apply` writes only the archives that change to `OUTPUT` and leaves `GAME_DIR` untouched. When you pass several patches, a later patch overrides files of an earlier one.

| Option | Meaning |
| --- | --- |
| `-o`, `--output DIR` | Folder for the patched `.hed`/`.pkg` pairs |
| `-x`, `--exclude GLOB` | Skip patch files whose path matches, e.g. `'*/title.2ld/*'`. Repeatable |
| `--split` | Write archives over 4 GiB as FAT32 concatenation folders (see below) |
| `-j`, `--jobs N` | Archives patched in parallel (default 3) |
| `-v`, `--verbose` | List every patch file that has no target |

`verify` decrypts and decompresses every asset of every `.hed`/`.pkg` pair in a folder and checks offsets and sizes. Run it on the output before you copy it anywhere.

### Where files go

A patch names the archive for each file (`kh2_first/original/voice/...`). kh-pcpatch looks the file up by name in every `.hed` in `GAME_DIR` and replaces it in the archive that actually contains it. A file that no archive contains is added to the archive the patch names. Keep all `.hed` files of the game in `GAME_DIR` so the lookup sees everything; a `.pkg` is only needed for archives that change.

This matters on the Switch, where some files live in a different archive than on PC.

## Nintendo Switch

The Switch release (title ID `010090C022978000`) contains one program per game, each with its own RomFS:

| Program ID | Game |
| --- | --- |
| `010090C022978001` | Kingdom Hearts Final Mix |
| `010090C022978002` | Re:Chain of Memories |
| `010090C022978003` | Kingdom Hearts II Final Mix |
| `010090C022978004` | Birth by Sleep Final Mix |
| `010090C022978005` | 358/2 Days (theater) |

1. Dump the `.hed`/`.pkg` files from the RomFS root of the game's program, e.g. with [hactool](https://github.com/SciresM/hactool) or [nxdumptool](https://github.com/DarkMatterCore/nxdumptool).
2. Patch them:

   ```sh
   ./kh-pcpatch.py apply kh2-romfs "German voices KH2 2.1.kh2pcpatch" \
       -o sd/atmosphere/contents/010090C022978003/romfs --split
   ./kh-pcpatch.py verify sd/atmosphere/contents/010090C022978003/romfs
   ```

3. Copy `sd/` to the root of the SD card. Atmosphère loads the files through LayeredFS.

Some PC patches replace textures that differ on the Switch. The Switch font atlas `msg/<lang>/fontimage.bar` holds the Switch button icons, and `menu/<lang>/title.2ld` has a different number of textures. Exclude such files with `-x`, for example `-x '*/fontimage.bar/*' -x '*/title.2ld/*'`.

### Files over 4 GiB

FAT32 cannot store files over 4 GiB, and `kh2_fifth.pkg` exceeds that. With `--split`, kh-pcpatch writes such an archive as a folder named like the file, containing parts `00`, `01`, … of 4294901760 bytes each. Horizon reads the folder as one file once its archive bit is set. After copying, set it with hekate: Tools → Arch bit • RCM • Touch • Partitions → Fix Archive Bit.

## Limitations

- When a patch replaces a BAR, MDLX or similar original together with the HD textures inside it, KHPCPatchManager recalculates the texture offsets. kh-pcpatch keeps the old offsets and prints a warning for each such file.
- The custom filename list of KHPCPatchManager is not needed, because files are matched by the paths inside the patch.

## License and credits

Apache License 2.0. The archive encryption and the patch layout follow [OpenKH](https://github.com/OpenKH/OpenKh) and [KHPCPatchManager](https://github.com/AntonioDePau/KHPCPatchManager), both Apache 2.0. See `NOTICE`.
