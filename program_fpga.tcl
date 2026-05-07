open_hw_manager
connect_hw_server -allow_non_jtag
open_hw_target
set dev ""
foreach d [get_hw_devices] {
    if {[string match "xc7z*" $d]} { set dev $d }
}
if {$dev eq ""} { error "No xc7z020 device found. Devices: [get_hw_devices]" }
current_hw_device $dev
set_property PROGRAM.FILE {build/eslepfl_systems_heepsilon_0/pynq-z1-vivado/eslepfl_systems_heepsilon_0.bit} [current_hw_device]
program_hw_devices [current_hw_device]
close_hw_manager
