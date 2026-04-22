/*
 * cgra_loop_preempt — cooperative CGRA kernel preemption sweep
 *
 * Kernel A loops over input pairs (SADD), polling a preempt flag at the start
 * of each iteration. The CPU sets that flag in the input array before launch,
 * making preemption fully deterministic.
 *
 * The test sweeps over multiple preemption points to show that:
 *   - earlier preemption → fewer CGRA cycles → lower CPU count
 *   - outputs beyond the preemption point remain zero (no over-run)
 *   - the CGRA can be re-launched cleanly after each preemption
 *
 * Input layout per iteration i: [flag_i, a_i, b_i]
 *   flag=0 → continue, flag≠0 → exit before this iteration
 * Slot MAX_ITER always holds flag=1 (natural completion sentinel).
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
#define SRC_R2    8
#define SRC_IMM   10

#define OP_SADD  1
#define OP_BNE   17
#define OP_JUMP  20
#define OP_LWD   21
#define OP_SWD   22
#define OP_EXIT  25

/* KMEM word: [15:12]=col_mask [11:5]=start_addr [4:0]=(n_instr-1) */
#define KMEM_WORD(cols, start, n) \
    (((uint32_t)(cols) << 12) | ((uint32_t)(start) << 5) | ((uint32_t)(n) - 1))

/* ── Sweep parameters ─────────────────────────────────────────────────────── */
#define MAX_ITER 20

/* preempt_at = N means N iterations complete, kernel exits at start of iter N.
 * preempt_at = MAX_ITER means all iterations complete (sentinel at slot MAX_ITER). */
static const int SWEEP[] = {0, 2, 5, 10, 15, MAX_ITER};
#define N_SWEEP ((int)(sizeof(SWEEP) / sizeof(SWEEP[0])))

/* ── Globals ──────────────────────────────────────────────────────────────── */
static volatile int cgra_done;
static cgra_t       cgra;

/* Extra sentinel slot at index MAX_ITER */
static int32_t input_a [(MAX_ITER + 1) * 3] __attribute__((aligned(4)));
static int32_t output_a[MAX_ITER           ] __attribute__((aligned(4)));

static uint32_t imem[CGRA_CMEM_TOT_DEPTH];
static uint32_t kmem[CGRA_KMEM_DEPTH];

void handler_irq_cgra(uint32_t id) { cgra_done = 1; }

/* ── Bitstream ────────────────────────────────────────────────────────────── */
static void build_bitstream(void)
{
    memset(imem, 0, sizeof(imem));
    memset(kmem, 0, sizeof(kmem));

    /*
     * Kernel 1 — bank 0 (row 0), slots 0-7
     *
     *   PC 0: LWD R0           load preempt flag
     *   PC 1: BNE(R0,0) → 7   branch to EXIT if flag set
     *   PC 2: LWD R1           load a
     *   PC 3: LWD R2           load b
     *   PC 4: SADD(R1,R2)→R1
     *   PC 5: SWD R1           store result
     *   PC 6: JUMP 0           loop back to PC 0
     *   PC 7: EXIT
     */
    imem[0] = INSTR(0,        0,        OP_LWD,  0, 1, 0, 4);
    imem[1] = INSTR(SRC_R0,   SRC_ZERO, OP_BNE,  0, 0, 0, 7);
    imem[2] = INSTR(0,        0,        OP_LWD,  1, 1, 0, 4);
    imem[3] = INSTR(0,        0,        OP_LWD,  2, 1, 0, 4);
    imem[4] = INSTR(SRC_R1,   SRC_R2,   OP_SADD, 1, 1, 0, 0);
    imem[5] = INSTR(SRC_R1,   0,        OP_SWD,  0, 0, 0, 4);
    imem[6] = INSTR(SRC_ZERO, SRC_IMM,  OP_JUMP, 0, 0, 0, 0);
    imem[7] = INSTR(0,        0,        OP_EXIT, 0, 0, 0, 0);
    kmem[1] = KMEM_WORD(0x1, 0, 8);
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
        int preempt_at = SWEEP[s];

        /* Build input array: [flag=0, a=i+1, b=(i+1)*10] per slot */
        for (int i = 0; i <= MAX_ITER; i++) {
            input_a[i*3 + 0] = 0;
            input_a[i*3 + 1] = i + 1;
            input_a[i*3 + 2] = (i + 1) * 10;
        }
        /* Natural-completion sentinel — always present */
        input_a[MAX_ITER * 3] = 1;
        /* Early preemption flag */
        if (preempt_at < MAX_ITER)
            input_a[preempt_at * 3] = 1;

        memset(output_a, 0, sizeof(output_a));

        cgra_wait_ready(&cgra);
        cgra_set_read_ptr (&cgra, (uint32_t)input_a,  0);
        cgra_set_write_ptr(&cgra, (uint32_t)output_a, 0);
        cgra_done = 0;
        cgra_set_kernel(&cgra, 1);

        volatile int count = 0;
        while (!cgra_done) count++;

        printf("preempt@%-2d: cpu_count=%-6d completed_iters=%d\n",
               preempt_at, count, preempt_at);

        /* Iterations 0..preempt_at-1 must have correct SADD results */
        int errors = 0;
        for (int i = 0; i < preempt_at; i++) {
            int32_t expected = (i + 1) + (i + 1) * 10;
            if (output_a[i] != expected) {
                PRINTF("  [%d] got %d expected %d FAIL\n", i, output_a[i], expected);
                errors++;
            }
        }
        /* Iterations preempt_at..MAX_ITER-1 must still be zero */
        for (int i = preempt_at; i < MAX_ITER; i++) {
            if (output_a[i] != 0) {
                PRINTF("  [%d] got %d expected 0 (post-preempt) FAIL\n", i, output_a[i]);
                errors++;
            }
        }

        PRINTF("  -> %d errors\n", errors);
        total_errors += errors;
    }

    printf("Total errors: %d\n", total_errors);
    return total_errors;
}
