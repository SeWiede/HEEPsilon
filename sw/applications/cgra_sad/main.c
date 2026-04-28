/*
 * cgra_sad — Sum of Absolute Differences: CGRA vs CPU cycle benchmark
 *
 * Runs three back-to-back SAD computations on the same N=1024 element pairs:
 *
 *   1. CPU reference
 *   2. CGRA kernel 1 — uses SABS (custom ALU op, opcode 26)
 *   3. CGRA kernel 2 — no SABS; branchless abs via SRA+LXOR+SSUB
 *                      (#define SKIP_NOSABS to omit)
 *
 * Both CGRA kernels live in the same IMEM of column 0:
 *   kernel 1: PCs  0–11  (N_K1 = 12 instructions)
 *   kernel 2: PCs 12–25  (N_K2 = 14 instructions)
 *
 * ── Kernel 1 (with SABS) ───────────────────────────────────────────────────
 *   PC  0: LWD R1          all rows: R1 = N_ITER
 *   PC  1: SADD R1,0→R2    all rows: R2 = loop counter
 *   PC  2: LWD R0          all rows: R0 = ref[4k+r]     ← loop start
 *   PC  3: LWD R1          all rows: R1 = cur[4k+r]
 *   PC  4: SSUB R0,R1→R0   all rows: diff
 *   PC  5: SABS R0→R0      all rows: |diff|
 *   PC  6: SADD R3,R0→R3   all rows: accumulate
 *   PC  7: SSUB R2,1→R2    ROW 0:    counter--
 *   PC  8: BNE R2,0→11     ROW 0:    if not done → PC11
 *   PC  9: SWD R3          all rows: write partial SAD
 *   PC 10: EXIT            all rows
 *   PC 11: JUMP→2          ROW 0:    loop back
 *
 * ── Kernel 2 (no SABS — branchless abs via BSFA flag-select) ──────────────
 *   PC 12: LWD R1          all rows: R1 = N_ITER
 *   PC 13: SADD R1,0→R2    all rows: R2 = loop counter
 *   PC 14: LWD R0          all rows: R0 = ref[4k+r]     ← loop start
 *   PC 15: LWD R1          all rows: R1 = cur[4k+r]
 *   PC 16: SSUB R0,R1→R0   all rows: diff (R1 safe to reuse)
 *   PC 17: SSUB ZERO,R0→R1 all rows: R1=-diff; sets sign_flag=1 iff diff>0
 *   PC 18: BSFA(R0,R1)→R0  all rows: flag? diff : -diff = |diff|
 *   PC 19: SADD R3,R0→R3   all rows: accumulate
 *   PC 20: SSUB R2,1→R2    ROW 0:    counter--
 *   PC 21: BNE R2,0→24     ROW 0:    if not done → PC24
 *   PC 22: SWD R3          all rows: write partial SAD
 *   PC 23: EXIT            all rows
 *   PC 24: JUMP→14         ROW 0:    loop back
 *
 * One-hot branch rule: only ROW 0 executes BNE/JUMP; rows 1-3 have NOP at
 * those PCs. cgra_rcs.sv accepts a branch only when exactly one row requests
 * it (one-hot check on rcs_br_req_row_s).
 *
 * Relative PC rule: branch targets are indices into conf_reg_file (0-based
 * from kernel start), NOT absolute CMEM addresses. The PC resets to 0 at each
 * kernel launch regardless of start_add. Kernel 1 (start=0) is unaffected;
 * kernel 2 (start=12) uses targets 2 and 12 — same offsets as kernel 1.
 *
 * Input layout (shared by both kernels):
 *   [N_ITER×4, ref[0..3], cur[0..3], ref[4..7], cur[4..7], ...]
 */

#include <stdio.h>
#include <string.h>
#include <stdint.h>

#include "csr.h"
#include "hart.h"
#include "handler.h"
#include "core_v_mini_mcu.h"
#include "rv_plic.h"
#include "rv_plic_regs.h"
#include "heepsilon.h"
#include "cgra.h"

/* ── Instruction encoding ─────────────────────────────────────────────────── */
#define INSTR(ma, mb, op, rs, we, fs, imm) \
    (  ((uint32_t)((ma)  & 0xF ) << 28) \
     | ((uint32_t)((mb)  & 0xF ) << 24) \
     | ((uint32_t)((op)  & 0x1F) << 19) \
     | ((uint32_t)((rs)  & 0x3 ) << 17) \
     | ((uint32_t)((we)  & 0x1 ) << 16) \
     | ((uint32_t)((fs)  & 0x7 ) << 13) \
     | ((uint32_t)((imm) & 0x1FFF)    ))

#define SRC_ZERO  0
#define SRC_R0    6
#define SRC_R1    7
#define SRC_R2    8
#define SRC_R3    9
#define SRC_IMM   10

#define OP_SADD   1
#define OP_SSUB   2
#define OP_BSFA   14
#define OP_BNE    17
#define OP_JUMP   20
#define OP_LWD    21
#define OP_SWD    22
#define OP_EXIT   25
#define OP_SABS   26

#define KMEM_WORD(cols, start, n) \
    (((uint32_t)(cols) << 12) | ((uint32_t)(start) << 5) | ((uint32_t)(n) - 1))

/* IMEM flat index: row r, col c, absolute pc p.
 * For c=0: II(r,0,p) = r*CGRA_CMEM_BK_DEPTH + p — N_K1 does not shift col 0. */
#define II(r, c, p) ((r) * CGRA_CMEM_BK_DEPTH + (c) * N_K1 + (p))
#define N_K1  12   /* kernel 1 instruction count  (PCs  0–11) */
#define K2_START 12
#define N_K2  13   /* kernel 2 instruction count  (PCs 12–24) */

/* ── Parameters ───────────────────────────────────────────────────────────── */
#define N        1024
#define N_ROWS   4
#define N_ITER   (N / N_ROWS)  /* 256 */

/* ── Globals ──────────────────────────────────────────────────────────────── */
static volatile int cgra_done;
static cgra_t       cgra;

static int32_t ref_arr[N];
static int32_t cur_arr[N];

static int32_t input_buf[N_ROWS + 2 * N] __attribute__((aligned(4)));
static int32_t output_buf[N_ROWS]         __attribute__((aligned(4)));
#ifndef SKIP_NOSABS
static int32_t output_buf2[N_ROWS]        __attribute__((aligned(4)));
#endif

static uint32_t imem[CGRA_CMEM_TOT_DEPTH];
static uint32_t kmem[CGRA_KMEM_DEPTH];

void handler_irq_cgra(uint32_t id) { cgra_done = 1; }

/* ── Bitstream ────────────────────────────────────────────────────────────── */
static void build_bitstream(void)
{
    memset(imem, 0, sizeof(imem));
    memset(kmem, 0, sizeof(kmem));

    /* ── Kernel 1: with SABS ── */
    for (int r = 0; r < N_ROWS; r++) {
        imem[II(r,0, 0)] = INSTR(0,       0,       OP_LWD,  1, 1, 0, 4);
        imem[II(r,0, 1)] = INSTR(SRC_R1,  SRC_ZERO,OP_SADD, 2, 1, 0, 0);
        imem[II(r,0, 2)] = INSTR(0,       0,       OP_LWD,  0, 1, 0, 4);
        imem[II(r,0, 3)] = INSTR(0,       0,       OP_LWD,  1, 1, 0, 4);
        imem[II(r,0, 4)] = INSTR(SRC_R0,  SRC_R1,  OP_SSUB, 0, 1, 0, 0);
        imem[II(r,0, 5)] = INSTR(SRC_R0,  SRC_ZERO,OP_SABS, 0, 1, 0, 0);
        imem[II(r,0, 6)] = INSTR(SRC_R3,  SRC_R0,  OP_SADD, 3, 1, 0, 0);
        /* PCs 7,8,11: NOP for rows 1-3 */
        imem[II(r,0, 9)] = INSTR(SRC_R3,  0,       OP_SWD,  0, 0, 0, 4);
        imem[II(r,0,10)] = INSTR(0,       0,       OP_EXIT, 0, 0, 0, 0);
    }
    imem[II(0,0, 7)] = INSTR(SRC_R2,  SRC_IMM,  OP_SSUB, 2, 1, 0, 1);
    imem[II(0,0, 8)] = INSTR(SRC_R2,  SRC_ZERO, OP_BNE,  0, 0, 0, 11);
    imem[II(0,0,11)] = INSTR(SRC_ZERO,SRC_IMM,  OP_JUMP, 0, 0, 0, 2);
    kmem[1] = KMEM_WORD(0x1, 0, N_K1);

#ifndef SKIP_NOSABS
    /* ── Kernel 2: no SABS — abs via SSUB(negate) + BSFA(flag-select) ── */
    for (int r = 0; r < N_ROWS; r++) {
        imem[II(r,0,12)] = INSTR(0,        0,        OP_LWD,  1, 1, 0, 4);  /* R1 = N_ITER */
        imem[II(r,0,13)] = INSTR(SRC_R1,   SRC_ZERO, OP_SADD, 2, 1, 0, 0); /* R2 = ctr */
        imem[II(r,0,14)] = INSTR(0,        0,        OP_LWD,  0, 1, 0, 4);  /* R0 = ref */
        imem[II(r,0,15)] = INSTR(0,        0,        OP_LWD,  1, 1, 0, 4);  /* R1 = cur */
        imem[II(r,0,16)] = INSTR(SRC_R0,   SRC_R1,   OP_SSUB, 0, 1, 0, 0); /* R0 = diff */
        imem[II(r,0,17)] = INSTR(SRC_ZERO, SRC_R0,   OP_SSUB, 1, 1, 0, 0); /* R1=-diff; flag=sign(-diff) */
        imem[II(r,0,18)] = INSTR(SRC_R0,   SRC_R1,   OP_BSFA, 0, 1, 0, 0); /* R0 = flag?diff:-diff */
        imem[II(r,0,19)] = INSTR(SRC_R3,   SRC_R0,   OP_SADD, 3, 1, 0, 0); /* R3 += |diff| */
        /* PCs 20,21,24: NOP for rows 1-3 */
        imem[II(r,0,22)] = INSTR(SRC_R3,   0,        OP_SWD,  0, 0, 0, 4); /* write SAD */
        imem[II(r,0,23)] = INSTR(0,        0,        OP_EXIT, 0, 0, 0, 0); /* EXIT */
    }
    imem[II(0,0,20)] = INSTR(SRC_R2,  SRC_IMM,  OP_SSUB, 2, 1, 0, 1);      /* R2-- */
    imem[II(0,0,21)] = INSTR(SRC_R2,  SRC_ZERO, OP_BNE,  0, 0, 0, 12);     /* if R2≠0 → conf[12]=JUMP */
    imem[II(0,0,24)] = INSTR(SRC_ZERO,SRC_IMM,  OP_JUMP, 0, 0, 0, 2);      /* JUMP → conf[2]=loop start */
    kmem[2] = KMEM_WORD(0x1, K2_START, N_K2);
#endif
}

/* ── main ─────────────────────────────────────────────────────────────────── */
int main(void)
{
    plic_Init();
    plic_irq_set_priority(CGRA_INTR, 1);
    plic_irq_set_enabled(CGRA_INTR, kPlicToggleEnabled);
    plic_assign_external_irq_handler(CGRA_INTR, &handler_irq_cgra);
    CSR_SET_BITS(CSR_REG_MSTATUS, 0x8);
    CSR_SET_BITS(CSR_REG_MIE, 1 << 11);
    cgra.base_addr = mmio_region_from_addr((uintptr_t)CGRA_PERIPH_START_ADDRESS);

    /* Generate test data */
    for (int i = 0; i < N; i++) {
        ref_arr[i] = (int32_t)((i * 31 + 7) % 512) - 256;
        cur_arr[i] = (int32_t)((i * 17 + 113) % 512) - 256;
    }

    /* ── CPU reference ── */
    uint32_t cpu_t0, cpu_t1;
    CSR_READ(CSR_REG_MCYCLE, &cpu_t0);
    volatile int32_t cpu_sad = 0;
    for (int i = 0; i < N; i++) {
        int32_t d = ref_arr[i] - cur_arr[i];
        cpu_sad += (d < 0 ? -d : d);
    }
    CSR_READ(CSR_REG_MCYCLE, &cpu_t1);
    uint32_t cpu_cycles = cpu_t1 - cpu_t0;

    /* Build input buffer (shared by both kernels) */
    for (int r = 0; r < N_ROWS; r++) input_buf[r] = N_ITER;
    for (int k = 0; k < N_ITER; k++) {
        int base = N_ROWS + k * 8;
        for (int r = 0; r < N_ROWS; r++) input_buf[base + r]          = ref_arr[k * 4 + r];
        for (int r = 0; r < N_ROWS; r++) input_buf[base + N_ROWS + r] = cur_arr[k * 4 + r];
    }

    build_bitstream();
    cgra_cmem_init(imem, kmem);

    /* ── CGRA kernel 1: SABS ── */
    cgra_wait_ready(&cgra);
    cgra_set_read_ptr (&cgra, (uint32_t)input_buf,  0);
    cgra_set_write_ptr(&cgra, (uint32_t)output_buf, 0);
    cgra_done = 0;
    uint32_t cgra_t0, cgra_t1;
    CSR_READ(CSR_REG_MCYCLE, &cgra_t0);
    cgra_set_kernel(&cgra, 1);
    while (!cgra_done) {}
    CSR_READ(CSR_REG_MCYCLE, &cgra_t1);
    uint32_t cgra_cycles = cgra_t1 - cgra_t0;
    int32_t cgra_sad = output_buf[0] + output_buf[1] + output_buf[2] + output_buf[3];

#ifndef SKIP_NOSABS
    /* ── CGRA kernel 2: no SABS ── */
    cgra_wait_ready(&cgra);
    cgra_set_read_ptr (&cgra, (uint32_t)input_buf,   0);
    cgra_set_write_ptr(&cgra, (uint32_t)output_buf2, 0);
    cgra_done = 0;
    uint32_t cgra2_t0, cgra2_t1;
    CSR_READ(CSR_REG_MCYCLE, &cgra2_t0);
    cgra_set_kernel(&cgra, 2);
    while (!cgra_done) {}
    CSR_READ(CSR_REG_MCYCLE, &cgra2_t1);
    uint32_t cgra2_cycles = cgra2_t1 - cgra2_t0;
    int32_t cgra2_sad = output_buf2[0] + output_buf2[1] + output_buf2[2] + output_buf2[3];
#endif

    /* ── Report ── */
    printf("N = %d element pairs\n", N);
    printf("CPU          cycles: %u\n", (unsigned)cpu_cycles);
    printf("CGRA SABS    cycles: %u  speedup %u.%ux  SAD %d %s\n",
           (unsigned)cgra_cycles,
           (unsigned)(cpu_cycles / cgra_cycles),
           (unsigned)(((cpu_cycles % cgra_cycles) * 10) / cgra_cycles),
           (int)cgra_sad,
           (cgra_sad == cpu_sad) ? "OK" : "FAIL");
#ifndef SKIP_NOSABS
    printf("CGRA no-SABS cycles: %u  speedup %u.%ux  SAD %d %s\n",
           (unsigned)cgra2_cycles,
           (unsigned)(cpu_cycles / cgra2_cycles),
           (unsigned)(((cpu_cycles % cgra2_cycles) * 10) / cgra2_cycles),
           (int)cgra2_sad,
           (cgra2_sad == cpu_sad) ? "OK" : "FAIL");
    printf("SABS benefit: %u fewer cycles (%u%%)\n",
           (unsigned)(cgra2_cycles - cgra_cycles),
           (unsigned)((cgra2_cycles - cgra_cycles) * 100 / cgra2_cycles));
#endif

    int fail = (cgra_sad != cpu_sad);
#ifndef SKIP_NOSABS
    fail |= (cgra2_sad != cpu_sad);
#endif
    return fail;
}
