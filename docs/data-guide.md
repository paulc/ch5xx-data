# ch5xx-data: how the data is made, read and checked

This guide covers three things:

1. how the YAML in `data/` turns into the `ch5xx-metapac` crate that
   `ch5xx-hal` builds on,
2. how to read each kind of data file,
3. how to cross-reference the data with a WCH datasheet, using CH592 as the
   worked example.

## 1. Pipeline

```
 MounRiver Studio 2 SDK                                       (not in this repo)
   <series>/<PART>.svd ──cleanup──▶ svd/fixed/*.svd ──scripts/gen-from-svd.py──┐
                                                                               │ draft, once per chip
                                                                               ▼
 data/  (committed; hand-edited from here on)
   chips/<CHIP>.yaml ── include_peripherals ──▶ family/<NAME>.yaml
                     └─ include_interrupts  ──▶ interrupts/<NAME>.yaml
   family entry  registers: {kind, version, block} ──▶ registers/<kind>_<version>.yaml
        │
        │  cargo run -p ch5xx-data-gen        (validates every IR, resolves includes)
        ▼
 build/data/chips/<CHIP>.json, build/data/registers/<kind>_<version>.json
        │
        │  cargo run -p ch5xx-metapac-gen -- "CH5*"
        ▼
 build/ch5xx-metapac/          the PAC + metadata crate, one Cargo feature per chip
        │
        │  path dependency (../ch5xx-data/build/ch5xx-metapac)
        ▼
 ch5xx-hal  build.rs reads ch5xx_metapac::metadata::METADATA
            (peripheral singletons, interrupt list, foreach_* macro tables)
```

Points worth knowing:

- **The SVDs and the SVD scripts are not in the repo.** `svd/` and `scripts/`
  are gitignored or kept local by upstream. Each chip was drafted from its SVD
  once; after that the YAML in `data/` is the source of truth and every fix
  is a hand edit. Do not expect to regenerate `data/` from an SVD.
- `ch5xx-data-gen` (`ch5xx-data-gen/src/main.rs`) only:
  - validates each `data/registers/*.yaml` with chiptool (`allow_unused_fieldsets: false`, so a
    fieldset no register uses is an error),
  - resolves `include_peripherals` / `include_interrupts` / `include_memory`,
  - writes JSON. A chip YAML that fails to parse is **skipped with a
    message, not an error**, so check the output for `skip CHxxx`.
- `ch5xx-metapac-gen` turns each `(kind, version)` pair into one Rust module
  (`src/peripherals/<kind>_<version>.rs`) and each chip into
  `src/chips/<chip>/{pac.rs,metadata.rs,device.x,memory_x/}`.
- `ch5xx-hal` depends on whatever is in `build/ch5xx-metapac` **right now**.
  After switching ch5xx-data branches, run `./d gen` again before building the
  HAL.

### Commands

```console
./d gen                          # data-gen + metapac-gen for every CH5* chip
cargo run -p ch5xx-data-gen      # just the JSON step
cargo run -p ch5xx-metapac-gen -- CH592     # one chip

# compile-check one chip (one chip feature at a time; build-std because the
# HAL targets riscv32imac with +xwchc)
cd build/ch5xx-metapac
cargo +nightly check -Zbuild-std=core --target riscv32imac-unknown-none-elf \
    --features ch592,pac,metadata

./d dump-memory-x CH592          # render memory.x for inspection
```

## 2. Reading the data

### `data/chips/<CHIP>.yaml`

One file per chip (or per flash variant, e.g. `CH584X`). The leading comment
records where every number came from; read it first.

| Key | Meaning |
| --- | --- |
| `packages[]` | `name` stops at the package letter (`CH592F`), `package` is the outline, `device_id` is the chip ID shifted into the top byte. |
| `memory[]` | Regions with `kind` (`flash`/`ram`), `address`, `size` (`K`/`M` suffixes allowed) and flash `modes` (`erase_size`, `write_size`). These drive `memory.x`. |
| `docs[]` | Datasheet and product page used. |
| `cores[].name` | QingKe core version (`qingke-v4c`). |
| `cores[].include_peripherals` | The family list (below). |
| `cores[].include_interrupts` | The vector table (below). |

### `data/family/<NAME>.yaml`: peripheral instances

```yaml
- name: TMR1                 # instance: the singleton name in the PAC and HAL
  address: 0x40002400        # base address, DS "Peripheral base address" table
  registers:
    kind: tmr                # type slug  → data/registers/tmr_ch592.yaml
    version: ch592           #            ↗
    block: TMR_FULL          # block/TMR_FULL inside that file
```

- An entry with no `registers:` is a **placeholder** that only records an
  address (BLE at `0x4000C000`, the Flash controller at `0x40001800`). The HAL
  skips it.
- `version` is a concrete chip number. A family may point at another chip's
  version when the layout is identical (CH592's `UART0` uses `uart/ch585`).
- Capability variants of one kind are **blocks in one file**, not separate
  versions: `TMR_FULL` (DMA) vs `TMR_NODMA`, `UART_FULL` (modem) vs
  `UART_NOMODEM`.
- **The SYS region is split.** On CH5xx one region at `0x40001000` holds
  clocks, power, RTC, GPIO, ADC and TouchKey. The data splits it into `SYS`,
  `RTC`, `System_control`, `TouchKey`, `ADC` and `GPIO`, **all with base
  `0x40001000`**. So in those blocks `byte_offset` is simply the low bits of the
  vendor address (ADC `CHANNEL` has `byte_offset: 88` = 0x58 → `R8_ADC_CHANNEL`
  at `0x40001058`).

### `data/interrupts/<NAME>.yaml`: vector table

`NAME: number`. The number is the vector index, i.e. the "No." column of the
datasheet's interrupt vector table (CH592 DS Table 3-1: `TMR0: 16` …
`WDOG_BAT: 35`). Core exceptions (0–15) are not listed.

### `data/registers/<kind>_<version>.yaml`: chiptool IR

```yaml
block/TMR_FULL:
  items:
  - name: INTER_EN           # register
    byte_offset: 2           # from the family base address
    bit_size: 8              # default 32
    access: Read             # Read | Write; omitted = ReadWrite
    fieldset: INTER_EN
    # array: {len, stride}   # register arrays
fieldset/INTER_EN:
  bit_size: 8
  fields:
  - name: IE_FIFO_OV
    bit_offset: 4
    bit_size: 1
    # array: {len, stride}   # field arrays (GPIO pins are modelled this way)
    # enum: NAME             # → enum/NAME
```

**Absolute address = family `address` + item `byte_offset`.**

Names are normalised from the vendor's (`data` commit 5c46e8a):

| Vendor (DS / SFR header) | Data | Generated Rust |
| --- | --- | --- |
| `R8_TMR1_INTER_EN` | `TMR1` + `INTER_EN` | `pac::TMR1.inter_en()` |
| `RB_TMR_IE_FIFO_OV` | `IE_FIFO_OV` | `.ie_fifo_ov()` / `.set_ie_fifo_ov(true)` |
| `R32_ADC_SCAN_CFG2`, `RB_ADC_SCAN_QUAN` | `SCAN_CFG2.SCAN_QUAN` | `pac::ADC.scan_cfg2().modify(\|w\| w.set_scan_quan(3))` |
| `R32_PA_DIR` bit n | `PA_DIR.PA_DIR[n]` | `.pa_dir(n)` / `.set_pa_dir(n, true)` |

The rules: drop the `R8_`/`R16_`/`R32_` width prefix, drop `RB_`, drop the
peripheral token (`TMR1_`, `ADC_`), keep the rest. The register width is
`bit_size`, not part of the name.

Descriptions start with the vendor access code (`RWA, …`). `SAM` / `RWA` means
the register can only be written in safe-access mode: write `0x57` then `0xA8`
to `System_control.SAFE_ACCESS_SIG` first. chiptool only knows Read / Write /
ReadWrite, so this lives in the description:

| DS access (Table 2-6) | `access:` |
| --- | --- |
| `RO`, `RF`, `RZ` | `Read` |
| `WO`, `WZ`, `WA` | `Write` |
| `RW`, `RW1`, `RWA` | omitted (ReadWrite) |

Comments at the top of a register file, and above individual items, record
anything added or removed against the SVD and where it came from. Keep doing
that.

## 3. Cross-referencing with the datasheet

For CH5xx the datasheet ("DS") is also the reference manual. Order of trust,
as upstream uses it:

1. the **datasheet**,
2. the **SFR header** (`CH5xxSFR.h`) and linker script from the newest EVT,
3. the vendor **SVD** (what `data/` was drafted from).

When they disagree, prefer the datasheet and write the disagreement in a YAML
comment.

### Where each data file comes from in the DS

| Data | CH592 DS v1.9 |
| --- | --- |
| `chips/*.yaml` `memory` | Table 2-2 (CODE area: CodeFlash, DataFlash, BootLoader, InfoFlash), Table 2-4 (SRAM) |
| `chips/*.yaml` `packages`, and which family list a chip uses | Product table on page 2 (part numbers vs peripherals) |
| `family/*.yaml` addresses | Table 2-5 (peripheral base addresses) |
| `interrupts/*.yaml` | Table 3-1 (interrupt vector table) |
| block items (`byte_offset`, `bit_size`) | the "Name / Access address / Description / Reset value" table at the start of each chapter's register section (e.g. Table 8-2 for TMR1) |
| fieldsets | the bit table under each register heading, e.g. "Interrupt Enable Register (R8_TMRx_INTER_EN)" |
| pin remap fields | Table 7-7 and `R16_PIN_ALTERNATE` |

### Worked example: TMR1 interrupt enable

1. DS Table 8-2 lists `R8_TMR1_INTER_EN` at `0x40002402`, 8-bit.
2. `data/family/CH592.yaml`: `TMR1` at `0x40002400`, `kind: tmr`,
   `version: ch592`, `block: TMR_FULL`.
3. `data/registers/tmr_ch592.yaml`, `block/TMR_FULL`: `INTER_EN`,
   `byte_offset: 2`, `bit_size: 8`. `0x40002400 + 2 = 0x40002402`. ✓
4. DS bit table "Interrupt Enable Register (R8_TMRx_INTER_EN)": bit 4
   `RB_TMR_IE_FIFO_OV` ↔ `fieldset/INTER_EN` field `IE_FIFO_OV`,
   `bit_offset: 4`. ✓
5. In Rust: `pac::TMR1.inter_en().modify(|w| w.set_ie_fifo_ov(true))`.

### Worked example: a SYS-region register

`R16_PIN_ALTERNATE` is at `0x40001018` (DS Table 7-8). It is in
`sys_ch592.yaml` `block/SYS` as `PIN_ALTERNATE`, `byte_offset: 24` (0x18),
because `SYS` has base `0x40001000`. To find which of the six SYS-region blocks
holds an address, search the `*_ch592.yaml` files for its low byte as a
decimal `byte_offset`.

### Automated check: `tools/xcheck.py`

Lining up a few hundred registers by hand does not scale. `tools/xcheck.py`
compares one family's data against the SFR header and/or the datasheet **by
address, width and bit mask**, so the name normalisation does not get in the
way:

```console
uv run --no-project --with pyyaml --with pypdf tools/xcheck.py \
    --data data --family CH592 \
    --header ../ch592/EVT/EXAM/SRC/StdPeriphDriver/inc/CH592SFR.h \
    --datasheet ../ch592/Datasheet/CH592DS1_EN.PDF \
    --fields
```

It prints, per source: registers missing from the data, data registers no
source lists, and (with `--fields`) bit fields present on one side only.

Reading the output:

- **Header results are exact**; datasheet results are heuristic (PDF text). Use
  the datasheet field section as a list of leads, not findings.
- Known noise that the tool already filters: vendor 32-bit views over several
  8-bit registers (`R32_UART0_CTRL`), byte views of wider registers
  (`R8_PA_DIR_0`), and aliases at one address (`R8_RESET_STATUS` /
  `R8_GLOB_ROM_CFG`, `R8_UEP2_CTRL` / `R8_UH_RX_CTRL`).
- A 32-bit vendor view that spans a reserved byte still shows up
  (`R32_RTC_CTRL`, `R32_MISC_CTRL`, `R32_USB_STATUS`); check that the data
  covers the real sub-registers.

### Things that go wrong

- **Several SFR header versions ship in one vendor repo.** `../ch592` has five
  different `CH592SFR.h` copies; the application projects carry older ones.
  Use `EVT/EXAM/SRC/StdPeriphDriver/inc/` and check `git log` for its EVT
  version. The CH592 data was drafted from an older SDK than EVT v1.7, which
  is why it had a register (SLEEP_CTRL2) that WCH has since removed.
- **The DS copies tables from bigger siblings.** CH592 DS v1.9 has a full
  SPI1 register table (Table 10-2) although its own memory map marks
  `0x40004400` reserved and the product table lists one SPI; it also lists
  `RB_SLP_CLK_SPI1`. Cross-check against the memory map and product table.
- **The header copies comments from bigger siblings too.** CH592SFR.h
  describes `RB_PIN_I2C` as remapping to PB20/PB21, pins no CH59x package has.
- **PDF text extraction drops glyphs.** The ✓ marks in the product table come
  out as nothing, which shifts the columns. Render the page to an image when a
  table matters.
- **Reserved bits.** The data often keeps fields the SVD had even when the DS
  marks those bits reserved for this chip. A HAL should not rely on such a field
  without checking the DS.

## 4. CH592 / CH591 review (DS v1.9, EVT v1.7)

State of `main` at f088cf1, checked with `tools/xcheck.py` and by hand.

What was already right: every base address in DS Table 2-5, the full vector
table (16–35), every UART/I2C/PWMx/LCD/GPIO register address and width, USB
apart from `EPX_MODE`, and the GPIO `SET` / interrupt edge-type registers that
had already been patched in from the DS.

Fixed on branch `ch592-ds-v1.9`:

| Area | Change | Source |
| --- | --- | --- |
| SYS | Added `IWDG_KR`/`IWDG_CFG` (0x00/0x04), the independent watchdog | DS ch. 14, header |
| SYS | Added `SLEEP_CTRL3` (0x14, `SLP_GPIO_EDGE_MODE`) | DS Table 5-1, header |
| SYS | Removed `SLEEP_CTRL2` (0x10, TouchKey wake), removed `AUX_POWER_ADJ.IB_TKEY_SEL` | not in DS; EVT v1.7 header removed both |
| SYS | `PIN_ALTERNATE`: added `DEBUG_EN` (bit 13); removed `PIN_TMR3`/`PIN_UART3`/`PIN_I2C` (bits 3/7/11) | DS: reserved; targets not bonded |
| SYS | `POWER_PLAN`: added `XT_PRE_CFG` (12:11), `XT_PRE_EN` (14) | DS, header |
| TMR | Added `PWM_MOD` (+0x04) to both blocks | DS Tables 8-1..8-4, header |
| SPI | Added `CTRL_MOD1`/`CTRL_MOD2` (+0x08/+0x09) | DS Table 10-1, header |
| ADC | Added `SCAN_CFG1`/`SCAN_CFG2` (0x70/0x74), the channel-scan feature | DS 15.4, header |
| CH592 chip | Added the CH592A package; mapped DATAFLASH (0x70000, 32KB, 256B erase, byte write); added the datasheet to `docs` | DS product table, Table 2-2, ISP592.h |
| CH592/CH591 chip | Core `qingke-v3c` → `qingke-v4c` | DS: "QingKe 32-bit RISC-V4C" |
| Family | Placeholder for the FlashROM controller at 0x40001800 | DS Table 2-5 |
| CH591 | Own family list without TouchKey, I2C0, UART2, UART3 | DS product table and overview |

Left open, on purpose:

- **Flash controller registers.** DS Table 4-1 names `R32_FLASH_DATA`,
  `R32_FLASH_CONTROL`, `R8_FLASH_CTRL` and `R8_FLASH_CFG`, but neither the DS
  nor the header defines their bits. Only the placeholder is recorded. Erase
  and program go through the ISP library.
- **USB `R32_EPX_MODE` (0x40008070, endpoints 8–15).** The DS lists it.
  The EVT v1.7 header does not, and neither source documents the
  EP8–15 buffer or control registers it would need, so it is not added.
- **USB host-mode names.** The data names the shared registers in device
  terms (`UEP2_CTRL`); the vendor's host aliases (`UH_RX_CTRL`, `UH_SETUP`,
  `UH_EP_MOD`, …) and their `RB_UH_*` bits are not modelled separately.
  This needs deciding before a USB host driver.
- **`__`-merged USB field names** (`UIE_BUS_RST__UIE_DETECT`) remain, as in
  the other USB files.
- **CH591 ADC without DMA.** The DS says it, but gives no reduced register
  map, so CH591 still uses the full ADC block.
- **Pins and alternate functions** (DS Table 7-7, pin definitions) are not in
  the data. `ch5xx-hal/build.rs` hard-codes port widths per chip.
- **GPIO field arrays are 32 wide** for both ports, as on every other chip;
  only PA4–PA15, PB0/4/6/7/10–15/22/23 are bonded on CH592.

## 5. Working on a fork

`ch5xx-data` and `ch5xx-hal` are forks of `ch32-rs/ch5xx-data` and
`ch32-rs/ch5xx-hal`. To keep merging upstream cheap:

- Keep each change in its own commit, split by concern (one per peripheral or
  chip), with the source cited in the YAML comment and commit message. Those
  can then go upstream as PRs, or be dropped when upstream fixes the same
  thing.
- Do not reformat or reorder existing YAML. Upstream regenerates some files,
  and a reflow turns every merge into a conflict.
- Follow the naming rules above (concrete chip versions, capability blocks,
  normalised names) so upstream can take the files unchanged.
- After `git merge upstream/main`, run `./d gen`, the per-chip `cargo check`,
  and `tools/xcheck.py` for each chip you care about.
