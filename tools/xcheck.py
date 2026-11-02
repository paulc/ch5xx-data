#!/usr/bin/env python3
"""Cross-check a chip's ch5xx-data register model against the vendor SFR header
and the datasheet register tables.

Every comparison is by absolute address, width and bit mask, never by name:
ch5xx-data normalises names (drops R8_/RB_ and the peripheral token), so names
only appear in the report to help a human line things up.

Reported, per source:
  - registers the source lists that no data register covers
  - data registers that no source lists
  - with --fields: bit fields on one side with no counterpart on the other

The header side is exact. The datasheet side parses pypdf layout text and is
heuristic: summary tables ("Name Access-address Description Reset") parse
well, per-register bit tables less so (wrapped names, merged cells). Treat
datasheet field mismatches as leads to check by hand, not as findings.

Requires PyYAML, and pypdf when --datasheet is a .pdf.

Usage:
  tools/xcheck.py --data data --family CH592 \
      --header ../ch592/EVT/EXAM/SRC/StdPeriphDriver/inc/CH592SFR.h \
      --datasheet ../ch592/Datasheet/CH592DS1_EN.PDF --fields
"""

import argparse
import re
from collections import defaultdict
from pathlib import Path

import yaml

WIDTH = {"8": 8, "16": 16, "32": 32}


# --------------------------------------------------------------------------
# ch5xx-data side
# --------------------------------------------------------------------------

def load_ir(data_dir, kind, version):
    return yaml.safe_load((data_dir / "registers" / f"{kind}_{version}.yaml").read_text())


def field_masks(ir, fieldset_name):
    """Return {mask: field_name} for a fieldset, expanding field arrays."""
    out = {}
    if not fieldset_name:
        return out
    fs = ir.get(f"fieldset/{fieldset_name}")
    if fs is None:
        return {"<missing fieldset>": fieldset_name}
    for f in fs.get("fields", []):
        size = f["bit_size"]
        offs = [f["bit_offset"]] if isinstance(f["bit_offset"], int) else []
        arr = f.get("array")
        if arr:
            if "offsets" in arr:
                offs = [f["bit_offset"] + o for o in arr["offsets"]]
            else:
                offs = [f["bit_offset"] + i * arr["stride"] for i in range(arr["len"])]
        for i, o in enumerate(offs):
            mask = ((1 << size) - 1) << o
            out[mask] = str(f["name"]) + (f"[{i}]" if arr else "")
    return out


def flatten_block(ir, block_name, base, prefix=""):
    """Yield (addr, bits, name, access, fieldset) for every register in a block."""
    blk = ir[f"block/{block_name}"]
    for item in blk["items"]:
        offs = [item["byte_offset"]]
        arr = item.get("array")
        if arr:
            if "offsets" in arr:
                offs = [item["byte_offset"] + o for o in arr["offsets"]]
            else:
                offs = [item["byte_offset"] + i * arr["stride"] for i in range(arr["len"])]
        for i, o in enumerate(offs):
            name = prefix + str(item["name"]) + (f"[{i}]" if arr else "")
            if "block" in item:
                yield from flatten_block(ir, item["block"], base + o, name + ".")
            else:
                yield (base + o, item.get("bit_size", 32), name,
                       item.get("access", "ReadWrite"), item.get("fieldset"))


def load_family(data_dir, family):
    """Return (periphs, regs) where regs is {addr: [reg dict, ...]}."""
    periphs = yaml.safe_load((data_dir / "family" / f"{family}.yaml").read_text())
    regs = defaultdict(list)
    for p in periphs:
        r = p.get("registers")
        if not r:
            continue
        ir = load_ir(data_dir, r["kind"], r["version"])
        for addr, bits, name, access, fs in flatten_block(ir, r["block"], p["address"]):
            regs[addr].append(dict(periph=p["name"], name=name, bits=bits, access=access,
                                   masks=field_masks(ir, fs),
                                   src=f"{r['kind']}_{r['version']}.yaml#{r['block']}"))
    return periphs, regs


# --------------------------------------------------------------------------
# SFR header side
# --------------------------------------------------------------------------

REG_RE = re.compile(r"^#define\s+(R(8|16|32)_\w+)\s+\(\*\(\(PUINT(8|16|32)V\)\s*\(?(0x[0-9A-Fa-f]+)")
ALIAS_RE = re.compile(r"^#define\s+(R(8|16|32)_\w+)\s+(R(8|16|32)_\w+)\b")
BA_RE = re.compile(r"^#define\s+BA_(\w+)\s+\(\(PUINT\d+V\)\s*(0x[0-9A-Fa-f]+)\)")
OFS_RE = re.compile(r"^#define\s+([A-Z]+)_(\w+)\s+(0x[0-9A-Fa-f]+|\d+)\s*(//.*)?$")
BIT_RE = re.compile(r"^#define\s+(RB_\w+|MASK_\w+)\s+(\(?[0-9xA-Fa-f<>()| ]+\)?)\s*(//\s*(.*))?$")


def parse_header(path):
    """Return {addr: [dict(name, bits, masks{mask:name})]}."""
    regs = defaultdict(list)
    by_name = {}
    bases = {}
    cur = None
    for line in path.read_text(errors="replace").splitlines():
        m = REG_RE.match(line)
        if m:
            name, w, _, addr = m.group(1), m.group(2), m.group(3), int(m.group(4), 16)
            cur = dict(name=name, bits=WIDTH[w], masks={}, comment=line.split("//", 1)[-1].strip())
            regs[addr].append(cur)
            by_name[name] = (addr, cur)
            continue
        m = ALIAS_RE.match(line)
        if m and m.group(3) in by_name:
            addr, target = by_name[m.group(3)]
            cur = dict(name=m.group(1), bits=WIDTH[m.group(2)], masks={}, alias_of=m.group(3),
                       comment=line.split("//", 1)[-1].strip())
            regs[addr].append(cur)
            by_name[m.group(1)] = (addr, cur)
            continue
        m = BIT_RE.match(line)
        if m and cur is not None:
            try:
                mask = eval(m.group(2), {"__builtins__": {}})
            except Exception:
                continue
            if isinstance(mask, int) and mask:
                cur["masks"][mask] = m.group(1)
            continue
        m = BA_RE.match(line)
        if m:
            bases[m.group(1)] = int(m.group(2), 16)
            cur = None
            continue
        m = OFS_RE.match(line)
        if m and any(re.sub(r"\d+$", "", b) == m.group(1) for b in bases):
            # generic per-instance offset, e.g. `#define TMR_PWM_MOD 4`; the RB_
            # bits that follow apply to every instance's register at that offset
            prefix, suffix, off = m.group(1), m.group(2), int(m.group(3), 0)
            cur = dict(name=f"{prefix}x_{suffix}", bits=0, masks={}, generic=True)
            for b, base in bases.items():
                if re.sub(r"\d+$", "", b) != prefix:
                    continue
                for r in regs.get(base + off, []):
                    if r["name"].endswith("_" + suffix):
                        r.setdefault("generic_masks", []).append(cur["masks"])
            continue
    # fold generic masks into the per-instance registers
    for rs in regs.values():
        for r in rs:
            for gm in r.pop("generic_masks", []):
                for mk, n in gm.items():
                    r["masks"].setdefault(mk, n)
    return regs


# --------------------------------------------------------------------------
# Datasheet side (pypdf "layout" text)
# --------------------------------------------------------------------------

DS_REG_RE = re.compile(r"^\s*(R(8|16|32)_\w+)\s+(0x[0-9A-Fa-f]{8})\b\s*(.*)$")
DS_HEAD_RE = re.compile(r"\((R(?:8|16|32)_[A-Z0-9_x]+)\)")
DS_BIT_RE = re.compile(r"^\s*(\[(\d+)\s*[:：]\s*(\d+)\]|(\d+))\s+([A-Za-z]\w*)\s+(RO|RW|WO|RZ|WZ|RW1|WA|RWA|RF|WA/)\b")


def datasheet_lines(path):
    """Datasheet text, one page after another. A .pdf is extracted with pypdf in
    layout mode (the register tables need the column spacing); anything else is
    read as text already extracted the same way."""
    if path.suffix.lower() != ".pdf":
        return path.read_text(errors="replace").splitlines()
    import logging
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    lines = []
    for i, page in enumerate(pypdf.PdfReader(path).pages):
        lines.append(f"===== PAGE {i + 1} =====")
        lines.extend((page.extract_text(extraction_mode="layout") or "").splitlines())
    return lines


def parse_datasheet(text):
    regs = defaultdict(list)
    seen = set()
    page = 0
    for line in text:
        if line.startswith("===== PAGE"):
            page = int(line.split()[2])
            continue
        m = DS_REG_RE.match(line)
        if m:
            name, w, addr = m.group(1), m.group(2), int(m.group(3), 16)
            if (name, addr) in seen:
                continue
            seen.add((name, addr))
            regs[addr].append(dict(name=name, bits=WIDTH[w], page=page,
                                   rest=re.sub(r"\s+", " ", m.group(4)).strip()))
    return regs


def parse_datasheet_fields(text):
    """Return {register name: {mask: field name}} from the per-register bit tables."""
    out = defaultdict(dict)
    cur = []
    for line in text:
        heads = DS_HEAD_RE.findall(line)
        if heads and "Bit" not in line:
            cur = heads
            continue
        m = DS_BIT_RE.match(line)
        if m and cur:
            if m.group(2):
                hi, lo = int(m.group(2)), int(m.group(3))
            else:
                hi = lo = int(m.group(4))
            if hi < lo:
                hi, lo = lo, hi
            fname = m.group(5)
            if fname.lower() == "reserved":
                continue
            mask = ((1 << (hi - lo + 1)) - 1) << lo
            for r in cur:
                out[r][mask] = fname
    return out


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def bytes_covered(data, addr, bits):
    """True if every byte of [addr, addr+bits/8) lies inside some data register."""
    for b in range(addr, addr + bits // 8):
        ok = False
        for a0, regs in data.items():
            for d in regs:
                if a0 <= b < a0 + d["bits"] // 8:
                    ok = True
                    break
            if ok:
                break
        if not ok:
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--family", required=True)
    ap.add_argument("--header", type=Path)
    ap.add_argument("--datasheet", type=Path)
    ap.add_argument("--fields", action="store_true", help="also compare bit fields")
    ap.add_argument("--range", default="0x40000000-0x4000FFFF",
                    help="only compare addresses in this range")
    a = ap.parse_args()

    lo, hi = (int(x, 16) for x in a.range.split("-"))
    periphs, data = load_family(a.data, a.family)
    sources = {}
    if a.header:
        sources["header"] = parse_header(a.header)
    ds_fields = {}
    if a.datasheet:
        ds_text = datasheet_lines(a.datasheet)
        sources["datasheet"] = parse_datasheet(ds_text)
        if a.fields:
            ds_fields = parse_datasheet_fields(ds_text)

    print(f"# {a.family}: {sum(len(v) for v in data.values())} data registers")
    for s, regs in sources.items():
        print(f"# {s}: {sum(len(v) for k, v in regs.items() if lo <= k <= hi)} registers in range")

    # 1. registers present in a source but absent (by address+width) from data
    for s, regs in sources.items():
        print(f"\n## In {s}, missing from data (address, width)")
        for addr in sorted(regs):
            if not (lo <= addr <= hi):
                continue
            for r in regs[addr]:
                widths = {d["bits"] for d in data.get(addr, [])}
                if r["bits"] in widths:
                    continue
                if bytes_covered(data, addr, r["bits"]):
                    # a wide vendor alias over narrower data registers, or a
                    # byte view of a wider data register
                    continue
                note = ""
                if widths:
                    note = f"  (data has {sorted(widths)}-bit: " + ", ".join(
                        f"{d['periph']}.{d['name']}" for d in data[addr]) + ")"
                extra = f"  p.{r['page']}" if "page" in r else ""
                print(f"  0x{addr:08X} {r['bits']:>2}-bit {r['name']}{extra}{note}")

    # 2. registers in data absent from every source
    print("\n## In data, absent from all sources (address, width)")
    for addr in sorted(data):
        if not (lo <= addr <= hi):
            continue
        for d in data[addr]:
            hits = [s for s, regs in sources.items()
                    if any(r["bits"] == d["bits"] for r in regs.get(addr, []))]
            if not hits:
                print(f"  0x{addr:08X} {d['bits']:>2}-bit {d['periph']}.{d['name']}  [{d['src']}]")

    # 3. bit fields, both directions, by mask
    if a.fields:
        fsrc = {}
        if "header" in sources:
            fsrc["header"] = {addr: [(r["name"], r["bits"], r["masks"]) for r in rs]
                              for addr, rs in sources["header"].items()}
        if ds_fields:
            fsrc["datasheet"] = {addr: [(r["name"], r["bits"], ds_fields.get(r["name"], {}))
                                        for r in rs]
                                 for addr, rs in sources["datasheet"].items()}
        # vendor aliases at one address (RESET_STATUS/GLOB_ROM_CFG, UEPn_*/UH_*)
        # describe the same bits; compare their union
        for src in fsrc.values():
            for addr, rs in src.items():
                merged = {}
                for rname, bits, masks in rs:
                    e = merged.setdefault(bits, [[], {}])
                    e[0].append(rname)
                    e[1].update(masks)
                src[addr] = [("/".join(n), b, m) for b, (n, m) in merged.items()]
        for s_name, src in fsrc.items():
            fwd, rev = [], []
            for addr in sorted(src):
                if not (lo <= addr <= hi):
                    continue
                for rname, bits, smasks in src[addr]:
                    cands = [d for d in data.get(addr, []) if d["bits"] == bits]
                    smasks = {m: n for m, n in smasks.items() if m < (1 << bits)}
                    if not cands or not smasks:
                        continue
                    dmasks = {}
                    for d in cands:
                        dmasks.update({m: n for m, n in d["masks"].items() if isinstance(m, int)})
                    dunion = 0
                    for m in dmasks:
                        dunion |= m
                    sunion = 0
                    for m in smasks:
                        sunion |= m
                    label = f"0x{addr:08X} {rname} -> " + "/".join(
                        f"{d['periph']}.{d['name']}" for d in cands)
                    miss = [f"{n}=0x{m:X}" for m, n in smasks.items()
                            if m not in dmasks and (m & dunion) != m]
                    extra = [f"{n}=0x{m:X}" for m, n in dmasks.items()
                             if m not in smasks and (m & sunion) != m]
                    if miss:
                        fwd.append(f"  {label}: " + ", ".join(miss))
                    if extra:
                        rev.append(f"  {label}: " + ", ".join(extra))
            print(f"\n## Fields in {s_name}, missing from data")
            print("\n".join(fwd) or "  (none)")
            print(f"\n## Fields in data, not in {s_name}")
            print("\n".join(rev) or "  (none)")


if __name__ == "__main__":
    main()
