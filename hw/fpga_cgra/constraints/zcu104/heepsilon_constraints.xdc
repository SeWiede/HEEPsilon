# HEEPsilon-specific constraints for ZCU104
# These override the default X-HEEP constraints with correct hierarchy

### Clock Constraints
create_clock -add -name jtag_clk_pin -period 100.00 -waveform {0 5} [get_ports {jtag_tck_i}]
create_clock -add -name spi_slave_clk_pin -period 16.00 -waveform {0 5} [get_ports {spi_slave_sck_io}]

### Reset Constraints
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/dm_obi_top_i/i_dm_top/i_dm_csrs/dmcontrol_q_reg[ndmreset]/C
set_false_path -from heepsilon_top_i/x_heep_system_i/rstgen_i/i_rstgen_bypass/synch_regs_q_reg[3]/C

### SPI Slave False Paths (corrected hierarchy with heepsilon_top_i prefix)
set_case_analysis 0 heepsilon_top_i/x_heep_system_i/pad_control_i/pad_control_reg_top_i/u_pad_mux_spi_slave_mosi/q_reg[*]/Q
set_case_analysis 0 heepsilon_top_i/x_heep_system_i/pad_control_i/pad_control_reg_top_i/u_pad_mux_spi_slave_miso/q_reg[*]/Q
set_case_analysis 0 heepsilon_top_i/x_heep_system_i/pad_control_i/pad_control_reg_top_i/u_pad_mux_spi_slave_sck/q_reg[*]/Q
set_case_analysis 0 heepsilon_top_i/x_heep_system_i/pad_control_i/pad_control_reg_top_i/u_pad_mux_spi_slave_cs/q_reg[*]/Q

set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_slave_sm/u_spiregs/reg2_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_obiplug/curr_addr_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_slave_sm/u_spiregs/reg1_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_obiplug/curr_addr_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_slave_sm/u_spiregs/reg2_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_obiplug/tx_counter_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_slave_sm/u_spiregs/reg1_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_obiplug/tx_counter_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_slave_sm/u_spiregs/reg2_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_obiplug/FSM_sequential_OBI_CS_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_slave_sm/u_spiregs/reg1_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_obiplug/FSM_sequential_OBI_CS_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_slave_sm/addr_reg_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_obiplug/curr_addr_reg[*]/D

set_false_path -from spi_slave_mosi_io -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_syncro/rdwr_reg_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_rxreg/data_int_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_syncro/rdwr_reg_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_rxreg/counter_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_syncro/rdwr_reg_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_rxreg/counter_trgt_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_syncro/rdwr_reg_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_rxreg/running_reg/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_syncro/rdwr_reg_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_rxreg/counter_trgt_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_syncro/rdwr_reg_reg[*]/D

set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_slave_sm/FSM_sequential_state_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_syncro/rdwr_reg_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_slave_sm/cmd_reg_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_syncro/rdwr_reg_reg[*]/D
set_false_path -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_slave_sm/ctrl_addr_valid_reg/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_syncro/valid_reg_reg[0]/D

set_max_delay -datapath_only -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_tx/i_cdc_fifo_gray/i_src/wptr_q_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_tx/i_cdc_fifo_gray/i_dst/gen_sync[*].i_sync/reg_q_reg[0]/D 5.00
set_max_delay -datapath_only -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_tx/i_cdc_fifo_gray/i_src/gen_word[*].data_q_reg[*][*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_tx/i_cdc_fifo_gray/i_dst/i_spill_register/spill_register_flushable_i/gen_spill_reg.a_data_q_reg[*]/D 5.00
set_max_delay -datapath_only -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_tx/i_cdc_fifo_gray/i_dst/rptr_q_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_tx/i_cdc_fifo_gray/i_src/gen_sync[*].i_sync/reg_q_reg[0]/D 5.00

set_max_delay -datapath_only -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_rx/i_cdc_fifo_gray/i_src/wptr_q_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_rx/i_cdc_fifo_gray/i_dst/gen_sync[*].i_sync/reg_q_reg[0]/D 5.00
set_max_delay -datapath_only -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_rx/i_cdc_fifo_gray/i_src/gen_word[*].data_q_reg[*][*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_rx/i_cdc_fifo_gray/i_dst/i_spill_register/spill_register_flushable_i/gen_spill_reg.a_data_q_reg[*]/D 5.00
set_max_delay -datapath_only -from heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_rx/i_cdc_fifo_gray/i_dst/rptr_q_reg[*]/C -to heepsilon_top_i/x_heep_system_i/core_v_mini_mcu_i/debug_subsystem_i/gen_spi_slave.obi_spi_slave_i/u_dcfifo_rx/i_cdc_fifo_gray/i_src/gen_sync[*].i_sync/reg_q_reg[0]/D 5.00
