/*
 * cgra_loop_preempt — asynchronous CGRA preemption via shared flag buffer
 *
 * The CGRA loops freely, reading one flag word per iteration from a sequential
 * flag buffer (all zeros at launch). When the CPU wants to preempt it writes 1
 * to the ENTIRE flag buffer. The CGRA hits the first 1 within 1-2 iterations
 * and exits — guaranteed, regardless of where in the loop it currently is.
 *
 * This is the closest equivalent to "write one address, CGRA stops":
 *   flag_buf IS the preemption memory region. Arm it → CGRA stops.
 *
 * The architectural reason a single-word flag does not work: OpenEdgeCGRA's
 * LWD always advances the read pointer sequentially. The kernel cannot poll
 * one fixed address; it reads a new word each iteration. Writing to the whole
 * buffer covers all future reads.
 *
 * Sweep: CPU loops for DELAY iterations before arming the flag buffer.
 * Longer delay → more CGRA iterations completed before preemption.
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

#ifdef DEBUG
  #define PRINTF(fmt, ...) printf(fmt, ##__VA_ARGS__)
#else
  #define PRINTF(...)
#endif

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
#define SRC_IMM   10

#define OP_SADD  1
#define OP_BNE   17
#define OP_JUMP  20
#define OP_LWD   21
#define OP_SWD   22
#define OP_EXIT  25

#define KMEM_WORD(cols, start, n) \
    (((uint32_t)(cols) << 12) | ((uint32_t)(start) << 5) | ((uint32_t)(n) - 1))

/* ── Parameters ───────────────────────────────────────────────────────────── */
#define MAX_ITER 60    /* sentinel at slot 60; DELAY+2 max = 52 */

static const int DELAYS[] = {0, 5, 10, 20, 30, 50};
#define N_SWEEP ((int)(sizeof(DELAYS) / sizeof(DELAYS[0])))

/* ── Globals ──────────────────────────────────────────────────────────────── */
static volatile int cgra_done;
static cgra_t       cgra;

/* flag_buf: THE preemption region. All zeros = run. Any non-zero = stop.
 * Slot MAX_ITER is the natural-completion sentinel (always 1). */
static volatile int32_t flag_buf  [MAX_ITER + 1] __attribute__((aligned(4)));
static volatile int32_t output_buf[MAX_ITER     ] __attribute__((aligned(4)));

static uint32_t imem[CGRA_CMEM_TOT_DEPTH];
static uint32_t kmem[CGRA_KMEM_DEPTH];

void handler_irq_cgra(uint32_t id) { cgra_done = 1; }

/* ── Bitstream ────────────────────────────────────────────────────────────── */
static void build_bitstream(void)
{
    memset(imem, 0, sizeof(imem));
    memset(kmem, 0, sizeof(kmem));

    /*
     * Kernel 1 — 6 instructions, bank 0 (row 0)
     *
     *   PC 0: LWD R1           read next flag from flag_buf (sequential)
     *   PC 1: BNE(R1,0) → 5   exit if flag is armed
     *   PC 2: SADD(R0, 1)→R0  R0++ (iteration counter, pure register work)
     *   PC 3: SWD R0           write count to output_buf
     *   PC 4: JUMP 0           loop
     *   PC 5: EXIT
     *
     * Input (flag_buf): one flag word per iteration, consumed sequentially.
     * Output (output_buf): running iteration count at each position.
     * No data arrays needed — work is entirely register-based.
     */
    imem[0] = INSTR(0,       0,        OP_LWD,  1, 1, 0, 4); /* LWD R1 */
    imem[1] = INSTR(SRC_R1,  SRC_ZERO, OP_BNE,  0, 0, 0, 5); /* BNE(R1≠0)→5 */
    imem[2] = INSTR(SRC_R0,  SRC_IMM,  OP_SADD, 0, 1, 0, 1); /* R0 = R0+1 */
    imem[3] = INSTR(SRC_R0,  0,        OP_SWD,  0, 0, 0, 4); /* SWD R0 */
    imem[4] = INSTR(SRC_ZERO,SRC_IMM,  OP_JUMP, 0, 0, 0, 0); /* JUMP 0 */
    imem[5] = INSTR(0,       0,        OP_EXIT, 0, 0, 0, 0); /* EXIT */
    kmem[1] = KMEM_WORD(0x1, 0, 6);
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

    build_bitstream();
    cgra_cmem_init(imem, kmem);

    int total_errors = 0;

    for (int s = 0; s < N_SWEEP; s++) {
        int delay = DELAYS[s];

        /* Reset flag buffer: all zeros = CGRA runs freely */
        for (int i = 0; i < MAX_ITER; i++) flag_buf[i]   = 0;
        flag_buf[MAX_ITER] = 1;          /* sentinel: never overrun */
        for (int i = 0; i < MAX_ITER; i++) output_buf[i] = 0;

        cgra_wait_ready(&cgra);
        cgra_set_read_ptr (&cgra, (uint32_t)flag_buf,   0);
        cgra_set_write_ptr(&cgra, (uint32_t)output_buf, 0);
        cgra_done = 0;
        cgra_set_kernel(&cgra, 1);

        /* ── CPU runs in parallel with CGRA ──────────────────────────────── */

        /* CPU observes CGRA progress via the output buffer.
         * When output_buf[delay-1] is non-zero, the CGRA just completed
         * iteration (delay-1) and is ~12 cycles from reading flag_buf[delay+2].
         * CPU reaction time (~6 cycles) is well within that window.
         * Result: one store, correct slot, CGRA stops at iteration delay+2. */
        if (delay > 0) {
            while (output_buf[delay - 1] == 0) { /* observe CGRA progress */ }
        }
        flag_buf[delay + 2] = 1;   /* single write — this IS the preemption */

        volatile int cpu_post = 0;
        while (!cgra_done) cpu_post++;

        /* Scan completed iterations: output_buf[i] = i+1 if iteration i ran */
        int cgra_iters = 0;
        while (cgra_iters < MAX_ITER && output_buf[cgra_iters] != 0)
            cgra_iters++;

        printf("cpu_delay=%-4d  cgra_iters=%d\n", delay, cgra_iters);

        /* Verify counter values */
        int errors = 0;
        for (int i = 0; i < cgra_iters; i++) {
            if (output_buf[i] != i + 1) {
                PRINTF("  [%d] got %d expected %d FAIL\n", i, (int)output_buf[i], i+1);
                errors++;
            }
        }
        for (int i = cgra_iters; i < MAX_ITER; i++) {
            if (output_buf[i] != 0) {
                PRINTF("  [%d] got %d expected 0 FAIL\n", i, (int)output_buf[i]);
                errors++;
            }
        }

        PRINTF("  -> %d errors\n", errors);
        total_errors += errors;
    }

    printf("Total errors: %d\n", total_errors);
    return total_errors;
}
