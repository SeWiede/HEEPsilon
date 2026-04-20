# HEEPsilon CGRA Data Flow

This document traces the complete path from software setup to hardware execution and interrupt return, with exact signal names and file:line references.

---

## 1. Bus Protocol (OBI) and CGRA Master Ports

HEEPsilon uses the **OBI (Open Bus Interface)** protocol throughout. An OBI transaction has a request phase (`req`/`gnt`) and a response phase (`rvalid`/`rdata`).

The CGRA exposes **N_COL (4) master ports** — one per column — for data memory accesses (LWD/SWD):

```
// hw/vendor/esl_epfl_cgra/hw/wrapper/cgra_top_wrapper.sv:16
output obi_req_t  [N_COL-1:0] masters_req_o,
input  obi_resp_t [N_COL-1:0] masters_resp_i,
```

One port per column is needed because each column can independently issue load/store requests during kernel execution; serialising all columns to a single port would eliminate the bandwidth advantage of the CGRA.

The CGRA also has **one OBI slave port** for CPU writes to context memory (used by `cgra_cmem_init()`):

```
// hw/vendor/esl_epfl_cgra/hw/wrapper/cgra_top_wrapper.sv:22
input  obi_req_t  slave_req_i,
output obi_resp_t slave_resp_o,
```

In `heepsilon_top.sv`, the `ext_bus` crossbar routes:
- CGRA master ports → `ext_master_req[N_COL-1:0]` → X-HEEP internal bus (lines 59–62, 136–138)
- X-HEEP CPU/peripheral bus → `ext_xbar_slave_req` → CGRA slave port (lines 140–141)

The CGRA control registers use a separate **reg bus** path (not OBI):
```
// hw/rtl/heepsilon_top.sv:255
.ext_peripheral_slave_req_o(ext_periph_slave_req),
// hw/rtl/heepsilon_top.sv:151
.reg_req_i(ext_periph_slave_req),
```

---

## 2. `cgra_set_read_ptr()` / `cgra_set_write_ptr()` → Hardware Registers

```c
// sw/external/drivers/cgra/cgra.c:33
mmio_region_write32(cgra->base_addr,
    (ptrdiff_t)(CGRA_PTR_IN_COL_0_REG_OFFSET + 0x8 * column_idx), read_ptr);

// sw/external/drivers/cgra/cgra.c:38
mmio_region_write32(cgra->base_addr,
    (ptrdiff_t)(CGRA_PTR_OUT_COL_0_REG_OFFSET + 0x8 * column_idx), write_ptr);
```

Register offsets (from `hw/vendor/esl_epfl_cgra/sw/cgra_regs.h`):

| Column | Read ptr offset | Write ptr offset |
|--------|----------------|-----------------|
| 0 | `0x08` | `0x0c` |
| 1 | `0x10` | `0x14` |
| 2 | `0x18` | `0x1c` |
| 3 | `0x20` | `0x24` |

Base address is `CGRA_PERIPH_START_ADDRESS = EXT_PERIPHERAL_START_ADDRESS + 0x0` (from `hw/rtl/heepsilon_pkg.sv.tpl:35`).

These writes go through the reg bus → `peripheral_regs` (instantiated inside `synchronizer`) → stored as `rd_ptr_o` / `wr_ptr_o` in `synchronizer.sv`:

```
// hw/vendor/esl_epfl_cgra/hw/rtl/synchronizer.sv:21
output logic [DP_WIDTH-1:0] rd_ptr_o [0:MAX_COL_REQ-1],
output logic [DP_WIDTH-1:0] wr_ptr_o [0:MAX_COL_REQ-1],
```

These flow to `data_bus_handler` as `rd_ptr_i` / `wr_ptr_i` (instantiated in `cgra_top.sv` lines 198–224). When `col_start_i[j]` pulses, the handler latches the base pointer into `rd_data_cnt_col[j]` / `wr_data_cnt_col[j]` (lines 92–112).

---

## 3. `cgra_set_kernel()` → Execution Trigger

```c
// sw/external/drivers/cgra/cgra.c:50
mmio_region_write32(cgra->base_addr,
    (ptrdiff_t)(CGRA_KERNEL_ID_REG_OFFSET), kernel_id);
// CGRA_KERNEL_ID_REG_OFFSET = 0x4  (cgra_regs.h:27)
```

**RTL path:**

1. `peripheral_regs` (inside `synchronizer`) captures the kernel ID and exposes it as `ker_id_o → ker_id_req_s` (synchronizer.sv:34, 200).

2. `synchronizer` FSM detects a non-zero kernel ID:
   ```
   // synchronizer.sv:92–98  SYNC_FSM_LOOP_REQ
   if ((|ker_id_req_s) == 1'b1) begin
     sync_fsm_n_state = SYNC_FSM_READ_CONF;
   ```

3. FSM progresses: `LOOP_REQ → READ_CONF → FIND_COL → WAIT_ACK`.  
   In `FIND_COL` it picks free columns matching the kernel's column-count requirement and latches the mapping:
   ```
   // synchronizer.sv:159
   acc_req_reg <= acc_req_mapped;
   ```

4. `acc_req_o = acc_req_reg` (synchronizer.sv:75) flows to `cgra_controller` as `acc_req_i`:
   ```
   // cgra_top.sv:151
   .acc_req_i ( acc_req_s ),
   ```

5. `cgra_controller` global FSM transitions `GLOB_FSM_IDLE → GLOB_FSM_RCS_CONF` and begins column configuration (controller.sv:229–264).

---

## 4. CGRA Fetches Instructions from IMEM

### Context Memory Layout

`cgra_cmem_init()` (cgra.c:13–28) writes:
- N_ROW banks of depth `CGRA_CMEM_BK_DEPTH = 128` words starting at `CGRA_START_ADDRESS` — this is the **instruction memory (IMEM)**, one bank per row.
- A kernel configuration table (`kmem`) at offset `N_ROW * 2^CMEM_BK_DEPTH_LOG2` words.

### Kernel Config Table Read (KMEM)

When the controller enters `GLOB_FSM_RCS_CONF`, it reads the kernel config word from KMEM:
```
// cgra_controller.sv:384–385
assign rcs_row_n_instr_s  = kmem_rdata_i[RCS_N_INSTR_HB:RCS_N_INSTR_LB];
assign rcs_imem_start_add = kmem_rdata_i[RCS_IMEM_ADD_HB:RCS_IMEM_ADD_LB];
```
This gives the start address and length of the instruction sequence in IMEM.

### IMEM Address Generation

The controller drives a read address counter `imem_radd_s` into `context_memory_decoder` (via `imem_radd_o`):
```
// cgra_controller.sv:408–419
always_ff @(posedge clk_i, negedge rst_ni) begin
  if (rcs_load_conf_add == 1'b1)
    imem_radd_s <= rcs_imem_start_add;           // load start address
  else if (any_conf_e == 1'b1)
    imem_radd_s <= imem_radd_s + 1;              // step each cycle a grant arrives
end
```

`context_memory_decoder` (instantiated in cgra_top.sv:226–248) translates `imem_radd_i` → row SRAM enables (`cm_row_req_o`) and address (`cm_addr_o`). The SRAM output `rcs_cmem_rdata_i[0:N_ROW-1]` (one 32-bit word per row) is fed directly into `cgra_rcs` as `rcs_conf_words_i`.

### Per-Column PC

Each column has its own program counter driven by `cgra_controller`:
```
// cgra_controller.sv:427–442  (generate for j=0..N_COL-1)
program_counter rcs_pc_i (
  .restart_i ( rcs_pc_rst[j]  ),
  .pc_e_i    ( rcs_pc_e[j]    ),   // enable: stall-free execution cycle
  .br_req_i  ( rcs_br_req_i[j]),
  .br_add_i  ( rcs_br_add_i[j]),
  .pc_o      ( rcs_pc[j]      )
);
```

`rcs_pc_o[j]` selects which configuration word the RC in that column reads from its local register file (`reconfigurable_cell.conf_rdata_i`).

---

## 5. LWD Execution — RTL Signal Path

When a `reconfigurable_cell` decodes an LWD instruction it asserts:
```
// cgra_rcs.sv:428–429 (reconfigurable_cell outputs)
.data_req_o    ( data_req_s[i][j] ),   // 1 = load/store request
.data_wen_o    ( data_wen_s[i][j] ),   // 1 = load (read), 0 = store (write)
.data_ind_o    ( data_ind_s[i][j] ),   // 0 = sequential (LWD), 1 = indirect (LWI)
.add_inc_o     ( add_inc_s[i][j]  ),   // signed byte increment for LWD
```

### Arbitration Inside `cgra_rcs`

`cgra_rcs` arbitrates N_ROW requests per column. The first unserved row wins:
```
// cgra_rcs.sv:121–123
always_comb begin
  for (int l=0; l<N_COL; l++)
    data_req_o[l] = |data_req_gnt_mask[l];  // OR of ungranted requests
```

`data_wen_o[col]`, `data_ind_o[col]`, `data_add_o[col]`, `data_wdata_o[col]` and `add_inc_o[col]` reflect the winning row's values.

### `data_bus_handler` — Address Calculation and OBI Drive

For sequential LWD (`data_ind_o == 0`, `data_wen_o == 1`):
```
// data_bus_handler.sv:141
bus_data_add_s[k] = rd_data_cnt_col[k];  // current sequential read pointer
```
On grant, the pointer auto-increments by the signed `rcs_add_inc_sign_ext` value (lines 94–95).

The handler drives the `tcdm_*` signals that become OBI fields in `cgra_top_wrapper`:
```
// cgra_top_wrapper.sv:61–65 (per column j)
masters_req_o[j].req   = tcdm_req[j];
masters_req_o[j].addr  = tcdm_add[j];
masters_req_o[j].we    = ~tcdm_wen[j];   // OBI we=1 means write; TCDM wen=1 means read
masters_req_o[j].be    = tcdm_be[j];
masters_req_o[j].wdata = tcdm_wdata[j];
```

### Stall Mechanism

While any row in a column still has an unserved request, `data_bus_handler` asserts:
```
// data_bus_handler.sv:157–158
if (rcs_data_req_i[k] == 1'b1 || ahb_fsm_n_state[k] == AHB_FSM_DATA_PHASE)
  data_stall_s[k] = 1'b1;
```

`data_stall_comb_s[col]` propagates upward through `cgra_top.sv` as `data_stall_s[col]`, reaching the controller:
```
// cgra_controller.sv:354–355  (RCS_FSM_EXEC)
rcs_pc_e[i] = ~(rcs_stall[i] | data_stall_i[i]);
```

PC enable is de-asserted, freezing the column's instruction pointer until the bus transaction completes.

Grant (`tcdm_gnt_i[col]`) and read-valid (`tcdm_r_valid_i[col]`) return from X-HEEP's memory. `tcdm_gnt_i` clears the per-row `gnt_mask` entry (cgra_rcs.sv:183–199); `tcdm_r_valid_i` clears the `rvalid_mask` entry and delivers `data_rdata_i[col]` to the winning RC (cgra_rcs.sv:236–263).

---

## 6. Interrupt Generation and CPU Routing

**Column completion:** When a column finishes all iterations, the `reconfigurable_cell`'s `exec_end_o` signal propagates up. `cgra_rcs` ORs it across rows and checks no branch is pending:
```
// cgra_rcs.sv:113
exec_end_s[l] = |(rcs_exec_end_col_merged & ~rcs_br_req_o & col_acc_map_i[l]);
```
`rcs_exec_end_s[col]` → `cgra_controller.rcs_exec_end_i[col]`.

The controller column FSM transitions `RCS_FSM_EXEC → RCS_FSM_DONE` and pulses:
```
// cgra_controller.sv:369
acc_end_o[i] = 1'b1;
```

**Interrupt generation chain:**

```
cgra_controller.acc_end_o[col]
  → cgra_top.acc_end_s[col]        (cgra_top.sv:53)
  → synchronizer.acc_end_i[col]    (cgra_top.sv:132)
  → evt_o = |acc_end_i             (synchronizer.sv:50)   ← fires when ANY column ends
  → cgra_top.evt_o                 (cgra_top.sv:39)
  → cgra_top_wrapper.cgra_evt      (cgra_top_wrapper.sv:48)
  → cgra_int_o = cgra_evt          (cgra_top_wrapper.sv:84)
  → heepsilon_top.cgra_int         (heepsilon_top.sv:81)
  → ext_intr_vector[0] = cgra_int  (heepsilon_top.sv:106)
  → x_heep_system.intr_vector_ext_i (heepsilon_top.sv:232)
  → CPU EXT_INTR_0 (CGRA_INTR)    (cgra.h:13)
```

The CPU handles this via the PLIC / fast-interrupt vector registered with `#define CGRA_INTR EXT_INTR_0`.

---

## 7. Top-Level Signal Flow Diagram

```
CPU (CV32E40P)
│
│  1. Write peripheral regs (reg bus)
│     CGRA_PTR_IN_COL_n   → rd_ptr_o[n]   ─────────────────────────┐
│     CGRA_PTR_OUT_COL_n  → wr_ptr_o[n]   ─────────────────────────┤
│     CGRA_KERNEL_ID      → ker_id_req_s  → synchronizer FSM        │
│                                              │ acc_req_o            │
│                                              ▼                      │
│                                         cgra_controller             │
│                                              │                      │
│  2. Context memory write (OBI slave)         │ imem_radd_o          │
│     cgra_cmem_init()                         ▼                      │
│     → slave_req_i → context_memory_decoder → context_memory         │
│                              │ rcs_cmem_rdata_i (per row)           │
│                              ▼                                       │
│                         cgra_rcs (N_ROW × N_COL reconfigurable_cell)│
│                              │                                       │
│   PC control:           rcs_pc_o[col] ──→ each RC instruction select│
│   rcs_pc_e_o (gated by data_stall / rcs_stall)                      │
│                              │                                       │
│   LWD/SWD:              data_req_s, data_wen_s, data_ind_s           │
│                         add_inc_s ──────────────────────────────────┤
│                              ▼                                       │
│                         data_bus_handler ◄──── rd_ptr_i / wr_ptr_i ─┘
│                              │ tcdm_req/add/wen/be/wdata
│                              │ ◄── tcdm_gnt / tcdm_rdata / tcdm_r_valid
│                              ▼
│                         cgra_top_wrapper
│                              │ masters_req_o[0..N_COL-1]  (OBI)
│                              │ masters_resp_i[0..N_COL-1]
│                              ▼
│                         ext_bus (heepsilon_top)
│                              │ heep_slave_req/resp
│                              ▼
│                         x_heep_system internal bus → SRAM banks
│
│  3. Interrupt return
│     reconfigurable_cell.exec_end_o
│       → cgra_rcs.exec_end_o[col]
│       → cgra_controller: RCS_FSM_EXEC→DONE, acc_end_o[col]=1
│       → synchronizer: evt_o = |acc_end_i
│       → cgra_top_wrapper: cgra_int_o
│       → heepsilon_top: ext_intr_vector[0]
│       → x_heep_system: EXT_INTR_0 → CPU handler
│
└──────────────────────────────────────────────────────────────────────
```

### Key module hierarchy summary

| File | Module | Role |
|------|--------|------|
| `hw/rtl/heepsilon_top.sv` | `heepsilon_top` | Top integrator: bus crossbar + interrupt routing |
| `hw/vendor/esl_epfl_cgra/hw/wrapper/cgra_top_wrapper.sv` | `cgra_top_wrapper` | OBI↔TCDM bridge, clock gating, context SRAM |
| `hw/vendor/esl_epfl_cgra/hw/rtl/cgra_top.sv` | `cgra_top` | Internal CGRA datapath and control wiring |
| `hw/vendor/esl_epfl_cgra/hw/rtl/synchronizer.sv` | `synchronizer` | Peripheral reg interface, kernel dispatch FSM, interrupt OR |
| `hw/vendor/esl_epfl_cgra/hw/rtl/cgra_controller.sv` | `cgra_controller` | Global FSM + per-column FSMs, PC control, IMEM address |
| `hw/vendor/esl_epfl_cgra/hw/rtl/cgra_rcs.sv` | `cgra_rcs` | N_ROW×N_COL RC array, per-column request arbitration |
| `hw/vendor/esl_epfl_cgra/hw/rtl/data_bus_handler.sv` | `data_bus_handler` | Pointer management, TCDM bus FSM, stall generation |
| `sw/external/drivers/cgra/cgra.c` | — | SW driver: reg writes, polling, `cgra_cmem_init` |
