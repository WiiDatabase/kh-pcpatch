#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click",
#     "rich",
# ]
# ///
import fnmatch
import hashlib
import struct
import zipfile
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import click
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn

MASTER = bytes.fromhex(
    "7E8897550B06F108EBBB141CD87AEC4134B2A346EF6BFEE1CF53A50512D28E52"
    "4A80E981B0F0B49CFF0F1513DA734E77BED730E5F65A113767BC836F2776D0CD"
    "690D2E514290B8B64CADCE5B1A1FF5AF01F85E3A6E688BE89FC9D9269229C833"
    "983254D4442566AC5F9921E48F1DC2D5A462F90261DE59E7079AFA2F953F86D3"
    "78A775EDD62D6487BDC7C1AAF28C17CB318AC3CC04EE6AAB5C2270CA9E716D85"
    "455DB9A9A61047FB827D847BC6E238FC2B0E209DC5F339A8A06558437CE33618"
    "724979AED17440C4914F2463BFBA239650B357DF1E03487F354D3EE6A1DD093C"
    "3D3B568D932A9B4B0C28B1E0608919DB2CF76CB51B94C0DCEAB70AF416FDA200"
)
RCON = [1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]
PASSES = 10
PART_SIZE = 0xFFFF0000
FAT32_MAX = 0xFFFFFFFF
DELTA_SUFFIX = ".delta"
COPY_CHUNK = 16 << 20
KINDS = ("original", "remastered", "raw")

console = Console(stderr=True)


def gen_key(seed: bytes) -> bytes:
    k = bytearray(0xB0)
    for i, b in enumerate(seed):
        k[i] = b if b else i
    for i in range(PASSES * 4):
        f = k[0x0C + i * 4 : 0x10 + i * 4]
        if i % 4 == 0:
            f = bytes([MASTER[f[1]] ^ RCON[i // 4], MASTER[f[2]], MASTER[f[3]], MASTER[f[0]]])
        for j in range(4):
            k[0x10 + i * 4 + j] = k[i * 4 + j] ^ f[j]
    return bytes(k)


def crypt(data: bytes, seed: bytes) -> bytes:
    k = gen_key(seed)
    d = bytearray(data)
    for idx in range(0, min(len(d), 0x100), 0x10):
        for p in range(PASSES, -1, -1):
            for j in range(min(len(d) - idx, 0x10)):
                d[idx + j] ^= k[0x10 * p + j]
    return bytes(d)


def pad16(data: bytes, fill: int) -> bytes:
    return data + bytes([fill]) * (-len(data) % 16)


def compress(data: bytes) -> bytes:
    return pad16(zlib.compress(data), 0)


def encode(data: bytes, comp: int, seed: bytes) -> tuple[bytes, int]:
    if comp > -1:
        data = compress(data)
    if comp > -2:
        data = crypt(data, seed)
    return data, (len(data) if comp > -1 else comp)


def decode(raw: bytes, comp: int, dec_len: int, seed: bytes) -> bytes:
    if comp > -2:
        raw = crypt(raw, seed)
    if comp > -1:
        return zlib.decompress(raw)
    return raw[:dec_len]


def md5(name: str) -> bytes:
    return hashlib.md5(name.encode()).digest()


@dataclass
class HedEntry:
    md5: bytes
    offset: int
    data_len: int
    actual_len: int


def read_hed(path: Path) -> list[HedEntry]:
    hd = path.read_bytes()
    return [HedEntry(hd[i : i + 16], *struct.unpack_from("<Qii", hd, i + 16)) for i in range(0, len(hd), 0x20)]


@dataclass
class Source:
    archive: zipfile.ZipFile
    member: str

    def read(self) -> bytes:
        return self.archive.read(self.member)


@dataclass
class Change:
    original: Source | None = None
    raw: Source | None = None
    remastered: dict[str, Source] = field(default_factory=dict)


@dataclass
class PkgPlan:
    changes: dict[bytes, Change] = field(default_factory=dict)
    names: dict[bytes, str] = field(default_factory=dict)
    added: list[tuple[str, Source, str]] = field(default_factory=list)


class SplitWriter:
    def __init__(self, path: Path, split: bool):
        self.path, self.split, self.pos = path, split, 0
        self.parts: list[Path] = []
        if split:
            path.mkdir(parents=True, exist_ok=True)
        self.fh: BinaryIO = self._open()

    def _open(self) -> BinaryIO:
        p = self.path / f"{len(self.parts):02d}" if self.split else self.path
        self.parts.append(p)
        return p.open("wb")

    def write(self, data: bytes | memoryview) -> None:
        mv = memoryview(data)
        while mv:
            n = len(mv) if not self.split else min(len(mv), PART_SIZE - self.pos % PART_SIZE)
            self.fh.write(mv[:n])
            self.pos += n
            mv = mv[n:]
            if self.split and self.pos % PART_SIZE == 0 and mv:
                self.fh.close()
                self.fh = self._open()

    def close(self) -> None:
        self.fh.close()
        if self.split and len(self.parts) == 1:
            tmp = self.path.with_name(self.path.name + ".tmp")
            self.parts[0].rename(tmp)
            self.path.rmdir()
            tmp.rename(self.path)


def load_patches(
    patches: tuple[Path, ...], excludes: tuple[str, ...]
) -> tuple[dict[tuple[str, str, str], Source], int]:
    tree: dict[tuple[str, str, str], Source] = {}
    skipped = 0
    for p in patches:
        z = zipfile.ZipFile(p)
        for m in z.namelist():
            parts = m.split("/")
            if m.endswith("/") or len(parts) < 3 or parts[1] not in KINDS:
                continue
            if any(fnmatch.fnmatch(m, pat) for pat in excludes):
                skipped += 1
                continue
            tree[(parts[0], parts[1], "/".join(parts[2:]))] = Source(z, m)
    return tree, skipped


def build_plan(
    game_dir: Path, tree: dict[tuple[str, str, str], Source]
) -> tuple[dict[str, PkgPlan], dict[str, list[HedEntry]], list[str]]:
    heds = {h.stem: read_hed(h) for h in sorted(game_dir.glob("*.hed"))}
    where: dict[bytes, list[str]] = defaultdict(list)
    for pkg, entries in heds.items():
        for e in entries:
            where[e.md5].append(pkg)
    plans: dict[str, PkgPlan] = defaultdict(PkgPlan)
    problems: list[str] = []

    for (pkg, kind, name), src in sorted(tree.items(), key=lambda kv: kv[0]):
        if kind in ("original", "raw"):
            targets = where.get(md5(name))
            if not targets:
                if pkg in heds:
                    plans[pkg].added.append((name, src, kind))
                else:
                    problems.append(f"{pkg}/{kind}/{name}: {pkg} not in game dir")
                continue
            for t in targets:
                ch = plans[t].changes.setdefault(md5(name), Change())
                setattr(ch, kind, src)
                plans[t].names[md5(name)] = name
            continue
        parts = name.split("/")
        for k in range(len(parts) - 1, 0, -1):
            parent, sub = "/".join(parts[:k]), "/".join(parts[k:])
            if md5(parent) in where:
                for t in where[md5(parent)]:
                    plans[t].changes.setdefault(md5(parent), Change()).remastered[sub] = src
                    plans[t].names[md5(parent)] = parent
                break
        else:
            problems.append(f"{pkg}/remastered/{name}: parent asset not found")
    return dict(plans), heds, problems


@dataclass
class Stats:
    replaced_original: int = 0
    replaced_remastered: int = 0
    added: int = 0
    unmatched_remastered: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def rebuild_asset(f: BinaryIO, e: HedEntry, ch: Change, name: str, st: Stats) -> tuple[bytes, int]:
    if ch.raw:
        data = ch.raw.read()
        st.replaced_original += 1
        return data, struct.unpack_from("<i", data)[0]
    f.seek(e.offset)
    old_seed = f.read(0x10)
    dec, cnt, comp, date = struct.unpack("<iiii", old_seed)
    subs = [struct.unpack("<32siiii", f.read(0x30)) for _ in range(cnt)]
    orig_raw = f.read(comp if comp >= 0 else dec)
    sub_raw = []
    for _, _, _, d, c in subs:
        ln = c if c >= 0 else d
        sub_raw.append(f.read(ln + (-ln % 16)))

    seed, enc, actual = old_seed, orig_raw, e.actual_len
    if ch.original:
        data = pad16(ch.original.read(), 0xCD)
        packed = compress(data) if comp > -1 else data
        new_comp = len(packed) if comp > -1 else comp
        seed = struct.pack("<iiii", len(data), cnt, new_comp, date)
        enc = crypt(packed, seed) if new_comp > -2 else packed
        dec, comp, actual = len(data), new_comp, len(data)
        st.replaced_original += 1
        if cnt:
            st.warnings.append(f"{name}: original replaced, {cnt} remastered assets kept with their old offsets")

    names = {raw_name.rstrip(b"\0").decode(errors="replace"): i for i, (raw_name, *_rest) in enumerate(subs)}
    lower = {n.lower(): n for n in names}
    replace: dict[int, Source] = {}
    for sub, src in ch.remastered.items():
        hit = sub if sub in names else lower.get(sub.lower())
        if hit is None:
            st.unmatched_remastered.append(f"{name}/{sub}")
        else:
            replace[names[hit]] = src

    head = bytearray(seed)
    body = bytearray()
    offset = 0x10 + 0x30 * cnt + dec
    for i, (raw_name, _, orig_off, d, c) in enumerate(subs):
        if i in replace:
            new = replace[i].read()
            d = len(new)
            data, c = encode(new, c, seed)
            st.replaced_remastered += 1
        elif seed != old_seed:
            data, c = encode(decode(sub_raw[i], c, d, old_seed), c, seed)
        else:
            data = sub_raw[i]
        head += struct.pack("<32siiii", raw_name, offset, orig_off, d, c)
        body += pad16(data, 0xCD)
        offset += d
    return bytes(head + enc + body), actual


def patch_pkg(
    game_dir: Path,
    out_dir: Path,
    pkg: str,
    entries: list[HedEntry],
    plan: PkgPlan,
    split: bool,
    delta: bool,
    progress: Progress,
) -> Stats:
    st = Stats()
    total = sum(e.data_len for e in entries)
    task = progress.add_task(pkg, total=total)
    src_pkg = game_dir / f"{pkg}.pkg"
    base = src_pkg.stat().st_size if delta else 0
    out = SplitWriter(out_dir / f"{pkg}.pkg{DELTA_SUFFIX if delta else ''}", split)
    hed = bytearray()

    def start_asset() -> int:
        if delta:
            out.write(bytes(-(base + out.pos) % 16))
        return base + out.pos

    with src_pkg.open("rb") as f:
        buf = bytearray(COPY_CHUNK)
        for e in entries:
            ch = plan.changes.get(e.md5)
            if delta and ch is None:
                hed += e.md5 + struct.pack("<Qii", e.offset, e.data_len, e.actual_len)
                progress.advance(task, e.data_len)
                continue
            pos = start_asset()
            if e.data_len == 0:
                hed += e.md5 + struct.pack("<Qii", pos, 0, e.actual_len)
                continue
            if ch is None:
                f.seek(e.offset)
                left = e.data_len
                while left:
                    n = f.readinto(memoryview(buf)[: min(left, COPY_CHUNK)])
                    if not n:
                        raise click.ClickException(f"{pkg}.pkg ends early at {e.offset:#x}")
                    out.write(memoryview(buf)[:n])
                    left -= n
                actual = e.actual_len
            else:
                data, actual = rebuild_asset(f, e, ch, plan.names.get(e.md5, e.md5.hex()), st)
                out.write(data)
            hed += e.md5 + struct.pack("<Qii", pos, base + out.pos - pos, actual)
            progress.advance(task, e.data_len)
    for name, src, kind in plan.added:
        pos = start_asset()
        raw = src.read()
        if kind == "raw":
            out.write(raw)
            actual = struct.unpack_from("<i", raw)[0]
        else:
            out.write(struct.pack("<iiii", len(raw), 0, -2, -1) + pad16(raw, 0xCD))
            actual = len(raw)
        hed += md5(name) + struct.pack("<Qii", pos, base + out.pos - pos, actual)
        st.added += 1
    out.close()
    if delta and out.pos > FAT32_MAX:
        raise click.ClickException(f"{pkg}{DELTA_SUFFIX} is {out.pos} bytes, over the FAT32 limit")
    (out_dir / f"{pkg}.hed").write_bytes(hed)
    progress.update(task, completed=total)
    return st


@click.group()
def cli() -> None:
    """Apply KHPCPatchManager patches (.kh1pcpatch, .kh2pcpatch, …) to Kingdom Hearts HD ReMIX hed/pkg archives."""


@cli.command()
@click.argument("game_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("patches", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-o",
    "--output",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory for the patched hed/pkg pairs.",
)
@click.option("-x", "--exclude", multiple=True, help="Glob on patch paths to skip, e.g. '*/title.2ld/*'. Repeatable.")
@click.option("--split", is_flag=True, help="Write pkgs over 4 GiB as FAT32 concatenation folders (parts 00, 01, …).")
@click.option(
    "--delta",
    is_flag=True,
    help="Write each changed pkg as <name>.pkg.delta holding only new assets, read after the original pkg "
    "(needs the kh-pkg-overlay plugin).",
)
@click.option("-j", "--jobs", default=3, show_default=True, help="Pkgs patched in parallel.")
@click.option("-v", "--verbose", is_flag=True, help="List every skipped patch file.")
def apply(
    game_dir: Path,
    patches: tuple[Path, ...],
    output: Path,
    exclude: tuple[str, ...],
    split: bool,
    delta: bool,
    jobs: int,
    verbose: bool,
) -> None:
    """Patch the hed/pkg pairs in GAME_DIR with PATCHES (later patches win) and write changed pairs to OUTPUT.

    Files are replaced in every pkg that contains them, wherever the patch put them.
    """
    if split and delta:
        raise click.UsageError("--split and --delta exclude each other")
    tree, skipped = load_patches(patches, exclude)
    plans, heds, problems = build_plan(game_dir, tree)
    missing = sorted(p for p in plans if not (game_dir / f"{p}.pkg").exists())
    if missing:
        raise click.ClickException(f"the patch changes {', '.join(missing)}, but GAME_DIR has no .pkg for them")
    console.print(f"{len(tree)} patch files, {skipped} excluded, {len(plans)} pkgs to write")
    for p in problems if verbose else problems[:5]:
        console.print(f"[yellow]skipped[/] {p}")
    if len(problems) > 5 and not verbose:
        console.print(f"[yellow]… {len(problems) - 5} more skipped (-v lists all)[/]")
    output.mkdir(parents=True, exist_ok=True)
    results: dict[str, Stats] = {}
    with (
        Progress(
            TextColumn("{task.description:12}"), BarColumn(), DownloadColumn(), TimeRemainingColumn(), console=console
        ) as progress,
        ThreadPoolExecutor(jobs) as pool,
    ):
        futures = {
            pool.submit(patch_pkg, game_dir, output, pkg, heds[pkg], plan, split, delta, progress): pkg
            for pkg, plan in plans.items()
        }
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    for pkg, st in sorted(results.items()):
        console.print(
            f"[bold]{pkg}[/]: {st.replaced_original} originals, {st.replaced_remastered} remastered assets replaced, {st.added} added"
        )
        for w in st.warnings:
            console.print(f"  [yellow]warning[/] {w}")
        if st.unmatched_remastered:
            console.print(
                f"  [yellow]{len(st.unmatched_remastered)} remastered files have no matching asset entry[/], e.g. {st.unmatched_remastered[:3]}"
            )


def length_ok(got: int, expected: int) -> bool:
    # Square Enix pads some data to 16 bytes before compressing but stores the unpadded length
    return got in (expected, expected + (-expected % 16))


class PkgReader:
    def __init__(self, pkg: Path, delta: Path | None):
        parts = sorted(pkg.iterdir()) if pkg.is_dir() else [pkg]
        self.handles = [p.open("rb") for p in parts]
        self.size = sum(p.stat().st_size for p in parts)
        self.delta = delta.open("rb") if delta else None

    def _read_base(self, off: int, n: int) -> bytes:
        out = bytearray()
        while n:
            part, rel = divmod(off, PART_SIZE) if len(self.handles) > 1 else (0, off)
            self.handles[part].seek(rel)
            chunk = self.handles[part].read(n if len(self.handles) == 1 else min(n, PART_SIZE - rel))
            if not chunk:
                break
            out += chunk
            off += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def read(self, off: int, n: int) -> bytes:
        head = b""
        if off < self.size:
            head = self._read_base(off, min(n, self.size - off))
            if len(head) == n or self.delta is None:
                return head
            off, n = self.size, n - len(head)
        if self.delta is None:
            return head
        self.delta.seek(off - self.size)
        return head + self.delta.read(n)

    def close(self) -> None:
        for h in self.handles:
            h.close()
        if self.delta:
            self.delta.close()


def verify_pkg(hed: Path, base_dir: Path | None, progress: Progress) -> list[str]:
    errors: list[str] = []
    entries = read_hed(hed)
    task = progress.add_task(hed.stem, total=sum(e.data_len for e in entries))
    delta = hed.with_name(hed.stem + ".pkg" + DELTA_SUFFIX)
    if delta.exists():
        if base_dir is None:
            return [f"{hed.stem}: {delta.name} needs --base with the original {hed.stem}.pkg"]
        reader = PkgReader(base_dir / f"{hed.stem}.pkg", delta)
    else:
        reader = PkgReader(hed.with_suffix(".pkg"), None)

    for e in entries:
        if e.data_len == 0:
            continue
        blob = reader.read(e.offset, e.data_len)
        if len(blob) != e.data_len:
            errors.append(f"{hed.stem} {e.md5.hex()}: archive ends inside the asset")
            continue
        try:
            dec, cnt, comp, _ = struct.unpack_from("<iiii", blob)
            seed = blob[:16]
            pos = 16 + 0x30 * cnt
            orig_len = comp if comp >= 0 else dec
            if not length_ok(len(decode(blob[pos : pos + orig_len], comp, dec, seed)), dec):
                errors.append(f"{hed.stem} {e.md5.hex()}: original length mismatch")
            if e.actual_len != dec:
                errors.append(f"{hed.stem} {e.md5.hex()}: hed actual length {e.actual_len} != {dec}")
            pos += orig_len
            expect = 0x10 + 0x30 * cnt + dec
            for i in range(cnt):
                _, off, _, d, c = struct.unpack_from("<32siiii", blob, 16 + 0x30 * i)
                if off != expect:
                    errors.append(f"{hed.stem} {e.md5.hex()}: remastered {i} offset {off} != {expect}")
                expect += d
                ln = c if c >= 0 else d
                if not length_ok(len(decode(blob[pos : pos + ln], c, d, seed)), d):
                    errors.append(f"{hed.stem} {e.md5.hex()}: remastered {i} length mismatch")
                pos += ln + (-ln % 16)
            if comp == -2 and cnt == 0:
                # KHPCPatchManager stores added files with the unpadded length but writes them padded
                pos += -pos % 16
            if pos != e.data_len:
                errors.append(f"{hed.stem} {e.md5.hex()}: layout size {pos} != hed {e.data_len}")
        except (zlib.error, struct.error) as ex:
            errors.append(f"{hed.stem} {e.md5.hex()}: {ex}")
        progress.advance(task, e.data_len)
    reader.close()
    return errors


@cli.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--base",
    "base_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Folder with the original pkgs, for checking .pkg.delta output.",
)
@click.option("-j", "--jobs", default=4, show_default=True)
def verify(directory: Path, base_dir: Path | None, jobs: int) -> None:
    """Decrypt and decompress every asset of every hed/pkg pair in DIRECTORY and check the layout."""
    heds = sorted(
        h
        for h in directory.glob("*.hed")
        if h.with_suffix(".pkg").exists() or h.with_name(h.stem + ".pkg" + DELTA_SUFFIX).exists()
    )
    with (
        Progress(
            TextColumn("{task.description:12}"), BarColumn(), DownloadColumn(), TimeRemainingColumn(), console=console
        ) as progress,
        ThreadPoolExecutor(jobs) as pool,
    ):
        errors = [err for errs in pool.map(lambda h: verify_pkg(h, base_dir, progress), heds) for err in errs]
    for err in errors[:50]:
        console.print(f"[red]{err}[/]")
    if errors:
        raise click.ClickException(f"{len(errors)} errors")
    console.print(f"[green]{len(heds)} archives OK[/]")


if __name__ == "__main__":
    cli()
