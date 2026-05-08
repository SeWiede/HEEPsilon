/*
 * rgb_led — cycles through colours on the PYNQ-Z1 tri-color LED (LD5).
 *
 * gpio_io[20] = Red   (M15)
 * gpio_io[21] = Green (G14)
 * gpio_io[22] = Blue  (L14)
 *
 * Sequence: R → G → B → Yellow → Cyan → Magenta → White → off → repeat
 */

#include <stdio.h>
#include "gpio.h"
#include "pad_control.h"
#include "pad_control_regs.h"
#include "core_v_mini_mcu.h"

#define LED_R 20
#define LED_G 21
#define LED_B 22

#define DELAY_CYCLES 2000000

static void delay(void) {
    for (volatile uint32_t i = 0; i < DELAY_CYCLES; i++);
}

static void set_rgb(bool r, bool g, bool b) {
    gpio_write(LED_R, r);
    gpio_write(LED_G, g);
    gpio_write(LED_B, b);
}

int main(void) {
    /* gpio[20:22] share pads with i2s_sck/ws/sd — switch to GPIO mode */
    pad_control_t pad_ctrl;
    pad_ctrl.base_addr = mmio_region_from_addr((uintptr_t)PAD_CONTROL_START_ADDRESS);
    pad_control_set_mux(&pad_ctrl, PAD_CONTROL_PAD_MUX_I2S_SCK_REG_OFFSET, 1);
    pad_control_set_mux(&pad_ctrl, PAD_CONTROL_PAD_MUX_I2S_WS_REG_OFFSET,  1);
    pad_control_set_mux(&pad_ctrl, PAD_CONTROL_PAD_MUX_I2S_SD_REG_OFFSET,  1);

    gpio_cfg_t cfg = { .mode = GpioModeOutPushPull };

    cfg.pin = LED_R; gpio_config(cfg);
    cfg.pin = LED_G; gpio_config(cfg);
    cfg.pin = LED_B; gpio_config(cfg);

    printf("RGB LED test starting\n");

    for (int cycle = 0; cycle < 3; cycle++) {
        set_rgb(1, 0, 0); printf("Red\n");     delay();
        set_rgb(0, 1, 0); printf("Green\n");   delay();
        set_rgb(0, 0, 1); printf("Blue\n");    delay();
        set_rgb(1, 1, 0); printf("Yellow\n");  delay();
        set_rgb(0, 1, 1); printf("Cyan\n");    delay();
        set_rgb(1, 0, 1); printf("Magenta\n"); delay();
        set_rgb(1, 1, 1); printf("White\n");   delay();
        set_rgb(0, 0, 0); printf("Off\n");     delay();
    }

    printf("### DONE ###\n");
    return 0;
}
